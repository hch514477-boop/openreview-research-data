"""Sequential public collection; inline form labels with original Note backups."""
import argparse
import copy
import json
import re
from datetime import datetime,timezone
from pathlib import Path

import config
from src.common import invitations, load, now, save, value
from src import iclr2025_collection as base

ROOT=config.ROOT/'data/collection/iclr_multi_year'
REPORT=config.ROOT/'reports/iclr_multi_year_collection.md'


def years_from_args(items):
    result=[]
    for item in items:
        if re.fullmatch(r'\d{4}-\d{4}',item):
            a,b=map(int,item.split('-'))
            if a>b: raise ValueError('Year range must be ascending')
            result.extend(range(a,b+1))
        elif re.fullmatch(r'\d{4}',item):
            result.append(int(item))
        else: raise ValueError('Use years or ranges, for example 2013 2014 2016-2027')
    if not result or any(y<2013 or y>2100 for y in result):
        raise ValueError('Year must be between 2013 and 2100')
    return sorted(set(result))


class Transport(base.Transport):
    def __init__(self,*args,offline=False,**kwargs):
        super().__init__(*args,**kwargs)
        self.offline=offline
        self.cache_directories=[]
        self.cache_hits=0

    def initialize(self):
        if self.offline:
            raise base.RequestFailure('not_attempted','Offline_missing_binary_file')
        return super().initialize()

    def request(self,endpoint,params,binary=False,response=False):
        if self.api_version==1 and endpoint=='/notes':
            # Current read-only API v1 already returns count and rejects the
            # v2-style count query parameter, including forum pagination calls.
            params={k:v for k,v in params.items() if k!='count'}
        if not self.offline:
            return super().request(endpoint,params,binary,response)
        if not binary:
            paths=[p for d in [self.directory,*self.cache_directories] for p in (d/'responses').glob('*.json')]
            for p in sorted(set(paths),key=lambda p:p.stat().st_mtime,reverse=True):
                record=load(p)
                req=record.get('request',{})
                if (req.get('url')==self.base_url+endpoint and req.get('params')==params
                        and req.get('http_status')==200):
                    self.cache_hits+=1
                    body=copy.deepcopy(record['response'])
                    body['_received_lengths']=req.get('received_array_lengths',{})
                    return body
        raise base.RequestFailure('not_attempted','Offline_cache_miss')


def v1_invitations(api,year,directory):
    path=directory/'submission_invitations_v1.json'
    cached=load(path) if path.exists() else None
    if cached and any(i['id'].rsplit('/',1)[-1].lower() in ('submission','blind_submission') for i in cached):
        return cached
    # Match conference submission stages only, excluding workshops/accepted lists.
    pattern=rf'^ICLR\.cc/{year}/(?:[Cc]onference/)?-/(?:[Bb]lind_[Ss]ubmission|[Ss]ubmission|[Ww]ithdrawn_[Ss]ubmission|[Dd]esk_[Rr]ejected_[Ss]ubmission)$'
    # API v1 currently rejects regex-only requests. Verify a bounded set of
    # candidate IDs with the supported ids filter; no candidate is assumed real.
    candidates=[root+'/-/'+name for root in (f'ICLR.cc/{year}/Conference',f'ICLR.cc/{year}/conference',f'ICLR.cc/{year}')
        for name in ('Submission','submission','Blind_Submission','blind_submission',
                     'Withdrawn_Submission','withdrawn_submission','Desk_Rejected_Submission','desk_rejected_submission')]
    records=list(cached or [])
    # The current read-only v1 invitation route also rejects expired/offset.
    # Small exact-ID batches need neither pagination nor those legacy options.
    for start in ([] if cached else range(0,len(candidates),8)):
        batch=api.request('/invitations',{'ids':candidates[start:start+8]})['invitations']
        records.extend(i for i in batch if re.fullmatch(pattern,i['id']))
    if not any(i['id'].rsplit('/',1)[-1].lower() in ('submission','blind_submission') for i in records):
        # Historical Invitation objects may not be exposed although public Notes
        # survive. One documented naming candidate is verified from an actual
        # Note's invitation field; never turn the candidate itself into evidence.
        candidates=([f'ICLR.cc/{year}/conference/-/submission'] if year in (2013,2014) else
            [f'ICLR.cc/{year}/Conference/-/Blind_Submission',
             f'ICLR.cc/{year}/conference/-/submission',
             f'ICLR.cc/{year}/Conference/-/Submission'] if 2017<=year<=2023 else [])
        for candidate in candidates:
            probe=api.request('/notes',{'invitation':candidate,'limit':1})
            confirmed=[n for n in probe['notes'] if candidate in (invitations(n) or []) and n.get('forum')==n.get('id')]
            if confirmed:
                save(directory/'invitation_note_evidence_v1.json',confirmed)
                records.append(dict(id=candidate,invitation_object_available=False,
                    verification='actual_public_note_invitation_field',evidence_file='invitation_note_evidence_v1.json'))
                break
        if not records:
            raise base.RequestFailure('unavailable','No_verified_public_submission_invitation_v1')
    records=list({i['id']:i for i in records}.values())
    save(path,records)
    return records


