import json

from fastapi.testclient import TestClient
from scanhub.minimal import create_app


def test_minimal_accepts_arbitrary_files_without_auth(tmp_path):
    client = TestClient(create_app(tmp_path))
    response = client.post("/api/upload", files=[("paper", ("../../a.jpg", b"one")),
                                               ("paper", ("a.jpg", b"two"))], data={"text": "test"})
    assert response.status_code == 200 and response.text == "OK"
    request = json.loads(next(tmp_path.glob("*/request.json")).read_text(encoding="utf-8"))
    assert request["method"] == "POST" and len(request["files"]) == 2
    assert {p.read_bytes() for p in tmp_path.glob("*/file-*.bin")} == {b"one", b"two"}
    assert next(tmp_path.glob("*/body.bin")).is_file()
    assert not (tmp_path / "scanhub.db").exists()


def test_minimal_preserves_unknown_and_malformed_requests(tmp_path):
    client = TestClient(create_app(tmp_path))
    assert client.put("/any/path", content=b"raw bytes").status_code == 200
    response = client.post("/api/upload", content=b"invalid form", headers={"Content-Type": "multipart/form-data"})
    assert response.status_code == 200
    records = [json.loads(p.read_text(encoding="utf-8")) for p in tmp_path.glob("*/request.json")]
    assert any(r.get("parse_error") for r in records)
    assert {p.read_bytes() for p in tmp_path.glob("*/body.bin")} == {b"raw bytes", b"invalid form"}


def test_minimal_health_and_response_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MINIMAL_RESPONSE_BODY", '{"success":true}')
    monkeypatch.setenv("MINIMAL_RESPONSE_TYPE", "application/json")
    client = TestClient(create_app(tmp_path))
    assert client.get("/api/ready").json()["mode"] == "minimal-no-auth"
    assert client.post("/api/upload", content=b"data").json() == {"success": True}
