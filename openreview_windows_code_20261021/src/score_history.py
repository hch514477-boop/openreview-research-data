"""Bounded public ICLR score-history audit. No LLM, identity resolution or PDF fetch."""
import argparse
import csv
import hashlib
import json
import math
import re
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import config
from src.collect import APIError, PublicAPI
from src.collect_edits import retry_deadline
from src.common import invitations, load, now, save, value

VENUE = 'ICLR.cc/2025/Conference'
# Exact UTC values are published in the official page's countdown variables.
# This is the published calendar, not a claim about actual platform release logs.
TIMELINE = {'source': 'https://iclr.cc/Conferences/2025/Dates',
            'timezone': 'AoE (UTC-12)', 'published_utc_boundaries_verified': True,
            'start_utc': '2024-11-14T11:59:59+00:00',
            'end_exclusive_utc': '2024-11-27T11:59:59+00:00',
            'source_variables': ['discussion_period_starts_1', 'discussion_period_ends_1'],
            'source_file': 'schema/dates.html',
            'actual_platform_opening_time_verified': False}
EVENT_FIELDS = ['forum_id', 'review_note_id', 'reviewer_signature', 'score_before',
                'score_after', 'score_delta', 'changed_at', 'discussion_phase',
                'original_edit_id', 'evidence_url', 'data_quality_status']


def milliseconds(text):
    return int(datetime.fromisoformat(text).timestamp() * 1000)


def phase(timestamp, venue=VENUE):
    if venue != VENUE or timestamp is None:
        return 'unknown'
    if timestamp < milliseconds(TIMELINE['start_utc']):
        return 'before_discussion'
    if timestamp >= milliseconds(TIMELINE['end_exclusive_utc']):
        return 'after_discussion'
    return 'during_discussion'


def private_field(field):
    readers = field.get('readers', ['everyone'])
    return not (readers == {'delete': True} or isinstance(readers, list) and 'everyone' in readers)


def score(raw):
    if isinstance(raw, dict):
        if private_field(raw):
            return None
        raw = raw.get('value')
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)) and math.isfinite(raw):
        return raw
    if isinstance(raw, str):
        match = re.fullmatch(r'\s*([+-]?\d+(?:\.\d+)?)\s*(?::[^\n]*)?', raw)
        if match:
            return float(match[1])
    return None


def patch(base, delta):
    """OpenReview object merge: omitted fields persist, delete removes fields."""
    result = dict(base) if isinstance(base, dict) else {}
    for key, val in delta.items():
        if isinstance(val, dict) and val.get('delete') is True:
            result.pop(key, None)
        elif isinstance(val, dict):
            result[key] = patch(result.get(key), val)
        else:
            result[key] = val
    return result


def restore(review, edits, rating_key='rating', venue=VENUE, complete_response=True):
    """Infer visible states without inventing initial ratings or hidden revisions."""
    result = {'forum_id': review['forum'], 'review_note_id': review['id'],
              'reviewer_signature': ';'.join(review.get('signatures', [])),
              'current_rating': score(review.get('content', {}).get(rating_key)),
              'rating_field': rating_key, 'history_status': 'unknown',
              'issues': [], 'versions': [], 'events': [], 'visible_edit_count': len(edits)}
    unique = {}
    for edit in edits:
        if not isinstance(edit, dict) or not edit.get('id') or edit.get('note', {}).get('id') != review['id']:
            result['issues'].append('edit_target_or_id_invalid')
            return result
        if edit['note'].get('forum', review['forum']) != review['forum']:
            result['issues'].append('edit_forum_mismatch')
            return result
        if 'signatures' in edit['note'] and set(edit['note']['signatures']) != set(review.get('signatures', [])):
            result['issues'].append('reviewer_signature_changed_or_mismatched')
            return result
        if edit['id'] in unique and unique[edit['id']] != edit:
            result['issues'].append('conflicting_duplicate_edit')
            return result
        unique[edit['id']] = edit
    ordered = list(unique.values())
    if any('everyone' not in e.get('readers', []) for e in ordered):
        result['issues'].append('nonpublic_edits_excluded')
        ordered = [e for e in ordered if 'everyone' in e.get('readers', [])]
    result['visible_edit_count'] = len(ordered)
    if not ordered:
        result['issues'].append('empty_history_is_not_no_change')
        return result
    if any(not isinstance(e.get('tcdate'), int) or isinstance(e.get('tcdate'), bool) for e in ordered):
        result['issues'].append('immutable_order_timestamp_missing')
        return result
    ordered.sort(key=lambda e: e['tcdate'])
    if len({e['tcdate'] for e in ordered}) != len(ordered):
        result['issues'].append('equal_timestamp_order_ambiguous')
        return result
    if any(e.get('ddate') or
           (rating_key in e['note'].get('content', {}) and e.get('tmdate', e['tcdate']) > e['tcdate']) for e in ordered):
        result['issues'].append('deleted_replacement_or_destructively_modified_history')
        return result
    baseline = (ordered[0]['tcdate'] == review.get('tcdate') and
                score(ordered[0]['note'].get('content', {}).get(rating_key)) is not None)
    if not baseline:
        result['issues'].append('original_rating_baseline_not_publicly_observed')
    if not complete_response:
        result['issues'].append('edits_response_truncated_or_count_missing')
    state = {}
    previous = None
    pending = []
    gap = False
    for index, e in enumerate(ordered):
        if e.get('replacement'):
            state, previous = {}, None
            if index or not baseline:
                result['issues'].append('replacement_resets_visible_state')
        rating_patch = e['note'].get('content', {}).get(rating_key)
        if isinstance(rating_patch, dict) and private_field(rating_patch):
            old_rating = state.get('content', {}).get(rating_key)
            if isinstance(old_rating, dict):
                old_rating.pop('value', None)
            result['issues'].append('private_rating_value_not_observed')
        state = patch(state, e['note'])
        rating = score(state.get('content', {}).get(rating_key))
        if rating is None:
            if rating_key in e['note'].get('content', {}):
                gap = True
                result['issues'].append('rating_deleted_hidden_or_non_numeric')
            previous = None
        elif previous is not None and rating != previous:
            ts = e['tcdate']
            pending.append(dict(forum_id=review['forum'], review_note_id=review['id'],
                reviewer_signature=result['reviewer_signature'], score_before=previous,
                score_after=rating, score_delta=rating-previous,
                changed_at=datetime.fromtimestamp(ts/1000, timezone.utc).isoformat(),
                changed_at_ms=ts, discussion_phase=phase(ts, venue), original_edit_id=e['id'],
                evidence_url='https://openreview.net/revisions?id='+review['id'],
                data_quality_status='visible_transition_partial_history'))
        result['versions'].append(dict(edit_id=e['id'], tcdate=e['tcdate'], tmdate=e.get('tmdate'),
            rating=rating, rating_raw=state.get('content', {}).get(rating_key),
            rating_field_changed=rating_key in e['note'].get('content', {})))
        previous = rating
    if previous != result['current_rating']:
        result['issues'].append('reconstructed_final_rating_mismatch')
    if gap:
        result['issues'].append('non_comparable_rating_gap')
    if not result['issues']:
        result['history_status'] = 'checked_accessible_history'
        for e in pending:
            e['data_quality_status'] = 'checked_accessible_history_not_independent_ui_verified'
    result['events'] = pending
    result['coverage_note'] = 'Visible Edits only; hidden/deleted/destructively overwritten versions cannot be ruled out'
    return result


