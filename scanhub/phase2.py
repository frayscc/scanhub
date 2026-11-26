"""Phase 2 management endpoints; uses the same authentication and storage services."""
import asyncio
import json
import os
import secrets
import tempfile
import time
import uuid
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .security import check_token, token_digest
from .storage import batches, clean_name, files, upload_logs


class DeviceProfile(BaseModel):
    source: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=100)
    location: str = Field(default="", max_length=200)


class Preferences(BaseModel):
    default_sort: Literal["newest", "oldest", "name", "size"] = "newest"
    default_view: Literal["grid", "list"] = "grid"
    thumbnail_size: int = Field(default=190, ge=150, le=300)
    batch_interval: int = Field(default=30, ge=1, le=3600)
    file_retention_days: int = Field(default=0, ge=0, le=36500)
    trash_retention_days: int = Field(default=0, ge=0, le=36500)
    max_upload_mb: int = Field(default=50, ge=1, le=1024)
    debug_enabled: bool = False
    device_profiles: list[DeviceProfile] = Field(default_factory=list, max_length=100)


class FileIDs(BaseModel):
    ids: list[str] = Field(min_length=1, max_length=500)


class Purge(FileIDs):
    confirm: Literal["PERMANENT_DELETE"]


class Confirm(BaseModel):
    confirm: Literal["PERMANENT_DELETE"]


class Organize(FileIDs):
    name: str = Field(min_length=1, max_length=150)


class Rename(BaseModel):
    name: str = Field(min_length=1, max_length=150)


class BatchOperation(BaseModel):
    action: Literal["delete", "zip", "pdf"]


class PDFPage(BaseModel):
    id: str = Field(max_length=100)
    rotation: Literal[0, 90, 180, 270] = 0


class PDFRequest(BaseModel):
    pages: list[PDFPage] = Field(min_length=1, max_length=200)
    name: str = Field(default="合并扫描.pdf", min_length=1, max_length=180)
    a4: bool = False


