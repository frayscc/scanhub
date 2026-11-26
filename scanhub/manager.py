"""Authenticated management APIs and bounded multipart adapter."""
import asyncio
import io
import json
import logging
import os
import secrets
import shutil
import tempfile
import time
import threading
import zipfile
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image, ImageOps
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask
from starlette.datastructures import UploadFile
from starlette.requests import ClientDisconnect
from starlette.concurrency import run_in_threadpool

from .security import check_token, token_digest, verify_password
from .work import BoundedWorker
from .media import preview_bytes, thumbnail_bytes, rotated_png, write_zip
from .storage import Store, batches, clean_name, files, inspect_file, sessions, upload_logs

log = logging.getLogger("scanhub")


class Login(BaseModel):
    username: str = Field(max_length=100)
    password: str = Field(max_length=1024)


class Batch(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)
    action: Literal["left", "right", "delete", "restore"]


class Archive(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=10000)
    name: str = Field(default="扫描文件.zip", max_length=180)


def attach_management(app, cfg):
    store = None
    work = BoundedWorker()
    app.state.work = work
    store_lock = threading.Lock()
    active = 0
    attempts = defaultdict(deque)

    @app.exception_handler(SQLAlchemyError)
    async def database_error(request, exc):
        log.error("database operation failed")
        return Response('{"detail":"数据库暂不可用，请检查磁盘空间和权限"}', status_code=503, media_type="application/json")

    def storage():
        nonlocal store
        with store_lock:
            if store is None:
                store = Store(cfg.data_dir)
                app.state.store = store
                saved = store.options()
                cfg.upload_token = saved.get("upload_token_hash", cfg.upload_token)
                cfg.max_bytes = saved.get("max_upload_mb", cfg.max_bytes / 1048576) * 1048576
                cfg.enabled = saved.get("debug_enabled", cfg.enabled)
        return store

    def auth(request, mutate=False):
        digest = token_digest(request.cookies.get("scanhub_session", ""))
        dbstore = storage()
        with dbstore.engine.connect() as db:
            row = db.execute(select(sessions).where(sessions.c.digest == digest,
                             sessions.c.expires > time.time())).mappings().first()
        if not row:
            raise HTTPException(401, "请先登录")
        if mutate and not secrets.compare_digest(request.headers.get("x-csrf-token", "").encode(), row["csrf"].encode()):
            raise HTTPException(403, "CSRF 校验失败，请重新登录")
        return row

    def get_file(request, file_id):
        auth(request)
        row = storage().get(file_id)
        if not row:
            raise HTTPException(404, "文件不存在")
        if not (storage().root / row["path"]).is_file():
            raise HTTPException(410, "原始文件缺失，请检查存储或备份")
        return row

    @app.middleware("http")
    async def security_headers(request, call_next):
        if request.url.path.startswith("/api/") and request.url.path not in ("/api/health", "/api/ready"):
            try:
                storage()
            except (OSError, SQLAlchemyError):
                return Response('{"detail":"存储暂不可用"}', status_code=503, media_type="application/json")
        # Small JSON endpoints are bounded before FastAPI/Pydantic decoding.
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.url.path not in ("/api/upload", "/api/debug/upload"):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 131072:
                    return Response('请求过大', status_code=413)
            request._body = bytes(body)
        if request.url.path == "/api/auth/login" and request.method == "POST" and not request.headers.get("content-type", "").startswith("application/json"):
            return Response('登录需要 JSON 请求', status_code=415)
        response = await call_next(request)
        if request.url.path == "/api/upload":
            # Never persist headers, token, filenames or QR contents in operational logs.
            try:
                with storage().engine.begin() as db:
                    db.execute(upload_logs.insert().values(id=uuid.uuid4().hex, received_at=time.time(),
                        status=response.status_code, bytes=getattr(request.state, "upload_bytes", 0),
                        file_count=getattr(request.state, "upload_count", 0),
                        device=getattr(request.state, "upload_device", "")))
                    old_ids = select(upload_logs.c.id).order_by(upload_logs.c.received_at.desc()).offset(1000)
                    db.execute(delete(upload_logs).where(upload_logs.c.id.in_(old_ids)))
            except Exception:
                log.error("upload log persistence failed")
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Frame-Options"] = "SAMEORIGIN"
        response.headers["Content-Security-Policy"] = "default-src 'self'; img-src 'self' blob: data:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
        if request.url.path.startswith("/api/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/api/ready")
    async def ready():
        from .readiness import check_ready
        try:
            available = await run_in_threadpool(check_ready, storage())
        except (OSError, SQLAlchemyError, ValueError):
            available = False
        return Response('{"status":"ready"}' if available else '{"status":"unavailable"}',
                        status_code=200 if available else 503, media_type="application/json",
                        headers={"Cache-Control": "no-store"})

    @app.post("/api/auth/login")
    async def login(request: Request):
        # Bound JSON before decoding, including chunked clients.
        body = bytearray()
        async for chunk in request.stream():
            body.extend(chunk)
            if len(body) > 4096:
                raise HTTPException(413, "登录请求过大")
        try:
            credentials = Login.model_validate_json(body)
        except ValueError:
            raise HTTPException(422, "登录信息格式错误") from None
        password_hash = os.getenv("ADMIN_PASSWORD_HASH", "")
        if not password_hash:
            raise HTTPException(503, "请先在服务器配置 ADMIN_PASSWORD_HASH")
        key = request.client.host if request.client else "unknown"
        # Keep per-source state bounded. Limit all login attempts, including successes.
        now = time.time()
        if len(attempts) > 1000:
            attempts.clear()
        history = attempts[key]
        while history and history[0] < now - 60:
            history.popleft()
        if len(history) >= 10:
            raise HTTPException(429, "登录尝试过多，请稍后重试")
        history.append(now)
        valid_password = await work.run(verify_password, credentials.password, password_hash)
        if credentials.username != os.getenv("ADMIN_USERNAME", "admin") or not valid_password:
            raise HTTPException(401, "用户名或密码错误")
        token, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with storage().engine.begin() as db:
            db.execute(delete(sessions).where(sessions.c.expires < now))
            db.execute(sessions.insert().values(digest=token_digest(token), csrf=csrf, expires=now + 12 * 3600))
        response = Response(json.dumps({"csrf": csrf}), media_type="application/json")
        response.set_cookie("scanhub_session", token, httponly=True, samesite="strict", max_age=43200,
                            secure=os.getenv("COOKIE_SECURE", "false").lower() == "true")
        return response

    @app.get("/api/auth/session")
    async def session(request: Request):
        row = auth(request)
        return {"csrf": row["csrf"], "username": os.getenv("ADMIN_USERNAME", "admin")}

    @app.post("/api/auth/logout")
    async def logout(request: Request):
        row = auth(request, True)
        with storage().engine.begin() as db:
            db.execute(delete(sessions).where(sessions.c.digest == row["digest"]))
        response = Response(status_code=204)
        response.delete_cookie("scanhub_session")
        return response

    @app.post("/api/upload")
    async def upload(request: Request):
        nonlocal active
        if active >= cfg.concurrency:
            raise HTTPException(503, "接收繁忙，请稍后重试")
        header_token = request.headers.get("x-upload-token") or request.query_params.get("token")
        web_upload = bool(request.cookies.get("scanhub_session"))
        if web_upload:
            auth(request, True)
        elif header_token and not check_token(header_token, cfg.upload_token):
            raise HTTPException(401, "上传令牌无效")
        if not request.headers.get("content-type", "").lower().startswith("multipart/form-data"):
            raise HTTPException(415, "正式接口当前仅接受 multipart/form-data")
        active += 1
        stage, form = None, None
        try:
            root = storage().root / "tmp"
            stage = Path(tempfile.mkdtemp(prefix="upload-", dir=root))
            raw = stage / "request.bin"
            size = 0
            with raw.open("wb") as out:
                async for chunk in request.stream():
                    size += len(chunk)
                    request.state.upload_bytes = size
                    if size > cfg.max_bytes:
                        raise HTTPException(413, "请求超过上传大小限制")
                    out.write(chunk)
            with raw.open("rb") as source:
                async def replay():
                    chunk = source.read(65536)
                    return {"type": "http.request", "body": chunk, "more_body": bool(chunk)}
                parsed = Request(request.scope, receive=replay)
                try:
                    form = await parsed.form(max_files=100, max_fields=100, max_part_size=65536)
                except Exception:
                    raise HTTPException(400, "表单格式错误或字段过多") from None
            token = header_token or str(form.get("token", ""))
            if not web_upload and not check_token(token, cfg.upload_token):
                raise HTTPException(401, "上传令牌无效")
            prepared = []
            for key, item in form.multi_items():
                if not isinstance(item, UploadFile):
                    continue
                target = stage / f"file-{len(prepared)}"
                with target.open("wb") as out:
                    while chunk := await item.read(65536):
                        out.write(chunk)
                    out.flush()
                    os.fsync(out.fileno())
                try:
                    mime, width, height = await work.run(inspect_file, target)
                except ValueError as exc:
                    raise HTTPException(415, str(exc)) from None
                prepared.append(dict(temp=target, name=item.filename, mime=mime, width=width, height=height))
            if not prepared:
                raise HTTPException(400, "请求未包含文件")
            fields = {key[:100]: value for key, value in form.multi_items()
                      if isinstance(value, str) and key not in ("token", "text", "device")
                      and not any(secret in key.lower() for secret in ("secret", "password", "token", "key"))}
            result = await work.run(storage().ingest, prepared, str(form.get("device", "未知设备"))[:200],
                str(form.get("text", ""))[:65536], json.dumps(fields, ensure_ascii=False),
                external_batch=str(form.get("batch_id", ""))[:200] or None,
                batch_interval=storage().options().get("batch_interval", 30))
            request.state.upload_count = len(result)
            request.state.upload_device = str(form.get("device", "未知设备"))[:200]
            return {"success": True, "files": result}
        except ClientDisconnect:
            raise HTTPException(400, "上传中断") from None
        except OSError:
            log.error("formal upload storage failure")
            raise HTTPException(507, "存储失败，请检查磁盘空间和权限") from None
        finally:
            if form is not None:
                await form.close()
            if stage:
                shutil.rmtree(stage, ignore_errors=True)
            active -= 1

    @app.get("/api/files")
    async def list_files(request: Request, q: str = "", device: str = "", kind: str = "",
                         deleted: bool = False, sort: str = "newest", offset: int = 0, limit: int = 100,
                         after: float = 0, before: float = 0, batch: str = ""):
        auth(request)
        condition = [files.c.deleted_at.is_not(None) if deleted else files.c.deleted_at.is_(None)]
        if q:
            condition.append(or_(files.c.name.contains(q[:200], autoescape=True), files.c.qr.contains(q[:200], autoescape=True)))
        if device:
            condition.append(files.c.device == device)
        if kind:
            condition.append(files.c.mime == kind)
        if batch:
            condition.append(files.c.batch_id.is_(None) if batch == "unassigned" else files.c.batch_id == batch)
        if after:
            condition.append(files.c.uploaded_at >= after)
        if before:
            condition.append(files.c.uploaded_at < before)
        order = {"newest": files.c.uploaded_at.desc(), "oldest": files.c.uploaded_at.asc(),
                 "name": files.c.name.asc(), "size": files.c.size.desc()}.get(sort, files.c.uploaded_at.desc())
        with storage().engine.connect() as db:
            total = db.execute(select(func.count()).select_from(files).where(*condition)).scalar_one()
            rows = db.execute(select(files).where(*condition).order_by(order, files.c.id)
                              .offset(max(0, offset)).limit(min(max(limit, 1), 200))).mappings().all()
        return {"items": [{k: v for k, v in row.items() if k not in ("path", "extra")} for row in rows], "total": total}

    @app.get("/api/storage")
    async def storage_info(request: Request):
        auth(request)
        with storage().engine.connect() as db:
            count, size = db.execute(select(func.count(), func.coalesce(func.sum(files.c.size), 0)).select_from(files)).one()
            trash = db.execute(select(func.coalesce(func.sum(files.c.size), 0)).where(files.c.deleted_at.is_not(None))).scalar_one()
            devices = list(db.execute(select(files.c.device).distinct()).scalars())
        return {"count": count, "bytes": size, "trash_bytes": trash,
                "free_bytes": shutil.disk_usage(storage().root).free, "devices": devices,
                "device_profiles": storage().options().get("device_profiles", [])}

    @app.post("/api/files/batch")
    async def batch(request: Request, payload: Batch):
        auth(request, True)
        ids = set(payload.ids)
        with storage().lock, storage().engine.begin() as db:
            rows = db.execute(select(files).where(files.c.id.in_(ids))).mappings().all()
            if len(rows) != len(ids):
                raise HTTPException(404, "部分文件不存在；未应用操作")
            if payload.action in ("left", "right"):
                if any(not row["mime"].startswith("image/") or row["deleted_at"] for row in rows):
                    raise HTTPException(422, "仅可旋转收件箱中的图片，请排除 PDF 和回收站文件")
                delta = 270 if payload.action == "left" else 90
                db.execute(update(files).where(files.c.id.in_(ids)).values(rotation=(files.c.rotation + delta) % 360))
            else:
                db.execute(update(files).where(files.c.id.in_(ids)).values(
                    deleted_at=time.time() if payload.action == "delete" else None))
            storage().bump(db)
        return {"updated": len(ids)}

    @app.get("/api/files/{file_id}/original")
    async def original(request: Request, file_id: str):
        row = get_file(request, file_id)
        return FileResponse(storage().root / row["path"], media_type=row["mime"], filename=row["name"])

    @app.get("/api/files/{file_id}/preview")
    async def preview(request: Request, file_id: str):
        row = get_file(request, file_id)
        if row["mime"] == "application/pdf":
            return FileResponse(storage().root / row["path"], media_type="application/pdf",
                                headers={"Content-Security-Policy": "sandbox"})
        try:
            data = await work.run(preview_bytes, storage().root, row)
            return Response(data, media_type="image/jpeg")
        except (OSError, ValueError):
            raise HTTPException(422, "无法生成图片预览；原始文件仍可下载") from None

    @app.get("/api/files/{file_id}/thumbnail")
    async def thumbnail(request: Request, file_id: str):
        row = get_file(request, file_id)
        if row["mime"] == "application/pdf":
            raise HTTPException(404, "PDF 使用类型图标")
        try:
            data = await work.run(thumbnail_bytes, storage(), row)
            return Response(data, media_type="image/jpeg")
        except (OSError, ValueError):
            raise HTTPException(422, "无法生成缩略图") from None

    @app.get("/api/files/{file_id}/rotated")
    async def rotated_download(request: Request, file_id: str):
        row = get_file(request, file_id)
        if row["mime"] not in ("image/jpeg", "image/png"):
            raise HTTPException(422, "修正图片下载目前支持 JPG/PNG；TIFF 请下载原件")
        target = None
        try:
            handle, target = tempfile.mkstemp(suffix=".png", dir=storage().root / "tmp")
            os.close(handle)
            await work.run(rotated_png, storage().root, row, target)
            return FileResponse(target, media_type="image/png", filename=Path(row["name"]).stem + "-方向修正.png",
                                background=BackgroundTask(Path(target).unlink, missing_ok=True))
        except HTTPException:
            if target:
                Path(target).unlink(missing_ok=True)
            raise
        except OSError:
            if target:
                Path(target).unlink(missing_ok=True)
            raise HTTPException(507, "生成修正图片失败") from None

    @app.post("/api/files/zip")
    async def archive(request: Request, payload: Archive):
        auth(request, True)
        target = None
        try:
            with storage().engine.connect() as db:
                rows = db.execute(select(files).where(files.c.id.in_(set(payload.ids)))).mappings().all()
            if len(rows) != len(set(payload.ids)):
                raise HTTPException(404, "部分文件不存在")
            handle, target = tempfile.mkstemp(suffix=".zip", dir=storage().root / "tmp")
            os.close(handle)
            await work.run(write_zip, storage().root, rows, target)
            return FileResponse(target, media_type="application/zip", filename=clean_name(payload.name) +
                                ("" if payload.name.lower().endswith(".zip") else ".zip"),
                                background=BackgroundTask(Path(target).unlink, missing_ok=True))
        except HTTPException:
            if target:
                Path(target).unlink(missing_ok=True)
            raise
        except OSError:
            if target:
                Path(target).unlink(missing_ok=True)
            raise HTTPException(507, "打包失败，请检查磁盘空间") from None

    @app.get("/api/events")
    async def events(request: Request):
        auth(request)
        async def generate():
            previous = -1
            while not await request.is_disconnected():
                try:
                    auth(request)
                except HTTPException:
                    yield "event: expired\ndata: {}\n\n"
                    return
                revision = storage().revision()
                if revision != previous:
                    yield f"data: {revision}\n\n"
                    previous = revision
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(2)
        return StreamingResponse(generate(), media_type="text/event-stream",
                                 headers={"X-Accel-Buffering": "no"})

    @app.get("/api/integrity")
    async def integrity(request: Request):
        auth(request)
        return storage().reconcile()

    from .phase2 import attach_phase2
    attach_phase2(app, cfg, storage, auth)

    @app.post("/api/batches/{batch_id}/zip")
    async def batch_zip(request: Request, batch_id: str):
        auth(request, True)
        with storage().engine.connect() as db:
            name = db.execute(select(batches.c.name).where(batches.c.id == batch_id)).scalar()
            ids = list(db.execute(select(files.c.id).where(files.c.batch_id == batch_id,
                                   files.c.deleted_at.is_(None))).scalars())
        if not ids:
            raise HTTPException(404, "批次为空或不存在")
        if len(ids) > 10000:
            raise HTTPException(413, "批次超过单次 10000 文件限制，请分批下载")
        return await archive(request, Archive(ids=ids, name=name or "扫描批次"))

    @app.post("/api/batches/{batch_id}/delete")
    async def batch_delete(request: Request, batch_id: str):
        auth(request, True)
        with storage().lock, storage().engine.begin() as db:
            result = db.execute(update(files).where(files.c.batch_id == batch_id, files.c.deleted_at.is_(None))
                                .values(deleted_at=time.time()))
            storage().bump(db)
        return {"updated": result.rowcount}

    static = Path(os.getenv("STATIC_DIR", Path(__file__).parent / "static"))
    if static.is_dir():
        app.mount("/", StaticFiles(directory=static, html=True), name="frontend")