def public_payload(body, meta):
    """Retain public response objects; do not persist account-private records."""
    if not isinstance(body, dict):
        return body
    meta['received_json_sha256'] = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
    meta['received_array_lengths'] = {}
    meta['nonpublic_records_omitted'] = 0
    meta['nonpublic_field_values_omitted'] = 0
    meta['writer_only_fields_omitted'] = 0
    for key in ('notes', 'edits'):
        if not isinstance(body.get(key), list):
            continue
        meta['received_array_lengths'][key] = len(body[key])
        public = []
        for obj in body[key]:
            if 'everyone' not in obj.get('readers', []):
                meta['nonpublic_records_omitted'] += 1
                continue
            note = obj.get('note', {}) if key == 'edits' else obj
            for entity in (obj, note):
                if 'tauthor' in entity:
                    entity.pop('tauthor')
                    meta['writer_only_fields_omitted'] += 1
            for item in note.get('content', {}).values():
                if isinstance(item, dict) and private_field(item):
                    if 'value' in item:
                        item.pop('value')
                        meta['nonpublic_field_values_omitted'] += 1
            public.append(obj)
        body[key] = public
    # Nested Invitation readers specify form permissions, not private answers.
    if isinstance(body.get('invitations'), list):
        records = body['invitations']
        meta['received_array_lengths']['invitations'] = len(records)
        body['invitations'] = [obj for obj in records if 'everyone' in obj.get('readers', [])]
        meta['nonpublic_records_omitted'] += len(records)-len(body['invitations'])
        for obj in body['invitations']:
            if 'tauthor' in obj:
                obj.pop('tauthor')
                meta['writer_only_fields_omitted'] += 1
            for item in obj.get('content', {}).values():
                if isinstance(item, dict) and private_field(item) and 'value' in item:
                    item.pop('value')
                    meta['nonpublic_field_values_omitted'] += 1
    return body


class StopRequest(APIError):
    pass


