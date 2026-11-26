"""Bounded diagnostic receiver. No assumptions about the scanner wire protocol."""
import hashlib
import hmac
import json
import logging
import os
import shutil
import time
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import Response
from starlette.datastructures import UploadFile
from starlette.requests import ClientDisconnect

log = logging.getLogger("scanhub")


@dataclass
class Settings:
    data_dir: Path
    enabled: bool = False
    upload_token: str = ""
    admin_token: str = ""
    max_bytes: int = 50 * 1024 * 1024
    concurrency: int = 2
    response_status: int = 200
    response_body: str = '{"success":true}'
    response_type: str = "application/json"

    @classmethod
    def env(cls):
        return cls(Path(os.getenv("DATA_DIR", "data")),
                   os.getenv("DEBUG_ENABLED", "false").lower() == "true",
                   os.getenv("UPLOAD_TOKEN", ""), os.getenv("ADMIN_TOKEN", ""),
                   int(os.getenv("MAX_UPLOAD_MB", "50")) * 1024 * 1024,
                   int(os.getenv("MAX_CONCURRENT_UPLOADS", "2")),
                   int(os.getenv("DEBUG_RESPONSE_STATUS", "200")),
                   os.getenv("DEBUG_RESPONSE_BODY", '{"success":true}'),
                   os.getenv("DEBUG_RESPONSE_CONTENT_TYPE", "application/json"))


def secret_key(key):
    return any(word in key.lower() for word in
               ("token", "secret", "password", "authorization", "cookie", "key", "credential"))


def safe_pairs(pairs):
    # Unknown metadata can contain personal information too: keep names and lengths only.
    return [{"name": k[:200], "value": "[REDACTED]" if secret_key(k)
             else {"length": len(v)}} for k, v in pairs]


def authorized(value, expected):
    from .security import check_token
    return check_token(value, expected)


def create_app(settings=None):
    cfg = settings or Settings.env()
    if cfg.max_bytes < 1 or cfg.concurrency < 1 or not 200 <= cfg.response_status <= 599:
        raise ValueError("Invalid receiver limits or response status")
    if cfg.enabled and (not cfg.upload_token or not cfg.admin_token or cfg.upload_token == cfg.admin_token):
        raise ValueError("Debug mode requires distinct UPLOAD_TOKEN and ADMIN_TOKEN")
    app = FastAPI(title="ScanHub", docs_url=None, redoc_url=None, openapi_url=None)
    active = 0
    previous = None

    @app.get("/api/health")
    async def health():
        from . import __version__
        return {"status": "ok", "version": __version__, "phase": 2, "debug_enabled": cfg.enabled,
                "instance": "acceptance" if os.getenv("SCANHUB_INSTANCE") == "acceptance" else "production"}

    def admin(request):
        if not authorized(request.headers.get("authorization", "").removeprefix("Bearer "), cfg.admin_token):
            raise HTTPException(401, "Management authentication required")

    @app.get("/api/debug/requests")
    async def records(request: Request, limit: int = 20):
        admin(request)
        # Manifest names are sortable UTC timestamps. Bounded response size.
        import heapq
        paths = heapq.nlargest(min(max(limit, 1), 100), (cfg.data_dir / "debug").glob("*/manifest.json"))
        return [json.loads(p.read_text(encoding="utf-8")) for p in paths]

    @app.api_route("/api/debug/upload", methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"])
    async def receive(request: Request):
        nonlocal active, previous
        if not cfg.enabled:
            raise HTTPException(404, "Debug receiver disabled")
        supplied_token = request.headers.get("x-upload-token") or request.query_params.get("token")
        if supplied_token and not authorized(supplied_token, cfg.upload_token):
            raise HTTPException(401, "Invalid upload token")
        if active >= cfg.concurrency:
            raise HTTPException(503, "Receiver busy; retry later")
        active += 1
        stage = None
        form = None
        try:
            now = datetime.now(timezone.utc)
            stamp = time.monotonic()
            interval = None if previous is None else round(stamp - previous, 3)
            previous = stamp
            rid = now.strftime("%Y%m%dT%H%M%S%fZ_") + uuid.uuid4().hex
            stage = cfg.data_dir / "tmp" / rid
            stage.mkdir(parents=True)
            body = stage / "body.bin"
            size = 0
            with body.open("wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > cfg.max_bytes:
                        raise HTTPException(413, "Request exceeds upload limit")
                    out.write(chunk)
            fields, files = [], []
            content_type = request.headers.get("content-type", "")
            if content_type.lower().startswith(("multipart/form-data", "application/x-www-form-urlencoded")):
                with body.open("rb") as source:
                    async def replay():
                        chunk = source.read(65536)
                        return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}
                    parsed = Request(request.scope, receive=replay)
                    try:
                        form = await parsed.form(max_files=100, max_fields=100, max_part_size=min(cfg.max_bytes, 1024 * 1024))
                    except Exception:
                        raise HTTPException(400, "Malformed or excessive form data") from None
                fields = [(k, v) for k, v in form.multi_items() if isinstance(v, str)]
            token = (request.headers.get("x-upload-token") or request.query_params.get("token")
                     or next((v for k, v in fields if k == "token"), ""))
            if not authorized(token, cfg.upload_token):
                raise HTTPException(401, "Invalid upload token")
            if form:
                for key, value in form.multi_items():
                    if not isinstance(value, UploadFile):
                        continue
                    filename = f"file-{len(files):03d}.bin"
                    digest = hashlib.sha256()
                    file_size = 0
                    with (stage / filename).open("wb") as out:
                        while chunk := await value.read(65536):
                            out.write(chunk)
                            digest.update(chunk)
                            file_size += len(chunk)
                    files.append({"field": key[:200], "original_name": (value.filename or "")[:500],
                                  "stored_name": filename, "size": file_size, "sha256": digest.hexdigest()})
            # Raw unknown protocols are useful for analysis. Parsed forms discard raw bodies
            # because the body may contain authentication secrets.
            raw_saved = form is None
            if not raw_saved:
                body.unlink()
            else:
                # Unknown bodies may contain secrets; do not expose through the API.
                pass
            manifest = {"id": rid, "received_at": now.isoformat(), "interval_seconds": interval,
                        "method": request.method, "content_type": content_type[:300],
                        "headers": safe_pairs(request.headers.items()),
                        "query": safe_pairs(request.query_params.multi_items()),
                        "fields": safe_pairs(fields), "files": files,
                        "request_bytes": size, "raw_body_saved": raw_saved,
                        "response_status": cfg.response_status}
            (stage / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
            target = cfg.data_dir / "debug" / rid
            target.parent.mkdir(parents=True, exist_ok=True)
            stage.replace(target)
            log.info("debug upload accepted id=%s bytes=%d files=%d", rid, size, len(files))
            return Response(cfg.response_body, status_code=cfg.response_status, media_type=cfg.response_type,
                            headers={"X-ScanHub-Request-ID": rid, "Cache-Control": "no-store"})
        except ClientDisconnect:
            log.warning("debug upload interrupted")
            raise HTTPException(400, "Upload interrupted") from None
        except HTTPException as exc:
            log.warning("debug upload rejected status=%d", exc.status_code)
            raise
        except OSError:
            log.error("debug storage operation failed")
            raise HTTPException(507, "Storage unavailable") from None
        finally:
            if form is not None:
                await form.close()
            if stage is not None and stage.exists():
                shutil.rmtree(stage, ignore_errors=True)
            active -= 1

    from .manager import attach_management
    attach_management(app, cfg)
    return app


app = create_app()
