"""Resume file downloads from local public Notes, without fetching Notes or Edits."""
import argparse
import hashlib
import json
import os
import re
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import config
from src.common import load,now,save,value
from src.iclr_collection import years_from_args
from src import iclr2025_collection as base


def file_tasks(folder,submission,notes):
    identifier=base.safe_id(submission['id'])
    if folder.name!=identifier or submission.get('forum')!=identifier:
        raise ValueError('Submission_directory_mismatch')
    seen=set()
    for note in [submission,*notes]:
        note_id=base.safe_id(note['id'])
        if note_id in seen or note.get('forum')!=identifier:
            raise ValueError('Duplicate_or_cross_forum_note')
        seen.add(note_id)
        if 'everyone' not in note.get('readers',[]):
            continue
        fields=base.attachment_fields(note)
        reference=value(note,'pdf')
        if note_id==identifier and base.arxiv_pdf_url(reference):
            fields.append(('pdf',reference))
        for field,reference in fields:
            suffix=Path(urlsplit(reference).path).suffix.lower() if reference.startswith('/') else '.pdf'
            suffix=suffix or ('.pdf' if field=='pdf' else '.bin')
            relative=(Path('paper.pdf' if field=='pdf' else field+suffix) if note_id==identifier
                else Path('attachments')/note_id/(field+suffix))
            if not (folder/relative).resolve().is_relative_to(folder.resolve()):
                raise ValueError('Attachment_path_outside_paper')
            key=hashlib.sha256(json.dumps([note_id,None,field,reference]).encode()).hexdigest()
            yield key,dict(note_id=note_id,field=field,reference=reference,path=relative.as_posix(),
                edit_id=None,historical=False,last_attempt_at=None,status='not_attempted',
                note_timestamps={k:note[k] for k in ('cdate','tcdate','mdate','tmdate') if k in note},
                version_binding='literal_saved_attachment_path' if reference.startswith('/') else 'explicit_arxiv_reference')


def cached_file(folder,target,previous,meta):
    path=folder/target['path']
    if not path.is_file():return None
    if previous.get('reference') and previous['reference']!=target['reference']:
        return None
    if target['path']=='paper.pdf' and meta.get('pdf',{}).get('reference') not in (None,target['reference']):
        return None
    content=path.read_bytes()
    try:base.check_attachment(content,path.suffix.lower())
    except base.RequestFailure:return None
    digest=hashlib.sha256(content).hexdigest()
    expected=previous.get('sha256') if previous.get('reference')==target['reference'] else None
    if target['path']=='paper.pdf' and not expected and meta.get('pdf',{}).get('reference')==target['reference']:
        expected=meta.get('pdf',{}).get('sha256')
    if not expected or digest!=expected:return None
    return dict(bytes=len(content),sha256=digest,cached=True,
        source_binding_verified=previous.get('source_binding_verified',True),
        version_binding=previous.get('version_binding',target['version_binding']),
        last_attempt_at=previous.get('last_attempt_at'))


def supplement(api,folder,submission,notes,meta):
    index_path=folder/'resources.json'
    index=load(index_path) if index_path.exists() else {}
    tasks=list(file_tasks(folder,submission,notes))
    for key,task in tasks:index.setdefault(key,task.copy())
    stop=None
    for key,task in tasks:
        previous=index.get(key,{})
        cached=cached_file(folder,task,previous,meta)
        if cached:
            task.update(cached,status='success')
        else:
            task['last_attempt_at']=now()
            try:
                if task['reference'].startswith('/'):
                    if (getattr(api,'api_version',2)==1
                            and task['reference'].startswith('/attachment/')):
                        # v1 attachments use the SDK Note-field endpoint directly.
                        # It returns the current field, not a verified snapshot.
                        task.update(
                            version_binding='current_note_field; saved_snapshot_unverified',
                            source_binding_verified=False,
                            download_lookup={'endpoint':'/attachment','id':task['note_id'],'name':task['field']})
                        api.initialize()
                        content=api.client.get_attachment(id=task['note_id'],field_name=task['field'])
                    else:
                        content=api.request(task['reference'],{},binary=True)
                else:
                    content=api.download_arxiv_pdf(task['reference'])
                if Path(task['path']).suffix.lower()=='.pdf':
                    content=base.pdf_payload(content)
                base.check_attachment(content,Path(task['path']).suffix.lower())
                path=folder/task['path']
                with base.binary_write_guard(api,folder,len(content),replace=path):
                    path.parent.mkdir(parents=True,exist_ok=True)
                    base.backup(path)
                    temporary=path.with_suffix(path.suffix+'.tmp')
                    temporary.write_bytes(content)
                    temporary.replace(path)
                task.update(status='success',bytes=len(content),sha256=hashlib.sha256(content).hexdigest(),
                    cached=False,source_binding_verified=task.get('source_binding_verified',True))
            except base.RequestFailure as exc:
                task.update(status=exc.status,error_type=exc.code,request_source=exc.source)
                if exc.status in ('forbidden','rate_limited','not_attempted','deferred') or exc.code.startswith(('SDK_authentication','Authentication_')):
                    stop=exc
        if base.arxiv_pdf_url(task['reference']):
            task.update(source_url=base.arxiv_pdf_url(task['reference']),
                version_scope='explicit_arxiv_version' if re.search(r'v\d+(?:\.pdf)?$',task['reference'])
                else 'arxiv_current_version; submission_time_version_unverified')
        index[key]=task
        if task['path']=='paper.pdf':
            meta['pdf']={**task,'status':'ok' if task['status']=='success' else task['status']}
        save(index_path,index)
        if stop:break
    current=[index.get(key,task) for key,task in tasks]
    # Unvisited tasks retain their previous status, not the previous request's error.
    counts=dict(Counter(t['status'] for t in current))
    main_pdf=any(t['path']=='paper.pdf' for t in current)
    if not main_pdf:
        meta['pdf']=dict(status='unavailable',reason='No supported public PDF reference')
    complete=main_pdf and all(t['status']=='success' for t in current)
    meta['resources']={**meta.get('resources',{}),
        'status':'ok' if all(t['status']=='success' for t in current) else 'incomplete',
        'current_counts':counts,'index':'resources.json'}
    meta['attachments_supplement']=dict(status='success' if complete else 'incomplete',
        checked_at=now(),current_counts=counts,history='not_attempted',
        stop_reason=stop.record() if stop else None,
        scope='Only public file references in saved Submission and Forum Notes; no external link traversal')
    meta['phase_a_complete']=bool(meta.get('submission',{}).get('status')=='ok'
        and meta.get('notes',{}).get('status')=='ok' and meta.get('pdf',{}).get('status')=='ok'
        and meta.get('last_validation',{}).get('unique_ids') and meta.get('last_validation',{}).get('forum_links'))
    meta['phase_a_with_resources_complete']=bool(meta['phase_a_complete'] and complete)
    meta['main_collection_complete']=bool(meta['phase_a_with_resources_complete']
        and meta.get('forms',{}).get('status')=='ok')
    base.write_existing(folder/'metadata.json',meta)
    if stop:raise stop
    return meta['attachments_supplement']


