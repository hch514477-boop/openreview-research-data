"""Bounded paper workers, one login, shared budget and stop state."""
import copy
import os
import threading
import time
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from datetime import datetime, timezone

import requests
from requests.adapters import HTTPAdapter

from src.iclr2025_collection import Transport, RequestFailure


def guard_run(root,directory):
    """Reject another collector for this project before login or checkpoint writes."""
    root=Path(root).resolve()
    proc=Path('/proc')
    if proc.exists():
        for process in proc.iterdir():
            if not process.name.isdigit() or int(process.name)==os.getpid(): continue
            try:
                args=(process/'cmdline').read_bytes().split(b'\0')
                if not args or b'python' not in args[0]: continue
                cwd=(process/'cwd').resolve()
                for arg in args[1:]:
                    path=Path(os.fsdecode(arg))
                    if path.name not in ('collect_iclr.py','collect_iclr2025.py','probe_iclr_edits.py'): continue
                    path=path if path.is_absolute() else cwd/path
                    if path.resolve().parent.parent==root:
                        raise RequestFailure('not_attempted','Another_collection_process_running_PID_'+process.name)
            except (FileNotFoundError,ProcessLookupError,PermissionError,OSError):
                continue
    if os.name!='posix': return None
    import fcntl
    directory.mkdir(parents=True,exist_ok=True)
    handle=(directory/'collector.lock').open('a+')
    try:
        fcntl.flock(handle,fcntl.LOCK_EX|fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RequestFailure('not_attempted','Another_collection_process_holds_lock') from None
    return handle  # Keep alive until main returns; closing releases the OS lock.


class Coordinator:
    def __init__(self,root):
        self.root=root
        self.condition=threading.Condition(threading.RLock())
        self.log_lock=threading.Lock()
        self.cache_lock=threading.RLock()
        self.disk_lock=threading.Lock()
        self.binary_slots=threading.BoundedSemaphore(getattr(root,'binary_workers',2))
        self.form_attempts={}
        self.failure=None
        self.arxiv_next=root.last+3 if root.last else 0

    def stop(self,failure):
        with self.condition:
            # Preserve a real access restriction over a secondary cancellation.
            priority={'forbidden':3,'rate_limited':2}
            if self.failure is None or priority.get(getattr(failure,'status',None),0)>priority.get(getattr(self.failure,'status',None),0):
                self.failure=failure
            self.condition.notify_all()

    def reserve(self,worker,bucket,minimum_delay=0):
        with self.condition:
            while True:
                if self.failure is not None:
                    raise RequestFailure('not_attempted','Concurrent_run_stopped')
                if bucket in self.root.blocked:
                    raise RequestFailure('not_attempted','Endpoint_stopped_this_run')
                deadline=self.root.state['cooldowns'].get(bucket)
                if deadline and datetime.now(timezone.utc)<datetime.fromisoformat(deadline):
                    failure=RequestFailure('rate_limited','Retry_After_cooldown_active')
                    self.stop(failure)
                    raise failure
                if self.root.calls>=self.root.max_requests:
                    failure=RequestFailure('not_attempted','Request_budget_exhausted')
                    self.stop(failure)
                    raise failure
                delay=max(worker.delay,minimum_delay)-(time.monotonic()-worker.last)
                delay=max(delay,self.root._attachment_wait(bucket))
                if bucket=='https://arxiv.org/pdf':
                    delay=max(delay,self.arxiv_next-time.monotonic())
                if delay>0:
                    self.condition.wait(delay)
                    continue
                self.root.calls+=1
                worker.calls+=1
                self.root._schedule_attachment(bucket)
                if bucket=='https://arxiv.org/pdf':
                    self.arxiv_next=time.monotonic()+3
                return


class WorkerTransport(Transport):
    def __init__(self,root,coordinator):
        super().__init__(root.directory,root.delay,root.max_requests,False,
            root.max_attachment_bytes/1024**2,root.min_free_bytes/1024**3,root.base_url,root.api_version)
        self.authenticate=root.authenticate
        self.state=root.state
        self.blocked=root.blocked
        self.state_path=root.state_path
        self.coordinator=coordinator
        self.state_lock=coordinator.condition
        self.log_lock=coordinator.log_lock
        self.form_cache_lock=coordinator.cache_lock
        self.disk_write_lock=coordinator.disk_lock
        self._form_attempts=coordinator.form_attempts
        self.attachment_delay=root.attachment_delay
        self.cache_hits=0
        self.offline=False
        self.client=copy.copy(root.client)
        self.client.headers=copy.deepcopy(root.client.headers)
        session=requests.Session()
        for prefix in list(session.adapters):
            session.mount(prefix,HTTPAdapter(max_retries=0))
        session.cookies.update(root.client.session.cookies)
        self.client.session=session
        self.original_get=session.get
        session.get=lambda url,**kwargs:self.request(url.removeprefix(self.base_url),
            kwargs.get('params') or {},binary=True,response=True)

    def request(self,endpoint,params,binary=False,response=False):
        if self.api_version==1 and endpoint=='/notes':
            params={key:value for key,value in params.items() if key!='count'}
        if binary:
            with self.coordinator.binary_slots:
                return super().request(endpoint,params,binary,response)
        return super().request(endpoint,params,binary,response)

    def download_arxiv_pdf(self,reference):
        with self.coordinator.binary_slots:
            return super().download_arxiv_pdf(reference)


def collect_parallel(api,papers,workers,collect,record):
    """At most workers papers in flight; drain saved work before propagating stop."""
    api.initialize()  # Credentials are entered exactly once, in the main thread.
    coordinator=Coordinator(api)
    local=threading.local()
    transports=[]
    transports_lock=threading.Lock()
    def run(paper):
        if not hasattr(local,'api'):
            local.api=WorkerTransport(api,coordinator)
            with transports_lock:
                transports.append(local.api)
        return collect(local.api,paper)
    iterator=iter(papers)
    pool=ThreadPoolExecutor(max_workers=workers,thread_name_prefix='iclr')
    pending={}
    failure=None
    def submit():
        if coordinator.failure is not None:
            return
        try:
            paper=next(iterator)
        except StopIteration:
            return
        pending[pool.submit(run,paper)]=paper
    try:
        for _ in range(workers): submit()
        while pending:
            try:
                done,_=wait(pending,return_when=FIRST_COMPLETED)
            except KeyboardInterrupt:
                failure=KeyboardInterrupt()
                coordinator.stop(RequestFailure('paused','Interrupted'))
                print('Stopping new requests; waiting for in-flight responses/checkpoints.',flush=True)
                continue
            for future in done:
                paper=pending.pop(future)
                try:
                    record(paper,future.result())
                except BaseException as exc:
                    if failure is None: failure=exc
                    coordinator.stop(exc)
                submit()
    finally:
        pool.shutdown(wait=True,cancel_futures=True)
        for worker in transports:
            api.last=max(api.last,worker.last)
            worker.client.session.close()
    if isinstance(failure,KeyboardInterrupt):
        raise failure
    if coordinator.failure is not None:
        raise coordinator.failure
    if failure is not None:
        raise failure
