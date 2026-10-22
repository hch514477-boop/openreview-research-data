"""Resume Edits for saved submissions/reviews; never rewrite original snapshots."""
import argparse
import logging
import sys
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from src.collect import APIError, PublicAPI
from src.common import kind, load, now, save, setup_logging

STATES = {'success', 'empty', 'rate_limited', 'forbidden', 'error', 'not_attempted'}


def state_path() -> Path:
    return config.ROOT / 'data/edits/state.json'


def retry_deadline(header: str | None, at: datetime | None = None) -> str | None:
    if not header:
        return None
    at = at or datetime.now(timezone.utc)
    try:
        deadline = at + timedelta(seconds=int(header.strip())) if header.strip().isdigit() else parsedate_to_datetime(header)
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=timezone.utc)
        return max(at, deadline.astimezone(timezone.utc)).isoformat()
    except (ValueError, TypeError, OverflowError):
        return None


def cooling_down(state: dict) -> bool:
    deadline = state.get('not_before')
    return bool(deadline and datetime.now(timezone.utc) < datetime.fromisoformat(deadline))


def prepare(paper_paths: list[Path] | None = None) -> dict:
    """Import historical states without inventing unavailable attempt timestamps."""
    state = load(state_path()) if state_path().exists() else {'targets': {}, 'not_before': None}
    if paper_paths is None:
        manifest = load(config.ROOT / 'data/authenticated_collection/latest.json')
        paper_paths = [config.RAW / f'ICLR_{r["year"]}' / p['id'] for r in manifest['runs'] for p in r['papers']]
    mapping = {'ok': 'success', 'no_records_found': 'empty', 'access_restricted': 'forbidden',
               'rate_limited': 'rate_limited', 'request_failed': 'error',
               'unsupported_or_unavailable': 'error', 'not_tested': 'not_attempted'}
    for directory in paper_paths:
        paper, meta = load(directory / 'submission.json'), load(directory / 'metadata.json')
        notes = load(directory / 'notes.json') if meta['notes']['status'] == 'ok' else []
        for note in [paper, *[n for n in notes if kind(n) == 'official_review']]:
            note_id = note['id']
            if note_id in state['targets']:
                continue
            old = meta.get('edits', {}).get(note_id, {})
            state['targets'][note_id] = {
                'note_id': note_id, 'paper_id': paper['id'], 'year': meta['year'],
                'source': config.API + '/notes/edits', 'params': {'note.id': note_id},
                'status': mapping.get(old.get('status'), 'not_attempted'),
                'last_attempt_at': None, 'http_status': 429 if old.get('status') == 'rate_limited' else None,
                'count': old.get('count'), 'raw_path': None,
                'legacy_status': old.get('status'), 'imported_at': now(),
                'reason': old.get('reason'),
                'provenance': directory.relative_to(config.ROOT).as_posix() + '/metadata.json',
                'timestamp_note': 'Historical request time unavailable; collection start is not used as attempt time'}
    save(state_path(), state)
    return state


def run_batch(api: PublicAPI, state: dict, note_ids: list[str] | None = None) -> dict:
    """One serial batch. 429 always stops it; future calls honor persisted cooldown."""
    if cooling_down(state):
        logging.warning('Edits cooldown active until %s; no requests sent', state['not_before'])
        return state
    selected = note_ids if note_ids is not None else list(state['targets'])
    for note_id in selected:
        if note_id not in state['targets']:
            raise ValueError('Note ID is outside the prepared saved-paper scope')
    state['batch_started_at'] = now()
    state['batch_stop_reason'] = None
    for note_id in selected:
        target = state['targets'][note_id]
        if target['status'] in ('success', 'empty'):
            continue
        target.update(last_attempt_at=now(), http_status=None, reason=None, retry_after=None)
        try:
            result = api.get('/notes/edits', {'note.id': note_id})
            edits = result['edits']
            if not isinstance(edits, list) or any(not isinstance(e.get('id'), str) or
                     e.get('note', {}).get('id') != note_id for e in edits):
                raise ValueError('Unexpected Edit target/schema')
            public = [e for e in edits if 'everyone' in e.get('readers', [])]
            path = config.ROOT / 'data/edits/records' / note_id / (now().replace(':', '-').replace('+', '_') + '.json')
            save(path, public)
            target.update(status='success' if edits else 'empty', http_status=200,
                          count=len(public), returned_count=len(edits),
                          public_unverified_excluded=len(edits) - len(public),
                          raw_path=path.relative_to(config.ROOT).as_posix())
        except APIError as exc:
            target.update(status='rate_limited' if exc.status == 429 else
                          'forbidden' if exc.status in (401, 403) else
                          'not_attempted' if exc.not_tested else 'error',
                          http_status=exc.status, reason=str(exc))
            if exc.not_tested:
                target['last_attempt_at'] = None
            if exc.status == 429:
                target['retry_after'] = exc.retry_after
                state['not_before'] = retry_deadline(exc.retry_after)
                state['batch_stop_reason'] = '429; stopped, no automatic retry'
            elif exc.status in (401, 403) or exc.not_tested:
                state['batch_stop_reason'] = 'Access restricted; stopped'
        except (ValueError, KeyError, TypeError) as exc:
            target.update(status='error', reason=type(exc).__name__)
        finally:
            save(state_path(), state)
        if state['batch_stop_reason']:
            break
    state['batch_finished_at'] = now()
    save(state_path(), state)
    return state


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepare-only', action='store_true', help='Inventory saved Notes; no authentication or network')
    parser.add_argument('--note-id', action='append', help='Retry only this prepared ID; may repeat flag')
    args = parser.parse_args()
    setup_logging(config.ROOT / 'logs')
    state = prepare()
    if args.prepare_only:
        logging.info('Prepared %s saved submission/review targets; no network requests', len(state['targets']))
        return 0
    if args.note_id and any(n not in state['targets'] for n in args.note_id):
        parser.error('--note-id must belong to prepared saved submissions/reviews')
    if cooling_down(state):
        logging.warning('Retry-After cooldown until %s; no login or requests', state['not_before'])
        return 1
    if not any(t['status'] not in ('success', 'empty') for key, t in state['targets'].items()
               if not args.note_id or key in args.note_id):
        logging.info('Selected targets already successful/empty; no login or requests')
        return 0
    try:
        api = PublicAPI(authenticate=True)
    except (APIError, ImportError) as exc:
        logging.error('%s', str(exc) if isinstance(exc, APIError) else type(exc).__name__)
        return 1
    state = run_batch(api, state, args.note_id)
    selected = args.note_id or list(state['targets'])
    return 0 if all(state['targets'][key]['status'] in ('success', 'empty') for key in selected) else 1


if __name__ == '__main__':
    raise SystemExit(main())
