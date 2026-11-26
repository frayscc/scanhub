import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from sqlalchemy import select

from scanhub.app import Settings, create_app
from scanhub.security import hash_password
from scanhub.storage import Store, files


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("long-test-password"))
    cfg = Settings(tmp_path, upload_token="scanner-test-token", max_bytes=8192)
    client = TestClient(create_app(cfg))
    response = client.post("/api/auth/login", json={"username": "admin", "password": "long-test-password"})
    assert response.status_code == 200
    return client, cfg, {"X-CSRF-Token": response.json()["csrf"]}


def png():
    out = io.BytesIO()
    image = Image.new("RGB", (30, 10), "white")
    image.putpixel((0, 0), (255, 0, 0))
    image.save(out, "PNG")
    return out.getvalue()


def send(client, csrf, names=("scan.png",)):
    response = client.post("/api/upload", headers=csrf,
                           files=[("paper", (name, png(), "image/png")) for name in names],
                           data={"device": "test-scanner", "text": "qr-123"})
    assert response.status_code == 200, response.text
    return [f["id"] for f in response.json()["files"]]


def test_batch_rotation_preserves_original_and_download(manager):
    client, cfg, csrf = manager
    ids = send(client, csrf, ("same.png", "same.png"))
    assert client.post("/api/files/batch", headers=csrf,
                       json={"ids": ids, "action": "right"}).status_code == 200
    rows = client.get("/api/files").json()["items"]
    assert [r["rotation"] for r in rows] == [90, 90]
    restarted = TestClient(create_app(cfg))
    restarted.cookies.update(client.cookies)
    assert {r["rotation"] for r in restarted.get("/api/files").json()["items"]} == {90}
    for file_id in ids:
        assert client.get(f"/api/files/{file_id}/original").content == png()
        rotated = client.get(f"/api/files/{file_id}/rotated")
        image = Image.open(io.BytesIO(rotated.content))
        assert image.size == (10, 30)
        assert image.getpixel((9, 0)) == (255, 0, 0)
        assert client.get(f"/api/files/{file_id}/thumbnail").status_code == 200
        assert client.get(f"/api/files/{file_id}/preview").status_code == 200
    assert client.post("/api/files/batch", headers=csrf,
                       json={"ids": ids, "action": "left"}).status_code == 200
    assert {r["rotation"] for r in client.get("/api/files").json()["items"]} == {0}
    for _ in range(4):
        client.post("/api/files/batch", headers=csrf, json={"ids": ids, "action": "left"})
    assert {r["rotation"] for r in client.get("/api/files").json()["items"]} == {0}


def test_auth_csrf_upload_separation(manager):
    client, cfg, csrf = manager
    attacker = TestClient(create_app(cfg))
    assert attacker.get("/api/files").status_code == 401
    assert attacker.get("/api/files", headers={"X-Upload-Token": cfg.upload_token}).status_code == 401
    assert client.post("/api/files/batch", json={"ids": ["nope"], "action": "left"}).status_code == 403
    assert client.post("/api/upload", files={"paper": ("scan.png", png())}).status_code == 403
    assert attacker.post("/api/upload", files={"paper": ("scan.png", png())}).status_code == 401
    assert attacker.post("/api/upload", data={"token": cfg.upload_token}, files={"paper": ("scan.png", png())}).status_code == 200
    assert client.post("/api/auth/logout", headers=csrf).status_code == 204
    assert client.get("/api/files").status_code == 401


def test_duplicate_zip_delete_restore_restart(manager):
    client, cfg, csrf = manager
    ids = send(client, csrf, ("../../same.png", "same.png"))
    response = client.post("/api/files/zip", headers=csrf, json={"ids": ids, "name": "test"})
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert len(set(archive.namelist())) == 2
        assert all("/" not in name and "\\" not in name for name in archive.namelist())
        assert all(archive.read(name) == png() for name in archive.namelist())
    assert not list((cfg.data_dir / "tmp").glob("*.zip"))
    client.post("/api/files/batch", headers=csrf, json={"ids": ids, "action": "delete"})
    assert client.get("/api/files").json()["total"] == 0
    assert client.get("/api/files?deleted=true").json()["total"] == 2
    restarted = TestClient(create_app(cfg))
    restarted.cookies.update(client.cookies)
    assert restarted.get("/api/files?deleted=true").json()["total"] == 2
    assert restarted.post("/api/files/batch", headers=csrf, json={"ids": ids, "action": "restore"}).status_code == 200
    assert client.get("/api/files?q=qr-123").json()["total"] == 2
    assert client.get("/api/files?q=nomatch").json()["total"] == 0
    assert client.get("/api/files?device=test-scanner&kind=image/png&limit=1").json()["total"] == 2
    assert client.get("/api/integrity").json() == {"missing": [], "orphaned": []}


