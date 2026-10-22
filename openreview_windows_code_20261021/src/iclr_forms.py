"""Public form snapshots and sourced display labels embedded in saved Notes."""
import copy
import contextlib
import hashlib
import json
from collections import Counter

from src.common import invitations, load, now, save

SCHEMA_VERSION = 1


def form_content(invitation, version):
    path = ('reply', 'content') if version==1 else ('edit', 'note', 'content')
    current = invitation
    if version==2 and not invitation.get('edit'):
        path = ('edit_schema', 'note', 'content')
    for key in path:
        if not isinstance(current, dict):
            return {}
        current = current.get(key, {})
    return current if isinstance(current, dict) else {}


def options(spec, version):
    if version == 1:
        values = spec.get('value-radio', spec.get('value-dropdown', spec.get('values-dropdown', [])))
    else:
        value = spec.get('value', {})
        param = value.get('param', {}) if isinstance(value, dict) else {}
        values = param.get('enum', []) if isinstance(param, dict) else []
    return values if isinstance(values, list) else []


def describe_fields(notes, records, version):
    """Resolve only exact typed enum matches on the Note's own Invitations."""
    result = {}
    counts = Counter()
    for note in notes:
        fields = {}
        ids = invitations(note) or []
        for field, item in note.get('content', {}).items():
            if isinstance(item, dict) and 'everyone' not in item.get('readers', ['everyone']):
                continue
            raw = item.get('value') if isinstance(item, dict) else item
            if isinstance(item, dict) and 'value' not in item:
                continue
            candidates = []
            has_options = False
            for identifier in ids:
                record = records.get(identifier, {})
                if record.get('status') != 'success':
                    continue
                spec = form_content(record['invitation'], version).get(field, {})
                if not isinstance(spec, dict):
                    continue
                enum = options(spec, version)
                has_options |= bool(enum)
                for option in enum:
                    val = option.get('value') if isinstance(option, dict) else option
                    selected = raw if isinstance(raw, list) else [raw]
                    if any(type(val) is type(element) and val == element for element in selected):
                        label = option.get('description') if isinstance(option, dict) else option
                        if isinstance(label, str):
                            candidates.append(dict(invitation_id=identifier, value=val, description=label))
            if not has_options and (not isinstance(raw, (int, float)) or isinstance(raw, bool)):
                continue
            selected = raw if isinstance(raw, list) else [raw]
            labels = [{x['description'] for x in candidates if type(x['value']) is type(element) and x['value']==element}
                      for element in selected]
            status = ('ambiguous' if any(len(matches)>1 for matches in labels) else
                      'resolved' if all(len(matches)==1 for matches in labels) else 'unresolved')
            fields[field] = dict(value=copy.deepcopy(raw), status=status, sources=candidates)
            if status == 'resolved':
                descriptions = [next(iter(matches)) for matches in labels]
                if isinstance(raw, list):
                    fields[field]['descriptions'] = descriptions
                else:
                    fields[field]['description'] = descriptions[0]
            counts[status] += 1
        result[note['id']] = dict(invitation_ids=ids, fields=fields)
    return result, dict(counts)


def enrich_notes(folder, descriptions, meta):
    """Add sourced labels in-place, retaining exact pre-enrichment bytes once."""
    path = folder/'notes.json'
    if not path.exists():
        return 0
    original_bytes = path.read_bytes()
    notes = load(path)
    backup = folder/'.snapshots/before_inline_descriptions/notes.json'
    originals = {n['id']:n for n in load(backup)} if backup.exists() else {n['id']:copy.deepcopy(n) for n in notes}
    changed = 0
    previous = meta.get('notes', {}).get('inline_descriptions', {}).get('added_fields', [])
    added = []
    for note in notes:
        original_content = originals.get(note['id'], {}).get('content', {})
        content = note.get('content', {})
        # Remove only our own prior additions before rebuilding, so an unavailable
        # or changed form cannot leave a stale label beside a different score.
        for owned in previous:
            if owned['note_id'] != note['id']:
                continue
            field, key = owned['field'], owned['key']
            if owned['wrapped']:
                item = content.get(field)
                original_item = original_content.get(field)
                if (isinstance(item,dict) and item.get(key)==owned['label']
                        and (not isinstance(original_item,dict) or key not in original_item)):
                    item.pop(key,None)
            elif content.get(key)==owned['label'] and key not in original_content:
                content.pop(key,None)
        for field, descriptor in descriptions.get(note['id'], {}).get('fields', {}).items():
            if descriptor.get('status')!='resolved' or field not in content:
                continue
            item = content[field]
            raw = item.get('value') if isinstance(item,dict) else item
            if type(raw) is not type(descriptor['value']) or raw!=descriptor['value']:
                continue
            key = 'descriptions' if isinstance(raw,list) else 'description'
            label = descriptor.get(key)
            if label is None:
                continue
            if isinstance(item,dict):
                if key in item:  # Preserve any label originally returned by API.
                    continue
                item[key] = copy.deepcopy(label)
                added.append(dict(note_id=note['id'],field=field,key=key,wrapped=True,label=label))
            else:
                # Preserve API v1's flat value and add a sibling description.
                key = field+'_'+key
                if key in content:
                    continue
                content[key] = copy.deepcopy(label)
                added.append(dict(note_id=note['id'],field=field,key=key,wrapped=False,label=label))
    if notes != json.loads(original_bytes):
        if not backup.exists():
            backup.parent.mkdir(parents=True,exist_ok=True)
            temporary = backup.with_suffix('.json.tmp')
            temporary.write_bytes(original_bytes)
            temporary.replace(backup)
        save(path,notes)
        changed = len(added)
    meta.setdefault('notes', {})['inline_descriptions'] = dict(
        status='applied',added_fields=added,fields=len(added),
        source='invitations.json',scope='current_public_form_labels_added_by_collector',
        original_backup=str(backup.relative_to(folder)) if backup.exists() else None,
        original_sha256=hashlib.sha256(backup.read_bytes()).hexdigest() if backup.exists() else None)
    return changed


