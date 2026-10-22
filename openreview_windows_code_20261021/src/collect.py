"""Collect public API v2 data, optionally using interactive SDK authentication."""
import argparse
import contextlib
import getpass
import io
import logging
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from src.common import kind, load, now, save, setup_logging, value


class APIError(RuntimeError):
    def __init__(self, message: str, status: int | None = None, not_tested: bool = False,
                 retry_after: str | None = None):
        super().__init__(message)
        self.status = status
        self.not_tested = not_tested
        self.retry_after = retry_after


class PublicAPI:
    def __init__(self, authenticate: bool = False):
        import openreview
        from requests.adapters import HTTPAdapter
        self.authenticated = False
        self.blocked = set()
        self.requests = []
        try:
            if authenticate:
                username = input('OpenReview 邮箱（不保存）: ').strip()
                password = getpass.getpass('OpenReview 密码（不回显、不保存）: ')
                try:
                    # Same constructor verified by test_auth.py; SDK login output
                    # is suppressed because it may contain raw error responses.
                    with contextlib.redirect_stdout(io.StringIO()):
                        self.client = openreview.api.OpenReviewClient(
                            baseurl=config.API, username=username, password=password)
                finally:
                    del username, password
                if not self.client.token:
                    raise APIError('Authentication failed: SDK returned no token')
                self.authenticated = True
                logging.info('SDK authentication succeeded (credentials are not saved)')
            else:
                import os
                if os.getenv('OPENREVIEW_USERNAME') or os.getenv('OPENREVIEW_PASSWORD'):
                    raise APIError('Use --authenticate for explicit interactive authentication')
                self.client = openreview.api.OpenReviewClient(baseurl=config.API)
        except APIError:
            raise
        except (Exception, KeyboardInterrupt) as exc:
            raise APIError(f'Authentication/client initialization failed: {type(exc).__name__}; use a local interactive terminal') from None
        # Own bounded retries and timeouts; retain raw JSON including unknown fields.
        for prefix in list(self.client.session.adapters):
            self.client.session.mount(prefix, HTTPAdapter(max_retries=0))
        original_request = self.client.session.request

        def request(method, url, **kwargs):
            kwargs.setdefault('timeout', config.TIMEOUT)
            return original_request(method, url, **kwargs)

        self.client.session.request = request

    def get(self, endpoint: str, params: dict, binary: bool = False):
        import requests
        if endpoint in getattr(self, 'blocked', set()):
            raise APIError(f'{endpoint}: not tested after earlier access restriction or rate limit', not_tested=True)
        for attempt in range(config.ATTEMPTS):
            time.sleep(config.DELAY)
            try:
                response = self.client.session.get(config.API + endpoint, params=params,
                                                   headers=self.client.headers,
                                                   timeout=config.TIMEOUT)
                if hasattr(self, 'requests'):
                    self.requests.append({'endpoint': endpoint, 'params': params, 'http_status': response.status_code})
                if response.status_code >= 400:
                    error_name = None
                    try:
                        candidate = response.json().get('name')
                        if candidate in ('ChallengeRequiredError', 'ForbiddenError', 'UnauthorizedError',
                                         'ValidationError', 'NotFoundError', 'RateLimitError', 'InvitationExpiredError'):
                            error_name = candidate
                    except (ValueError, AttributeError):
                        pass
                    if error_name and hasattr(self, 'requests'):
                        self.requests[-1]['error_type'] = error_name
                    if response.status_code in (401, 403, 429) and hasattr(self, 'blocked'):
                        self.blocked.add(endpoint)
                    retry_header = getattr(response, 'headers', {}).get('Retry-After')
                    error = APIError(f'{endpoint}: HTTP {response.status_code}' +
                                     (f' {error_name}' if error_name else ''), response.status_code,
                                     retry_after=retry_header)
                    if response.status_code == 429:
                        logging.warning('%s rate limited; no retry or further requests to this endpoint in this run', endpoint)
                    if response.status_code not in (500, 502, 503, 504):
                        raise error
                    if attempt == config.ATTEMPTS - 1:
                        raise error
                    retry_after = response.headers.get('Retry-After', '')
                    time.sleep(min(float(retry_after), 60) if retry_after.isdigit() else 2 ** attempt)
                    continue
                return response.content if binary else response.json()
            except (requests.RequestException, ValueError) as exc:
                if hasattr(self, 'requests'):
                    self.requests.append({'endpoint': endpoint, 'params': params, 'http_status': None,
                                          'error_type': type(exc).__name__})
                logging.warning('Request failed (%s/%s): %s', attempt + 1, config.ATTEMPTS, type(exc).__name__)
                if attempt == config.ATTEMPTS - 1:
                    raise APIError(f'{endpoint}: {type(exc).__name__}') from None
                time.sleep(2 ** attempt)

    def attachment(self, note_id: str) -> bytes:
        endpoint = '/attachment'
        if endpoint in self.blocked:
            raise APIError('PDF not tested after earlier access restriction', not_tested=True)
        time.sleep(config.DELAY)
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                return self.client.get_attachment(field_name='pdf', id=note_id)
        except Exception as exc:
            payload = exc.args[0] if exc.args and isinstance(exc.args[0], dict) else {}
            status = payload.get('status')
            if status in (401, 403):
                self.blocked.add(endpoint)
            raise APIError(f'/attachment: HTTP {status}' if status else
                           f'/attachment: {type(exc).__name__}', status) from None

    def pages(self, endpoint: str, key: str, params: dict, limit: int | None = None,
              page_size: int | None = None) -> list:
        if endpoint == '/notes/edits':
            # Match SDK get_note_edits(note_id=...) exactly; do not add offset.
            return self.get(endpoint, params)[key]
        result = []
        offset = 0
        while limit is None or len(result) < limit:
            maximum = page_size or config.PAGE_SIZE
            size = min(maximum, limit - len(result)) if limit else maximum
            batch = self.get(endpoint, {**params, 'limit': size, 'offset': offset})[key]
            result.extend(batch)
            if len(batch) < size:
                break
            offset += len(batch)
        return result


