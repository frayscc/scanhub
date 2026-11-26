import io
import time

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfReader
from sqlalchemy import select, text, update

from scanhub.app import Settings, create_app
from scanhub.security import hash_password
from scanhub.storage import Store, batches, files, purge_jobs, upload_logs


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("phase2-test-password"))
    cfg = Settings(tmp_path, upload_token="phase2-scanner", admin_token="phase2-admin", max_bytes=1024*1024)
    client = TestClient(create_app(cfg))
    login = client.post("/api/auth/login", json={"username":"admin","password":"phase2-test-password"})
    return client, cfg, {"X-CSRF-Token":login.json()["csrf"]}


def image_bytes(color="red", size=(120, 60), fmt="PNG"):
    out = io.BytesIO()
    Image.new("RGB", size, color).save(out, fmt)
    return out.getvalue()


def upload(client, csrf, device="scanner A", batch="", color="red", fmt="PNG"):
    response = client.post("/api/upload", headers=csrf,
        data={"device":device,"batch_id":batch}, files={"paper":("scan."+fmt.lower(), image_bytes(color, fmt=fmt))})
    assert response.status_code == 200, response.text
    return response.json()["files"][0]["id"]


def test_device_and_inferred_batches(setup):
    client,cfg,csrf=setup
    first=upload(client,csrf)
    second=upload(client,csrf)
    third=upload(client,csrf,device="scanner B")
    store=Store(cfg.data_dir)
    assert store.get(first)["batch_id"] == store.get(second)["batch_id"]
    assert store.get(third)["batch_id"] != store.get(first)["batch_id"]
    with store.engine.begin() as db:
        db.execute(update(batches).where(batches.c.id==store.get(first)["batch_id"]).values(last_at=time.time()-60))
    fourth=upload(client,csrf)
    assert store.get(fourth)["batch_id"] != store.get(first)["batch_id"]
    actual1=upload(client,csrf,batch="job-123")
    actual2=upload(client,csrf,batch="job-123")
    actual3=upload(client,csrf,batch="job-456")
    assert store.get(actual1)["batch_id"] == store.get(actual2)["batch_id"]
    assert store.get(actual1)["batch_id"] != store.get(actual3)["batch_id"]
    listed=client.get("/api/batches").json()
    assert {b["kind"] for b in listed["items"]} == {"device","inferred"}
    batch_id=store.get(actual1)["batch_id"]
    assert client.get("/api/files?batch="+batch_id).json()["total"] == 2
    assert client.post(f"/api/batches/{batch_id}/zip",headers=csrf).status_code==200
    assert client.patch(f"/api/batches/{batch_id}",headers=csrf,json={"name":"真实任务"}).status_code==200
    assert client.post(f"/api/batches/{batch_id}/delete",headers=csrf).json()["updated"]==2


def test_manual_split_and_merge(setup):
    client,cfg,csrf=setup
    first=upload(client,csrf)
    second=upload(client,csrf)
    batch_id=client.post("/api/batches/organize",headers=csrf,json={"ids":[first],"name":"拆分"}).json()["id"]
    assert client.get("/api/files?batch="+batch_id).json()["total"]==1
    other=Store(cfg.data_dir).get(second)["batch_id"]
    merged=client.post("/api/batches/merge",headers=csrf,json={"ids":[batch_id,other],"name":"合并"})
    assert merged.status_code==200
    assert client.get("/api/files?batch="+merged.json()["id"]).json()["total"]==2
    assert client.get("/api/batches").json()["items"][0]["kind"]=="manual"


def test_pdf_order_rotation_a4_and_jpeg_bytes(setup):
    client,cfg,csrf=setup
    red=upload(client,csrf,color="red",fmt="JPEG")
    blue=upload(client,csrf,color="blue")
    original=client.get(f"/api/files/{red}/original").content
    client.post("/api/files/batch",headers=csrf,json={"ids":[red],"action":"right"})
    payload={"name":"测试合并","pages":[{"id":blue,"rotation":270},{"id":red,"rotation":90}]}
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
    assert client.post("/api/files/pdf",headers=csrf,json={"pages":[{"id":file_id}]}).status_code==422
    assert client.post("/api/files/pdf",json={"pages":[{"id":file_id}]}).status_code==403


