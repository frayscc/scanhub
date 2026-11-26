from concurrent.futures import ThreadPoolExecutor
from ftplib import FTP, error_perm, error_temp
import io
import json
import logging
import time
import uuid

from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import select

from scanhub.app import Settings,create_app
from scanhub.storage import receipts


@pytest.fixture
def server(tmp_path,monkeypatch):
    monkeypatch.setenv('FTP_USERNAME','scanner')
    monkeypatch.setenv('FTP_PASSWORD','ftp-test-password')
    monkeypatch.setenv('FTP_PORT','0')
    monkeypatch.setenv('FTP_BIND','127.0.0.1')
    monkeypatch.setenv('FTP_PUBLIC_HOST','127.0.0.1')
    app=create_app(Settings(tmp_path,max_bytes=8192))
    with TestClient(app) as client:
        data=client.post('/api/workspaces').json();client.workspace_id=data['id'];client.headers.update({'X-Workspace-Token':data['token']})
        client.post('/api/workspaces/start')
        csrf=data['token']
        yield app,client,app.state.ftp.server.socket.getsockname()[1],csrf


def image():
    output=io.BytesIO();Image.new('RGB',(60,100),'white').save(output,'JPEG');return output.getvalue()


def connect(port,password='ftp-test-password'):
    ftp=FTP();ftp.connect('127.0.0.1',port,timeout=5);ftp.login('scanner',password);return ftp


def wait_for(client,count):
    for _ in range(150):
        response=client.get('/api/files').json()
        if response['total']==count:return response['items']
        time.sleep(.03)
    raise AssertionError('FTP originals did not enter inbox')


def test_real_ftp_duplicate_names_originals_and_rotation(server):
    app,client,port,csrf=server
    with connect(port) as ftp:
        assert ftp.storbinary('STOR same.jpg',io.BytesIO(image())).startswith('226')
        assert ftp.size('same.jpg')==len(image())
        ftp.storbinary('STOR same.jpg',io.BytesIO(image()))
    rows=wait_for(client,2)
    assert {row['name'] for row in rows}=={'same.jpg'}
    assert len({row['id'] for row in rows})==2
    assert all(client.get('/api/files/'+row['id']+'/original').content==image() for row in rows)
    assert client.post('/api/files/batch',headers={'X-Workspace-Token':csrf},json={'ids':[r['id'] for r in rows],'action':'right'}).status_code==200
    assert client.get('/api/upload-logs').status_code==404
    assert app.state.store.reconcile()=={'missing':[],'orphaned':[]}


def test_ftp_upload_only_permissions_and_invalid_password(server):
    app,client,port,csrf=server
    ftp=FTP();ftp.connect('127.0.0.1',port)
    with pytest.raises(error_perm):ftp.login('scanner','wrong')
    ftp.close()
    with connect(port) as ftp:
        for operation in [lambda:ftp.retrbinary('RETR anything',lambda data:None),lambda:ftp.delete('anything'),lambda:ftp.rename('a','b')]:
            with pytest.raises(error_perm):operation()
    assert client.post('/api/upload').status_code==404
    assert client.post('/api/debug/upload').status_code==404


def test_invalid_and_interrupted_files_never_appear(server):
    app,client,port,csrf=server
    with connect(port) as ftp:
        ftp.storbinary('STOR fake.jpg',io.BytesIO(b'not an image'))
        ftp.storbinary('STOR interrupted.jpg',io.BytesIO(image()[:30]))
    for _ in range(150):
        if len(list((app.state.ftp.root/'failed').glob('*.ready')))==2:break
        time.sleep(.03)
    else:pytest.fail('Invalid FTP files were not quarantined')
    assert client.get('/api/files').json()['total']==0


