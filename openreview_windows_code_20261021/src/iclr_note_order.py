"""Reading order based on real replyto edges; original Note objects unchanged."""
from collections import defaultdict

from src.common import load


def discussion_order(notes,forum):
    by_id={n['id']:n for n in notes}
    if len(by_id)!=len(notes): raise ValueError('Duplicate Note ID; cannot order discussion')
    def key(identifier):
        note=by_id[identifier]
        stamp=next((note[k] for k in ('cdate','tcdate') if type(note.get(k)) in (int,float)),float('inf'))
        return stamp,identifier
    children=defaultdict(list)
    roots=[]
    orphans=[]
    for identifier,note in by_id.items():
        parent=note.get('replyto')
        if parent==forum: roots.append(identifier)
        elif parent in by_id:children[parent].append(identifier)
        else:orphans.append(identifier)
    for siblings in children.values():siblings.sort(key=key)
    colors={}
    ordered=[]
    cycles=[]
    def visit(root):
        stack=[(root,False,None)]
        while stack:
            identifier,exiting,parent=stack.pop()
            if exiting:
                colors[identifier]=2
                continue
            if colors.get(identifier)==1:
                cycles.append(dict(note_id=parent,reply_child_id=identifier))
                continue
            if colors.get(identifier)==2:continue
            colors[identifier]=1
            ordered.append(by_id[identifier])
            stack.append((identifier,True,None))
            stack.extend((child,False,identifier) for child in reversed(children[identifier]))
    for identifier in sorted(roots,key=key)+sorted(orphans,key=key):visit(identifier)
    unrooted=sorted(set(by_id)-set(colors),key=key)
    for identifier in unrooted:visit(identifier)
    return ordered,dict(policy='replyto_depth_first_v1',sibling_order='cdate_or_tcdate_then_id',
        scope='Reading order only; no inferred question-answer links',
        orphan_note_ids=sorted(orphans),unrooted_note_ids=unrooted,cycle_edges=cycles)


def order_saved_notes(folder,forum,meta):
    path=folder/'notes.json'
    if not path.exists():return False
    notes=load(path)
    ordered,audit=discussion_order(notes,forum)
    changed=notes!=ordered
    from src.iclr_note_display import enrich_display
    submission_path=folder/'submission.json'
    submission=load(submission_path) if submission_path.exists() else None
    changed=enrich_display(ordered,submission,meta) or changed
    if changed:
        from src.iclr2025_collection import write_existing
        write_existing(path,ordered)
    audit['original_order_sources']=['Forum/batch response checkpoints',
        '.snapshots/before_full_collector/notes.json when reordered']
    meta.setdefault('notes',{})['ordering']=audit
    return changed
