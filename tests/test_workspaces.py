import io
import json
import time
import uuid

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
import pytest
from sqlalchemy import update

from scanhub.app import Settings,create_app
from scanhub.storage import workspaces
from scanhub.workspaces import WorkspaceService
from scanhub.ftp import FTPReceiver
from tests.helpers import seed


@pytest.fixture
def guests(tmp_path):
    app=create_app(Settings(tmp_path))
    a,b=TestClient(app),TestClient(app)
    data_a=a.post('/api/workspaces').json();data_b=b.post('/api/workspaces').json()
    for client,data in ((a,data_a),(b,data_b)):
        client.workspace_id=data['id'];client.headers.update({'X-Workspace-Token':data['token']})
    yield app,a,b
    app.state.store.engine.dispose()


def png():
    output=io.BytesIO();Image.new('RGB',(30,50),'white').save(output,'PNG');return output.getvalue()


def test_no_login_per_page_isolation_all_file_operations(guests):
    app,a,b=guests
    file_id=seed(a,[('private.png',png())])[0]
    assert a.get('/api/files').json()['total']==1
    assert b.get('/api/files').json()['total']==0
    for kind in ('original','preview','thumbnail','rotated'):
        assert b.get(f'/api/files/{file_id}/{kind}').status_code==404
    assert b.post('/api/files/zip',json={'ids':[file_id]}).status_code==404
    assert b.post('/api/files/pdf',json={'pages':[{'id':file_id}]}).status_code==404
    assert b.post('/api/files/batch',json={'ids':[file_id],'action':'delete'}).status_code==404
    assert TestClient(app).get('/api/files').status_code==401
    for endpoint in ('/api/auth/login','/api/trash/empty','/api/upload','/api/debug/upload'):
        assert a.post(endpoint,json={}).status_code==404


def test_claim_is_exclusive_and_end_revokes_and_deletes(guests):
    app,a,b=guests
    assert a.post('/api/workspaces/start').status_code==200
    assert b.post('/api/workspaces/start').status_code==409
    file_id=seed(a,[('private.png',png())])[0]
    path=app.state.store.root/app.state.store.get(file_id)['path']
    assert a.get(f'/api/files/{file_id}/thumbnail').status_code==200
    token=a.headers['X-Workspace-Token']
    assert TestClient(app).post('/api/workspaces/end',json={'token':token}).status_code==204
    assert a.get('/api/files').status_code==401
    assert not path.exists()
    assert not (app.state.store.root/'thumbnails'/f'{file_id}.jpg').exists()
    assert b.post('/api/workspaces/start').status_code==200
    assert app.state.store.reconcile()=={'missing':[],'orphaned':[]}


def test_timeout_cleanup_and_late_ftp_file_do_not_reach_next_guest(guests):
    app,a,b=guests
    file_id=seed(a,[('expires.png',png())])[0]
    receiver=FTPReceiver(app.state.store,Settings(app.state.store.root))
    receipt=uuid.uuid4().hex
    ready=receiver.root/'queue'/(receipt+'.ready')
    ready.write_bytes(png())
    ready.with_suffix('.json').write_text(json.dumps({'id':receipt,'name':'late.png','device':'FTP','workspace_id':a.workspace_id}))
    with app.state.store.engine.begin() as db:
        db.execute(update(workspaces).where(workspaces.c.id==a.workspace_id).values(expires=time.time()-1))
    WorkspaceService(app.state.store).cleanup()
    assert a.get('/api/files').status_code==401
    assert b.post('/api/workspaces/start').status_code==200
    receiver.import_one(ready)
    assert not ready.exists()
    assert b.get('/api/files').json()['total']==0
    assert app.state.store.get(file_id) is None


def test_legacy_files_are_preserved_but_not_public(guests):
    app,a,b=guests
    store=app.state.store
    staged=store.root/'tmp'/'legacy.png';staged.write_bytes(png())
    from scanhub.storage import inspect_file
    mime,w,h=inspect_file(staged)
    old=store.ingest([dict(temp=staged,name='old.png',mime=mime,width=w,height=h)],'legacy','', '{}')[0]['id']
    assert a.get('/api/files').json()['total']==0
    assert b.get(f'/api/files/{old}/original').status_code==404
    a.post('/api/workspaces/end')
    WorkspaceService(store).cleanup()
    assert (store.root/store.get(old)['path']).exists()


def test_workspace_limit_and_no_browser_cookie(tmp_path):
    app=create_app(Settings(tmp_path));client=TestClient(app)
    first=client.post('/api/workspaces')
    assert first.status_code==200 and 'set-cookie' not in first.headers
    for _ in range(63):assert client.post('/api/workspaces').status_code==200
    assert client.post('/api/workspaces').status_code==503
    app.state.store.engine.dispose()