def test_invalid_oversize_and_atomic_upload(manager):
    client, cfg, csrf = manager
    assert client.post("/api/upload", headers=csrf, files={"paper": ("fake.png", b"not a png")}).status_code == 415
    assert client.post("/api/upload", headers=csrf, files={"paper": ("large.png", b"x" * 9000)}).status_code == 413
    response = client.post("/api/upload", headers=csrf,
                           files=[("paper", ("real.png", png())), ("paper", ("bad.pdf", b"%PDF-broken"))])
    assert response.status_code == 415
    assert client.get("/api/files").json()["total"] == 0
    assert not list((cfg.data_dir / "tmp").iterdir())
    assert not list((cfg.data_dir / "uploads").rglob("*"))


def test_pdf_and_atomic_batch_validation(manager):
    client, cfg, csrf = manager
    ids = send(client, csrf)
    out = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.write(out)
    result = client.post("/api/upload", headers=csrf, files={"paper": ("scan.pdf", out.getvalue())})
    assert result.status_code == 200
    pdf_id = result.json()["files"][0]["id"]
    assert client.get(f"/api/files/{pdf_id}/preview").content == out.getvalue()
    assert client.post("/api/files/batch", headers=csrf,
                       json={"ids": ids + [pdf_id], "action": "right"}).status_code == 422
    assert all(row["rotation"] == 0 for row in client.get("/api/files").json()["items"])
    assert client.post("/api/files/batch", headers=csrf,
                       json={"ids": ids + ["unknown"], "action": "delete"}).status_code == 404
    assert client.get("/api/files").json()["total"] == 2


def test_ingest_commit_failure_cleans_originals(manager, monkeypatch):
    client, cfg, csrf = manager
    store = Store(cfg.data_dir)
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr(Store, "bump", fail)
    result = client.post("/api/upload", headers=csrf, files={"paper": ("scan.png", png())})
    assert result.status_code == 507
    assert client.get("/api/files").json()["total"] == 0
    assert client.get("/api/integrity").json() == {"missing": [], "orphaned": []}
    store.engine.dispose()


def test_login_and_json_limits(manager):
    client, cfg, csrf = manager
    attacker = TestClient(create_app(cfg))
    assert attacker.post("/api/auth/login", json={"username":"admin","password":"bad"}).status_code == 401
    assert attacker.post("/api/auth/login", content=b"x"*4097, headers={"Content-Type":"application/json"}).status_code == 413
    assert attacker.post("/api/files/batch", content=b"x"*131073).status_code == 413


def test_password_hash_is_salted():
    from scanhub.security import verify_password
    first, second = hash_password("test-pass"), hash_password("test-pass")
    assert first != second
    assert verify_password("test-pass", first)
    assert not verify_password("wrong", first)


def test_simulator_image_accepted_by_formal_receiver(manager):
    from scripts.simulate_scanner import PNG
    client, cfg, csrf = manager
    response = client.post("/api/upload", headers=csrf, files={"paper": ("simulated.png", PNG)})
    assert response.status_code == 200
    file_id = response.json()["files"][0]["id"]
    assert client.get(f"/api/files/{file_id}/original").content == PNG


def test_formal_concurrent_uploads_and_duplicates(manager):
    from concurrent.futures import ThreadPoolExecutor
    client, cfg, csrf = manager
    scanner = TestClient(create_app(cfg))
    def send_one(index):
        return scanner.post("/api/upload", headers={"X-Upload-Token": cfg.upload_token},
                            files={"paper": ("same.png", png())})
    with ThreadPoolExecutor(max_workers=2) as pool:
        responses = list(pool.map(send_one, range(6)))
    assert all(r.status_code in (200, 503) for r in responses)
    successful = [r for r in responses if r.status_code == 200]
    assert successful
    assert client.get("/api/files").json()["total"] == len(successful)
    assert sum(bool(r.json()["files"][0]["duplicate_of"]) for r in successful) == len(successful) - 1
    assert client.get("/api/integrity").json() == {"missing": [], "orphaned": []}


def test_formal_interrupted_upload(manager):
    import asyncio
    client, cfg, csrf = manager
    messages = iter([{"type": "http.request", "body": b"--boundary\r\npartial", "more_body": True},
                     {"type": "http.disconnect"}])
    async def receive():
        try:
            return next(messages)
        except StopIteration:
            await asyncio.sleep(0)
            return {"type": "http.disconnect"}
    sent = []
    async def send(msg):
        sent.append(msg)
    asyncio.run(create_app(cfg)({"type": "http", "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1", "method": "POST", "scheme": "http", "path": "/api/upload",
        "raw_path": b"/api/upload", "query_string": b"", "headers": [
            (b"x-upload-token", cfg.upload_token.encode()), (b"content-type", b"multipart/form-data; boundary=boundary")],
        "server": ("test", 80), "client": ("test", 1234)}, receive, send))
    assert sent[0]["status"] == 400
    assert client.get("/api/files").json()["total"] == 0
    assert not list((cfg.data_dir / "tmp").iterdir())