class CachedAPI:
    """Reuse SDK client/session; preserve raw JSON and count every HTTP attempt."""
    def __init__(self, directory, args):
        self.directory, self.args = directory, args
        self.state_path = directory/'request_state.json'
        self.state = load(self.state_path) if self.state_path.exists() else {'tasks': {}, 'blocked': {}, 'not_before': None}
        self.calls = 0
        self.last_request = 0.0
        self.api = None
        self.attempt_log = []
        self.stopped = False

    def get(self, endpoint, params):
        key = hashlib.sha256(json.dumps([endpoint, params], sort_keys=True).encode()).hexdigest()
        task = self.state['tasks'].get(key, {})
        if task.get('status') == 'success':
            blob = load(self.directory/task['file'])
            return blob['response'], task['file']
        if self.args.offline:
            error = StopRequest('offline_cached_'+task['status'] if task else 'offline_cache_miss',
                task.get('http_status'), not_tested=True)
            error.source_file = task.get('file')
            raise error
        if self.stopped:
            raise StopRequest('batch_stopped_after_rate_limit', not_tested=True)
        deadline = self.state.get('not_before')
        if deadline and datetime.now(timezone.utc) < datetime.fromisoformat(deadline):
            raise StopRequest('retry_after_cooldown_active', not_tested=True)
        if self.calls >= self.args.max_requests:
            raise StopRequest('max_requests_reached', not_tested=True)
        # Authenticate only after checking persisted server cooldown/budget.
        if self.state.get('access_mode') == 'authenticated' and not self.args.authenticate:
            raise StopRequest('authenticated_resume_requires_--authenticate', not_tested=True)
        if self.args.authenticate and self.state.get('access_mode') != 'authenticated':
            try:
                self.api = PublicAPI(authenticate=True)
            except APIError:
                self.stopped = True
                raise StopRequest('official_authentication_failed_batch_stopped', not_tested=True) from None
            self.state.update(access_mode='authenticated', blocked={})
            save(self.state_path, self.state)
        if endpoint in self.state['blocked']:
            raise StopRequest('endpoint_stopped_after_access_restriction',
                self.state['blocked'][endpoint]['status'], not_tested=True)
        if self.api is None:
            try:
                self.api = PublicAPI(authenticate=self.args.authenticate)
            except APIError:
                self.stopped = True
                raise StopRequest('official_client_or_authentication_failed_batch_stopped', not_tested=True) from None
        time.sleep(max(0, self.args.request_delay-(time.monotonic()-self.last_request)))
        self.calls += 1
        start = time.monotonic()
        meta = dict(url=config.API+endpoint, method='GET', params=params, requested_at=now(),
                    sdk_automatic_retries=0, attempt=self.calls)
        body = None
        try:
            response = self.api.client.session.get(meta['url'], params=params,
                headers=self.api.client.headers, timeout=config.TIMEOUT)
            self.last_request = time.monotonic()
            meta.update(http_status=response.status_code, elapsed_seconds=round(time.monotonic()-start, 3),
                response_headers={k:v for k,v in response.headers.items() if k.lower() in
                                  ['content-type', 'date', 'retry-after', 'x-request-id', 'cf-ray']})
            try:
                body = response.json()
            except ValueError:
                body = {'non_json_response': True, 'content_type': response.headers.get('Content-Type')}
            if isinstance(body, dict) and isinstance(body.get('name'), str):
                meta['error_type'] = body['name']
            if response.status_code == 200:
                body = public_payload(body, meta)
            status = 'success' if response.status_code == 200 and isinstance(body, dict) and not body.get('non_json_response') else 'error'
            if response.status_code in (401, 403):
                status = 'forbidden'
                self.state['blocked'][endpoint] = dict(status=response.status_code, at=now())
            if response.status_code == 429:
                status = 'rate_limited'
                self.state['not_before'] = retry_deadline(response.headers.get('Retry-After'))
                # Stop this run. A later --resume may retry after the exact cooldown.
                self.stopped = True
        except Exception as exc:
            import requests
            if not isinstance(exc, requests.RequestException):
                raise
            meta.update(http_status=None, error_type=type(exc).__name__, elapsed_seconds=round(time.monotonic()-start, 3))
            status = 'error'
        dest = Path('responses')/(key[:16]+'_'+format(time.time_ns(), 'x')+'.json')
        save(self.directory/dest, {'request': meta, 'response': body})
        meta['response_sha256'] = hashlib.sha256((self.directory/dest).read_bytes()).hexdigest()
        self.state['tasks'][key] = dict(status=status, file=dest.as_posix(), **meta)
        self.attempt_log.append(dict(endpoint=endpoint, **meta))
        save(self.state_path, self.state)
        print(endpoint, status, meta.get('http_status'), flush=True)
        if status != 'success':
            error = APIError(f'{endpoint}: {status}; HTTP {meta.get("http_status")}', meta.get('http_status'))
            error.source_file = dest.as_posix()
            raise error
        return body, dest.as_posix()

    def pages(self, endpoint, key, params, maximum=None):
        records, sources, offset = [], [], 0
        expected = None
        while maximum is None or len(records) < maximum:
            size = min(100, maximum-len(records)) if maximum is not None else 100
            body, source = self.get(endpoint, {**params, 'limit': size, 'offset': offset})
            batch = body[key]
            if not isinstance(batch, list):
                raise APIError('unexpected_list_schema')
            if batch and offset and {n['id'] for n in batch} <= {n['id'] for n in records}:
                raise APIError('pagination_did_not_advance')
            received = load(self.directory/source)['request'].get('received_array_lengths', {}).get(key, len(batch))
            records.extend(batch);sources.append(source);offset += received
            expected = body.get('count') if isinstance(body.get('count'), int) else expected
            if received < size or expected is not None and offset >= expected:
                break
        return records, sources, expected


def decision(paper, notes, venue):
    raw = value(paper, 'venueid')
    status_map = {'Withdrawn_Submission': 'Withdraw', 'Desk_Rejected_Submission': 'Desk_Reject',
                  'Rejected_Submission': 'Reject'}
    for suffix, label in status_map.items():
        if raw == venue+'/'+suffix:
            return label, raw
    if venue == VENUE and raw == VENUE and value(paper, 'venue') in {
            'ICLR 2025 Poster', 'ICLR 2025 Spotlight', 'ICLR 2025 Oral'}:
        return 'Accept', value(paper, 'venue')
    labels = {value(n, 'decision') for n in notes if any(i.endswith('/-/Decision') for i in invitations(n) or [])}
    labels.discard(None)
    if len(labels) == 1:
        label = next(iter(labels))
        if isinstance(label, str) and re.match(r'^Accept\b', label, re.I):
            return 'Accept', label
        if label == 'Reject':
            return 'Reject', label
        return 'Other', label
    return 'Unknown', raw


def jsonl(path, rows):
    path.write_text(''.join(json.dumps(r, ensure_ascii=False)+'\n' for r in rows), encoding='utf-8')