def test_permanent_delete_safe_and_cleans_thumbnail(setup):
    client,cfg,csrf=setup
    file_id=upload(client,csrf)
    client.get(f"/api/files/{file_id}/thumbnail")
    store=Store(cfg.data_dir)
    path=cfg.data_dir/store.get(file_id)["path"]
    payload={"ids":[file_id],"confirm":"PERMANENT_DELETE"}
    assert client.post("/api/trash/purge",headers=csrf,json=payload).status_code==409
    assert path.exists()
    client.post("/api/files/batch",headers=csrf,json={"ids":[file_id],"action":"delete"})
    assert client.post("/api/trash/purge",json=payload).status_code==403
    assert client.post("/api/trash/purge",headers=csrf,json={"ids":[file_id]}).status_code==422
    result=client.post("/api/trash/purge",headers=csrf,json=payload)
    assert result.json()=={"deleted":1,"pending":0}
    assert not path.exists()
    assert not (cfg.data_dir/"thumbnails"/f"{file_id}.jpg").exists()
    assert client.get(f"/api/files/{file_id}/original").status_code==404
    assert client.get("/api/integrity").json()=={"missing":[],"orphaned":[]}


def test_purge_retry_after_storage_failure(setup,monkeypatch):
    from pathlib import Path
    client,cfg,csrf=setup
    file_id=upload(client,csrf)
    client.post("/api/files/batch",headers=csrf,json={"ids":[file_id],"action":"delete"})
    original_unlink=Path.unlink
    def fail(self,*args,**kwargs):
        if self.name==file_id:
            raise OSError("disk error")
        return original_unlink(self,*args,**kwargs)
    monkeypatch.setattr(Path,"unlink",fail)
    result=client.post("/api/trash/purge",headers=csrf,json={"ids":[file_id],"confirm":"PERMANENT_DELETE"})
    assert result.json()["pending"]==1
    monkeypatch.setattr(Path,"unlink",original_unlink)
    reopened=Store(cfg.data_dir)
    with reopened.engine.connect() as db:
        assert not db.execute(select(purge_jobs)).all()
    assert reopened.reconcile()=={"missing":[],"orphaned":[]}


def test_retention_default_and_staged_cleanup(setup):
    client,cfg,csrf=setup
    file_id=upload(client,csrf)
    store=Store(cfg.data_dir)
    now=time.time()
    with store.engine.begin() as db:
        db.execute(update(files).where(files.c.id==file_id).values(uploaded_at=now-100*86400))
    assert store.cleanup(now)=={"moved_to_trash":0,"purged":0,"pending":0}
    store.save_options({"file_retention_days":7,"trash_retention_days":3})
    result=store.cleanup(now)
    assert result["moved_to_trash"]==1 and result["purged"]==0
    assert store.get(file_id)["deleted_at"]==now
    assert store.cleanup(now+2*86400)["purged"]==0
    assert store.cleanup(now+4*86400)["purged"]==1


def test_settings_reset_token_persists_and_logs_redact(setup):
    client,cfg,csrf=setup
    assert client.get("/api/settings").json()["file_retention_days"]==0
    config=client.get("/api/settings").json()
    config.update(default_view="list",batch_interval=60)
    assert client.put("/api/settings",headers=csrf,json=config).status_code==200
    token=client.post("/api/settings/upload-token",headers=csrf).json()["token"]
    scanner=TestClient(create_app(Settings(cfg.data_dir,upload_token="old-token")))
    assert scanner.post("/api/upload",headers={"X-Upload-Token":"old-token"},files={"paper":("scan.png",image_bytes())}).status_code==401
    assert scanner.post("/api/upload",headers={"X-Upload-Token":token},files={"paper":("scan.png",image_bytes())}).status_code==200
    assert client.get("/api/settings").json()["default_view"]=="list"
    logs=client.get("/api/upload-logs").json()
    assert {row["status"] for row in logs}=={200,401}
    assert token not in str(logs)
    assert token not in (cfg.data_dir/"scanhub.db").read_bytes().decode("latin1")


