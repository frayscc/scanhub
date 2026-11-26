"""ScanHub web management; scanner ingress is FTP only."""
import os
from dataclasses import dataclass
from pathlib import Path
from fastapi import FastAPI

@dataclass
class Settings:
    data_dir: Path
    max_bytes: int = 50 * 1024 * 1024
    concurrency: int = 2

    @classmethod
    def env(cls):
        return cls(Path(os.getenv("DATA_DIR", "data")),
                   int(os.getenv("MAX_FILE_MB",os.getenv("MAX_UPLOAD_MB","50"))) * 1024 * 1024,
                   int(os.getenv("MAX_CONCURRENT_UPLOADS", "2")))

def create_app(settings=None):
    cfg=settings or Settings.env()
    if cfg.max_bytes<1:raise ValueError("Invalid file limit")
    app=FastAPI(title="ScanHub",docs_url=None,redoc_url=None,openapi_url=None)
    @app.get("/api/health")
    async def health():
        from . import __version__
        return {"status":"ok","version":__version__,"ingress":"ftp",
                "instance":"acceptance" if os.getenv("SCANHUB_INSTANCE")=="acceptance" else "production"}
    from .workspace_api import attach_workspace
    attach_workspace(app,cfg)
    return app

app=create_app()
