"""Isolated browser-test service; test credentials never affect the real data directory."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scanhub.security import hash_password

if __name__ == "__main__":
    import uvicorn
    with tempfile.TemporaryDirectory(prefix="scanhub-browser-test-") as data:
        os.environ.update(DATA_DIR=data, DEBUG_ENABLED="false", ADMIN_USERNAME="admin",
            ADMIN_PASSWORD_HASH=hash_password("browser-test-password"),
            UPLOAD_TOKEN="isolated-browser-test-upload-token", ADMIN_TOKEN="isolated-browser-admin-token",
            STATIC_DIR=str(Path(__file__).resolve().parents[1] / "frontend" / "dist"))
        uvicorn.run("scanhub.app:app", host="127.0.0.1", port=18767, access_log=False)
