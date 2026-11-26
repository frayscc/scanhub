import asyncio
from concurrent.futures import ThreadPoolExecutor
import io
import json
from pathlib import Path
import sqlite3
import threading
import time
import zipfile

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image
import pytest

from scanhub.app import Settings, create_app
from scanhub.maintenance import backup, inspect_data, restore, MANIFEST
from scanhub.security import hash_password
from scanhub.work import BoundedWorker


@pytest.fixture
def release_data(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("release-test-password"))
    cfg = Settings(tmp_path / "data", upload_token="release-scanner")
    app = create_app(cfg)
    client = TestClient(app)
    csrf = client.post("/api/auth/login", json={"username":"admin", "password":"release-test-password"}).json()["csrf"]
    image = io.BytesIO()
    Image.new("RGB", (30, 20), "blue").save(image, "PNG")
    response = client.post("/api/upload", headers={"X-CSRF-Token": csrf}, files={"paper":("scan.png", image.getvalue())})
    file_id = response.json()["files"][0]["id"]
    client.post("/api/files/batch", headers={"X-CSRF-Token":csrf}, json={"ids":[file_id],"action":"right"})
    yield cfg.data_dir, app.state.store, file_id, image.getvalue(), app, client
    app.state.store.engine.dispose()


def test_backup_restore_roundtrip_preserves_original_rotation_and_revokes_sessions(release_data,tmp_path):
    root,store,file_id,original,app,client = release_data
    store.save_options({"upload_token_hash":"sha256:example", "default_view":"list"})
    archive = tmp_path / "backup.zip"
    report = backup(root, archive, offline_confirmed=True)
    assert report["originals"] == 1
    target = tmp_path / "restored"
    assert restore(archive,target,offline_confirmed=True)["sessions_revoked"]
    assert inspect_data(target,verify_hashes=True)["ok"]
    with sqlite3.connect(target / "scanhub.db") as db:
        path,rotation = db.execute("SELECT path,rotation FROM files WHERE id=?", (file_id,)).fetchone()
        assert rotation == 90
        assert db.execute("SELECT count(*) FROM sessions").fetchone()[0] == 0
        assert db.execute("SELECT value FROM preferences WHERE key='default_view'").fetchone()[0] == '"list"'
    assert (target/path).read_bytes() == original
    assert archive.exists() and not list(tmp_path.glob("*.partial"))


def test_offline_acknowledgement_and_no_overwrite(release_data,tmp_path):
    root,*_ = release_data
    archive=tmp_path / "backup.zip"
    with pytest.raises(ValueError): backup(root,archive)
    assert not archive.exists()
    backup(root,archive,True)
    before=archive.read_bytes()
    with pytest.raises(ValueError): backup(root,archive,True)
    assert archive.read_bytes()==before
    with pytest.raises(ValueError): backup(root,root/"inside.zip",True)
    target=tmp_path/"existing"
    target.mkdir()
    (target/"keep").write_text("keep")
    with pytest.raises(ValueError): restore(archive,target,True)
    assert (target/"keep").read_text()=="keep"
    with pytest.raises(ValueError): restore(archive,tmp_path/"new")


def test_restore_rejects_tamper_and_traversal_without_publication(release_data,tmp_path):
    root,*_ = release_data
    original=tmp_path/"backup.zip"
    backup(root,original,True)
    tampered=tmp_path/"tampered.zip"
    with zipfile.ZipFile(original) as source, zipfile.ZipFile(tampered,"w") as out:
        for name in source.namelist():
            data=source.read(name)
            if name.startswith("uploads/"):
                data=bytes([data[0]^1])+data[1:]
            out.writestr(name,data)
    with pytest.raises(ValueError): restore(tampered,tmp_path/"bad-restore",True)
    assert not (tmp_path/"bad-restore").exists()
    traversal=tmp_path/"traversal.zip"
    with zipfile.ZipFile(traversal,"w") as out:
        out.writestr(MANIFEST,json.dumps({"format":1,"schema":2,"files":[{"path":"../escape","size":1,"sha256":"x"}]}))
        out.writestr("../escape",b"x")
    with pytest.raises(ValueError): restore(traversal,tmp_path/"unsafe",True)
    assert not (tmp_path/"escape").exists()
    assert not (tmp_path/"unsafe").exists()