def test_migration_from_v1_preserves_files(setup):
    client,cfg,csrf=setup
    file_id=upload(client,csrf)
    store=Store(cfg.data_dir)
    with store.engine.begin() as db:
        for table in ("batches","preferences","upload_logs","purge_jobs"):
            db.exec_driver_sql("DROP TABLE "+table)
        db.execute(text("PRAGMA user_version=1"))
    reopened=Store(cfg.data_dir)
    assert reopened.get(file_id)["name"]=="scan.png"
    assert (cfg.data_dir/reopened.get(file_id)["path"]).exists()
    with reopened.engine.connect() as db:
        assert db.execute(text("PRAGMA user_version")).scalar_one()==2


def test_empty_trash_preserves_inbox(setup):
    client,cfg,csrf=setup
    active=upload(client,csrf)
    removed=upload(client,csrf)
    client.post("/api/files/batch",headers=csrf,json={"ids":[removed],"action":"delete"})
    assert client.post("/api/trash/empty",headers=csrf,json={"confirm":"PERMANENT_DELETE"}).json()["deleted"]==1
    assert client.get(f"/api/files/{active}/original").status_code==200
    assert client.get(f"/api/files/{removed}/original").status_code==404


def test_device_profiles_and_debug_token_separation(setup):
    client,cfg,csrf=setup
    config=client.get("/api/settings").json()
    config["device_profiles"]=[{"source":"A","label":"办公室","location":"二楼"}]
    assert client.put("/api/settings",headers=csrf,json=config).status_code==200
    assert client.get("/api/storage").json()["device_profiles"][0]["label"]=="办公室"
    config["device_profiles"]*=2
    assert client.put("/api/settings",headers=csrf,json=config).status_code==422
    config["device_profiles"]=[]
    config["debug_enabled"]=True
    cfg.admin_token=cfg.upload_token
    assert client.put("/api/settings",headers=csrf,json=config).status_code==422
    cfg.admin_token="separate-debug-admin"
    assert client.put("/api/settings",headers=csrf,json=config).status_code==200
    assert client.get("/api/health").json()["debug_enabled"] is True
    assert client.post("/api/debug/upload",headers={"X-Upload-Token":cfg.upload_token},content=b"raw").status_code==200
    assert len(client.get("/api/protocol-logs").json())==1
    assert TestClient(create_app(cfg)).get("/api/protocol-logs").status_code==401


def test_pdf_tiff_frames_and_failures_cleanup(setup, monkeypatch):
    client,cfg,csrf=setup
    out=io.BytesIO()
    Image.new("RGB",(100,200),"red").save(out,"TIFF",save_all=True,
        append_images=[Image.new("RGB",(200,100),"blue")])
    response=client.post("/api/upload",headers=csrf,files={"paper":("two.tif",out.getvalue())})
    file_id=response.json()["files"][0]["id"]
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


def test_lifespan_runs_saved_retention_and_closes_store(setup):
    client,cfg,csrf=setup
    file_id=upload(client,csrf)
    store=Store(cfg.data_dir)
    store.save_options({"file_retention_days":1})
    with store.engine.begin() as db:
        db.execute(update(files).where(files.c.id==file_id).values(uploaded_at=time.time()-3*86400))
    application=create_app(cfg)
    with TestClient(application) as background:
        background.cookies.update(client.cookies)
        for _ in range(100):
            if background.get("/api/files?deleted=true").json()["total"]==1:
                break
            time.sleep(.01)
        else:
            pytest.fail("startup maintenance did not run")
    assert application.state.store.engine.pool.checkedout()==0
    store.engine.dispose()
