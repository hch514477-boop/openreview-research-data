"""ICLR 2025 public data: checkpoint discovery, then files, then optional Edits.

Uses the official SDK session and attachment method. Raw API JSON is retained
instead of round-tripping SDK Note objects, which can omit unknown fields.
"""
import argparse
import contextlib
import copy
import getpass
import hashlib
import io
import json
import os
import re
import shutil
import time
import zipfile
from collections import Counter
from datetime import datetime, timezone, timedelta
from email.parser import BytesParser
from email import policy
from pathlib import Path
from urllib.parse import urlsplit, urljoin

import requests

import config
from src.common import kind, load, now, save, value
from src.collect_edits import retry_deadline
from src.score_history import public_payload

VENUE = 'ICLR.cc/2025/Conference'
RAW = config.RAW / 'ICLR_2025'
STATE = config.ROOT / 'data/collection/iclr2025'
REPORT = config.ROOT / 'reports/iclr2025_collection_report.md'


def safe_id(identifier):
    if not isinstance(identifier, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', identifier):
        raise ValueError('Unsafe OpenReview ID')
    if identifier.upper() in {'CON', 'PRN', 'AUX', 'NUL', *[f'COM{i}' for i in range(1, 10)],
                              *[f'LPT{i}' for i in range(1, 10)]}:
        raise ValueError('Reserved Windows filename')
    return identifier


class RequestFailure(RuntimeError):
    def __init__(self, status, code, source=None):
        super().__init__(code)
        self.status, self.code, self.source = status, code, source

    def record(self):
        return dict(status=self.status, error_type=self.code, source=self.source,
                    last_attempt_at=now() if self.source else None)


def valid_pdf(content):
    # Header alone also occurs in truncated downloads; require a final EOF marker.
    return content.startswith(b'%PDF-') and b'%%EOF' in content[-4096:]


def pdf_payload(content):
    """Extract one MIME PDF part; never trim arbitrary prefixes from a PDF."""
    if valid_pdf(content) or not content.startswith(b'--'):
        return content
    boundary=content.split(b'\r\n',1)[0][2:]
    if not re.fullmatch(rb"[A-Za-z0-9'()+_,./:=?-]{1,70}",boundary):
        return content
    message=BytesParser(policy=policy.default).parsebytes(
        b'Content-Type: multipart/form-data; boundary="'+boundary+b'"\r\nMIME-Version: 1.0\r\n\r\n'+content)
    if not message.is_multipart() or message.defects:
        return content
    parts=list(message.iter_parts())
    if len(parts)!=1:
        return content
    part=parts[0]
    if part.get_content_type()!='application/pdf' and not (part.get_filename() or '').lower().endswith('.pdf'):
        return content
    payload=part.get_payload(decode=True)
    return payload if isinstance(payload,bytes) and valid_pdf(payload) else content


def arxiv_pdf_url(reference):
    if not isinstance(reference,str):return None
    parsed=urlsplit(reference)
    if parsed.scheme not in ('https','http') or parsed.netloc not in ('arxiv.org','www.arxiv.org','export.arxiv.org'):
        return None
    match=re.fullmatch(r'/(?:abs|pdf)/((?:\d{4}\.\d{4,5}|[A-Za-z-]+(?:\.[A-Za-z-]+)?/\d{7})(?:v\d+)?)(?:\.pdf)?',parsed.path)
    return 'https://arxiv.org/pdf/'+match.group(1) if match and not parsed.query and not parsed.fragment else None


def pdf_diagnostics(content,directory,identifier):
    """Keep invalid PDF candidates separate; never persist HTML/login responses."""
    digest=hashlib.sha256(content).hexdigest()
    header=content.find(b'%PDF-')
    eof=content.rfind(b'%%EOF')
    result=dict(bytes=len(content),sha256=digest,pdf_header_offset=header,last_eof_offset=eof,
        trailing_bytes=len(content)-eof-5 if eof>=0 else None,validated=False)
    root=directory/'invalid_downloads'/safe_id(identifier)
    if 0<=header<1024:
        root.mkdir(parents=True,exist_ok=True)
        path=root/(digest+'.bin')
        if not path.exists():
            temporary=path.with_suffix('.bin.tmp')
            temporary.write_bytes(content)
            temporary.replace(path)
        result['candidate_path']=str(path)
    save(root/(digest+'.json'),result)
    return result


def public_objects(body, key):
    meta = {}
    filtered = public_payload(copy.deepcopy(body), meta)
    return filtered[key], meta


class Transport:
    def __init__(self, directory, delay=2, max_requests=100000, authenticate=False,
                 max_attachment_mb=512, min_free_gb=15, api_base=config.API, api_version=2):
        self.directory, self.delay, self.max_requests = directory, delay, max_requests
        self.calls, self.last = 0, 0
        self.client = None
        self.authenticate = authenticate
        self.base_url, self.api_version = api_base, api_version
        self.max_attachment_bytes = int(max_attachment_mb * 1024**2)
        self.min_free_bytes = int(min_free_gb * 1024**3)
        self.state_path = directory / 'transport.json'
        self.state = load(self.state_path) if self.state_path.exists() else {'cooldowns': {}}
        self.blocked = set()
        self.access_stop = None
        self.attachment_delay = 0

    def _check_access_stop(self):
        if self.access_stop is not None:
            raise RequestFailure(self.access_stop.status,self.access_stop.code,self.access_stop.source)

    def _attachment_wait(self,bucket):
        if bucket not in ('/attachment','/pdf') or self.attachment_delay<=0:
            return 0
        deadline=self.state.get('attachment_next_at')
        return max(0,(datetime.fromisoformat(deadline)-datetime.now(timezone.utc)).total_seconds()) if deadline else 0

    def _schedule_attachment(self,bucket):
        if bucket in ('/attachment','/pdf') and self.attachment_delay>0:
            self.state['attachment_next_at']=(datetime.now(timezone.utc)+timedelta(seconds=self.attachment_delay)).isoformat()
            save(self.state_path,self.state)

    def download_arxiv_pdf(self,reference):
        """Only a submission's explicit arXiv PDF reference; no OR auth forwarded."""
        self._check_access_stop()
        if getattr(self,'offline',False):
            raise RequestFailure('not_attempted','Offline_missing_binary_file')
        url=arxiv_pdf_url(reference)
        if not url:raise RequestFailure('unavailable','Unsupported_external_PDF_reference')
        bucket='https://arxiv.org/pdf'
        if bucket in self.blocked:raise RequestFailure('not_attempted','Arxiv_endpoint_stopped_this_run')
        cooldown=self.state['cooldowns'].get(bucket)
        if cooldown and datetime.now(timezone.utc)<datetime.fromisoformat(cooldown):
            raise RequestFailure('rate_limited','Retry_After_cooldown_active')
        # Fresh session: no OpenReview headers, token or cookies.
        with requests.Session() as session:
            for _ in range(4):
                if self.calls>=self.max_requests:raise RequestFailure('not_attempted','Request_budget_exhausted')
                self._before_request(bucket,minimum_delay=3)
                meta=dict(url=url,method='GET',params={},requested_at=now(),authenticated=False,sdk_retries=0)
                import uuid
                filename=f'responses/{time.time_ns()}_{uuid.uuid4().hex}.json'
                body=None
                content=None
                redirect=None
                try:
                    with session.get(url,stream=True,allow_redirects=False,timeout=(15,90)) as response:
                        meta.update(http_status=response.status_code,headers={k:v for k,v in response.headers.items()
                            if k.lower() in ('content-type','content-length','retry-after','date')})
                        if response.status_code in (301,302,303,307,308):
                            redirect=urljoin(url,response.headers.get('Location',''))
                            parsed=urlsplit(redirect)
                            if (parsed.scheme!='https' or parsed.netloc not in ('arxiv.org','export.arxiv.org')
                                    or not re.fullmatch(r'/pdf/.+',parsed.path)):
                                raise RequestFailure('error','Untrusted_arxiv_redirect')
                            status,code='redirect',None
                        elif response.status_code==200:
                            content=bounded_binary(response,self.max_attachment_bytes,self.min_free_bytes,self.directory)
                            check_attachment(content,'.pdf')
                            body=dict(bytes=len(content),sha256=hashlib.sha256(content).hexdigest())
                            status,code='success',None
                        else:
                            status='rate_limited' if response.status_code==429 else 'forbidden' if response.status_code in (401,403) else 'error'
                            code='Arxiv_HTTP_'+str(response.status_code)
                            if status in ('rate_limited','forbidden'):
                                self._restriction(bucket,status,code,response,{},source=filename)
                except RequestFailure as exc:
                    status,code=exc.status,exc.code
                except requests.RequestException as exc:
                    status,code='error',type(exc).__name__
                    meta['http_status']=None
                self.last=time.monotonic()
                self._log_response(filename,meta,body,status,code)
                print('arxiv/pdf',meta.get('http_status'),status,flush=True)
                if status=='success':return content
                if status=='redirect':
                    url=redirect
                    continue
                raise RequestFailure(status,code,filename)
        raise RequestFailure('error','Arxiv_redirect_limit')

    def initialize(self):
        if self.client is not None:
            return
        import openreview
        from requests.adapters import HTTPAdapter
        username, password = None, None
        if self.authenticate:
            try:
                username = input('OpenReview 邮箱（不保存）: ').strip()
                password = getpass.getpass('OpenReview 密码（不回显、不保存）: ')
            except (EOFError, KeyboardInterrupt):
                raise RequestFailure('error', 'Authentication_terminal_input_unavailable') from None
            if not username or not password:
                raise RequestFailure('error', 'Authentication_credentials_empty')
        # Build anonymous first, then disable every SDK retry adapter before login.
        # Temporarily hide env credentials so constructor cannot log in implicitly.
        credentials = {k: os.environ.pop(k) for k in ('OPENREVIEW_USERNAME', 'OPENREVIEW_PASSWORD') if k in os.environ}
        try:
            with contextlib.redirect_stdout(io.StringIO()):
                factory = openreview.api.OpenReviewClient if self.api_version==2 else openreview.Client
                self.client = factory(baseurl=self.base_url)
        finally:
            os.environ.update(credentials)
        for prefix in list(self.client.session.adapters):
            self.client.session.mount(prefix, HTTPAdapter(max_retries=0))
        if self.authenticate:
            try:
                # Login is not a data request; never persist its body or response.
                original = self.client.session.request
                def timed(method, url, **kwargs):
                    kwargs.setdefault('timeout', (15, 90))
                    return original(method, url, **kwargs)
                self.client.session.request = timed
                with contextlib.redirect_stdout(io.StringIO()):
                    self.client.login_user(username, password)
                if not self.client.token:
                    raise ValueError('No authentication token')
            except Exception:
                self.client = None
                raise RequestFailure('error', 'SDK_authentication_failed') from None
            finally:
                del username, password
        # Route SDK attachment requests through the same bounded transport.
        self.original_get = self.client.session.get
        self.client.session.get = lambda url, **kw: self.request(url.removeprefix(self.base_url),
            kw.get('params') or {}, binary=True, response=True)

    def _before_request(self, bucket, minimum_delay=0):
        self._check_access_stop()
        coordinator=getattr(self,'coordinator',None)
        if coordinator is not None:
            coordinator.reserve(self,bucket,minimum_delay)
            return
        if self.calls>=self.max_requests:
            raise RequestFailure('not_attempted','Request_budget_exhausted')
        time.sleep(max(0,max(self.delay,minimum_delay)-(time.monotonic()-self.last),self._attachment_wait(bucket)))
        self._check_access_stop()
        self._schedule_attachment(bucket)
        self.calls+=1

    def _restriction(self,bucket,status,code,result,body,source=None):
        self.access_stop=RequestFailure(status,code,source)
        coordinator=getattr(self,'coordinator',None)
        if coordinator is not None:
            coordinator.stop(self.access_stop)
        with getattr(self,'state_lock',contextlib.nullcontext()):
            self.blocked.add(bucket)
            if status=='rate_limited':
                headers=getattr(result,'headers',{})
                details=(body or {}).get('details') or {}
                reset=details.get('resetTime')
                candidates=[]
                for candidate in (retry_deadline(headers.get('Retry-After')),reset,
                                  self.state['cooldowns'].get(bucket)):
                    try:
                        parsed=datetime.fromisoformat(candidate.replace('Z','+00:00'))
                        if parsed.tzinfo is not None and parsed>datetime.now(timezone.utc):
                            candidates.append(parsed)
                    except (ValueError,TypeError,AttributeError):
                        pass
                # Missing or expired server hints must not cause a tight retry loop.
                deadline=max(candidates) if candidates else datetime.now(timezone.utc)+timedelta(hours=1)
                self.state['cooldowns'][bucket]=deadline.isoformat()
                save(self.state_path,self.state)

    def _log_response(self,filename,meta,body,status,code):
        with getattr(self,'log_lock',contextlib.nullcontext()):
            save(self.directory/filename,dict(request=meta,response=body,status=status,error_type=code))
            with (self.directory/'requests.jsonl').open('a',encoding='utf-8') as stream:
                stream.write(json.dumps(dict(**meta,status=status,error_type=code,source=filename))+'\n')

    def request(self, endpoint, params, binary=False, response=False):
        self._check_access_stop()
        # All literal attachment versions share the endpoint's restriction bucket.
        bucket = '/attachment' if endpoint.startswith('/attachment/') else '/pdf' if endpoint.startswith('/pdf/') else endpoint
        if bucket in self.blocked:
            raise RequestFailure('not_attempted', 'Endpoint_stopped_this_run')
        cooldown = self.state['cooldowns'].get(bucket)
        if cooldown and datetime.now(timezone.utc) < datetime.fromisoformat(cooldown):
            raise RequestFailure('rate_limited', 'Retry_After_cooldown_active')
        if self.calls >= self.max_requests:
            raise RequestFailure('not_attempted', 'Request_budget_exhausted')
        self.initialize()
        for attempt in range(3):
            if self.calls >= self.max_requests:
                raise RequestFailure('not_attempted', 'Request_budget_exhausted')
            self._before_request(bucket)
            meta = dict(url=self.base_url+endpoint, params=params, method='GET', requested_at=now(),
                        authenticated=self.authenticate, attempt=attempt+1, sdk_retries=0)
            started = time.monotonic()
            body, content, result = None, None, None
            try:
                result = self.original_get(self.base_url+endpoint, params=params,
                    headers=self.client.headers, timeout=(15, 90), stream=binary)
                meta.update(http_status=result.status_code, elapsed_seconds=round(time.monotonic()-started, 3),
                    headers={k:v for k,v in result.headers.items() if k.lower() in
                             {'date', 'retry-after', 'content-type', 'x-request-id',
                              'ratelimit-limit','ratelimit-remaining','ratelimit-reset','ratelimit-policy',
                              'x-ratelimit-limit','x-ratelimit-remaining','x-ratelimit-reset'}})
                if binary and result.status_code == 200:
                    try:
                        content = bounded_binary(result, self.max_attachment_bytes, self.min_free_bytes, self.directory)
                        result._content = content  # SDK get_attachment reads response.content.
                    finally:
                        result.close()
                    body = {'bytes': len(content), 'sha256': hashlib.sha256(content).hexdigest()}
                else:
                    try:
                        body = result.json()
                    except ValueError:
                        body = {'name': 'NonJSONResponse'}
                if result.status_code == 200:
                    if not binary and (not isinstance(body, dict) or body.get('name') == 'NonJSONResponse'):
                        raise ValueError('Invalid JSON response')
                    if not binary:
                        body = public_payload(copy.deepcopy(body), meta)
                    status, code = 'success', None
                else:
                    code = body.get('name', 'HTTPError') if isinstance(body, dict) else 'HTTPError'
                    # Persist only allowlisted error fields: no cookie/header echoes.
                    details = body.get('details', {}) if isinstance(body, dict) else {}
                    body = {'name': code, 'details': {k:details[k] for k in
                        ('limit', 'remaining', 'resetTime', 'used', 'current', 'reqId') if k in details}}
                    if result.status_code==400 and code in ('ValidationError','AdditionalPropertiesError'):
                        message=result.json().get('message','')
                        if isinstance(message,str):
                            message=re.sub(r'Bearer\s+\S+','Bearer [redacted]',message,flags=re.I)
                            body['message']=re.sub(r'[\w.+-]+@[\w.-]+','[redacted email]',message)[:1000]
                    status = 'rate_limited' if result.status_code == 429 else 'forbidden' if result.status_code in (401,403) else 'error'
            except RequestFailure as exc:
                status, code = exc.status, exc.code
            except (requests.RequestException, ValueError) as exc:
                status, code = 'error', type(exc).__name__
                meta.update(http_status=None, elapsed_seconds=round(time.monotonic()-started, 3))
            self.last = time.monotonic()
            import uuid
            filename = f'responses/{time.time_ns()}_{uuid.uuid4().hex}.json'
            if status in ('forbidden','rate_limited'):
                self._restriction(bucket,status,code,result,body,source=filename)
            self._log_response(filename,meta,body,status,code)
            print(endpoint, meta['http_status'], status, flush=True)
            if status == 'success':
                if not binary:
                    body['_received_lengths'] = meta.get('received_array_lengths', {})
                return result if response else content if binary else body
            if status in ('forbidden', 'rate_limited'):
                raise RequestFailure(status, code, filename)
            if meta['http_status'] not in (None, 500, 502, 503, 504) or attempt == 2:
                raise RequestFailure(status, code, filename)
            time.sleep(2 ** (attempt+1))


def bounded_binary(response, maximum, reserve, directory):
    """Bound memory/download size and check disk capacity; never unpack archives."""
    length = response.headers.get('Content-Length','')
    if length.isdigit() and int(length)>maximum:
        raise RequestFailure('deferred','Attachment_exceeds_size_limit')
    directory.mkdir(parents=True,exist_ok=True)
    if shutil.disk_usage(directory).free < reserve + (int(length) if length.isdigit() else 0):
        raise RequestFailure('deferred','Insufficient_disk_space')
    data=bytearray()
    for chunk in response.iter_content(chunk_size=1024*1024):
        if len(data)+len(chunk)>maximum:
            raise RequestFailure('deferred','Attachment_exceeds_size_limit')
        data.extend(chunk)
        if shutil.disk_usage(directory).free < reserve+len(data):
            raise RequestFailure('deferred','Insufficient_disk_space')
    if length.isdigit() and len(data)!=int(length) and not response.headers.get('Content-Encoding'):
        raise RequestFailure('error','Truncated_attachment')
    return bytes(data)


def attachment_fields(note):
    """Only structured public file references, never citations embedded in prose."""
    result=[]
    for field,item in note.get('content',{}).items():
        reference = item.get('value') if isinstance(item,dict) else item
        if isinstance(item,dict) and 'everyone' not in item.get('readers',['everyone']):
            continue
        if (isinstance(reference,str) and re.fullmatch(r'/(?:attachment|pdf)/[A-Za-z0-9_-]+(?:\.[A-Za-z0-9]{1,12})?',reference)):
            safe_id(field)
            result.append((field,reference))
    return result


def check_attachment(content, suffix):
    if not content or content.lstrip().lower().startswith((b'<!doctype html',b'<html')):
        raise RequestFailure('error','Empty_or_HTML_attachment')
    if suffix=='.pdf' and not valid_pdf(content):
        raise RequestFailure('error','Invalid_or_truncated_PDF')
    if suffix=='.zip' and not zipfile.is_zipfile(io.BytesIO(content)):
        raise RequestFailure('error','Invalid_or_truncated_ZIP')


@contextlib.contextmanager
def binary_write_guard(api,folder,size,replace=None):
    with getattr(api,'disk_write_lock',contextlib.nullcontext()):
        if replace is not None and replace.exists() and not (replace.parent/'.snapshots/before_full_collector'/replace.name).exists():
            size+=replace.stat().st_size
        if shutil.disk_usage(folder).free<getattr(api,'min_free_bytes',0)+size:
            raise RequestFailure('deferred','Insufficient_disk_space')
        yield


def collect_resources(api, folder, submission, notes, meta, include_history=False, download_files=True):
    """Supplement current files; versions are bound to literal public Edit paths."""
    index_path=folder/'resources.json'
    index=load(index_path) if index_path.exists() else {}
    sources=[(submission,None), *[(n,None) for n in notes]]
    if include_history and (folder/'edits.json').exists():
        for edit in load(folder/'edits.json'):
            if 'everyone' in edit.get('readers',[]):
                sources.append((edit.get('note',{}),edit))
    links=[]
    for note,edit in sources:
        note_id=safe_id(note['id'])
        for field,item in note.get('content',{}).items():
            if isinstance(item,dict) and 'everyone' not in item.get('readers',['everyone']):
                continue
            text=item.get('value') if isinstance(item,dict) else item
            if isinstance(text,str):
                for url in sorted(set(re.findall(r'https?://[^\s<>"\u201d]+',text))):
                    url=url.rstrip('.,;)]}')
                    links.append(dict(note_id=note_id,field=field,url=url,edit_id=edit.get('id') if edit else None,
                        status='link_only',scope='public content; target relevance/access not verified'))
        for field,reference in attachment_fields(note):
            suffix=Path(urlsplit(reference).path).suffix.lower() or ('.pdf' if field=='pdf' else '.bin')
            edit_id=safe_id(edit['id']) if edit else None
            key=hashlib.sha256(json.dumps([note_id,edit_id,field,reference]).encode()).hexdigest()
            if edit:
                relative=Path('revisions')/note_id/edit_id/(field+suffix)
            elif note_id==submission['id']:
                relative=Path('paper.pdf' if field=='pdf' else field+suffix)
            else:
                relative=Path('attachments')/note_id/(field+suffix)
            path=folder/relative
            previous=index.get(key,{})
            target=dict(note_id=note_id,field=field,reference=reference,edit_id=edit_id,
                note_timestamps={k:note[k] for k in ('cdate','tcdate','mdate','tmdate') if k in note},
                edit_timestamps={k:edit[k] for k in ('cdate','tcdate','mdate','tmdate') if edit and k in edit},
                path=relative.as_posix(),status='not_attempted',last_attempt_at=None,
                historical=bool(edit),version_binding='literal_edit_attachment_path' if edit else 'SDK_current_note_field')
            # paper.pdf has already passed main collection; do not fetch it twice.
            existing_pdf=not edit and note_id==submission['id'] and field=='pdf' and meta.get('pdf',{}).get('status')=='ok'
            failed_main_pdf=(not edit and note_id==submission['id'] and field=='pdf'
                and meta.get('pdf',{}).get('status') not in (None,'ok','not_attempted'))
            if not download_files:
                # Register exact sources for later resumption without reading or
                # downloading binary files. Preserve prior successful records.
                if (previous.get('status')=='success' and previous.get('reference')==reference
                        and path.is_file() and path.stat().st_size==previous.get('bytes')):
                    target=previous
                elif existing_pdf and path.is_file() and path.stat().st_size==meta['pdf'].get('bytes'):
                    target.update(status='success',bytes=meta['pdf']['bytes'],sha256=meta['pdf'].get('sha256'))
                else:
                    target.update(status='deferred',error_type='Downloads_disabled',reason='JSON-only collection; file download pending')
                index[key]=target
                continue
            if failed_main_pdf:
                # Main stage already attempted this exact PDF. Keep its failure;
                # do not immediately download the same invalid bytes a second time.
                target.update(status=meta['pdf']['status'],error_type=meta['pdf'].get('error_type'),
                    diagnostics=meta['pdf'].get('diagnostics'),last_attempt_at=meta['pdf'].get('last_attempt_at'))
                index[key]=target
                save(index_path,index)
                continue
            try:
                cached=path.exists() and (existing_pdf or previous.get('status')=='success' and previous.get('reference')==reference)
                if cached:
                    content=path.read_bytes()
                    try:
                        check_attachment(content,suffix)
                        cached=existing_pdf or hashlib.sha256(content).hexdigest()==previous.get('sha256')
                    except RequestFailure:
                        cached=False
                if not cached:
                    target['last_attempt_at']=now()
                    if edit:
                        # Never use current Note ID to retrieve an old version.
                        content=api.request(reference,{},binary=True)
                    else:
                        api.initialize()
                        content=(api.client.get_pdf(id=note_id) if field=='pdf' and getattr(api,'api_version',2)==1
                                 else api.client.get_attachment(field_name=field,id=note_id))
                    check_attachment(content,suffix)
                    with binary_write_guard(api,folder,len(content),replace=path):
                        path.parent.mkdir(parents=True,exist_ok=True)
                        backup(path)
                        temporary=path.with_suffix(path.suffix+'.tmp')
                        temporary.write_bytes(content)
                        temporary.replace(path)
                target.update(status='success',bytes=len(content),sha256=hashlib.sha256(content).hexdigest(),
                    cached=cached,last_attempt_at=previous.get('last_attempt_at') if cached else target['last_attempt_at'])
            except RequestFailure as exc:
                target.update(status=exc.status,error_type=exc.code,request_source=exc.source)
            index[key]=target
            save(index_path,index)
    save(index_path,index)
    external_ref=value(submission,'pdf')
    if arxiv_pdf_url(external_ref):
        target=dict(note_id=submission['id'],field='pdf',reference=external_ref,edit_id=None,
            path='paper.pdf',historical=False,version_binding='explicit_arxiv_reference',
            source_url=arxiv_pdf_url(external_ref),status=meta.get('pdf',{}).get('status','not_attempted'))
        target['status']='success' if target['status']=='ok' else target['status']
        if not download_files and target['status']!='success':
            target.update(status='deferred',error_type='Downloads_disabled',reason='JSON-only collection; file download pending')
        target.update({k:v for k,v in meta.get('pdf',{}).items() if k in ('sha256','error_type','last_attempt_at')})
        if (folder/'paper.pdf').exists():target['bytes']=(folder/'paper.pdf').stat().st_size
        key=hashlib.sha256(json.dumps([submission['id'],None,'pdf',external_ref]).encode()).hexdigest()
        index[key]=target
        save(index_path,index)
    # Preserve links found during a previous history pass when main files resume.
    link_path=folder/'resource_links.json'
    old_links=load(link_path) if link_path.exists() else []
    unique={json.dumps(x,sort_keys=True):x for x in [*old_links,*links]}
    save(link_path,list(unique.values()))
    current=[t for t in index.values() if not t['historical']]
    meta['resources']=dict(status='ok' if all(t['status']=='success' for t in current) else 'incomplete',
        current_counts=dict(Counter(t['status'] for t in current)),
        historical_counts=dict(Counter(t['status'] for t in index.values() if t['historical'])),
        index='resources.json',links='resource_links.json',external_downloads='explicit_arxiv_paper_PDF_only' if arxiv_pdf_url(external_ref) else False,
        history_discovery='available_public_edits_only' if include_history else meta.get('resources',{}).get('history_discovery','not_attempted'),
        complete_history_verified=False)


def paged(api, params, checkpoint, key='notes'):
    """Persist each page before advancing; never equate search caps with totals."""
    state = load(checkpoint) if checkpoint.exists() else dict(offset=0, records={}, complete=False,
        filter=params, totals=[], duplicate_ids=[])
    if state['filter'] != params:
        raise ValueError('Checkpoint filter mismatch')
    if state['complete']:
        return list(state['records'].values()), state
    while True:
        result = api.request('/notes', {**params, 'limit': 1000, 'offset': state['offset'],
                                        'sort': 'id:asc', 'count': 'true'})
        batch = result[key]
        received = len(batch)  # In anonymous/public case identical; auth may filter records.
        # Transport retains original length metadata in envelopes; pagination uses
        # raw server length returned via an internal response-only metadata key.
        received = result.get('_received_lengths', {}).get(key, received)
        total = result.get('count')
        if not isinstance(batch, list) or not isinstance(total, int):
            raise ValueError('Missing pagination array/count')
        for note in batch:
            identifier = safe_id(note['id'])
            if identifier in state['records']:
                state['duplicate_ids'].append(identifier)
                save(checkpoint, state)
                raise ValueError('Duplicate ID across pagination pages; enumeration not certified')
            state['records'][identifier] = note
        state['offset'] += received
        state['totals'].append(total)
        finished = received < 1000
        state['complete'] = finished and state['offset'] == total and len(set(state['totals'])) == 1
        save(checkpoint, state)
        if finished:
            if not state['complete']:
                raise ValueError('Pagination total changed/mismatched; incomplete checkpoint')
            return list(state['records'].values()), state


def discover(api, directory, venue=VENUE):
    manifest_path = directory/'manifest.json'
    manifest = load(manifest_path) if manifest_path.exists() else dict(venue=venue, api=getattr(api,'base_url',config.API),
        complete=False, papers={}, started_at=now())
    if manifest['complete']:
        return manifest
    group_path = directory/'venue.json'
    if not group_path.exists():
        groups = api.request('/groups', {'id': venue})['groups']
        group = next(g for g in groups if g['id'] == venue)
        save(group_path, group)
    group = load(group_path)
    invitation = value(group, 'submission_id')
    if not invitation:
        name = value(group, 'submission_name')
        if not name:
            raise ValueError('Venue did not identify Submission invitation')
        invitation = venue+'/-/'+name
    ipath = directory/'invitation.json'
    if not ipath.exists():
        result = api.request('/invitations', {'id': invitation, 'expired': 'true'})
        if not any(i['id'] == invitation for i in result['invitations']):
            raise ValueError('Submission invitation not verified')
        save(ipath, result)
    filters = [('submission', {'invitation': invitation})]
    # Supplement by actual venue group status IDs, including withdrawn/desk rejected.
    ids = {venue}
    ids.update(value(group, k) for k in ('submission_venue_id','withdrawn_venue_id',
        'desk_rejected_venue_id','rejected_venue_id') if value(group,k))
    filters += [(f'status_{i}', {'content.venueid': v}) for i,v in enumerate(sorted(ids))]
    manifest.update(invitation=invitation, filters=filters)
    save(manifest_path, manifest)
    for label, params in filters:
        records, state = paged(api, params, directory/'pages'/f'{label}.json')
        for note in records:
            if note.get('forum') != note['id']:
                raise ValueError('Non-submission forum encountered in discovery')
            manifest['papers'][note['id']] = note
        manifest.setdefault('query_coverage', {})[label] = dict(total=state['totals'][-1],
            public_unique=len(records), complete=state['complete'], filter=params)
        save(manifest_path, manifest)
    manifest.update(complete=True, finished_at=now(), public_unique=len(manifest['papers']),
        limitations=['Public records visible to this client at query time; hidden/deleted records unknown',
                     'No search fallback; not an accepted-only list; paginated live view is not a transaction snapshot'])
    save(manifest_path, manifest)
    save(directory/'submission_ids.json', list(manifest['papers']))
    return manifest


def backup(path):
    if path.exists():
        dest = path.parent/'.snapshots'/'before_full_collector'/path.name
        if not dest.exists():
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(path.read_bytes())


def write_existing(path, data):
    backup(path)
    save(path, data)


def collect_paper(api, paper, raw, directory, year=2025, venue=VENUE, include_forms=True, download_files=True):
    identifier = safe_id(paper['id'])
    folder = raw/identifier
    folder.mkdir(parents=True, exist_ok=True)
    mp = folder/'metadata.json'
    api_base=getattr(api,'base_url',config.API)
    meta = load(mp) if mp.exists() else dict(paper_id=identifier, year=year, source=api_base,
        collected_at=now(), submission={}, notes={}, pdf={}, edits={})
    if meta.get('source',api_base)!=api_base or meta.get('api_version',getattr(api,'api_version',2))!=getattr(api,'api_version',2):
        raise ValueError('Saved paper API mismatch; do not mix API versions')
    meta.update(venue=venue, api_version=getattr(api,'api_version',2), url='https://openreview.net/forum?id='+identifier,
                title=value(paper,'title'), collector='iclr2025_collection', updated_at=now())
    if not (folder/'submission.json').exists():
        save(folder/'submission.json', paper)
    saved = load(folder/'submission.json')
    if saved['id'] != identifier or saved.get('forum') != identifier:
        raise ValueError('Saved Submission identity mismatch')
    meta['submission'] = dict(status='ok', source='verified_submission_enumeration', preserved=True)
    pdf_inspection=None
    pdf_transport=None
    try:
        if (meta.get('notes', {}).get('status') != 'ok' or
                not meta.get('notes', {}).get('pagination_complete') or not (folder/'notes.json').exists()):
            notes, coverage = paged(api, {'forum': identifier}, directory/'forums'/f'{identifier}.json')
            notes = [n for n in notes if n['id'] != identifier]
            if any(n.get('forum') != identifier for n in notes):
                raise ValueError('Forum mismatch')
            write_existing(folder/'notes.json', notes)
            meta['notes'] = dict(status='ok', count=len(notes), scope='all current public notes returned by forum pagination',
                pagination_complete=coverage['complete'], api_count=coverage['totals'][-1],
                history_complete=False, source=str(directory/'forums'/f'{identifier}.json'))
            evidence=coverage.get('bulk_evidence')
            if evidence:
                verified=evidence.get('per_forum_completeness_verified',True)
                meta['notes'].update(bulk_evidence=evidence,per_forum_completeness_verified=verified,
                    pagination_complete=bool(coverage['complete'] and verified),
                    batch_expansion_saved=True)
    except RequestFailure as exc:
        meta['notes'] = exc.record()
    notes = load(folder/'notes.json') if (folder/'notes.json').exists() else []
    for note in [saved, *notes]:
        if note['id'] == identifier or kind(note) == 'official_review':
            meta['edits'].setdefault(note['id'], dict(status='not_attempted', complete_history_verified=False,
                source=api_base+'/notes/edits' if getattr(api,'api_version',2)==2 else api_base+'/references',
                params={'note.id':note['id']} if getattr(api,'api_version',2)==2 else {'referent':note['id']},
                last_attempt_at=None))
    if not (folder/'edits.json').exists():
        save(folder/'edits.json', [])
    if not download_files:
        pdf=folder/'paper.pdf'
        if not (meta.get('pdf',{}).get('status')=='ok' and pdf.is_file()
                and pdf.stat().st_size==meta['pdf'].get('bytes')):
            meta['pdf']=dict(status='not_attempted',reason='Downloads_disabled',reference=value(saved,'pdf'))
        collect_resources(api,folder,saved,notes,meta,download_files=False)
        return finalize_paper(api,folder,saved,notes,directory,meta,include_forms)
    pdf = folder/'paper.pdf'
    # A prior rejected response may be a MIME envelope, not a damaged PDF.
    # Reuse only a hash-verified candidate in this paper's diagnostic directory.
    previous_pdf=meta.get('pdf',{})
    candidate=previous_pdf.get('diagnostics',{}).get('candidate_path')
    if not pdf.exists() and candidate:
        candidate_path=Path(candidate)
        allowed=(directory/'invalid_downloads'/identifier).resolve()
        if candidate_path.resolve().is_relative_to(allowed) and candidate_path.exists():
            received=candidate_path.read_bytes()
            if hashlib.sha256(received).hexdigest()==previous_pdf['diagnostics'].get('sha256'):
                extracted=pdf_payload(received)
                if extracted!=received and valid_pdf(extracted):
                    with binary_write_guard(api,folder,len(extracted)):
                        temporary=pdf.with_suffix('.pdf.tmp')
                        temporary.write_bytes(extracted)
                        temporary.replace(pdf)
                    pdf_transport=dict(encoding='multipart/form-data',source_sha256=hashlib.sha256(received).hexdigest(),
                        source_bytes=len(received),candidate_path=str(candidate_path),recovered_offline=True)
    received=None
    extracted=None
    try:
        cached_content = pdf.read_bytes() if pdf.exists() else None
        cached_hash = hashlib.sha256(cached_content).hexdigest() if cached_content is not None else None
        if (cached_content is not None and valid_pdf(cached_content)
                and (not previous_pdf.get('sha256') or cached_hash==previous_pdf['sha256'])):
            meta['pdf'] = dict(status='ok', cached=True, sha256=cached_hash,
                bytes=len(cached_content),reference=value(saved,'pdf'))
            if previous_pdf.get('transport_wrapper'):
                meta['pdf']['transport_wrapper']=previous_pdf['transport_wrapper']
        elif value(saved,'pdf'):
            if arxiv_pdf_url(value(saved,'pdf')):
                content=api.download_arxiv_pdf(value(saved,'pdf'))
            else:
                api.initialize()
                content = (api.client.get_pdf(id=identifier) if getattr(api,'api_version',2)==1
                           else api.client.get_attachment(field_name='pdf', id=identifier))
            received=content
            content=pdf_payload(received)
            if content!=received:
                pdf_transport=dict(encoding='multipart/form-data',source_sha256=hashlib.sha256(received).hexdigest(),
                    source_bytes=len(received),recovered_offline=False)
            if not valid_pdf(content):
                with binary_write_guard(api,folder,len(content)):
                    pdf_inspection=pdf_diagnostics(content,directory,identifier)
                raise RequestFailure('error', 'Invalid_or_truncated_PDF')
            with binary_write_guard(api,folder,len(content),replace=pdf):
                backup(pdf)
                temporary = pdf.with_suffix('.pdf.tmp')
                temporary.write_bytes(content)
                temporary.replace(pdf)
            meta['pdf'] = dict(status='ok', bytes=len(content),sha256=hashlib.sha256(content).hexdigest(), reference=value(saved,'pdf'))
        else:
            meta['pdf'] = dict(status='unavailable', reason='No public PDF reference')
    except RequestFailure as exc:
        meta['pdf'] = exc.record()
        if pdf_inspection is not None:
            meta['pdf'].update(diagnostics=pdf_inspection,last_attempt_at=now())
    if pdf_transport is not None:
        meta['pdf']['transport_wrapper']=pdf_transport
    if arxiv_pdf_url(value(saved,'pdf')):
        meta['pdf'].update(reference=value(saved,'pdf'),source_url=arxiv_pdf_url(value(saved,'pdf')),
            version_scope='explicit_arxiv_version' if re.search(r'v\d+(?:\.pdf)?$',value(saved,'pdf'))
                          else 'arxiv_current_version; submission_time_version_unverified')
    # Release large main-PDF buffers before current resources are revalidated.
    cached_content=None
    content=None
    received=None
    collect_resources(api,folder,saved,notes,meta)
    return finalize_paper(api,folder,saved,notes,directory,meta,include_forms)


def finalize_paper(api,folder,saved,notes,directory,meta,include_forms):
    identifier=saved['id']
    if not include_forms:
        from src.iclr_note_order import order_saved_notes
        order_saved_notes(folder,identifier,meta)
    meta['completeness_verified'] = False  # No claim about invisible/deleted/history records.
    meta['note_types'] = dict(Counter(kind(n) for n in notes))
    meta['last_validation'] = dict(unique_ids=len({n['id'] for n in notes}) == len(notes),
        forum_links=all(n.get('forum') == identifier for n in notes),
        unresolved_replyto=[n['id'] for n in notes if n.get('replyto') not in {identifier, *[x['id'] for x in notes]}],
        timestamps_missing=[n['id'] for n in notes if not any(n.get(k) is not None for k in ('tcdate','cdate'))])
    meta['notes']['reply_graph_complete'] = not meta['last_validation']['unresolved_replyto']
    meta['notes']['missing_parent_scope'] = ('No missing parent in this saved public snapshot'
        if meta['notes']['reply_graph_complete'] else
        'Referenced parent not returned in public forum snapshot; hidden/deleted/other-forum status unknown; no placeholder invented')
    meta['phase_a_complete'] = (all(meta[k]['status'] == 'ok' for k in ('submission','notes','pdf'))
        and meta['last_validation']['unique_ids'] and meta['last_validation']['forum_links'])
    meta['phase_a_with_resources_complete'] = meta['phase_a_complete'] and meta['resources']['status']=='ok'
    if include_forms:
        from src.iclr_forms import collect_forms
        collect_forms(api,folder,saved,notes,directory,meta)
    meta['main_collection_complete'] = (meta['phase_a_with_resources_complete']
        and meta.get('forms',{}).get('status')=='ok')
    forms=meta.get('forms',{})
    # Public Notes can survive their public Invitation in both API versions. Keep that gap
    # explicit, but do not refetch a successfully saved Forum indefinitely.
    forms_accounted_for=(bool(forms.get('invitation_counts'))
        and set(forms['invitation_counts'])<= {'success','unavailable'}
        and not forms.get('stop_reason') and not forms.get('notes_without_invitation')
        and not forms.get('field_counts',{}).get('unresolved')
        and not forms.get('field_counts',{}).get('ambiguous'))
    meta['json_collection_complete'] = (all(meta[k]['status']=='ok' for k in ('submission','notes'))
        and bool(meta['notes'].get('pagination_complete'))
        and meta['last_validation']['unique_ids'] and meta['last_validation']['forum_links']
        and (forms.get('status')=='ok' or forms_accounted_for))
    meta['batch_json_saved'] = (bool(meta['notes'].get('batch_expansion_saved'))
        and all(meta[k]['status']=='ok' for k in ('submission','notes'))
        and meta['last_validation']['unique_ids'] and meta['last_validation']['forum_links']
        and meta.get('forms',{}).get('status')=='ok')
    write_existing(folder/'metadata.json', meta)
    stop = meta.get('forms',{}).get('stop_reason') if include_forms else None
    if stop:
        raise RequestFailure(stop['status'],stop['error_type'],stop.get('source'))
    return meta


def collect_edits(api, manifest, raw):
    for identifier in manifest['papers']:
        folder = raw/safe_id(identifier)
        if not (folder/'metadata.json').exists() or not (folder/'notes.json').exists():
            continue
        meta = load(folder/'metadata.json')
        edits = {e['id']:e for e in load(folder/'edits.json')} if (folder/'edits.json').exists() else {}
        submission=load(folder/'submission.json')
        notes=load(folder/'notes.json')
        collect_resources(api,folder,submission,notes,meta,include_history=True)
        write_existing(folder/'metadata.json',meta)
        # Submission as well as actual Review Note IDs; never substitute Forum for Review.
        targets = [submission, *[n for n in notes if kind(n)=='official_review' or attachment_fields(n)]]
        for note in targets:
            target = meta.setdefault('edits', {}).setdefault(note['id'], {})
            if target.get('status') in ('success','empty','ok','no_records_found'):
                continue
            try:
                result = api.request('/notes/edits', {'note.id': safe_id(note['id']), 'sort':'tcdate:asc', 'limit':1000, 'count':'true'})
                batch = result['edits']
                if any(e.get('note',{}).get('id') != note['id'] for e in batch):
                    raise ValueError('Edit target mismatch')
                received = result.get('_received_lengths',{}).get('edits',len(batch))
                target.update(status='success' if received else 'empty', count=len(batch), returned_count=received,
                    last_attempt_at=now(), source=config.API+'/notes/edits', params={'note.id':note['id']},
                    complete_history_verified=False, current_query_complete=isinstance(result.get('count'),int) and result['count']==received)
                for edit in batch:
                    edits[edit['id']] = edit
                write_existing(folder/'edits.json', list(edits.values()))
                collect_resources(api,folder,submission,notes,meta,include_history=True)
            except RequestFailure as exc:
                target.update(exc.record(), complete_history_verified=False)
                write_existing(folder/'metadata.json', meta)
                if exc.status in ('rate_limited','forbidden','not_attempted'):
                    return
            write_existing(folder/'metadata.json', meta)


def report(directory, raw, mode, failure=None):
    manifest = load(directory/'manifest.json') if (directory/'manifest.json').exists() else {'papers':{},'complete':False}
    metas = {p.name:load(p/'metadata.json') for p in raw.iterdir() if p.is_dir() and (p/'metadata.json').exists()} if raw.exists() else {}
    selected = {k:v for k,v in metas.items() if k in manifest['papers']}
    execution_path = directory/'execution.json'
    execution = load(execution_path) if execution_path.exists() else {}
    edits = Counter(t.get('status','not_attempted') for m in selected.values() for t in m.get('edits',{}).values())
    edits_papers = Counter()
    for m in selected.values():
        targets = list(m.get('edits',{}).values())
        done = sum(t.get('status') in ('success','empty') and t.get('current_query_complete',False) for t in targets)
        failed = any(t.get('status') in ('rate_limited','forbidden','error') for t in targets)
        category = 'completed_current_queries' if targets and done==len(targets) else 'partial' if done else 'failed' if failed else 'not_attempted'
        edits_papers[category] += 1
    distributions = Counter(str(value(p,'venue') or value(p,'venueid') or 'unknown') for p in manifest['papers'].values())
    failures = [(k,f,m.get(f,{}).get('status')) for k,m in selected.items() for f in ('submission','notes','pdf') if m.get(f,{}).get('status')!='ok']
    edits_failures = [(k,n,t.get('status'),t.get('error_type')) for k,m in selected.items()
        for n,t in m.get('edits',{}).items() if t.get('status') in ('rate_limited','forbidden','error')]
    successful = sum(m.get('phase_a_complete',False) for m in selected.values())
    resources=Counter(t.get('status','not_attempted') for k in selected
        for t in (load(raw/k/'resources.json').values() if (raw/k/'resources.json').exists() else []))
    summary = dict(venue=VENUE, api=config.API, mode=mode, updated_at=now(), discovery_complete=manifest['complete'],
        discovered_public_ids=len(manifest['papers']), existing_paper_directories=len(metas), phase_a_complete=successful,
        files={k:sum(m.get(k,{}).get('status')=='ok' for m in selected.values()) for k in ('submission','notes','pdf')},
        edits=dict(edits), edits_papers=dict(edits_papers), remaining=len(manifest['papers'])-successful if manifest['complete'] else None,
        failures=failures, edits_failures=edits_failures, stop_reason=failure,
        resources=dict(resources),phase_a_with_resources_complete=sum(m.get('phase_a_with_resources_complete',False) for m in selected.values()),
        forms=dict(Counter(m.get('forms',{}).get('status','not_attempted') for m in selected.values())),
        main_collection_complete=sum(m.get('main_collection_complete',False) for m in selected.values()),
        papers_with_unresolved_replyto=sum(bool(m.get('last_validation',{}).get('unresolved_replyto')) for m in selected.values()),
        remaining_main_collection=(len(manifest['papers'])-sum(m.get('main_collection_complete',False) for m in selected.values())) if manifest['complete'] else None,
        status_distribution=dict(distributions))
    save(directory/'summary.json', summary)
    REPORT.parent.mkdir(parents=True, exist_ok=True)
    text = ['# ICLR 2025 全量采集状态', '', f'更新时间（UTC）：{now()}', '',
        '会议：`'+VENUE+'`；API v2。', '',
        '| 项目 | 实际值 |','|---|---|',
        *[f'| {k} | {summary[k]} |' for k in ('discovery_complete','discovered_public_ids','existing_paper_directories','phase_a_complete','remaining')],
        f'| 文件成功数（仅本清单） | {summary["files"]} |',f'| Edits Note 状态 | {dict(edits)} |', '',
        f'Edits 论文级状态：{dict(edits_papers)}。completed_current_queries 仅指当前查询数量一致，不代表完整历史。', '',
        f'附件状态（含 PDF）：{dict(resources)}；含当前附件完整论文数：{summary["phase_a_with_resources_complete"]}。', '',
        f'公开表单及选项说明状态：{summary["forms"]}；文件、附件和表单说明完成论文数：{summary["main_collection_complete"]}；主采集剩余：{summary["remaining_main_collection"]}。', '',
        '补充材料保存在论文目录，讨论附件在 attachments/<note_id>/；公开 Edits 明确给出的版本文件在 revisions/<note_id>/<edit_id>/。',
        'resources.json 记录附件来源、状态、大小和哈希；resource_links.json 仅保存外部/文本链接，不批量下载外部仓库或数据集。',
        '未查询或未公开的历史文件仍属 unknown；不将当前 PDF 作为旧版本。过大或磁盘空间不足的附件标记 deferred，可调整限制后补采。', '',
        '## 覆盖与限制', '',
        '仅对所有已验证查询的分页数量一致后标记清单完成。不可见、删除及非公开记录数量 unknown。',
        'remaining=null 表示枚举未完成、剩余总量未知，不能解释为没有剩余任务。',
        '各枚举条件的 API 总量与公开唯一 ID 数：'+json.dumps(manifest.get('query_coverage',{}),ensure_ascii=False),
        'Forum 保存当前可见的全部公开类型及嵌套 content，保留 replyto；不能保证历史完整。',
        'Edits 空结果不等于从未修改；success 也不等于完整历史。超过单次 Edits 返回量时标记不完整。',
        '旧的成功原始文件保留；必要更新前备份至 .snapshots/before_full_collector。',
        '## 状态字段分布', '', json.dumps(dict(distributions),ensure_ascii=False), '',
        '## 失败与停止原因', '', str(failure or '无本轮顶层错误'), '',
        *[f'- {i} / {f}: {s}' for i,f,s in failures], '',
        *[f'- Edits {i} / {n}: {s}, {e}' for i,n,s,e in edits_failures], '',
        '## 本次执行与验证', '', json.dumps(execution,ensure_ascii=False,indent=2), '',
        '## 继续运行（PowerShell）','', '```powershell',
        r'.\.venv311\Scripts\python.exe scripts/collect_iclr2025.py --mode pilot --limit 20 --authenticate',
        r'.\.venv311\Scripts\python.exe scripts/collect_iclr2025.py --mode full --authenticate',
        r'.\.venv311\Scripts\python.exe scripts/collect_iclr2025.py --mode retry-failed --authenticate',
        r'.\.venv311\Scripts\python.exe scripts/collect_iclr2025.py --mode edits --authenticate --edits-budget 10 --request-delay 60',
        '```', '', '使用 --authenticate 时在终端输入邮箱和密码（getpass 不回显），凭据不保存，不接收命令行凭据。',
        '429 停止对应接口本轮请求，后续运行遵守持久化 Retry-After；403 不自动重试。',
        '## 官方依据', '',
        '[OpenReview 数据读取指南](https://docs.openreview.net/how-to-guides/data-retrieval-and-modification/how-to-get-all-notes-for-submissions-reviews-rebuttals-etc)', '']
    REPORT.write_text('\n'.join(text), encoding='utf-8')
    print(json.dumps(summary,ensure_ascii=True),flush=True)
    return summary


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--mode', choices=['discover','pilot','full','retry-failed','edits'], required=True)
    parser.add_argument('--limit', type=int, default=20)
    parser.add_argument('--authenticate', action='store_true', help='Prompt in terminal; password is hidden and credentials are not saved')
    parser.add_argument('--request-delay', type=float, default=2)
    parser.add_argument('--max-requests', type=int, default=100000)
    parser.add_argument('--edits-budget', type=int, default=10)
    parser.add_argument('--max-attachment-mb', type=float, default=512, help='Maximum bytes per binary response in MiB')
    parser.add_argument('--min-free-gb', type=float, default=15, help='Keep this many GiB free when downloading files')
    args = parser.parse_args(argv)
    if (args.limit < 1 or args.request_delay < 0 or args.max_requests < 1 or args.edits_budget < 1
            or args.max_attachment_mb<=0 or args.min_free_gb<0):
        parser.error('Limits/budgets must be positive; delay must be nonnegative')
    STATE.mkdir(parents=True, exist_ok=True)
    api = Transport(STATE, args.request_delay, min(args.max_requests,args.edits_budget) if args.mode=='edits' else args.max_requests,
                    args.authenticate,args.max_attachment_mb,args.min_free_gb)
    failure = None
    try:
        manifest = discover(api, STATE)
        if args.mode == 'edits':
            collect_edits(api, manifest, RAW)
        elif args.mode != 'discover':
            papers = list(manifest['papers'].values())
            if args.mode == 'pilot':
                papers = papers[:args.limit]
            elif args.mode == 'retry-failed':
                papers = [p for p in papers if (RAW/safe_id(p['id'])/'metadata.json').exists()
                    and not load(RAW/p['id']/'metadata.json').get('main_collection_complete',False)]
            elif args.mode == 'full':
                pilot_path = STATE/'pilot_validation.json'
                pilot = load(pilot_path) if pilot_path.exists() else {}
                if not pilot.get('passed') or pilot.get('validated_papers',0)<20:
                    raise RequestFailure('not_attempted','Run_and_pass_20_paper_pilot_before_full')
                if not pilot.get('resources_checked'):
                    raise RequestFailure('not_attempted','Rerun_20_paper_pilot_to_validate_added_resources')
                if not pilot.get('forms_checked'):
                    raise RequestFailure('not_attempted','Rerun_20_paper_pilot_to_validate_public_forms')
            completed = []
            for index,paper in enumerate(papers):
                print(f'Paper {index+1}/{len(papers)} {paper["id"]}', flush=True)
                completed.append(collect_paper(api, paper, RAW, STATE))
                if '/notes' in api.blocked or '/invitations' in api.blocked or (api.calls >= api.max_requests and index+1<len(papers)):
                    raise RequestFailure('not_attempted','Main_endpoint_stopped_or_budget_exhausted')
            if args.mode == 'pilot':
                passed = len(completed)==args.limit and all(m['main_collection_complete'] and
                    m['notes'].get('pagination_complete') and not m['last_validation']['timestamps_missing']
                    for m in completed)
                save(STATE/'pilot_validation.json', dict(passed=passed, validated_papers=len(completed),
                    paper_ids=[m['paper_id'] for m in completed], checked_at=now(),resources_checked=True,forms_checked=True))
                if not passed:
                    raise RequestFailure('error','Pilot_incomplete_no_full_run')
    except RequestFailure as exc:
        failure = exc.record()
    except (ValueError, KeyError, StopIteration) as exc:
        failure = dict(status='error',error_type=type(exc).__name__,reason='Schema/pagination validation failed; inspect checkpoints')
    except KeyboardInterrupt:
        failure = dict(status='paused',reason='Interrupted; checkpoints preserved')
    summary=report(STATE, RAW, args.mode, failure)
    unfinished = args.mode in ('full','retry-failed') and summary['remaining_main_collection']!=0
    return 1 if failure or unfinished else 0