def test_backup_does_not_include_tmp_and_detects_changed_original(release_data,tmp_path):
    root,store,file_id,*_=release_data
    (root/"tmp"/"partial").write_bytes(b"not complete")
    target=tmp_path/"backup.zip"
    backup(root,target,True)
    with zipfile.ZipFile(target) as archive:
        assert not any(name.startswith("tmp/") for name in archive.namelist())
    path=root/store.get(file_id)["path"]
    data=path.read_bytes()
    path.write_bytes(bytes([data[0]^1])+data[1:])
    assert not inspect_data(root,verify_hashes=True)["ok"]
    with pytest.raises(ValueError): backup(root,tmp_path/"broken.zip",True)
    assert not (tmp_path/"broken.zip").exists()


def test_check_does_not_create_or_migrate_database(tmp_path):
    absent=tmp_path/"absent"
    assert not inspect_data(absent)["ok"]
    assert not absent.exists()


def test_ready_reports_storage_and_low_space(release_data,monkeypatch):
    root,store,file_id,original,app,client=release_data
    assert client.get("/api/ready").status_code==200
    assert client.get("/api/health").json()["instance"]=="production"
    monkeypatch.setenv("MIN_FREE_MB","999999999999")
    response=client.get("/api/ready")
    assert response.status_code==503
    assert response.json()=={"status":"unavailable"}
    assert client.get("/api/health").status_code==200
    assert not list((root/"tmp").glob(".ready-*"))


def test_worker_does_not_block_loop_and_limits_parallelism():
    async def exercise():
        worker=BoundedWorker()
        started=threading.Event()
        release=threading.Event()
        def block():
            started.set()
            release.wait(3)
            return 42
        first=asyncio.create_task(worker.run(block))
        while not started.is_set(): await asyncio.sleep(.001)
        try:
            with pytest.raises(HTTPException) as caught:
                await worker.run(lambda:0,_wait_timeout=.02)
            assert caught.value.status_code==503
        finally:
            release.set()
        assert await first==42
        assert await worker.run(lambda:7)==7
    asyncio.run(exercise())


def test_liveness_responds_during_image_work(release_data,monkeypatch):
    import scanhub.manager
    root,store,file_id,original,app,client=release_data
    entered,release=threading.Event(),threading.Event()
    real=scanhub.manager.preview_bytes
    def slow(*args):
        entered.set()
        release.wait(3)
        return real(*args)
    monkeypatch.setattr(scanhub.manager,"preview_bytes",slow)
    # Context manager keeps requests on the same ASGI event loop, matching production.
    with TestClient(app) as browser, ThreadPoolExecutor(max_workers=1) as pool:
        browser.cookies.update(client.cookies)
        request=pool.submit(browser.get,f"/api/files/{file_id}/preview")
        assert entered.wait(3)
        try:
            start=time.monotonic()
            assert browser.get("/api/health").status_code==200
            assert time.monotonic()-start<1
        finally:
            release.set()
        assert request.result(timeout=3).status_code==200


def test_cancelled_worker_waits_for_thread_before_releasing_capacity():
    async def exercise():
        worker=BoundedWorker()
        entered,release=threading.Event(),threading.Event()
        def slow():
            entered.set()
            release.wait(3)
        job=asyncio.create_task(worker.run(slow))
        while not entered.is_set(): await asyncio.sleep(.001)
        job.cancel()
        await asyncio.sleep(.01)
        try:
            assert not job.done()
            with pytest.raises(HTTPException):
                await worker.run(lambda:None,_wait_timeout=.01)
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError): await job
        assert await worker.run(lambda:True)
    asyncio.run(exercise())
