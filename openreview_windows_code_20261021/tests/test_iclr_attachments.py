import hashlib
import io
import json
import zipfile
from types import SimpleNamespace

import pytest

from src import iclr_attachments as a
from src import iclr2025_collection as base
from src.common import load,save

PDF=b'%PDF-1.4\nfixture\n%%EOF'


def fixture(tmp_path,version=2):
    folder=tmp_path/'raw/ICLR_2024/paper'
    paper=dict(id='paper',forum='paper',readers=['everyone'],tcdate=1,
        content={'pdf':{'value':'/pdf/main.pdf'},'supplementary_material':{'value':'/attachment/supp.zip'}})
    notes=[dict(id='review',forum='paper',replyto='paper',readers=['everyone'],
        content={'file':{'value':'/attachment/reply.pdf'},'comment':{'value':'https://example.org/external.zip'}})]
    meta=dict(paper_id='paper',year=2024,api_version=version,
        source='https://api2.openreview.net' if version==2 else 'https://api.openreview.net',
        submission={'status':'ok'},notes={'status':'ok','pagination_complete':True},pdf={},
        forms={'status':'incomplete'},json_collection_complete=True,
        last_validation={'unique_ids':True,'forum_links':True},edits={'paper':{'status':'not_attempted'}})
    for name,body in [('submission',paper),('notes',notes),('metadata',meta),('edits',[])]:save(folder/(name+'.json'),body)
    buf=io.BytesIO()
    with zipfile.ZipFile(buf,'w') as z:z.writestr('results.txt','real fixture, no extraction')
    class API:
        min_free_bytes=0
        def __init__(self):self.calls=[]
        def request(self,path,params,binary=False):
            assert binary and params=={}
            assert path.startswith(('/pdf/','/attachment/'))
            self.calls.append(path)
            return buf.getvalue() if path.endswith('.zip') else PDF
    return folder,paper,notes,meta,API()


def test_downloads_only_saved_files_and_resume_without_network(tmp_path):
    folder,paper,notes,meta,api=fixture(tmp_path)
    protected={name:(folder/name).read_bytes() for name in ('submission.json','notes.json','edits.json')}
    result=a.supplement(api,folder,paper,notes,meta)
    assert result['status']=='success' and len(api.calls)==3
    assert (folder/'paper.pdf').read_bytes()==PDF
    assert (folder/'attachments/review/file.pdf').read_bytes()==PDF
    assert not (folder/'results.txt').exists()
    assert all(t['version_binding']=='literal_saved_attachment_path' for t in load(folder/'resources.json').values())
    assert all((folder/name).read_bytes()==body for name,body in protected.items())
    a.supplement(api,folder,paper,notes,load(folder/'metadata.json'))
    assert len(api.calls)==3
    assert load(folder/'metadata.json')['json_collection_complete']
    assert load(folder/'metadata.json')['forms']['status']=='incomplete'


@pytest.mark.parametrize('status',['rate_limited','forbidden','not_attempted','deferred'])
def test_access_or_budget_stop_does_not_mark_unvisited_files_failed(tmp_path,status):
    folder,paper,notes,meta,api=fixture(tmp_path)
    def blocked(*args,**kwargs):
        api.calls.append(args[0]);raise base.RequestFailure(status,'test_stop')
    api.request=blocked
    with pytest.raises(base.RequestFailure):a.supplement(api,folder,paper,notes,meta)
    tasks=list(load(folder/'resources.json').values())
    assert len(api.calls)==1 and tasks[0]['status']==status
    assert all(t['status']=='not_attempted' for t in tasks[1:])
    assert not (folder/'paper.pdf').exists()
    assert load(folder/'metadata.json')['attachments_supplement']['stop_reason']['status']==status