def csvfile(path, fields, rows):
    with path.open('w', encoding='utf-8-sig', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fields, extrasaction='ignore')
        writer.writeheader();writer.writerows(rows)


def run(args):
    slug = 'iclr2025' if args.venue == VENUE else re.sub('[^a-z0-9]+', '_', args.venue.lower())
    directory = Path(args.output_dir).resolve() if getattr(args,'output_dir',None) else config.ROOT/'data/score_history'/slug
    directory.mkdir(parents=True, exist_ok=True)
    if (directory/'coverage.json').exists() and not (args.resume or args.offline):
        raise ValueError('Existing run: use --resume or --offline; successful responses are immutable')
    api = CachedAPI(directory, args)
    papers, histories, paper_rows, warnings = [], [], [], []
    source_total = None
    discovery_complete = False
    discovery_mode = 'submission_invitation_number_order_not_random'
    group = None
    review_form_attempted = False
    schema_checks = {}
    try:
        payload, _ = api.get('/groups', {'id': args.venue})
        group = payload['groups'][0]
        submission_id = value(group, 'submission_id')
        review_name = value(group, 'review_name')
        rating_key = value(group, 'review_rating')
        submission_name = value(group, 'submission_name')
        if not all(isinstance(x, str) and x for x in [submission_id, review_name, rating_key, submission_name]):
            raise APIError('venue_schema_missing_required_fields')
        submission_form, submission_source = api.get('/invitations', {'id': submission_id, 'expired': 'true'})
        if not any(i.get('id') == submission_id for i in submission_form['invitations']):
            raise APIError('submission_invitation_not_found')
        schema_checks.update(submission_invitation=submission_id, submission_invitation_source=submission_source)
        selection_path = directory/'selection.json'
        if selection_path.exists():
            frozen = load(selection_path)
            if frozen['venue'] != args.venue or frozen['requested_limit'] != args.limit:
                raise APIError('frozen_sample_scope_mismatch')
            found = frozen['notes']
            sources = frozen['source_files']
            discovery_mode = frozen['sample_method']
            source_total = frozen.get('listing_reported_total')
            discovery_complete = frozen.get('conference_census_complete', False)
        else:
            try:
                found, sources, source_total = api.pages('/notes', 'notes', {'invitation': submission_id,
                    'sort': 'number:asc'}, args.limit)
                discovery_complete = source_total is not None and len(found) >= source_total
            except APIError as exc:
                warnings.append(str(exc))
                discovery_mode = 'official_search_the_limited_not_random_not_conference_census'
                found, sources, _ = api.pages('/notes/search', 'notes', {'term': 'the', 'content': 'all',
                    'group': args.venue+'/', 'source': 'forum'}, args.limit)
            save(selection_path, dict(venue=args.venue, requested_limit=args.limit, notes=found,
                source_files=sources, sample_method=discovery_mode, listing_reported_total=source_total,
                conference_census_complete=discovery_complete))
        selected = {}
        for n in found:
            if submission_id in (invitations(n) or []) and n.get('forum') == n.get('id') and 'everyone' in n.get('readers', []):
                selected[n['id']] = n
        papers = list(selected.values())[:args.limit]
        submission_sources = list(sources)
        if source_total is None:
            try:
                count_body, count_source = api.get('/notes', {'invitation': submission_id,
                    'limit': 1, 'offset': 0, 'count': 'true'})
                source_total = count_body.get('count')
                schema_checks['listing_count_source'] = count_source
            except APIError as exc:
                warnings.append('listing_count_probe: '+str(exc))
        discovery_complete = (discovery_mode == 'submission_invitation_number_order_not_random' and
            isinstance(source_total, int) and len(papers) == source_total)
        for p in papers:
            row = dict(forum_id=p['id'], title=value(p, 'title'), decision_group='Unknown', decision_raw=None,
                       forum_status='unknown', review_count=None, review_history_unknown_count=None)
            paper_rows.append(row)
            row['decision_group'], row['decision_raw'] = decision(p, [], args.venue)
            try:
                try:
                    notes, sources, _ = api.pages('/notes', 'notes', {'forum': p['id']})
                    row['forum_status'] = 'accessible_notes_pagination_complete'
                except APIError as exc:
                    row['notes_error'] = str(exc)
                    notes, sources, _ = api.pages('/notes/search', 'notes', {'term': 'the', 'content': 'all',
                        'group': f'{args.venue}/{submission_name}{p["number"]}/', 'source': 'reply'})
                    row['forum_status'] = 'search_partial_coverage'
                notes = [n for n in notes if n.get('forum') == p['id'] and 'everyone' in n.get('readers', [])]
                row['decision_group'], row['decision_raw'] = decision(p, notes, args.venue)
                expected = f'{args.venue}/{submission_name}{p["number"]}/-/{review_name}'
                reviews = {n['id']:n for n in notes if expected in (invitations(n) or [])}
                row['review_count'] = len(reviews)
                row['review_invitation'] = expected
                row['review_history_unknown_count'] = 0
                if reviews and not review_form_attempted:
                    review_form_attempted = True
                    try:
                        form, form_source = api.get('/invitations', {'id': expected, 'expired': 'true'})
                        matches = [i for i in form['invitations'] if i.get('id') == expected]
                        schema_checks['official_review_form'] = dict(invitation=expected, source=form_source,
                            found=bool(matches), edit_schema=matches[0].get('edit') if matches else None)
                    except (APIError, KeyError, TypeError) as exc:
                        schema_checks['official_review_form'] = dict(invitation=expected, error=str(exc))
                for review in reviews.values():
                    provenance = {'forum_sources': sources}
                    calls_before = api.calls
                    try:
                        blob, source = api.get('/notes/edits', {'note.id': review['id'], 'sort': 'tcdate:asc',
                                                              'limit': 1000, 'count': 'true'})
                        edits = blob['edits']
                        complete = isinstance(blob.get('count'), int) and blob['count'] == len(edits)
                        restored = restore(review, edits, rating_key, args.venue, complete)
                        provenance['edits_source'] = source
                        restored['request_status'] = 'success' if edits else 'empty'
                    except (APIError, KeyError, TypeError, ValueError) as exc:
                        restored = restore(review, [], rating_key, args.venue)
                        status = ('rate_limited' if getattr(exc,'status',None)==429 else
                                  'forbidden' if getattr(exc,'status',None) in (401,403) else
                                  'not_attempted' if getattr(exc,'not_tested',False) else 'error')
                        restored.update(request_status=status, request_error=str(exc))
                        restored['issues'] = ['history_request_'+status]
                        restored['visible_edit_count'] = None
                        provenance['last_failed_response'] = getattr(exc,'source_file',None)
                    restored['request_attempted_this_run'] = api.calls > calls_before
                    restored['provenance'] = provenance
                    restored['review_invitation'] = expected
                    restored['decision_group'] = row['decision_group']
                    histories.append(restored)
                    if restored['history_status'] == 'unknown':
                        row['review_history_unknown_count'] += 1
                jsonl(directory/'review_score_histories.jsonl', histories)
            except (APIError, KeyError, TypeError, ValueError) as exc:
                row['forum_error'] = str(exc)
    except (APIError, KeyError, IndexError, TypeError, ValueError) as exc:
        warnings.append(str(exc))
    events = [e for h in histories for e in h['events']]
    checked_events = [e for h in histories if h['history_status'] != 'unknown' for e in h['events']]
    for row in paper_rows:
        ev = [e for e in events if e['forum_id'] == row['forum_id']]
        checked = [e for e in checked_events if e['forum_id'] == row['forum_id']]
        row.update(observed_change_events=len(ev), observed_increase=any(e['score_delta']>0 for e in ev),
                   observed_decrease=any(e['score_delta']<0 for e in ev),
                   checked_history_change_events=len(checked),
                   data_quality_status='checked_accessible_history' if row['forum_status']=='accessible_notes_pagination_complete'
                   and not row['review_history_unknown_count'] else 'partial_or_unknown')
    previous_attempts = [load(f)['request'] for f in sorted((directory/'responses').glob('*.json'))]
    ui_checks = load(directory/'revision_ui_checks.json') if (directory/'revision_ui_checks.json').exists() else []
    summary = dict(venue=args.venue, requested_limit=args.limit, sample_method=discovery_mode,
        generated_at=now(), discussion_timeline=TIMELINE if args.venue==VENUE else None,
        conference_accessible_submission_count=source_total if discovery_complete else None,
        listing_reported_total=source_total, conference_census_complete=discovery_complete,
        actual_public_submissions=len(papers), papers_with_official_reviews=sum(bool(r['review_count']) for r in paper_rows),
        reviews_discovered=len(histories), review_history_http_success=sum(h.get('request_status') in ('success','empty') for h in histories),
        review_histories_checked=sum(h['history_status']!='unknown' for h in histories),
        papers_with_observed_score_changes=len({e['forum_id'] for e in events}),
        papers_with_increase=len({e['forum_id'] for e in events if e['score_delta']>0}),
        papers_with_decrease=len({e['forum_id'] for e in events if e['score_delta']<0}),
        observed_score_change_events=len(events), checked_history_score_change_events=len(checked_events),
        papers_with_during_discussion_increase=len({e['forum_id'] for e in events if e['score_delta']>0 and e['discussion_phase']=='during_discussion'}),
        papers_with_during_discussion_decrease=len({e['forum_id'] for e in events if e['score_delta']<0 and e['discussion_phase']=='during_discussion'}),
        observed_delta_distribution=dict(Counter(f'{e["score_delta"]:+g}' for e in events)),
        unknown_reviews=sum(h['history_status']=='unknown' for h in histories),
        papers_with_unknown_history=sum(r['forum_status']!='accessible_notes_pagination_complete' or
            bool(r.get('review_history_unknown_count')) for r in paper_rows),
        decision_groups={}, requests_this_run=api.calls,
        http_success_this_run=sum(r.get('http_status')==200 for r in api.attempt_log),
        warnings=warnings, raw_response_state='request_state.json',
        authentication_context=api.state.get('access_mode','anonymous'),
        sdk_login_requests_in_data_budget=False,
        retry_not_before=api.state.get('not_before'), blocked_endpoints=api.state['blocked'],
        cooldown_active=bool(api.state.get('not_before') and
            datetime.now(timezone.utc) < datetime.fromisoformat(api.state['not_before'])),
        schema_checks=schema_checks,
        rating_schema_observed=dict(field=value(group,'review_rating') if group else None,
            types=dict(Counter(type(h['current_rating']).__name__ for h in histories)),
            values=sorted({h['current_rating'] for h in histories if h['current_rating'] is not None}),
            review_invitation_examples=sorted({h['review_invitation'] for h in histories})[:5]),
        data_http_attempts_all_runs=len(previous_attempts),
        data_http_statuses_all_runs=dict(Counter(str(e.get('http_status')) for e in previous_attempts)),
        known_review_count_is_lower_bound=any(r['forum_status']!='accessible_notes_pagination_complete' for r in paper_rows),
        pilot_histories_completed=(len(papers)==args.limit and all(r['forum_status']=='accessible_notes_pagination_complete' and
            not r['review_history_unknown_count'] for r in paper_rows)),
        missing_history_not_counted_as_no_change=True, revision_ui_checks=ui_checks,
        ui_validation_status='verified' if ui_checks and all(c.get('verified') for c in ui_checks) else 'not_verified')
    for label in sorted({r['decision_group'] for r in paper_rows}):
        ids={r['forum_id'] for r in paper_rows if r['decision_group']==label}
        summary['decision_groups'][label]=dict(papers=len(ids),
            observed_changed_papers=len(ids & {e['forum_id'] for e in events}),
            observed_change_events=sum(e['forum_id'] in ids for e in events))
    jsonl(directory/'submissions.jsonl', [dict(forum_id=p['id'], submission_note_id=p['id'],
        note=p, discovery_source=discovery_mode, response_files=submission_sources) for p in papers])
    jsonl(directory/'review_score_histories.jsonl', histories)
    csvfile(directory/'score_change_events.csv', EVENT_FIELDS, events)
    csvfile(directory/'papers_with_score_changes.csv',
        ['forum_id','title','decision_group','decision_raw','observed_change_events','checked_history_change_events',
         'observed_increase','observed_decrease','review_history_unknown_count','data_quality_status'],
        [r for r in paper_rows if r['observed_change_events']])
    save(directory/'paper_coverage.json', paper_rows)
    save(directory/'coverage.json', summary)
    with (directory/'run_log.jsonl').open('a',encoding='utf-8') as f:
        f.write(json.dumps(dict(at=now(),args=vars(args),data_requests=api.calls,
            http_statuses=dict(Counter(str(e.get('http_status')) for e in api.attempt_log)),
            pilot_complete=summary['pilot_histories_completed']),ensure_ascii=False)+'\n')
    write_report(directory, summary, events, args)
    print(json.dumps({k:summary[k] for k in ['actual_public_submissions','reviews_discovered',
        'review_history_http_success','review_histories_checked','observed_score_change_events',
        'unknown_reviews','requests_this_run','pilot_histories_completed','cooldown_active']}, ensure_ascii=False), flush=True)
    return 0 if len(papers)==args.limit and summary['unknown_reviews']==0 and summary['papers_with_unknown_history']==0 else 1