def canonical_submissions(records):
    """Prefer public blind copies when their explicit original link is available."""
    alias={n['original']:n['id'] for n in records if isinstance(n.get('original'),str)}
    result={}
    for note in records:
        base.safe_id(note['id'])
        if note['id'] in alias and alias[note['id']]!=note['id']:
            continue
        if note.get('forum')!=note['id']:
            raise ValueError('Submission is not the actual Forum root; inspect source instead of inventing ID')
        result[note['id']]=note
    return result,alias


def sample(api,year,directory,limit):
    complete_path=directory/'manifest.json'
    if complete_path.exists():
        manifest=load(complete_path)
        if manifest.get('complete'):
            if manifest.get('api') and manifest['api']!=getattr(api,'base_url',manifest['api']):
                raise base.RequestFailure('error','Cached_API_mismatch_do_not_mix_versions')
            return {**manifest,'papers':dict(list(manifest['papers'].items())[:limit]),'sample':True}
    path=directory/'pilot_manifest.json'
    if path.exists():
        prior=load(path)
        if api.api_version!=1 and prior.get('sample_limit')==limit and prior.get('api')==api.base_url: return prior
    venue=f'ICLR.cc/{year}/Conference'
    if api.api_version==1:
        stages=v1_invitations(api,year,directory)
        # Blind copies normally carry the public forum; other stages are still
        # enumerated in full mode, so this sample never claims conference coverage.
        ids=sorted((x['id'] for x in stages),key=lambda x:('blind' not in x.lower(),x))
        venue=ids[0].split('/-/')[0]
    else:
        group_result=api.request('/groups',{'id':venue})
        group=next((g for g in group_result['groups'] if g['id']==venue),None)
        if group is None: raise base.RequestFailure('unavailable','Venue_group_not_visible')
        save(directory/'venue.json',group)
        invitation=value(group,'submission_id')
        if not invitation and value(group,'submission_name'):
            invitation=venue+'/-/'+value(group,'submission_name')
        if not invitation: raise base.RequestFailure('unavailable','Submission_invitation_not_identified')
        verified=api.request('/invitations',{'id':invitation,'expired':'true'})
        if not any(i['id']==invitation for i in verified['invitations']):
            raise base.RequestFailure('unavailable','Submission_invitation_not_verified')
        save(directory/'invitation.json',verified)
        ids=[invitation]
    papers={}
    queried=[]
    for invitation in ids:
        if len(papers)>=limit:break
        params={'invitation':invitation,'limit':limit-len(papers)}
        if api.api_version==2:
            params.update(offset=0,sort='number:asc',count='true')
        result=api.request('/notes',params)
        for note in result['notes']:
            if invitation not in (invitations(note) or []): raise ValueError('Invitation mismatch')
        found,_=canonical_submissions(result['notes'])
        papers.update(found)
        queried.append(dict(invitation=invitation,api_total=result.get('count'),sampled=len(found)))
    manifest=dict(venue=venue,year=year,api=api.base_url,api_version=api.api_version,papers=papers,
        complete=False,sample=True,sample_limit=limit,query_coverage=queried,created_at=now())
    save(path,manifest)
    return manifest


