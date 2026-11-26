import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select, text, update

from scanhub.app import Settings, create_app
from tests.helpers import seed
from scanhub.security import hash_password
from scanhub.storage import Store, batches, files, purge_jobs, upload_logs


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("phase2-test-password"))
    cfg = Settings(tmp_path, max_bytes=1024*1024)
    client = TestClient(create_app(cfg))
    data=client.post('/api/workspaces').json()
    client.workspace_id=data['id'];client.headers.update({'X-Workspace-Token':data['token']})
    return client,cfg,{'X-Workspace-Token':data['token']}



def image_bytes(color="red", size=(120, 60), fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    return out.getvalue()


def upload(client,csrf,device="scanner A",batch="",color="red",fmt="PNG"):
    return seed(client,[("scan."+fmt.lower(),image_bytes(color,fmt=fmt))],device=device,batch=batch)[0]







def test_pdf_order_rotation_a4_and_jpeg_bytes(setup):
    client,cfg,csrf=setup
    red=upload(client,csrf,color="red",fmt="JPEG")
    blue=upload(client,csrf,color="blue")
    original=client.get(f"/api/files/{red}/original").content
    client.post("/api/files/batch",headers=csrf,json={"ids":[red],"action":"right"})
    payload={"name":"娴嬭瘯鍚堝苟","pages":[{"id":blue,"rotation":270},{"id":red,"rotation":90}]}
    response=client.post("/api/files/pdf",headers=csrf,json=payload)
    assert response.status_code==200,response.text
    reader=PdfReader(io.BytesIO(response.content))
    assert len(reader.pages)==2
    assert reader.pages[0].rotation==270
    assert reader.pages[1].rotation==180
    jpeg_image=list(reader.pages[1].images)[0]
    assert jpeg_image.data==original  # Direct embedding, no JPEG re-encoding.
    assert client.get(f"/api/files/{red}/original").content==original
    payload["a4"]=True
    a4=client.post("/api/files/pdf",headers=csrf,json=payload)
    assert a4.status_code==200,a4.text
    for page in PdfReader(io.BytesIO(a4.content)).pages:
        assert abs(float(page.mediabox.width)-595.2756)<.01
        assert abs(float(page.mediabox.height)-841.8898)<.01
        assert page.rotation==0
    assert not list((cfg.data_dir/"tmp").iterdir())


def test_pdf_missing_and_deleted_rejected(setup):
    client,cfg,csrf=setup
    file_id=upload(client,csrf)
    assert client.post("/api/files/pdf",headers=csrf,json={"pages":[{"id":"unknown"}]}).status_code==404
    client.post("/api/files/batch",headers=csrf,json={"ids":[file_id],"action":"delete"})
    assert client.post("/api/files/pdf",headers=csrf,json={"pages":[{"id":file_id}]}).status_code==404
    assert TestClient(create_app(cfg)).post("/api/files/pdf",json={"pages":[{"id":file_id}]}).status_code==401











def test_migration_from_v1_preserves_files(setup):
    client,cfg,csrf=setup
    file_id=upload(client,csrf)
    store=Store(cfg.data_dir)
    with store.engine.begin() as db:
        for table in ("batches","preferences","upload_logs","purge_jobs","workspaces","scanner_lease","ingestion_receipts"):
            db.exec_driver_sql("DROP TABLE "+table)
        db.execute(text("PRAGMA user_version=1"))
    reopened=Store(cfg.data_dir)
    assert reopened.get(file_id)["name"]=="scan.png"
    assert (cfg.data_dir/reopened.get(file_id)["path"]).exists()
    with reopened.engine.connect() as db:
        assert db.execute(text("PRAGMA user_version")).scalar_one()==4







def test_pdf_tiff_frames_and_failures_cleanup(setup, monkeypatch):
    client,cfg,csrf=setup
    out=io.BytesIO()
    Image.new("RGB",(100,200),"red").save(out,"TIFF",save_all=True,
        append_images=[Image.new("RGB",(200,100),"blue")])
    file_id=seed(client,[("two.tif",out.getvalue())])[0]
    result=client.post("/api/files/pdf",headers=csrf,json={"pages":[{"id":file_id}]})
    assert result.status_code==200,result.text
    assert len(PdfReader(io.BytesIO(result.content)).pages)==2
    import scanhub.pdf_export
    def fail(*args,**kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(scanhub.pdf_export,"build_pdf",fail)
    result=client.post("/api/files/pdf",headers=csrf,json={"pages":[{"id":file_id}]})
    assert result.status_code==507
    assert not list((cfg.data_dir/"tmp").iterdir())
    assert client.get(f"/api/files/{file_id}/original").content==out.getvalue()


def test_pdf_resource_limit_for_repeated_pages(setup):
    client,cfg,csrf=setup
    file_id=upload(client,csrf)
    store=Store(cfg.data_dir)
    with store.engine.begin() as db:
        db.execute(update(files).where(files.c.id==file_id).values(size=40*1024*1024))
    result=client.post("/api/files/pdf",headers=csrf,json={"pages":[{"id":file_id},{"id":file_id}]})
    assert result.status_code==422
    assert not list((cfg.data_dir/"tmp").iterdir())


def test_pdf_conversion_error_is_safe_and_cleans_temp(setup,monkeypatch):
    import img2pdf
    client,cfg,csrf=setup
    file_id=upload(client,csrf)
    def fail(*args,**kwargs):
        raise img2pdf.UnsupportedColorspaceError("private detail")
    monkeypatch.setattr(img2pdf,"convert",fail)
    response=client.post("/api/files/pdf",headers=csrf,json={"pages":[{"id":file_id}]})
    assert response.status_code==422
    assert "private detail" not in response.text
    assert not list((cfg.data_dir/"tmp").iterdir())