def parallel_year(args,year,raw,state,summary,apis):
    from src.iclr_parallel import collect_parallel
    folders=[p for p in sorted(raw.iterdir()) if p.is_dir() and (p/'submission.json').exists()
        and (not args.paper_id or p.name==args.paper_id)]
    if args.limit is not None:folders=folders[:max(0,args.limit-summary['papers'])]
    if not folders:return
    first=load(folders[0]/'metadata.json')
    version=first.get('api_version')
    url={1:'https://api.openreview.net',2:'https://api2.openreview.net'}.get(version)
    if not url or first.get('source')!=url:raise ValueError('Invalid saved API metadata')
    api=apis.get(version)
    if api is None:
        api=base.Transport(state/f'api_v{version}',args.request_delay,args.max_requests,
            args.authenticate,args.max_attachment_mb,args.min_free_gb,url,version)
        apis[version]=api
    api.max_requests=api.calls+max(0,args.max_requests-sum(a.calls for a in apis.values()))
    api.attachment_delay=args.request_delay  # Shared spacing across every worker.
    api.binary_workers=args.workers
    def collect(worker,folder):
        paper=load(folder/'submission.json');notes=load(folder/'notes.json');meta=load(folder/'metadata.json')
        if meta.get('api_version')!=version or meta.get('source')!=url:
            raise ValueError('Saved paper API mismatch')
        tasks=list(file_tasks(folder,paper,notes))
        previous=load(folder/'resources.json') if (folder/'resources.json').exists() else {}
        cached=sum(cached_file(folder,t,previous.get(k,{}),meta) is not None for k,t in tasks)
        print(f'Attachments ICLR {year}: {folder.name}',flush=True)
        return supplement(worker,folder,paper,notes,meta),len(tasks),cached
    def record(folder,result):
        status,count,cached=result
        summary['papers']+=1;summary['files']+=count;summary['cached_files']+=cached
        summary['success_papers']+=status['status']=='success'
        if status['status']!='success':summary['failures'].append(dict(year=year,paper_id=folder.name,status='incomplete'))
        save(state/'progress.json',{**summary,'updated_at':now(),'requests':sum(a.calls for a in apis.values())})
    collect_parallel(api,folders,args.workers,collect,record)


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--raw-root',type=Path,required=True,help='Existing raw data root containing ICLR_YYYY')
    parser.add_argument('--years',nargs='+',default=['2013-2025'])
    parser.add_argument('--limit',type=int,help='Maximum saved papers across all selected years')
    parser.add_argument('--paper-id',help='Optional single saved Forum ID')
    parser.add_argument('--authenticate',action='store_true')
    parser.add_argument('--workers',type=int,default=1)
    parser.add_argument('--dry-run',action='store_true',help='Local inventory only; no login, network or paper writes')
    parser.add_argument('--wait-on-429',action='store_true')
    parser.add_argument('--request-delay',type=float,default=10)
    parser.add_argument('--max-requests',type=int,default=1000)
    parser.add_argument('--max-attachment-mb',type=float,default=512)
    parser.add_argument('--min-free-gb',type=float,default=20)
    args=parser.parse_args(argv)
    if not 1<=args.workers<=5:parser.error('--workers must be between 1 and 5')
    if args.workers>1 and args.wait_on_429:parser.error('Parallel downloads stop on 429; rerun after cooldown')
    if (args.limit is not None and args.limit<1) or args.request_delay<0 or args.max_requests<1 or args.max_attachment_mb<=0 or args.min_free_gb<0:
        parser.error('Invalid limit, delay, budget or disk threshold')
    try:years=years_from_args(args.years)
    except ValueError as exc:parser.error(str(exc))
    if not args.raw_root.is_dir():parser.error('--raw-root does not exist')
    if args.paper_id:
        try:base.safe_id(args.paper_id)
        except ValueError as exc:parser.error(str(exc))
    state=config.ROOT/'data/collection/iclr_attachments'
    summary=dict(started_at=now(),dry_run=args.dry_run,raw_root=str(args.raw_root.resolve()),
        years=years,missing_years=[],papers=0,files=0,cached_files=0,success_papers=0,failures=[],status='running')
    apis={}
    run_lock=None
    if not args.dry_run:
        from src.iclr_parallel import guard_run
        run_lock=guard_run(config.ROOT,config.ROOT/'data/collection/iclr_multi_year')
        if os.name=='nt':
            import msvcrt
            state.mkdir(parents=True,exist_ok=True)
            run_lock=(state/'collector.lock').open('a+b')
            if run_lock.tell()==0:run_lock.write(b'0');run_lock.flush()
            run_lock.seek(0)
            try:msvcrt.locking(run_lock.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:parser.exit(1,'Another attachment download process is running\n')
    try:
        for year in years:
            raw=args.raw_root/f'ICLR_{year}'
            if not raw.is_dir():summary['missing_years'].append(year);continue
            if args.workers>1 and not args.dry_run:
                parallel_year(args,year,raw,state,summary,apis)
                continue
            for folder in sorted(raw.iterdir()):
                if not folder.is_dir() or args.paper_id and folder.name!=args.paper_id:continue
                if args.limit is not None and summary['papers']>=args.limit:break
                submission_path=folder/'submission.json'
                if not submission_path.exists():continue
                try:
                    submission=load(submission_path);notes=load(folder/'notes.json');meta=load(folder/'metadata.json')
                    tasks=list(file_tasks(folder,submission,notes))
                    version=meta.get('api_version')
                    api_url=meta.get('source')
                    if version not in (1,2) or api_url!={1:'https://api.openreview.net',2:'https://api2.openreview.net'}[version]:
                        raise ValueError('Missing_or_inconsistent_saved_API_version')
                except (ValueError,KeyError,TypeError,OSError):
                    summary['failures'].append(dict(year=year,paper_id=folder.name,error_type='Invalid_local_JSON_or_API_metadata'))
                    continue
                summary['papers']+=1;summary['files']+=len(tasks)
                previous=load(folder/'resources.json') if (folder/'resources.json').exists() else {}
                summary['cached_files']+=sum(cached_file(folder,t,previous.get(k,{}),meta) is not None for k,t in tasks)
                if args.dry_run:continue
                if version not in apis:
                    apis[version]=base.Transport(state/f'api_v{version}',args.request_delay,args.max_requests,
                        args.authenticate,args.max_attachment_mb,args.min_free_gb,api_url,version)
                api=apis[version]
                api.max_requests=api.calls+max(0,args.max_requests-sum(a.calls for a in apis.values()))
                print(f'Attachments ICLR {year}: {folder.name}',flush=True)
                while True:
                    try:
                        result=supplement(api,folder,submission,notes,meta)
                        summary['success_papers']+=result['status']=='success'
                        if result['status']!='success':summary['failures'].append(dict(year=year,paper_id=folder.name,status='incomplete'))
                        break
                    except base.RequestFailure as exc:
                        if exc.status=='rate_limited' and args.wait_on_429 and api.calls<api.max_requests:
                            from src.iclr_cooldown import wait_for_limit
                            wait_for_limit(api,year,{'stop_reason':exc.record()},state/'wait_status.json')
                            continue
                        raise
                save(state/'progress.json',{**summary,'updated_at':now(),'requests':sum(a.calls for a in apis.values())})
        summary['status']='success' if not summary['failures'] else 'incomplete'
        if not summary['papers'] and not summary['failures']:
            summary.update(status='not_attempted',stop_reason={'error_type':'No_matching_saved_papers'})
    except base.RequestFailure as exc:
        summary.update(status=exc.status,stop_reason=exc.record())
    except KeyboardInterrupt:
        summary.update(status='paused',stop_reason={'error_type':'Interrupted; saved files retained'})
    summary.update(finished_at=now(),requests=sum(a.calls for a in apis.values()))
    report=config.ROOT/'reports/iclr_attachments_report.json'
    save(report,summary)
    report.with_suffix('.md').write_text('# ICLR 附件补采报告\n\n```json\n'+json.dumps(summary,ensure_ascii=False,indent=2)+'\n```\n',encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=True),flush=True)
    return 0 if summary['status']=='success' else 1