def discover(api,year,directory):
    cached=directory/'manifest.json'
    if cached.exists() and load(cached).get('api')!=api.base_url:
        raise base.RequestFailure('error','Cached_API_mismatch_do_not_mix_versions')
    if api.api_version==2:
        return base.discover(api,directory,venue=f'ICLR.cc/{year}/Conference')
    path=directory/'manifest.json'
    if path.exists() and load(path).get('complete'): return load(path)
    stages=v1_invitations(api,year,directory)
    records=[]
    coverage=[]
    for index,stage in enumerate(stages):
        notes,state=base.paged(api,{'invitation':stage['id']},directory/'pages'/f'v1_submission_{index}.json')
        if any(stage['id'] not in (invitations(n) or []) for n in notes):
            raise ValueError('Invitation mismatch')
        records.extend(notes)
        coverage.append(dict(invitation=stage['id'],api_total=state['totals'][-1],public_unique=len(notes)))
    papers,aliases=canonical_submissions(records)
    objects_verified=all(i.get('invitation_object_available',True) for i in stages)
    has_unblinded_submission=any(i['id'].rsplit('/',1)[-1].lower()=='submission' for i in stages)
    manifest=dict(year=year,venue=stages[0]['id'].split('/-/')[0],api=api.base_url,api_version=1,
        complete=objects_verified and has_unblinded_submission,queries_exhausted=True,
        coverage_status='verified_stage_queries_only; other public stages may be missing',
        papers=papers,aliases=aliases,query_coverage=coverage,created_at=now(),
        limitations=['Only publicly visible verified submission stages; hidden/deleted/other tracks unknown',
                     'Blind/original copies deduplicated only when explicit original ID exists',
                     'API v1 history uses References; main stage does not query history'])
    save(path,manifest)
    save(directory/'submission_ids.json',list(papers))
    return manifest


def saved_paper_complete(folder,api,json_only=False):
    """Quick resume inventory; no network or repeated full-file hash scan."""
    path=folder/'metadata.json'
    if not path.exists(): return None
    try:
        meta=load(path)
        complete=(meta.get('json_collection_complete',meta.get('main_collection_complete',False))
            if json_only else meta.get('main_collection_complete'))
        if (not complete or meta.get('api_version')!=api.api_version
                or meta.get('source')!=api.base_url or meta.get('forms',{}).get('descriptions')!='notes.json'
                or not meta.get('forms',{}).get('descriptions_in_notes')
                or not meta.get('notes',{}).get('pagination_complete')
                or (folder/'field_descriptions.json').exists()):
            return None
        required=('submission.json','notes.json','edits.json','resources.json','resource_links.json','invitations.json')
        for name in required+(() if json_only else ('paper.pdf',)):
            if not (folder/name).is_file() or not (folder/name).stat().st_size:
                return None
        for resource in ([] if json_only else load(folder/'resources.json').values()):
            if resource.get('historical'): continue
            path=folder/resource['path']
            if (resource.get('status')!='success' or not path.resolve().is_relative_to(folder.resolve())
                    or not path.is_file() or path.stat().st_size!=resource.get('bytes')):
                return None
        original=meta['notes'].get('inline_descriptions',{}).get('original_backup')
        if original and not (folder/original).is_file(): return None
        return meta
    except (ValueError,KeyError,TypeError,OSError):
        return None


