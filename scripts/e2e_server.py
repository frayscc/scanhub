"""Isolated browser-test service; test credentials never affect the real data directory."""
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if __name__ == "__main__":
    import uvicorn
    with tempfile.TemporaryDirectory(prefix="scanhub-browser-test-") as data:
        os.environ.update(DATA_DIR=data, SCANHUB_INSTANCE="acceptance",
            FTP_USERNAME="scanner", FTP_PASSWORD="isolated-browser-ftp-password", FTP_PORT="18768", FTP_PASSIVE_MIN="18769", FTP_PASSIVE_MAX="18779", FTP_PUBLIC_HOST="127.0.0.1",
            STATIC_DIR=str(Path(__file__).resolve().parents[1] / "frontend" / "dist"))
        uvicorn.run("scanhub.app:app", host="127.0.0.1", port=18767, access_log=False)
