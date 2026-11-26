import io
import json
import zipfile

import pytest
from fastapi.testclient import TestClient
from PIL import Image
from pypdf import PdfWriter
from sqlalchemy import select

from scanhub.app import Settings, create_app
from tests.helpers import seed
from scanhub.security import hash_password
from scanhub.storage import Store, files


@pytest.fixture
def manager(tmp_path, monkeypatch):
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", hash_password("long-test-password"))
    cfg = Settings(tmp_path, max_bytes=8192)
    client = TestClient(create_app(cfg))
    data=client.post('/api/workspaces').json()
    client.workspace_id=data['id']
    client.headers.update({'X-Workspace-Token':data['token']})
    return client,cfg,{'X-Workspace-Token':data['token']}



def png():
    out = io.BytesIO()
    image = Image.new("RGB", (30, 10), "white")
    image.putpixel((0, 0), (255, 0, 0))
    image.save(out, "PNG")
    return out.getvalue()


def send(client, csrf, names=("scan.png",)):
    return seed(client,[(name,png()) for name in names])



def test_batch_rotation_preserves_original_and_download(manager):
    client, cfg, csrf = manager
    ids = send(client, csrf, ("same.png", "same.png"))
    assert client.post("/api/files/batch", headers=csrf,
                       json={"ids": ids, "action": "right"}).status_code == 200
    rows = client.get("/api/files").json()["items"]
    assert [r["rotation"] for r in rows] == [90, 90]
    restarted = TestClient(create_app(cfg))
    restarted.headers.update(client.headers)
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
    assert client.get(f'/api/files/{ids[0]}/original').status_code==404
    assert client.post('/api/trash/purge',headers=csrf,json={'ids':ids,'confirm':'PERMANENT_DELETE'}).status_code==404
    assert client.get('/api/files').json()['total']==0



def test_pdf_and_atomic_batch_validation(manager):
    client, cfg, csrf = manager
    ids = send(client, csrf)
    out = io.BytesIO()
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=200)
    writer.write(out)
    pdf_id = seed(client,[("scan.pdf",out.getvalue())])[0]
    assert client.get(f"/api/files/{pdf_id}/preview").content == out.getvalue()
    assert client.post("/api/files/batch", headers=csrf,
                       json={"ids": ids + [pdf_id], "action": "right"}).status_code == 422
    assert all(row["rotation"] == 0 for row in client.get("/api/files").json()["items"])
    assert client.post("/api/files/batch", headers=csrf,
                       json={"ids": ids + ["unknown"], "action": "delete"}).status_code == 404
    assert client.get("/api/files").json()["total"] == 2