def attach_phase2(app, cfg, storage, auth):
    # One PDF job at a time: J1900 memory and CPU are shared with uploads.
    pdf_busy = False

    @asynccontextmanager
    async def lifespan(application):
        async def maintenance():
            while True:
                try:
                    await run_in_threadpool(storage().cleanup)
                except Exception:
                    import logging
                    logging.getLogger("scanhub").error("retention maintenance failed")
                await asyncio.sleep(300)
        task = asyncio.create_task(maintenance())
        try:
            yield
        finally:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            storage().engine.dispose()
    app.router.lifespan_context = lifespan

    @app.get("/api/settings")
    async def settings(request: Request):
        auth(request)
        defaults = Preferences(max_upload_mb=max(1, cfg.max_bytes // (1024 * 1024)),
                               debug_enabled=cfg.enabled).model_dump()
        saved = storage().options()
        result = defaults | {k: v for k, v in saved.items() if k in defaults}
        return result | {"upload_token_configured": bool(cfg.upload_token),
            "upload_url": str(request.base_url).rstrip("/") + "/api/upload"}

    @app.put("/api/settings")
    async def update_settings(request: Request, payload: Preferences):
        auth(request, True)
        if payload.debug_enabled and (not (cfg.upload_token and cfg.admin_token) or check_token(cfg.admin_token, cfg.upload_token)):
            raise HTTPException(422, "启用调试前须配置独立上传令牌和 ADMIN_TOKEN")
        sources = [profile.source for profile in payload.device_profiles]
        if len(sources) != len(set(sources)):
            raise HTTPException(422, "设备来源标识不能重复")
        storage().save_options(payload.model_dump())
        cfg.max_bytes = payload.max_upload_mb * 1024 * 1024
        cfg.enabled = payload.debug_enabled
        return {"saved": True}

    @app.post("/api/settings/upload-token")
    async def reset_token(request: Request):
        auth(request, True)
        token = secrets.token_urlsafe(32)
        hashed = "sha256:" + token_digest(token)
        storage().save_options({"upload_token_hash": hashed})
        cfg.upload_token = hashed
        return {"token": token}

    @app.get("/api/upload-logs")
    async def logs(request: Request, offset: int = 0, limit: int = 30):
        auth(request)
        with storage().engine.connect() as db:
            rows = db.execute(select(upload_logs).order_by(upload_logs.c.received_at.desc())
                              .offset(max(0, offset)).limit(min(max(limit, 1), 100))).mappings().all()
        return [dict(row) for row in rows]

    @app.get("/api/protocol-logs")
    async def protocol_logs(request: Request, limit: int = 20):
        auth(request)
        import heapq
        paths = heapq.nlargest(min(max(limit, 1), 100), (storage().root / "debug").glob("*/manifest.json"))
        rows = []
        for path in paths:
            try:
                row = json.loads(path.read_text(encoding="utf-8"))
                rows.append(row)
            except (OSError, ValueError):
                continue
        return rows

    @app.post("/api/maintenance/run")
    async def run_cleanup(request: Request):
        auth(request, True)
        return await run_in_threadpool(storage().cleanup)

    @app.post("/api/trash/purge")
    async def purge(request: Request, payload: Purge):
        auth(request, True)
        try:
            pending = storage().purge(payload.ids)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"deleted": len(set(payload.ids)), "pending": pending}

    @app.post("/api/trash/empty")
    async def empty(request: Request, payload: Confirm):
        auth(request, True)
        # Fixed snapshot: never chase files moved to trash after this request began.
        with storage().engine.connect() as db:
            ids = list(db.execute(select(files.c.id).where(files.c.deleted_at.is_not(None))).scalars())
        count = 0
        for start in range(0, len(ids), 500):
            group = ids[start:start + 500]
            try:
                storage().purge(group)
                count += len(group)
            except ValueError:
                continue
        return {"deleted": count, "pending": storage().finish_purges()}

    @app.get("/api/batches")
    async def list_batches(request: Request, offset: int = 0, limit: int = 50):
        auth(request)
        with storage().engine.connect() as db:
            grouping = select(files.c.batch_id.label("batch"), func.count().label("count"),
                              func.sum(files.c.size).label("bytes")).where(files.c.deleted_at.is_(None))\
                              .group_by(files.c.batch_id).subquery()
            query = select(batches, grouping.c.count, grouping.c.bytes).join(grouping, batches.c.id == grouping.c.batch)
            rows = db.execute(query.order_by(batches.c.last_at.desc()).offset(max(0, offset))
                              .limit(min(max(limit, 1), 100))).mappings().all()
            total = db.execute(select(func.count()).select_from(query.subquery())).scalar_one()
            unassigned = db.execute(select(func.count()).select_from(files).where(files.c.batch_id.is_(None),
                files.c.deleted_at.is_(None))).scalar_one()
        return {"items": [dict(row) for row in rows], "total": total, "unassigned": unassigned}

    @app.post("/api/batches/organize")
    async def organize(request: Request, payload: Organize):
        auth(request, True)
        ids = set(payload.ids)
        with storage().lock, storage().engine.begin() as db:
            rows = db.execute(select(files).where(files.c.id.in_(ids))).mappings().all()
            if len(rows) != len(ids) or any(row["deleted_at"] is not None for row in rows):
                raise HTTPException(409, "部分文件不存在或已在回收站")
            batch_id = uuid.uuid4().hex
            db.execute(batches.insert().values(id=batch_id, name=payload.name.strip() or "手动批次", kind="manual",
                device="、".join(sorted({r["device"] for r in rows}))[:200], external_id=None,
                created_at=time.time(), last_at=max(r["uploaded_at"] for r in rows)))
            db.execute(update(files).where(files.c.id.in_(ids)).values(batch_id=batch_id))
            storage().bump(db)
        return {"id": batch_id}

    @app.patch("/api/batches/{batch_id}")
    async def rename(request: Request, batch_id: str, payload: Rename):
        auth(request, True)
        with storage().engine.begin() as db:
            result = db.execute(update(batches).where(batches.c.id == batch_id).values(name=payload.name.strip() or "扫描批次"))
            if not result.rowcount:
                raise HTTPException(404, "批次不存在")
            storage().bump(db)
        return {"saved": True}

    @app.post("/api/batches/merge")
    async def merge_batches(request: Request, payload: Organize):
        auth(request, True)
        ids = set(payload.ids)
        if len(ids) < 2:
            raise HTTPException(422, "至少选择两个批次")
        with storage().lock, storage().engine.begin() as db:
            rows = db.execute(select(batches).where(batches.c.id.in_(ids))).mappings().all()
            if len(rows) != len(ids):
                raise HTTPException(404, "部分批次不存在")
            batch_id = uuid.uuid4().hex
            db.execute(batches.insert().values(id=batch_id, name=payload.name.strip() or "手动合并批次",
                kind="manual", device="、".join(sorted({r["device"] for r in rows}))[:200], external_id=None,
                created_at=time.time(), last_at=max(r["last_at"] for r in rows)))
            db.execute(update(files).where(files.c.batch_id.in_(ids), files.c.deleted_at.is_(None)).values(batch_id=batch_id))
            storage().bump(db)
        return {"id": batch_id}

    @app.post("/api/files/pdf")
    async def export_pdf(request: Request, payload: PDFRequest):
        nonlocal pdf_busy
        auth(request, True)
        if pdf_busy:
            raise HTTPException(503, "已有 PDF 正在生成，请稍后重试")
        pdf_busy = True
        target = None
        try:
            with storage().engine.connect() as db:
                rows = {r["id"]: r for r in db.execute(select(files).where(
                    files.c.id.in_({p.id for p in payload.pages}))).mappings()}
            if len(rows) != len({p.id for p in payload.pages}):
                raise HTTPException(404, "部分文件不存在")
            if any(not r["mime"].startswith("image/") or r["deleted_at"] is not None for r in rows.values()):
                raise HTTPException(422, "只支持合并收件箱中的图片")
            handle, target = tempfile.mkstemp(suffix=".pdf", dir=storage().root / "tmp")
            os.close(handle)
            from .pdf_export import build_pdf
            await app.state.work.run(build_pdf, storage().root, rows, payload.pages, target, payload.a4)
            response = FileResponse(target, media_type="application/pdf",
                filename=clean_name(payload.name) + ("" if payload.name.lower().endswith(".pdf") else ".pdf"),
                background=BackgroundTask(Path(target).unlink, missing_ok=True))
            target = None
            return response
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        except OSError:
            raise HTTPException(507, "PDF 生成失败，请检查磁盘和图片文件") from None
        finally:
            pdf_busy = False
            if target:
                Path(target).unlink(missing_ok=True)