def collect_forms(api, folder, submission, notes, directory, meta):
    # Imported here to avoid a module cycle with the shared collector.
    from src.iclr2025_collection import RequestFailure
    from src.iclr_note_order import order_saved_notes
    order_saved_notes(folder,submission['id'],meta)

    version = getattr(api, 'api_version', 2)
    base = api.base_url
    documents = [submission, *notes]
    ids = sorted({identifier for note in documents for identifier in invitations(note) or []})
    cache = directory/'invitations'/f'api_v{version}'
    records, pending = {}, []
    attempted = getattr(api, '_form_attempts', None)
    if attempted is None:
        api._form_attempts = attempted = {}
    for identifier in ids:
        key = hashlib.sha256((base+'\n'+identifier).encode()).hexdigest()
        path = cache/(key+'.json')
        cached = None
        if path.exists():
            try:
                candidate = load(path)
                invitation = candidate['invitation']
                digest = hashlib.sha256(json.dumps(invitation, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                if (candidate['api'] == base and candidate['api_version'] == version
                        and invitation['id'] == identifier and 'everyone' in invitation.get('readers', [])
                        and digest == candidate['sha256']):
                    cached = candidate
            except (KeyError, ValueError, TypeError):
                pass
        if cached is not None:
            records[identifier] = cached
        elif (base, identifier) in attempted:
            records[identifier] = attempted[base, identifier]
        else:
            pending.append(identifier)
    stop = None
    for start in range(0, len(pending), 8):
        batch = pending[start:start+8]
        params = dict(ids=batch)
        if version == 2:
            params['expired'] = 'true'
        try:
            body = api.request('/invitations', params)
            returned = body.get('invitations')
            if not isinstance(returned, list):
                raise RequestFailure('error', 'Invalid_invitation_response')
            found = {obj['id']:obj for obj in returned
                     if obj.get('id') in batch and 'everyone' in obj.get('readers', [])}
            for identifier in batch:
                record = dict(api=base, api_version=version, source_url=base+'/invitations',
                    params=params, retrieved_at=now(), version_scope='current_public_form; historical_form_unverified')
                if identifier not in found:
                    record.update(status='unavailable', error_type='Public_invitation_not_returned')
                else:
                    obj = found[identifier]
                    digest = hashlib.sha256(json.dumps(obj, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
                    record.update(status='success', invitation=obj, sha256=digest)
                    key = hashlib.sha256((base+'\n'+identifier).encode()).hexdigest()
                    with getattr(api,'form_cache_lock',contextlib.nullcontext()):
                        save(cache/(key+'.json'), record)
                records[identifier] = record
                if record['status'] != 'success':
                    attempted[base, identifier] = record
        except RequestFailure as exc:
            for identifier in batch:
                records[identifier] = attempted[base, identifier] = dict(api=base, api_version=version,
                    source_url=base+'/invitations', params=params, **exc.record())
            if exc.status in ('forbidden', 'rate_limited', 'not_attempted') or exc.code in (
                    'SDK_authentication_failed', 'Authentication_terminal_input_unavailable', 'ConnectionError', 'ProxyError'):
                stop = exc.record()
                break
    for identifier in ids:
        records.setdefault(identifier, dict(status='not_attempted', error_type='Form_collection_stopped'))
    descriptions, counts = describe_fields(documents, records, version)
    missing_ids = [note['id'] for note in documents if not invitations(note)]
    without_form = [note['id'] for note in documents if note.get('content') and not any(
        records.get(identifier, {}).get('status')=='success'
        and form_content(records[identifier]['invitation'], version)
        for identifier in invitations(note) or [])]
    complete = (not missing_ids and not without_form
                and not counts.get('unresolved') and not counts.get('ambiguous'))
    # Keep complete public Invitation objects intact, including field instructions,
    # option descriptions and unknown future fields.
    save(folder/'invitations.json', records)
    enrich_notes(folder, descriptions, meta)
    # Retire only our own redundant generated sidecar, after Notes are saved.
    legacy=folder/'field_descriptions.json'
    if legacy.exists() and (folder/'notes.json').exists():
        payload=load(legacy)
        if (payload.get('schema_version')==SCHEMA_VERSION
                and str(payload.get('scope','')).startswith('derived_from_current_public_invitations;')
                and isinstance(payload.get('notes'),dict)):
            legacy.unlink()
    meta['forms'] = dict(status='ok' if complete else 'incomplete', schema_version=SCHEMA_VERSION,
        scope='public_form_available_per_note_and_enum_labels_resolved',
        all_referenced_invitations_archived=all(r['status']=='success' for r in records.values()),
        invitation_counts=dict(Counter(r['status'] for r in records.values())), field_counts=counts,
        notes_without_invitation=missing_ids, notes_without_public_form=without_form,
        index='invitations.json', descriptions='notes.json',
        descriptions_in_notes=True,
        history_complete=False, checked_at=now(), stop_reason=stop)
    return meta['forms']
