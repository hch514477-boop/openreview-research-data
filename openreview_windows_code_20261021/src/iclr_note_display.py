"""Derived reading labels, kept separate from original content.title values."""
import re

from src.common import invitations,value


POLICY='public_note_display_v1'


def signature_label(signature):
    if signature.startswith('~'):
        return re.sub(r'\d+$','',signature[1:]).replace('_',' ').strip()
    if signature=='everyone':return 'Everyone'
    # Use only the public signature itself, never resolve anonymous identities.
    return signature.rsplit('/',1)[-1].replace('_',' ')


def display_title(note):
    title=value(note,'title')
    if isinstance(title,str) and title.strip():return title
    ids=invitations(note) or []
    primary=next((i for i in ids if i.rsplit('/',1)[-1] not in ('Edit','Revision')),ids[0] if ids else '')
    label=primary.rsplit('/',1)[-1].replace('_',' ') if primary else 'Note'
    signatures=note.get('signatures') or []
    signer=signature_label(signatures[0]) if signatures else ''
    # Mirrors the ordinary per-paper review title fallback in the public UI.
    paper=re.search(r'/(Paper\d+|Submission\d+)/',primary)
    if paper and 'review' in label.lower():label+=' of '+paper.group(1)
    return label+(' by '+signer if signer else '')


def enrich_display(notes,submission,meta):
    by_id={n['id']:n for n in notes}
    if submission is not None:by_id[submission['id']]=submission
    previous=meta.get('notes',{}).get('display_fields',{}).get('added_fields',[])
    owned={record['note_id']:record['fields'] for record in previous}
    added=[]
    changed=False
    missing=[]
    for note in notes:
        desired={'display_title':display_title(note)}
        parent=by_id.get(note.get('replyto'))
        if parent is not None:
            desired['replying_to']='Replying to '+display_title(parent)
        else:missing.append(note['id'])
        ours={}
        old=owned.get(note['id'],{})
        for key in ('display_title','replying_to'):
            is_owned=key in old and note.get(key)==old[key]
            if key not in desired:
                if is_owned:
                    note.pop(key,None)
                    changed=True
                continue
            if key not in note or is_owned:
                if key not in note or note[key]!=desired[key]:changed=True
                note[key]=desired[key]
                ours[key]=desired[key]
        if ours:
            # Put the reading headings before the content in the saved JSON.
            leading=[key for key in ('replying_to','display_title') if key in ours]
            if list(note)[:len(leading)]!=leading:
                original=dict(note)
                note.clear()
                note.update({key:original[key] for key in leading})
                note.update({key:item for key,item in original.items() if key not in leading})
                changed=True
            added.append(dict(note_id=note['id'],fields=ours))
    meta.setdefault('notes',{})['display_fields']=dict(policy=POLICY,
        scope='Collector-derived reading headings; not original API title fields',
        sources=['content.title','invitation(s)','signatures','replyto and parent public Note'],
        ui_reference='https://github.com/openreview/openreview-web/blob/master/components/NoteTitle.js',
        added_fields=added,unresolved_parent_note_ids=missing)
    return changed