def failed(exc: APIError) -> dict:
    return {'status': 'not_tested' if exc.not_tested else 'access_restricted' if exc.status in (401, 403) else
            'rate_limited' if exc.status == 429 else
            'unsupported_or_unavailable' if exc.status == 404 else 'request_failed', 'reason': str(exc)}


def collect_paper(api: PublicAPI, paper: dict, year: int, edits_reviews_only: bool = False,
                  skip_edits: bool = False) -> dict:
    directory = config.RAW / f'ICLR_{year}' / paper['id']
    previous = load(directory / 'metadata.json') if (directory / 'metadata.json').exists() else {}
    if directory.exists():
        # Preserve previous successful snapshots and diagnostic evidence before
        # replacing the current files; exclude snapshots to avoid recursive copies.
        snapshot = directory / '.snapshots' / now().replace(':', '-').replace('+', '_')
        shutil.copytree(directory, snapshot, ignore=shutil.ignore_patterns('.snapshots', '*.tmp'))
    save(directory / 'submission.json', paper)
    meta = {'paper_id': paper['id'], 'year': year, 'source': config.API, 'collected_at': now(),
            'submission': {'status': 'ok'}, 'edits': {},
            'notes': {'status': 'not_tested'}, 'pdf': {'status': 'not_tested'},
            'authenticated': getattr(api, 'authenticated', False)}
    try:
        notes = api.pages('/notes', 'notes', {'forum': paper.get('forum') or paper['id']})
        notes = [n for n in notes if n['id'] != paper['id']]
        if getattr(api, 'authenticated', False):
            accessible = len(notes)
            notes = [n for n in notes if 'everyone' in n.get('readers', [])]
            meta['nonpublic_notes_excluded'] = accessible - len(notes)
        save(directory / 'notes.json', notes)
        meta['notes'] = {'status': 'ok', 'count': len(notes), 'scope': 'current public forum notes; not full historical archive'}
    except APIError as exc:
        notes = []
        meta['notes'] = failed(exc)
        logging.error('%s notes: %s', paper['id'], exc)
    edits = {}
    save(directory / 'metadata.json', meta)
    for note in [paper, *notes]:
        if skip_edits:
            meta['edits'][note['id']] = {'status': 'not_tested', 'reason': 'Edits skipped; use independent supplement batch'}
            continue
        if edits_reviews_only and note['id'] != paper['id'] and kind(note) != 'official_review':
            meta['edits'][note['id']] = {'status': 'not_tested', 'reason': 'MVP scope: submission and Official Review Edits only'}
            continue
        try:
            batch = api.pages('/notes/edits', 'edits', {'note.id': note['id']})
            returned_count = len(batch)
            if getattr(api, 'authenticated', False):
                batch = [edit for edit in batch if 'everyone' in edit.get('readers', [])]
            meta['edits'][note['id']] = {'status': 'ok' if returned_count else 'no_records_found', 'count': len(batch),
                                        'returned_count': returned_count, 'nonpublic_or_unverified_excluded': returned_count - len(batch),
                                        'complete_history_verified': False}
            for edit in batch:
                edits[edit['id']] = edit
        except APIError as exc:
            meta['edits'][note['id']] = failed(exc)
            logging.error('%s edits: %s', note['id'], exc)
        finally:
            save(directory / 'edits.json', list(edits.values()))
            save(directory / 'metadata.json', meta)
    save(directory / 'edits.json', list(edits.values()))
    pdf = directory / 'paper.pdf'
    try:
        pdf_ref = value(paper, 'pdf')
        if pdf.exists() and previous.get('pdf', {}).get('reference') == pdf_ref and pdf.read_bytes().startswith(b'%PDF-'):
            meta['pdf'] = {'status': 'ok', 'reference': pdf_ref, 'cached': True}
        elif pdf_ref:
            content = api.attachment(paper['id'])
            if not content.startswith(b'%PDF-'):
                raise APIError('Response is not a PDF')
            temporary = pdf.with_suffix('.pdf.tmp')
            temporary.write_bytes(content)
            temporary.replace(pdf)
            meta['pdf'] = {'status': 'ok', 'reference': pdf_ref, 'cached': False}
        else:
            meta['pdf'] = {'status': 'missing_reference', 'reason': 'No public PDF field'}
    except APIError as exc:
        meta['pdf'] = failed(exc)
        logging.error('%s PDF: %s', paper['id'], exc)
    save(directory / 'metadata.json', meta)
    return meta


