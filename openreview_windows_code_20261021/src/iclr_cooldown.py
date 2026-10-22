"""Wait without HTTP polling, then resume the existing authenticated transport."""
import time
from datetime import datetime,timedelta,timezone

from src.common import now,save
from src.iclr2025_collection import RequestFailure


def utcnow():
    return datetime.now(timezone.utc)


def wait_for_limit(api,year,result,status_path,buckets=None):
    deadlines=[]
    for bucket,value in api.state.get('cooldowns',{}).items():
        if buckets is not None and bucket not in buckets: continue
        try:
            parsed=datetime.fromisoformat(value)
            if parsed.tzinfo is not None: deadlines.append(parsed)
        except (ValueError,TypeError):
            pass
    # A saved, already expired cooldown can be resumed immediately. A 429
    # without any valid server hint gets a conservative one-hour fallback.
    deadline=max(deadlines) if deadlines else utcnow()+timedelta(hours=1)
    deadline+=timedelta(seconds=2)
    state=dict(year=year,status='waiting',cooldown_until=deadline.isoformat(),
        data_http_attempts=api.calls,max_requests=api.max_requests,
        stop_reason=result.get('stop_reason'),updated_at=now())
    save(status_path,state)
    print(f'ICLR {year}: 429; waiting until {deadline.isoformat()} (UTC); automatic resume enabled',flush=True)
    try:
        while True:
            remaining=(deadline-utcnow()).total_seconds()
            if remaining<=0: break
            time.sleep(min(30,remaining))
            state['updated_at']=now()
            save(status_path,state)
        # Workers have drained before this function is entered. Only reset
        # rate-limit restrictions; permission failures must never be retried.
        if getattr(api,'access_stop',None) is not None and api.access_stop.status!='rate_limited':
            raise api.access_stop
        for bucket,value in api.state.get('cooldowns',{}).items():
            try:
                if value and datetime.fromisoformat(value)<=utcnow(): api.blocked.discard(bucket)
            except (ValueError,TypeError):
                pass
        api.access_stop=None
        if hasattr(api,'_form_attempts'): api._form_attempts.clear()
        state.update(status='resuming',updated_at=now())
        save(status_path,state)
    except KeyboardInterrupt:
        state.update(status='paused',updated_at=now())
        save(status_path,state)
        raise


def run_with_wait(api,year,args,run_year,status_path):
    before=api.calls
    recoveries=0
    try:
        # Login now, before a potentially long persisted cooldown, so an
        # unattended resume will not prompt for credentials hours later.
        if args.authenticate: api.initialize()
        while True:
            result=run_year(api,year,args)
            if result['status']!='rate_limited': break
            if api.calls>=api.max_requests:
                result.update(status='not_attempted',stop_reason=RequestFailure(
                    'not_attempted','Request_budget_exhausted').record())
                break
            buckets=('/notes','/invitations','/groups') if getattr(args,'skip_downloads',False) else None
            wait_for_limit(api,year,result,status_path,buckets=buckets)
            recoveries+=1
    except (RequestFailure,KeyboardInterrupt) as exc:
        result=locals().get('result',dict(year=year,api_version=api.api_version,
            sampled_papers=0,files_completed_papers=0,forms_completed_papers=0,
            all_invitations_archived_papers=0,completed_papers=0))
        result.update(status='paused' if isinstance(exc,KeyboardInterrupt) else exc.status,
            stop_reason={'reason':'Interrupted; checkpoints retained'} if isinstance(exc,KeyboardInterrupt) else exc.record())
    result.update(data_http_attempts=api.calls-before,rate_limit_recoveries=recoveries,finished_at=now())
    save(status_path,dict(year=year,status=result['status'],updated_at=now(),
        data_http_attempts=api.calls,max_requests=api.max_requests,rate_limit_recoveries=recoveries))
    return result