def test_corrupt_cached_file_is_replaced_with_backup(tmp_path):
    folder,paper,notes,meta,api=fixture(tmp_path)
    a.supplement(api,folder,paper,notes,meta)
    (folder/'paper.pdf').write_bytes(b'corrupt')
    a.supplement(api,folder,paper,notes,meta)
    assert len(api.calls)==4 and (folder/'paper.pdf').read_bytes()==PDF
    assert (folder/'.snapshots/before_full_collector/paper.pdf').read_bytes()==b'corrupt'


def test_invalid_response_never_overwrites_existing_file(tmp_path):
    folder,paper,notes,meta,api=fixture(tmp_path)
    (folder/'paper.pdf').write_bytes(PDF)
    meta['pdf']={'reference':'/pdf/old.pdf','sha256':hashlib.sha256(PDF).hexdigest()}
    api.request=lambda *args,**kwargs:b'<html>access page</html>'
    result=a.supplement(api,folder,paper,notes,meta)
    assert result['status']=='incomplete'
    assert (folder/'paper.pdf').read_bytes()==PDF
    assert load(folder/'metadata.json')['pdf']['status']=='error'
    # The preserved old PDF cannot become a successful new reference on resume.
    a.supplement(api,folder,paper,notes,load(folder/'metadata.json'))
    assert load(folder/'metadata.json')['pdf']['status']=='error'


def test_literal_reference_change_does_not_reuse_old_file(tmp_path):
    folder,paper,notes,meta,api=fixture(tmp_path)
    a.supplement(api,folder,paper,notes,meta)
    paper['content']['pdf']['value']='/pdf/new.pdf'
    a.supplement(api,folder,paper,notes,meta)
    assert api.calls[-1]=='/pdf/new.pdf' and len(api.calls)==4


def test_private_fields_and_external_links_are_not_download_tasks(tmp_path):
    folder,paper,notes,meta,api=fixture(tmp_path)
    paper['content']['private']={'value':'/attachment/private.zip','readers':['authors']}
    notes.append(dict(id='private',forum='paper',readers=['authors'],content={'file':'/attachment/private.zip'}))
    a.supplement(api,folder,paper,notes,meta)
    assert len(api.calls)==3 and not any('private' in p for p in api.calls)


def test_cross_forum_and_unsafe_path_rejected(tmp_path):
    folder,paper,notes,meta,api=fixture(tmp_path)
    notes[0]['forum']='other'
    with pytest.raises(ValueError):a.supplement(api,folder,paper,notes,meta)
    notes[0]['forum']='paper';notes[0]['content']['../escape']='/attachment/x.zip'
    with pytest.raises(ValueError):list(a.file_tasks(folder,paper,notes))
    assert not api.calls


def test_explicit_arxiv_pdf_uses_existing_isolated_downloader(tmp_path):
    folder,paper,notes,meta,api=fixture(tmp_path)
    paper['content']={'pdf':'https://arxiv.org/abs/1502.00001v2'}
    notes=[]
    seen=[]
    api.download_arxiv_pdf=lambda ref:seen.append(ref) or PDF
    a.supplement(api,folder,paper,notes,meta)
    assert seen==['https://arxiv.org/abs/1502.00001v2'] and not api.calls
    assert meta['pdf']['version_scope']=='explicit_arxiv_version'


def test_dry_run_no_authentication_or_paper_writes(tmp_path,monkeypatch):
    folder,_,_,_,_=fixture(tmp_path)
    before={p.name:p.read_bytes() for p in folder.iterdir() if p.is_file()}
    monkeypatch.setattr(a.config,'ROOT',tmp_path/'code')
    monkeypatch.setattr(base,'Transport',lambda *args,**kwargs:pytest.fail('dry run must not initialize transport'))
    assert a.main(['--raw-root',str(tmp_path/'raw'),'--years','2024','2025','--dry-run','--authenticate'])==0
    assert all((folder/name).read_bytes()==body for name,body in before.items())
    report=load(tmp_path/'code/reports/iclr_attachments_report.json')
    assert report['requests']==0 and report['files']==3 and report['missing_years']==[2025]