def collect_year(api: PublicAPI, year: int, limit: int, mvp: bool = False, skip_edits: bool = True) -> dict:
    venue = f'ICLR.cc/{year}/Conference'
    api.blocked.discard('/notes')  # Each year gets its own initial list probe.
    request_start = len(api.requests)
    run = {'year': year, 'started_at': now(), 'requested': limit, 'papers': [], 'errors': [],
           'authenticated': api.authenticated, 'discovery_source': 'submission_invitation',
           'coverage': 'bounded sample, not conference coverage'}
    try:
        group = api.get('/groups', {'id': venue})['groups'][0]
        save(config.RAW / f'ICLR_{year}' / 'venue.json', group)
        invitation = value(group, 'submission_id') or venue + '/-/Submission'
        verified = api.get('/invitations', {'id': invitation, 'expired': 'true'})
        save(config.RAW / f'ICLR_{year}' / 'invitation.json', verified)
        run['invitation'] = invitation
        # Stable number order, bounded candidate pool; prefer public discussion and
        # alternate observed venue labels without inferring accept/reject decisions.
        candidates = api.pages('/notes', 'notes', {'invitation': invitation, 'sort': 'number:asc',
                                                  'details': 'replyCount'},
                               limit=limit if mvp else limit * 3, page_size=1 if mvp else None)
        if len({p['id'] for p in candidates}) != len(candidates):
            raise APIError('Submission pagination returned duplicate IDs')
        if api.authenticated:
            candidates = [p for p in candidates if 'everyone' in p.get('readers', [])]
        if any(invitation not in (p.get('invitations') or []) for p in candidates):
            raise APIError('Submission invitation mismatch')
        run['discovery_ids'] = [p['id'] for p in candidates]
        buckets = {}
        for paper in sorted(candidates, key=lambda p: -p.get('details', {}).get('replyCount', 0)):
            buckets.setdefault(str(value(paper, 'venue')), []).append(paper)
        selected = []
        while len(selected) < limit and any(buckets.values()):
            for bucket in buckets.values():
                if bucket and len(selected) < limit:
                    selected.append(bucket.pop(0))
        for paper in selected:
            logging.info('Collecting %s %s (%s/%s)', year, paper['id'], len(run['papers']) + 1, len(selected))
            try:
                meta = collect_paper(api, paper, year, edits_reviews_only=mvp, skip_edits=skip_edits)
                run['papers'].append({'id': paper['id'], 'notes_status': meta['notes']['status'], 'metadata': meta})
            except OSError as exc:
                run['errors'].append(f'{paper["id"]}: {type(exc).__name__}')
        run['status'] = ('completed_with_errors' if any(
                         p['notes_status'] != 'ok' or p['metadata']['pdf']['status'] != 'ok'
                         for p in run['papers']) else
                         'completed' if len(run['papers']) == limit and not run['errors'] else 'insufficient_public_candidates')
    except APIError as exc:
        logging.error('Collection stopped: %s', exc)
        run['status'] = 'failed'
        run['errors'].append(str(exc))
    run['finished_at'] = now()
    run['requests'] = api.requests[request_start:]
    save(config.RAW / f'ICLR_{year}' / 'run.json', run)
    # Retain previous attempts as well as the latest run summary.
    with (config.RAW / f'ICLR_{year}' / 'runs.jsonl').open('a', encoding='utf-8') as stream:
        import json
        stream.write(json.dumps(run, ensure_ascii=False) + '\n')
    return run


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--year', type=int, choices=config.YEARS)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--authenticate', action='store_true', help='Prompt locally for SDK credentials')
    parser.add_argument('--authenticated-mvp', action='store_true', help='One login, 2 papers per year, normalize and validate')
    parser.add_argument('--skip-edits', action='store_true', help='Skip Edits (default; supplement with collect_edits.py)')
    parser.add_argument('--with-edits', action='store_true', help='Optionally run a separate Edits batch after collection')
    args = parser.parse_args()
    if args.limit < 1 or (not args.authenticated_mvp and args.year is None):
        parser.error('Provide --year and a positive --limit, or --authenticated-mvp')
    if args.authenticated_mvp and (args.year is not None or args.limit != 20):
        parser.error('--authenticated-mvp fixes both years and 2 papers each; omit --year/--limit')
    if args.skip_edits and args.with_edits:
        parser.error('--skip-edits and --with-edits are mutually exclusive')
    setup_logging(config.ROOT / 'logs')
    try:
        api = PublicAPI(authenticate=args.authenticate or args.authenticated_mvp)
    except (APIError, ImportError) as exc:
        message = str(exc) if isinstance(exc, APIError) else type(exc).__name__
        logging.error('%s', message)
        if args.authenticated_mvp:
            from src.authenticated_collection_report import write_report
            write_report([], authentication_error=message)
        return 1
    years = config.YEARS if args.authenticated_mvp else (args.year,)
    runs = [collect_year(api, year, 2 if args.authenticated_mvp else args.limit,
                         mvp=args.authenticated_mvp) for year in years]
    from src.collect_edits import prepare, run_batch
    paper_paths = [config.RAW / f'ICLR_{run["year"]}' / p['id'] for run in runs for p in run['papers']]
    state = prepare(paper_paths)
    if args.with_edits:
        selected_papers = {p['id'] for run in runs for p in run['papers']}
        run_batch(api, state, [key for key, target in state['targets'].items()
                              if target['paper_id'] in selected_papers])
    if args.authenticated_mvp:
        from src.normalize import main as normalize
        from src.validate import validate
        from src.authenticated_collection_report import write_report
        try:
            normalize()
            result = validate()
            before = {name: (config.PROCESSED / f'{name}.jsonl').read_bytes() for name in ('papers', 'notes', 'edits')}
            normalize()
            result['normalization_idempotent'] = all(
                (config.PROCESSED / f'{name}.jsonl').read_bytes() == content for name, content in before.items())
        except (OSError, ValueError, KeyError) as exc:
            result = {'passed': False, 'normalization_error': type(exc).__name__}
        import subprocess
        test_directory = config.ROOT / 'data' / 'test_runs' / now().replace(':', '-').replace('+', '_')
        test_directory.parent.mkdir(parents=True, exist_ok=True)
        tests = subprocess.run([sys.executable, '-X', 'utf8', '-m', 'pytest', '-q', 'tests',
                                '--basetemp', str(test_directory)], cwd=config.ROOT, capture_output=True,
                               text=True, encoding='utf-8')
        test_result = {'passed': tests.returncode == 0, 'exit_code': tests.returncode,
                       'summary': tests.stdout.strip().splitlines()[-1:]}
        output_path = config.ROOT / 'reports' / 'checks' / (test_directory.name + '_pytest.txt')
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(tests.stdout + '\n' + tests.stderr, encoding='utf-8')
        test_result['output_path'] = output_path.relative_to(config.ROOT).as_posix()
        logging.info('Offline pytest: %s', 'passed' if test_result['passed'] else 'failed (run pytest tests for details)')
        write_report(runs, validation=result, tests=test_result)
        if not test_result['passed'] or not result.get('normalization_idempotent', False):
            return 1
        if not result['passed']:
            return 1
    return 0 if all(run['status'] == 'completed' for run in runs) else 1


if __name__ == '__main__':
    raise SystemExit(main())
