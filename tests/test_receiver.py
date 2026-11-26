import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient

from scanhub.app import Settings, create_app


@pytest.fixture
def receiver(tmp_path):
    cfg = Settings(tmp_path, True, "test-upload-only", "test-admin-only", max_bytes=8192)
    return TestClient(create_app(cfg)), cfg


def manifests(cfg):
    return [json.loads(p.read_text(encoding="utf-8")) for p in cfg.data_dir.glob("debug/*/manifest.json")]


def test_multi_same_name_duplicate_and_traversal(receiver):
    client, cfg = receiver
    for _ in range(2):
        response = client.post("/api/debug/upload", data={"token": cfg.upload_token, "text": "private QR"},
                               files=[("paper", ("../../same.jpg", b"original1")),
                                      ("paper", ("../../same.jpg", b"original2"))])
        assert response.status_code == 200
    rows = manifests(cfg)
    assert len(rows) == 2
    assert len(rows[0]["files"]) == 2
    assert rows[0]["files"][0]["original_name"] == "../../same.jpg"
    assert "private QR" not in json.dumps(rows)
    assert cfg.upload_token not in json.dumps(rows)
    stored = list(cfg.data_dir.glob("debug/*/file-*.bin"))
    assert len(stored) == 4
    assert {p.read_bytes() for p in stored} == {b"original1", b"original2"}
    assert not list(cfg.data_dir.glob("debug/*/body.bin"))


@pytest.mark.parametrize("method", ["get", "post", "put", "patch", "delete", "options", "head"])
def test_raw_methods(receiver, method):
    client, cfg = receiver
    response = client.request(method, "/api/debug/upload?token=" + cfg.upload_token,
                              content=b"raw bytes", headers={"content-type": "application/octet-stream"})
    assert response.status_code == 200
    assert manifests(cfg)[0]["method"] == method.upper()
    assert next(cfg.data_dir.glob("debug/*/body.bin")).read_bytes() == b"raw bytes"


def test_limits_auth_and_disabled(receiver, tmp_path):
    client, cfg = receiver
    assert client.post("/api/debug/upload", content=b"bad").status_code == 401
    assert client.post("/api/debug/upload", content=b"x" * 8193).status_code == 413
    assert client.get("/api/debug/requests").status_code == 401
    assert client.get("/api/debug/requests", headers={"Authorization": "Bearer " + cfg.upload_token}).status_code == 401
    assert not manifests(cfg)
    assert not list((tmp_path / "tmp").iterdir())
    disabled = TestClient(create_app(Settings(tmp_path)))
    assert disabled.post("/api/debug/upload").status_code == 404
    assert disabled.get("/api/health").json()["status"] == "ok"


def test_custom_response_and_persistence(receiver):
    client, cfg = receiver
    cfg.response_status, cfg.response_body, cfg.response_type = 201, "OK", "text/plain"
    response = client.post("/api/debug/upload", files={"paper": ("test.exe", b"untrusted")},
                           headers={"X-Upload-Token": cfg.upload_token})
    # Phase 0 deliberately captures arbitrary protocol evidence; nothing is executed or served.
    assert response.status_code == 201 and response.text == "OK"
    restarted = TestClient(create_app(cfg))
    rows = restarted.get("/api/debug/requests", headers={"Authorization": "Bearer " + cfg.admin_token}).json()
    assert len(rows) == 1 and rows[0]["files"][0]["size"] == 9


def test_storage_failure(receiver, monkeypatch):
    from pathlib import Path
    client, cfg = receiver
    def fail(*args, **kwargs):
        raise OSError("disk full")
    monkeypatch.setattr(Path, "replace", fail)
    assert client.post("/api/debug/upload", content=b"sample",
                       headers={"X-Upload-Token": cfg.upload_token}).status_code == 507
    assert not manifests(cfg)
    assert not list((cfg.data_dir / "tmp").iterdir())


def test_malformed_form(receiver):
    client, cfg = receiver
    result = client.post("/api/debug/upload", content=b"invalid", headers={
        "X-Upload-Token": cfg.upload_token, "Content-Type": "multipart/form-data"})
    assert result.status_code == 400
    assert not manifests(cfg)


def test_concurrent(receiver):
    client, cfg = receiver
    def send(i):
        return client.post("/api/debug/upload", content=str(i).encode(),
                           headers={"X-Upload-Token": cfg.upload_token}).status_code
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(send, range(8)))
    assert all(status in (200, 503) for status in results)
    assert len(manifests(cfg)) == results.count(200)


def test_interruption(receiver):
    import asyncio
    client, cfg = receiver
    app = create_app(cfg)
    messages = iter([{"type": "http.request", "body": b"partial", "more_body": True},
                     {"type": "http.disconnect"}])
    async def receive():
        return next(messages)
    sent = []
    async def send(msg):
        sent.append(msg)
    asyncio.run(app({"type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
                     "method": "POST", "scheme": "http", "path": "/api/debug/upload",
                     "raw_path": b"/api/debug/upload", "query_string": b"", "headers": [],
                     "server": ("test", 80), "client": ("test", 1234)}, receive, send))
    assert sent[0]["status"] == 400
    assert not manifests(cfg)
    assert not list((cfg.data_dir / "tmp").iterdir())