def collect_one_paper(api,paper,raw,directory,year,args,venue):
    identifier=base.safe_id(paper['id'])
    task_path=directory/'tasks'/(identifier+'.json')
    task=dict(paper_id=identifier,mode=args.mode,status='running',started_at=now())
    save(task_path,task)
    print(f'ICLR {year}: {identifier}',flush=True)
    try:
        if args.mode=='forms':
            from src.iclr_forms import collect_forms
            folder=raw/identifier
            meta=load(folder/'metadata.json') if (folder/'metadata.json').exists() else {}
            if meta.get('api_version',api.api_version)!=api.api_version or meta.get('source',api.base_url)!=api.base_url:
                raise ValueError('Saved paper API mismatch')
            if not (folder/'notes.json').exists():
                raise base.RequestFailure('error','Missing_saved_notes_run_main_collection')
            collect_forms(api,folder,paper,load(folder/'notes.json'),directory,meta)
            meta['main_collection_complete']=bool(meta.get('phase_a_with_resources_complete') and meta['forms']['status']=='ok')
            base.write_existing(folder/'metadata.json',meta)
            stop=meta['forms'].get('stop_reason')
            if stop:
                raise base.RequestFailure(stop['status'],stop['error_type'],stop.get('source'))
        else:
            options={'download_files':False} if getattr(args,'skip_downloads',False) else {}
            meta=base.collect_paper(api,paper,raw,directory,year=year,venue=venue,**options)
        complete=(meta['forms']['status']=='ok' if args.mode=='forms' else
            meta.get('json_collection_complete',False) if getattr(args,'skip_downloads',False) else meta['main_collection_complete'])
        task.update(status='complete' if complete else 'incomplete',finished_at=now(),
            main_collection_complete=meta.get('main_collection_complete',False),
            json_collection_complete=meta.get('json_collection_complete',False),skip_downloads=getattr(args,'skip_downloads',False))
        save(task_path,task)
        return meta
    except BaseException as exc:
        task.update(status='interrupted' if isinstance(exc,KeyboardInterrupt) else 'failed',
            finished_at=now(),error_type=type(exc).__name__)
        save(task_path,task)
        raise