def write_report(directory, s, events, args):
    form = s['schema_checks'].get('official_review_form', {})
    rating_spec = (form.get('edit_schema') or {}).get('note', {}).get('content', {}).get(
        s['rating_schema_observed']['field'], {})
    rating_param = rating_spec.get('value', {}).get('param', {})
    rating_options = [x.get('value') if isinstance(x, dict) else x for x in rating_param.get('enum', [])]
    labels = [('实际公开投稿样本','actual_public_submissions'),('发现Official Review的论文','papers_with_official_reviews'),
        ('发现Review','reviews_discovered'),('Edits HTTP成功Review','review_history_http_success'),
        ('历史成功检查Review','review_histories_checked'),('观察到改分论文','papers_with_observed_score_changes'),
        ('观察到加分论文','papers_with_increase'),('观察到减分论文','papers_with_decrease'),
        ('可见评分变化事件','observed_score_change_events'),('历史检查通过的变化事件','checked_history_score_change_events'),
        ('公布讨论时段加分论文','papers_with_during_discussion_increase'),('公布讨论时段减分论文','papers_with_during_discussion_decrease'),
        ('历史unknown Review','unknown_reviews'),('历史或Forum覆盖unknown论文','papers_with_unknown_history')]
    lines=['# ICLR 2025 评分历史试运行','',f'生成UTC：{s["generated_at"]}。目标{args.limit}篇，实际取得{s["actual_public_submissions"]}篇。',
        '', '仅访问官方公开接口，无LLM调用、真实身份推断或评分原因分析。未启动全会议采集。',
        '',f'采样：`{s["sample_method"]}`。按官方列表编号顺序取有限样本，不冒充随机样本；搜索回退则为含词限制的便利样本。',
        '', '## 实际运行','',f'```powershell\n.\\.venv311\\Scripts\\python.exe src/score_history.py --venue {args.venue} --limit {args.limit} --request-delay {args.request_delay:g} --max-requests {args.max_requests}'+(' --resume' if args.resume else '')+(' --authenticate' if args.authenticate else '')+(' --offline' if args.offline else '')+'\n```',
        '',f'本次HTTP尝试{s["requests_this_run"]}次，200为{s["http_success_this_run"]}次；查询成功率：'+(f'{s["http_success_this_run"]/s["requests_this_run"]:.1%}' if s['requests_this_run'] else '不适用（无网络请求）')+'。',
        '',f'累计数据HTTP请求{s["data_http_attempts_all_runs"]}次，状态分布`{s["data_http_statuses_all_runs"]}`。SDK登录请求不在数据请求预算内，不保存其请求体或响应。认证上下文：{s["authentication_context"]}。',
        '',f'{args.limit}篇完整历史测试是否完成：**{s["pilot_histories_completed"]}**。取得Submission并不等于完成评分历史检查。',
        '', '| 指标 | 实测 |','|---|---:|']
    lines += [f'| {label} | {s[key]} |' for label,key in labels]
    lines += ['', '## 实际Invitation和字段', '',
        f'Submission：`{s["schema_checks"].get("submission_invitation")}`；来源`{s["schema_checks"].get("submission_invitation_source")}`。',
        '',f'Official Review表单：`{form.get("invitation")}`；来源`{form.get("source")}`；是否取得：{form.get("found", False)}。',
        '',f'Rating字段：`{s["rating_schema_observed"]["field"]}`；表单声明类型`{rating_param.get("type")}`；选项`{rating_options}`。未取得表单时保持未核验，不猜测其它会议结构。',
        '', '发现的Review Invitation示例：`'+str(s['rating_schema_observed']['review_invitation_examples'])+'`。']
    lines += ['',f'全会议公开可访问投稿数：{s["conference_accessible_submission_count"] if s["conference_census_complete"] else "unknown；本次未穷举会议"}。列表接口报告总量：{s["listing_reported_total"]}。',
        '', '以上为可见记录下界，不能把unknown当未改分。未知Review数量也仅指已发现的Review；未成功读取Forum的论文，其Review总数仍未知。可见历史不完整时，事件标为`visible_transition_partial_history`，不纳入“历史检查通过事件”；其delta仅是相邻可见数值差，不能排除中间隐藏修改。同篇可同时加减分；论文、Review、事件分别统计。',
        '', '## 真实事件示例','', '| Forum | Review | 前→后 | UTC | 阶段 | Edit | 质量 |','|---|---|---|---|---|---|---|']
    for e in events[:10]:
        lines.append(f'| {e["forum_id"]} | {e["review_note_id"]} | {e["score_before"]}→{e["score_after"]} | {e["changed_at"]} | {e["discussion_phase"]} | {e["original_edit_id"]} | {e["data_quality_status"]} |')
    if not events:lines += ['', '未取得可报告的真实评分变化事件；不补造正向样本，不据此断言会议没有改分。']
    lines += ['', '变化幅度分布：`'+json.dumps(s['observed_delta_distribution'],ensure_ascii=False)+'`。',
        '', '决定分组（保留原决定，Withdraw/Desk Reject/Reject/Accept/Unknown不合并）：','',
        '```json',json.dumps(s['decision_groups'],ensure_ascii=False,indent=2),'```',
        '', '## 时间和版本语义','',
        '[官方Dates](https://iclr.cc/Conferences/2025/Dates)展示讨论日期为2024-11-13至11-26，时区AoE（UTC-12）；另列作者最后回复日11-27，不混为讨论截止。保存的HTML倒计时变量明确公布`discussion_period_starts_1="2024/11/14 11:59:59 UTC"`与`discussion_period_ends_1="2024/11/27 11:59:59 UTC"`。本脚本采用该公布日历的左闭右开区间；这不是系统真实开放操作的日志。CFP旧描述Nov12–26与Dates有差异，予以保留。缺时间或非本会议记unknown，不能把批量公开评审的时间擅自当正式讨论开始。',
        '', '[官方Edits推断规则](https://docs.openreview.net/reference/api-v2/entities/edit/inference)：按不可变tcdate排序，省略字段保持状态，delete删除。首次数值不算改分、仅文字变化不计分、非数值/删除断开可比较链。replacement、删除Edit、同时间顺序冲突、评分Edit被破坏性修改均保守unknown。公开前的私有首次评分缺失时不能声称完整历史；最终值一致也不证明无隐藏修改。',
        '', '## 访问、恢复与缓存','',
        '复用现有PublicAPI的官方SDK v2初始化、会话、超时和禁用SDK重试设置，以及collect_edits.retry_deadline。独立脚本通过SDK会话GET官方URL，保存公开响应且逐次计数，不叠加PublicAPI.get自动重试。数据HTTP尝试计入max-requests，数据阶段SDK重试为0；交互登录由现有SDK管理，不在数据请求预算内。',
        '', '`--resume`复用成功缓存、重试失败任务，并复用冻结的相同样本。403/401在同一认证上下文内停止相应endpoint；只有明确请求官方登录且认证成功后才可重新测试匿名受限接口。429记录Retry-After并停止本次批次；恢复时间前resume不请求或登录，无明确时间不密集重试。网络/5xx失败保存状态，后续显式resume才重试。`--offline`只读缓存，无登录、无网络。',
        '', '各次公开响应、URL、参数、UTC、安全响应头和SHA-256保存在responses/及request_state.json。若认证返回非公开记录/字段，则省去私有内容并记录排除数、接收JSON哈希和原数组长度以保证分页正确；不把私有历史当公开证据。不记录认证头、密码、Token或Cookie。`--authenticate`复用getpass终端认证，不通过命令行传凭据。',
        '', '## 限制及下一步','',
        'Revision History页面对照状态：'+s['ui_validation_status']+'；不将离线算法测试冒充页面人工核验。`coverage.json`中的revision_ui_checks记录独立核验状态。',
        '', '本次失败/限制：'+json.dumps(s['warnings'],ensure_ascii=False),
        '',f'最近服务器冷却截止UTC：`{s["retry_not_before"]}`；当前是否仍在冷却：`{s["cooldown_active"]}`；受限endpoint：`{s["blocked_endpoints"]}`。429限额及恢复时间以原始响应为准；限额作用范围未证实，不猜测是全站还是特定路由。',
        '', '预计全会议请求量（未执行）：设公开投稿N，每篇平均R条Review，Edits不截断，则约ceil(N/100)+N+N×R，加上Groups与Invitation元数据；例如R=4约5N次。若采用每篇Invitation核对会再增加最多N次，分页Forum和补查另计。Edits单次最多1000条，返回count大于所得记录时标unknown，不声称分页完整。若总N尚不可得，不臆造会议总请求数。',
        '', '未确认完整历史之前，不能根据当前评分、mdate或作者回复时间推断评分变化；讨论期内加分也不意味着由Rebuttal或补实验导致。全量运行前必须另获用户确认，本脚本默认limit=50，不提供隐式无限采集。']
    if isinstance(s['listing_reported_total'], int):
        n=s['listing_reported_total']
        lines += ['',f'以本账号列表接口报告N={n:,}为估算基数，假设R=4，含3次元数据请求约需 **{math.ceil(n/100)+5*n+3:,} 次数据请求**。这不是已执行量，也不是已完成公有记录全覆盖的证明。便利样本不能外推全会议改分率。']
    if (directory/'execution_audit.json').exists():
        lines += ['', '## 本次实际联网执行记录', '']
        for entry in load(directory/'execution_audit.json'):
            lines += ['```powershell',entry['command'],'```',
                f'实际数据请求：{entry["data_requests"]}；结果：{entry["result"]}。','']
        statuses=s['data_http_statuses_all_runs']
        if s['data_http_attempts_all_runs']:
            lines += [f'累计数据查询成功率：{statuses.get("200",0)}/{s["data_http_attempts_all_runs"]} = '+
                f'{statuses.get("200",0)/s["data_http_attempts_all_runs"]:.1%}。HTTP成功不等于评分历史完整。','']
    check_file=directory/'checks/validation.json'
    if check_file.exists():
        checks=load(check_file)
        lines += ['', '## 已保存的本地校验快照与页面核验', '',
            '以下为checks/validation.json对应的历史校验快照，不自动代替之后新增数据的检查。', '',
            f'pytest：{checks["pytest"]}；完整输出`{checks["pytest_output"]}`。初次测试的临时路径错误输出也保留在`pytest_output.txt`，后续使用仓库内短路径并修正缓存文件名长度后通过。',
            '',f'{checks["preexisting_files_hash_checked"]}份已有文件SHA-256保持不变；{checks["submissions"]}个Forum ID、{checks["reviews"]}个已发现Review ID无重复，原始请求与Review关联检查通过。扫描未发现保存认证头、密码、Token、Cookie的字段。',
            '', '额外只读复核本地既有ICLR 2025的6条真实Review缓存：均缺少可确认的初始评分，未观察到可恢复的变化。这6条不计入50篇试验统计，也不冒充真实正向改分样本。位置`checks/cached_real_2025_review_checks.json`。',
            '', '本次初始浏览器对照环境初始化失败（kernel assets / os error 3）；网页读取工具也未取得第一条Revision History内容或可确认HTTP状态码，不能将此工具失败写成“页面403”。当前页面核验状态以coverage.json及下面各条记录为准。', '']
        for item in s['revision_ui_checks']:
            lines += [f'- [Review `{item["review_note_id"]}`]({item["url"]})：{item["status"]}；verified={item["verified"]}。']
    lines += ['', '## 当前结论及最小下一步', '',
        f'本轮取得{s["actual_public_submissions"]}篇投稿，发现{s["reviews_discovered"]}条Review；请求目标的完整评分历史核验'+('已完成。' if s['pilot_histories_completed'] else '未完成。')+
        f' 可见变化事件{s["observed_score_change_events"]}条，完整可访问历史检查通过的事件{s["checked_history_score_change_events"]}条；unknown不算未改分。',
        '', '若存在unknown，需按每条记录区分限速、权限、未尝试和初始评分缺失。历史缺失的具体原因（公开权限或保留方式）未由官方确认时不能武断归因。全量统计前应先核对Revision History初始评分可见性，并向OpenReview确认合法研究额度或公开历史批量导出方式。',
        '', f'继续同一{args.limit}篇试验需等retry_not_before后再使用--resume及本地交互认证；成功缓存不重复请求，样本ID不重抽。本次没有安排后台任务。全量仍需另行确认；可用--output-dir选择独立目录保留冻结试验。',
        '', 'phase仅表示所选官方公布日历口径；Dates与旧CFP的日期差异未通过平台开放日志消解，不可用作实际Rebuttal开始时间的独立证明，更不可据此推断评分变化原因。']
    report=(directory/'score_history_report.md') if getattr(args,'output_dir',None) else config.ROOT/'reports'/('iclr2025_score_history.md' if args.venue==VENUE else directory.name+'_score_history.md')
    report.write_text('\n'.join(lines)+'\n',encoding='utf-8')


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--venue',default=VENUE)
    parser.add_argument('--output-dir',help='Separate run directory; never expand/replace a frozen pilot sample on resume')
    parser.add_argument('--limit',type=int,default=50)
    parser.add_argument('--resume',action='store_true')
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--authenticate',action='store_true')
    parser.add_argument('--request-delay',type=float,default=15)
    parser.add_argument('--max-requests',type=int,default=350,help='Maximum data HTTP attempts; SDK login requests are separate')
    args=parser.parse_args()
    if args.limit<1 or args.max_requests<0 or not math.isfinite(args.request_delay) or args.request_delay<0:
        parser.error('limit must be positive; request delay and max requests must be nonnegative')
    return run(args)


if __name__=='__main__':
    raise SystemExit(main())
