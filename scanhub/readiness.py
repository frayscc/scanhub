"""Small readiness probe; deliberately does not hash originals or expose system details."""
import os
import shutil
import tempfile
from pathlib import Path

from sqlalchemy import text


def check_ready(store):
    reserve = int(os.getenv("MIN_FREE_MB", "64"))
    if reserve < 0:
        raise ValueError("MIN_FREE_MB must be nonnegative")
    with store.engine.connect() as db:
        if db.execute(text("SELECT revision FROM state WHERE id=1")).scalar_one_or_none() is None:
            return False
    handle, name = tempfile.mkstemp(prefix=".ready-", dir=store.root / "tmp")
    try:
        os.write(handle, b"ok")
    finally:
        os.close(handle)
        Path(name).unlink(missing_ok=True)
    return shutil.disk_usage(store.root).free >= reserve * 1024 * 1024
