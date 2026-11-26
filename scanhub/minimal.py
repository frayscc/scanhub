"""Standalone no-auth scanner probe. Run explicitly; never mounted by the main app."""
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import uuid
from datetime import datetime, timezone

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile

log = logging.getLogger("uvicorn.error")


def create_app(data_dir=None):
    root = Path(data_dir or os.getenv("MINIMAL_DIR", "/data/minimal"))
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/")
    @app.get("/api/health")
    @app.get("/api/ready")
    async def health():
        return {"status": "ok", "mode": "minimal-no-auth"}

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def receive(request: Request, path: str):
        root.mkdir(parents=True, exist_ok=True)
        stage = Path(tempfile.mkdtemp(prefix=".receiving-", dir=root))
        form = None
        try:
            raw = stage / "body.bin"
            size = 0
            with raw.open("wb") as output:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > 50 * 1024 * 1024:
                        raise HTTPException(413, "测试请求超过 50 MiB")
                    output.write(chunk)
            content_type = request.headers.get("content-type", "")
            metadata = {"received_at": datetime.now(timezone.utc).isoformat(), "method": request.method,
                "path": request.url.path, "content_type": content_type, "bytes": size,
                "query_fields": list(request.query_params.keys()), "fields": [], "files": []}
            if content_type.lower().startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
                with raw.open("rb") as source:
                    async def replay():
                        chunk = source.read(65536)
                        return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}
                    try:
                        form = await Request(request.scope, receive=replay).form(
                            max_files=100, max_fields=100, max_part_size=1024 * 1024)
                    except Exception:
                        metadata["parse_error"] = "无法解析表单，已保留原始请求体"
                if form is not None:
                    for key, value in form.multi_items():
                        if isinstance(value, UploadFile):
                            name = f"file-{len(metadata['files']):03d}.bin"
                            file_size = 0
                            with (stage / name).open("wb") as output:
                                while chunk := await value.read(65536):
                                    output.write(chunk)
                                    file_size += len(chunk)
                            metadata["files"].append({"field": key, "original_name": value.filename,
                                                       "stored_name": name, "bytes": file_size})
                        else:
                            metadata["fields"].append({"name": key, "length": len(value)})
            (stage / "request.json").write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
            request_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S_") + uuid.uuid4().hex[:8]
            stage.rename(root / request_id)
            log.info("MINIMAL id=%s method=%s content_type=%r bytes=%d file_fields=%s fields=%s parse_error=%s",
                request_id, request.method, content_type, size,
                [item["field"] for item in metadata["files"]],
                [item["name"] for item in metadata["fields"]], metadata.get("parse_error", "none"))
            return Response(os.getenv("MINIMAL_RESPONSE_BODY", "OK"),
                status_code=int(os.getenv("MINIMAL_RESPONSE_STATUS", "200")),
                media_type=os.getenv("MINIMAL_RESPONSE_TYPE", "text/plain"))
        finally:
            if form is not None:
                await form.close()
            if stage.exists():
                shutil.rmtree(stage)

    return app


app = create_app()