def test_receipt_recovery_does_not_duplicate_after_commit(server,monkeypatch):
    app,client,port,csrf=server
    receiver=app.state.ftp
    receipt=uuid.uuid4().hex
    ready=receiver.root/'queue'/(receipt+'.ready')
    meta=ready.with_suffix('.json')
    ready.write_bytes(image());meta.write_text(json.dumps({'id':receipt,'name':'recover.jpg','device':'FTP扫描仪','workspace_id':client.workspace_id}))
    # Process synchronously through the same durable ingest path before background polling.
    receiver.import_one(ready)
    ready.write_bytes(image());meta.write_text(json.dumps({'id':receipt,'name':'recover.jpg','device':'FTP扫描仪','workspace_id':client.workspace_id}))
    receiver.import_one(ready)
    assert client.get('/api/files').json()['total']==1
    with app.state.store.engine.connect() as db:
        assert len(db.execute(select(receipts)).all())==1


def test_concurrent_ftp_transfers(server):
    app,client,port,csrf=server
    def send(_):
        with connect(port) as ftp:ftp.storbinary('STOR same.jpg',io.BytesIO(image()))
    with ThreadPoolExecutor(max_workers=2) as pool:list(pool.map(send,range(4)))
    wait_for(client,4)


def test_ftp_size_limit(server):
    app,client,port,csrf=server
    ftp=connect(port)
    try:
        try:ftp.storbinary('STOR large.jpg',io.BytesIO(b'x'*100000))
        except Exception:pass
    finally:ftp.close()
    time.sleep(.2)
    assert client.get('/api/files').json()['total']==0
    assert list((app.state.ftp.root/'failed').iterdir())


def test_no_workspace_rejects_upload_and_new_guest_can_claim(server):
    app,client,port,csrf=server
    assert client.post('/api/workspaces/end').status_code==204
    with connect(port) as ftp:
        with pytest.raises(error_temp,match='450'):
            ftp.storbinary('STOR unclaimed.jpg',io.BytesIO(image()))
    assert not list((app.state.ftp.root/'queue').glob('*.json'))
    token=client.post('/api/workspaces').json()['token']
    client.headers['X-Workspace-Token']=token
    assert client.post('/api/workspaces/start').status_code==200
    with connect(port) as ftp:ftp.storbinary('STOR next.jpg',io.BytesIO(image()))
    assert wait_for(client,1)[0]['name']=='next.jpg'


@pytest.mark.parametrize('passive',[True,False])
def test_scanner_directory_and_both_data_modes(server,passive):
    app,client,port,csrf=server
    with connect(port) as ftp:
        ftp.trust_server_pasv_ipv4_address=True
        ftp.set_pasv(passive)
        ftp.cwd('/')
        ftp.mkd('scans')
        ftp.cwd('scans')
        ftp.storbinary('STOR page.jpg',io.BytesIO(image()))
    assert wait_for(client,1)[0]['name']=='page.jpg'


def test_protocol_logs_explain_rejection_without_credentials_or_paths(server,caplog):
    app,client,port,csrf=server
    caplog.set_level(logging.INFO,logger='uvicorn.error')
    with connect(port) as ftp:
        with pytest.raises(error_perm):ftp.cwd('private-document-folder')
        ftp.storbinary('STOR private-document.jpg',io.BytesIO(image()))
    wait_for(client,1)
    client.post('/api/workspaces/end')
    with connect(port) as ftp:
        with pytest.raises(error_temp):ftp.storbinary('STOR rejected-document.jpg',io.BytesIO(image()))
    assert 'cmd=CWD code=550' in caplog.text
    assert 'cmd=STOR code=450' in caplog.text
    assert 'cmd=PASV code=227' in caplog.text
    for secret in ('ftp-test-password','private-document','rejected-document',csrf):assert secret not in caplog.text


def test_readonly_probe_uses_advertised_passive_address(server,monkeypatch):
    from scanhub.ftp_probe import probe
    app,client,port,csrf=server
    monkeypatch.setenv('FTP_PORT',str(port))
    result=probe()
    assert result['ok'] and result['passive_host']=='127.0.0.1'
    assert result['passive_connection']=='ok'
    assert client.get('/api/files').json()['total']==0
    assert 'ftp-test-password' not in json.dumps(result)