def run_year(api,year,args):
    directory=config.ROOT/f'data/collection/iclr{year}'
    raw=args.output_root/f'ICLR_{year}'
    directory.mkdir(parents=True,exist_ok=True)
    api.cache_directories=[directory]
    before=api.calls
    result=dict(year=year,api=api.base_url,api_version=api.api_version,mode=args.mode,started_at=now(),
        offline=args.offline,completed_papers=0,files_completed_papers=0,forms_completed_papers=0,
        all_invitations_archived_papers=0,
        papers_with_unresolved_replyto=0,
        sampled_papers=0,enumeration_complete=False,errors=[])
    result.update(workers=getattr(args,'workers',1),skipped_completed_papers=0,resumed_incomplete_papers=0)
    result['request_delay']=getattr(api,'delay',None)
    result['attachment_delay']=getattr(api,'attachment_delay',0)
    json_only=getattr(args,'skip_downloads',False)
    result.update(skip_downloads=json_only,json_completed_papers=0,main_completed_papers=0)
    try:
        if not args.offline:
            buckets=(('/invitations',) if args.mode=='forms' else
                ('/notes','/invitations','/groups') if json_only else ('/notes','/invitations','/attachment','/pdf'))
            for bucket in buckets:
                deadline=getattr(api,'state',{}).get('cooldowns',{}).get(bucket)
                if deadline and datetime.now(timezone.utc)<datetime.fromisoformat(deadline):
                    result['cooldown_until']=deadline
                    raise base.RequestFailure('rate_limited','Retry_After_cooldown_active')
        gate_name='multi_year_pilot_json.json' if json_only else 'multi_year_pilot.json'
        if args.mode=='full':
            gate=directory/gate_name
            pilot=load(gate) if gate.exists() else {}
            # A validated file-inclusive pilot also validates its JSON stage.
            if (json_only and (not pilot.get('passed') or not pilot.get('forms_checked')
                    or year==2025 and pilot.get('validated_papers',0)<20)
                    and (directory/'multi_year_pilot.json').exists()):
                pilot=load(directory/'multi_year_pilot.json')
            if not pilot.get('passed') or not pilot.get('forms_checked'):
                raise base.RequestFailure('not_attempted','Rerun_pilot_with_public_forms_before_full')
            if year==2025 and pilot.get('validated_papers',0)<20:
                raise base.RequestFailure('not_attempted','Run_20_paper_pilot_before_2025_full')
        if args.mode=='pilot':
            gate=directory/(gate_name.replace('.json','_offline.json') if args.offline else gate_name)
            save(gate,dict(passed=False,checked_at=now(),status='in_progress',offline=args.offline))
        if args.mode=='forms':
            papers={}
            for path in sorted(raw.glob('*/submission.json')):
                paper=load(path)
                if path.parent.name!=paper.get('id'):
                    raise ValueError('Saved submission directory mismatch')
                papers[paper['id']]=paper
            if not papers:
                raise base.RequestFailure('not_attempted','No_saved_submissions_for_forms')
            manifest=dict(papers=papers,venue=f'ICLR.cc/{year}/Conference',complete=False)
        else:
            manifest=sample(api,year,directory,args.limit) if args.mode=='pilot' else discover(api,year,directory)
        if args.mode=='discover':
            result.update(enumeration_complete=manifest['complete'],discovered_public_ids=len(manifest['papers']))
        else:
            result.update(enumeration_complete=manifest.get('complete',False) and not manifest.get('sample'),
                          selected_papers=len(manifest['papers']))
            def record(paper,meta,skipped=False):
                result['sampled_papers']+=not skipped
                result['papers_with_unresolved_replyto']+=bool(meta.get('last_validation',{}).get('unresolved_replyto'))
                result['files_completed_papers']+=bool(meta.get('phase_a_with_resources_complete'))
                result['forms_completed_papers']+=meta['forms']['status']=='ok'
                json_complete=meta.get('json_collection_complete',meta.get('main_collection_complete',False))
                result['json_completed_papers']+=bool(json_complete)
                result['main_completed_papers']+=bool(meta.get('main_collection_complete'))
                result['all_invitations_archived_papers']+=meta['forms']['all_referenced_invitations_archived']
                completed=(meta['forms']['status']=='ok' if args.mode=='forms' else
                    json_complete if json_only else meta['main_collection_complete'])
                result['completed_papers']+=bool(completed)
                if not completed:
                    result['errors'].append(dict(paper_id=paper['id'],notes=meta.get('notes',{}).get('status'),
                        pdf=meta.get('pdf',{}).get('status'),resources=meta.get('resources',{}).get('status'),
                        forms=meta['forms']['status'],field_counts=meta['forms']['field_counts']))
            pending=[]
            resume=[]
            for paper in manifest['papers'].values():
                folder=raw/base.safe_id(paper['id'])
                complete=saved_paper_complete(folder,api,json_only=True) if args.mode=='full' and json_only else saved_paper_complete(folder,api) if args.mode=='full' else None
                if complete is not None:
                    if (complete.get('notes',{}).get('ordering',{}).get('policy')!='replyto_depth_first_v1'
                            or complete.get('notes',{}).get('display_fields',{}).get('policy')!='public_note_display_v1'):
                        from src.iclr_note_order import order_saved_notes
                        order_saved_notes(folder,paper['id'],complete)
                        base.write_existing(folder/'metadata.json',complete)
                    record(paper,complete,skipped=True)
                    result['skipped_completed_papers']+=1
                elif args.mode=='full' and folder.exists():
                    resume.append(paper)
                else:
                    pending.append(paper)
            def resume_priority(paper):
                folder=raw/paper['id']
                task=directory/'tasks'/(paper['id']+'.json')
                try:
                    state=load(task).get('status') if task.exists() else None
                except (ValueError,OSError):
                    state=None
                return 0 if state in ('running','interrupted') or not (folder/'metadata.json').exists() else 1
            resume.sort(key=resume_priority)
            pending=resume+pending
            result['resumed_incomplete_papers']=len(resume)
            result['pending_at_start']=len(pending)
            print(f'ICLR {year}: skip {result["skipped_completed_papers"]} complete; '
                f'resume {len(resume)} incomplete; pending {len(pending)}; workers {result["workers"]}',flush=True)
            def collect(worker,paper):
                return collect_one_paper(worker,paper,raw,directory,year,args,manifest['venue'])
            if pending and result['workers']>1:
                from src.iclr_parallel import collect_parallel
                collect_parallel(api,pending,result['workers'],collect,record)
            else:
                for index,paper in enumerate(pending):
                    record(paper,collect(api,paper))
                    if '/notes' in api.blocked or '/invitations' in api.blocked or (api.calls>=api.max_requests and index+1<len(pending)):
                        raise base.RequestFailure('not_attempted','Endpoint_stopped_or_budget_exhausted')
            if args.mode=='pilot':
                passed=result['completed_papers']==args.limit
                save(gate,dict(passed=passed,checked_at=now(),
                    paper_ids=list(manifest['papers']),api=api.base_url,resources_checked=not json_only,forms_checked=True,
                    skip_downloads=json_only,
                    validated_papers=result['completed_papers'],offline=args.offline,
                    workers=result['workers'],request_delay=result['request_delay']))
                if not passed: raise base.RequestFailure('error','Pilot_incomplete')
        result['status']='success' if not result['errors'] else 'incomplete'
        if args.mode in ('full','discover') and not result['enumeration_complete']:
            result['status']='incomplete'
            result['errors'].append({'reason':'All verified queries exhausted, but all-status conference coverage is unverified'})
    except base.RequestFailure as exc:
        result.update(status=exc.status,stop_reason=exc.record())
    except (ValueError,KeyError,TypeError) as exc:
        result.update(status='error',stop_reason={'error_type':type(exc).__name__,
            'reason':'Schema or pagination check failed; inspect source files'})
    except KeyboardInterrupt:
        result.update(status='paused',stop_reason={'reason':'Interrupted; checkpoints retained'})
    result.update(finished_at=now(),data_http_attempts=api.calls-before,offline_cache_hits=api.cache_hits,
        remaining_selected_papers=(result['selected_papers']-result['completed_papers']) if 'selected_papers' in result else None,
        history='not_attempted; v1 References and v2 Edits are independent from main collection')
    save(directory/('multi_year_last_run_offline.json' if args.offline else 'multi_year_last_run.json'),result)
    return result


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--years',nargs='+',required=True,help='2013 2014 2016-2027; use 2013-2027 to include 2015')
    parser.add_argument('--mode',choices=['pilot','discover','full','forms'],default='pilot',
        help='forms: supplement forms for all saved papers without downloading files')
    parser.add_argument('--limit',type=int,default=1,help='Pilot sample per year; never claims full enumeration')
    parser.add_argument('--api-version',choices=['auto','1','2'],default='auto')
    parser.add_argument('--authenticate',action='store_true')
    parser.add_argument('--offline',action='store_true')
    parser.add_argument('--skip-downloads',action='store_true',
        help='Collect public JSON and source references only; defer PDF and all attachment downloads')
    parser.add_argument('--wait-on-429',action='store_true',
        help='Wait for rate-limit cooldown and resume automatically; no HTTP polling; same total budget')
    parser.add_argument('--output-root',type=Path,default=config.RAW,help='Raw data root; choose another disk if needed')
    parser.add_argument('--max-requests',type=int,default=30,help='Total data HTTP attempts across all selected years/APIs')
    parser.add_argument('--request-delay',type=float,default=2)
    parser.add_argument('--workers',type=int,default=1,help='1–5 paper workers; each uses its own request delay; shared total budget')
    parser.add_argument('--attachment-delay',type=float,default=10,
        help='Global minimum seconds between OpenReview attachment/PDF request starts, shared by all workers (default: 10)')
    parser.add_argument('--max-attachment-mb',type=float,default=512)
    parser.add_argument('--min-free-gb',type=float,default=15)
    args=parser.parse_args(argv)
    if args.skip_downloads and args.mode not in ('pilot','full'):
        parser.error('--skip-downloads is supported in pilot/full modes')
    if args.offline and args.wait_on_429:
        parser.error('--wait-on-429 requires online mode')
    try: years=years_from_args(args.years)
    except ValueError as exc: parser.error(str(exc))
    if args.limit<1 or args.max_requests<1 or args.request_delay<0 or args.attachment_delay<0 or args.max_attachment_mb<=0 or args.min_free_gb<0:
        parser.error('Invalid limit, budget, delay or storage threshold')
    if not 1<=args.workers<=5 or (args.workers>1 and (args.offline or args.request_delay<1)):
        parser.error('Use 1–5 workers; parallel online mode requires request-delay >=1; offline uses one worker')
    from src.iclr_parallel import guard_run
    try:
        run_lock=guard_run(config.ROOT,ROOT)
    except base.RequestFailure as exc:
        parser.exit(1,exc.code+'; stop the existing task before starting another\n')
    ROOT.mkdir(parents=True,exist_ok=True)
    apis={}
    results=[]
    for year in years:
        version=int(args.api_version) if args.api_version!='auto' else (2 if year>=2024 else 1)
        if version not in apis:
            api_base='https://api2.openreview.net' if version==2 else 'https://api.openreview.net'
            apis[version]=Transport(ROOT/f'api_v{version}',args.request_delay,args.max_requests,args.authenticate,
                args.max_attachment_mb,args.min_free_gb,api_base,version,offline=args.offline)
            apis[version].attachment_delay=args.attachment_delay
        api=apis[version]
        used=sum(a.calls for a in apis.values())
        api.max_requests=api.calls+max(0,args.max_requests-used)
        if args.wait_on_429:
            from src.iclr_cooldown import run_with_wait
            result=run_with_wait(api,year,args,run_year,ROOT/'wait_status.json')
            save(config.ROOT/f'data/collection/iclr{year}/multi_year_last_run.json',result)
        else:
            result=run_year(api,year,args)
        results.append(result)
        print(json.dumps(result,ensure_ascii=True),flush=True)
        reason=result.get('stop_reason',{}).get('error_type')
        if result['status'] in ('paused','forbidden','rate_limited') or reason in (
                'ProxyError','ConnectionError','SDK_authentication_failed','Authentication_terminal_input_unavailable'):
            break
        if sum(a.calls for a in apis.values())>=args.max_requests:break
    save(ROOT/('last_run_offline.json' if args.offline else 'last_run.json'),dict(years_requested=years,results=results,finished_at=now()))
    text=['# ICLR 跨年份采集最小验证','',f'更新时间（UTC）：{now()}','',
        '| 年份 | API | 状态 | 已处理 | 文件及附件完成 | 表单及选项说明完成 | 全部引用表单归档 | 本模式完成 | 数据 HTTP 尝试 |',
        '|---|---|---|---|---|---|---|---|---|',
        *[f'| {r["year"]} | v{r["api_version"]} | {r["status"]} | {r["sampled_papers"]} | {r["files_completed_papers"]} | {r["forms_completed_papers"]} | {r["all_invitations_archived_papers"]} | {r["completed_papers"]} | {r["data_http_attempts"]} |' for r in results],
        '', '未执行年份：'+str([y for y in years if y not in {r['year'] for r in results}]),'',
        'pilot 是有限样本，绝不视为完整枚举；离线运行只验证已有缓存，不证明当前接口在线可用。',
        '表单及选项说明完成表示每条 Note 有可用的公开表单且枚举值可解释；全部引用表单归档另行统计。不可返回的辅助 Edit/旧表单仍记 unavailable，不算归档成功。',
        'invitations.json 保存公开表单快照、来源与哈希；说明直接补入 notes.json 的 description/descriptions，首次补充前保存原始备份；不再生成重复的 field_descriptions.json，不证明历史时点的表单。',
        'papers_with_unresolved_replyto 单独记录缺少父节点的讨论；保留真实 replyto，不猜测该父节点是隐藏、删除或属于其他 Forum，也不伪造内容。',
        '默认 2024 起使用 API v2，较早年份用 v1；这是可覆盖的选择策略，实际 Invitation 必须由服务器响应验证。',
        '不在遇到访问挑战时切换 API 绕过限制。可通过 --api-version 明确指定已确认迁移的会议。',
        '未公开、删除、尚无匹配 Invitation 或不匹配的历史命名均保留未验证，不能解释为该年零篇。',
        '主采集保存当前论文、Forum、PDF、公开附件与资源链接；Edits/References 不在本轮执行，历史仍为未知。',
        'skip_downloads=true 时，本模式完成仅表示 JSON 阶段完成，未下载文件记 deferred；main_completed_papers 才表示含文件的主采集完成。json_completed_papers 单独记录 JSON 完成数。',
        '新入口没有提供 API v1 References 批量补采；不得把空 edits.json 当作没有修改。',
        '', '## 逐年执行记录','', '```json',json.dumps(results,ensure_ascii=False,indent=2),'```','']
    output=REPORT.with_stem(REPORT.stem+'_offline') if args.offline else REPORT
    output.parent.mkdir(parents=True,exist_ok=True)
    output.write_text('\n'.join(text),encoding='utf-8')
    return 0 if len(results)==len(years) and all(r['status']=='success' for r in results) else 1
