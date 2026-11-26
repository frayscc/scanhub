"""Protocol-independent file ingestion and SQLite metadata storage."""
import hashlib
import os
import threading
import time
import uuid
import warnings
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageOps
from pypdf import PdfReader
from sqlalchemy import (Column, Float, Integer, MetaData, String, Table, Text,
                        create_engine, delete, event, select, text, update)

Image.MAX_IMAGE_PIXELS = 25_000_000
metadata = MetaData()
files = Table("files", metadata,
    Column("id", String, primary_key=True), Column("name", Text, nullable=False),
    Column("path", String, nullable=False), Column("mime", String, nullable=False),
    Column("size", Integer, nullable=False), Column("sha256", String, index=True),
    Column("uploaded_at", Float, index=True), Column("device", String), Column("qr", Text),
    Column("extra", Text), Column("batch_id", String), Column("width", Integer),
    Column("height", Integer), Column("rotation", Integer, default=0),
    Column("deleted_at", Float, nullable=True, index=True),Column("workspace_id",String,nullable=True,index=True))
sessions = Table("sessions", metadata, Column("digest", String, primary_key=True),
    Column("csrf", String), Column("expires", Float))
state = Table("state", metadata, Column("id", Integer, primary_key=True), Column("revision", Integer))
batches = Table("batches", metadata, Column("id", String, primary_key=True),
    Column("name", String), Column("kind", String), Column("device", String),
    Column("external_id", String), Column("created_at", Float), Column("last_at", Float))
preferences = Table("preferences", metadata, Column("key", String, primary_key=True), Column("value", Text))
upload_logs = Table("upload_logs", metadata, Column("id", String, primary_key=True),
    Column("received_at", Float, index=True), Column("status", Integer), Column("bytes", Integer),
    Column("file_count", Integer), Column("device", String),Column("workspace_id",String,nullable=True))
purge_jobs = Table("purge_jobs", metadata, Column("id", String, primary_key=True), Column("paths", Text))
receipts = Table("ingestion_receipts",metadata,Column("id",String,primary_key=True),Column("file_id",String))
workspaces=Table("workspaces",metadata,Column("id",String,primary_key=True),Column("digest",String,unique=True),
    Column("expires",Float,index=True),Column("state",String))
lease=Table("scanner_lease",metadata,Column("id",Integer,primary_key=True),Column("workspace_id",String,nullable=True))


def clean_name(name):
    name = (name or "scan").replace("\\", "/").split("/")[-1]
    name = "".join(c for c in name if c.isprintable() and c not in '<>:"|?*')
    return name[:180].strip(" .") or "scan"


def inspect_file(path):
    """Determine MIME from content, never from supplied filename or MIME."""
    with path.open("rb") as source:
        signature = source.read(5)
    if signature == b"%PDF-":
        try:
            reader = PdfReader(str(path), strict=True)
            if reader.is_encrypted or not 0 < len(reader.pages) <= 2000:
                raise ValueError()
            # Browser renders PDF in a sandboxed iframe, no same-origin scripts.
            return "application/pdf", None, None
        except Exception:
            raise ValueError("PDF 无效、加密或页数超限") from None
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", Image.DecompressionBombWarning)
            with Image.open(path) as image:
                mime = {"JPEG": "image/jpeg", "PNG": "image/png", "TIFF": "image/tiff"}.get(image.format)
                if not mime or image.width * image.height > Image.MAX_IMAGE_PIXELS:
                    raise ValueError()
                width, height = image.size
                image.verify()
            with Image.open(path) as decoded:
                count = getattr(decoded, "n_frames", 1)
                if count > 200:
                    raise ValueError()
                for index in range(count):
                    decoded.seek(index)
                    if decoded.width * decoded.height > Image.MAX_IMAGE_PIXELS:
                        raise ValueError()
                    decoded.load()
        return mime, width, height
    except Exception:
        raise ValueError("仅支持有效 JPG、PNG、TIFF 或 PDF，图片不得超过 2500 万像素") from None


class Store:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        for folder in ("uploads", "thumbnails", "tmp"):
            (self.root / folder).mkdir(exist_ok=True)
        self.lock = threading.RLock()
        self.engine = create_engine(f"sqlite:///{(self.root / 'scanhub.db').as_posix()}",
                                    connect_args={"check_same_thread": False, "timeout": 30})
        @event.listens_for(self.engine, "connect")
        def configure(connection, _):
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
        # Ordered schema migrations; later releases append a migration, not overwrite data.
        with self.engine.begin() as db:
            db.exec_driver_sql("BEGIN IMMEDIATE")
            version = db.execute(text("PRAGMA user_version")).scalar()
            if version > 4:
                raise RuntimeError("数据库版本高于应用，请升级应用")
            if version == 0:
                metadata.create_all(db)
                db.execute(state.insert().values(id=1, revision=0))
                db.execute(text("PRAGMA user_version=1"))
            if version < 2:
                metadata.create_all(db)
                db.execute(text("PRAGMA user_version=2"))
            if version < 3:
                metadata.create_all(db)
                db.execute(text("PRAGMA user_version=3"))
            if version < 4:
                columns=[row[1] for row in db.exec_driver_sql('PRAGMA table_info(files)')]
                if 'workspace_id' not in columns:db.exec_driver_sql('ALTER TABLE files ADD COLUMN workspace_id VARCHAR')
                metadata.create_all(db)
                log_columns=[row[1] for row in db.exec_driver_sql('PRAGMA table_info(upload_logs)')]
                if 'workspace_id' not in log_columns:db.exec_driver_sql('ALTER TABLE upload_logs ADD COLUMN workspace_id VARCHAR')
                db.exec_driver_sql('CREATE INDEX IF NOT EXISTS ix_files_workspace_id ON files(workspace_id)')
                db.execute(lease.insert().values(id=1,workspace_id=None))
                db.execute(text('PRAGMA user_version=4'))
        self.finish_purges()

    def options(self):
        import json
        with self.engine.connect() as db:
            return {k: json.loads(v) for k, v in db.execute(select(preferences))}

    def save_options(self, values):
        import json
        from sqlalchemy.dialects.sqlite import insert
        with self.lock, self.engine.begin() as db:
            for key, value in values.items():
                statement = insert(preferences).values(key=key, value=json.dumps(value))
                db.execute(statement.on_conflict_do_update(index_elements=[preferences.c.key],
                                                          set_={"value": statement.excluded.value}))
            self.bump(db)

    @staticmethod
    def bump(db):
        db.execute(update(state).where(state.c.id == 1).values(revision=state.c.revision + 1))

    def revision(self):
        with self.engine.connect() as db:
            return db.execute(select(state.c.revision)).scalar_one()

    def get(self, file_id):
        with self.engine.connect() as db:
            return db.execute(select(files).where(files.c.id == file_id)).mappings().first()

    def ingest(self, prepared, device, qr, extra, external_batch=None, batch_interval=30,receipt_id=None,workspace_id=None):
        """All files in one request commit together. Originals are never re-encoded."""
        moved, records = [], []
        try:
            with self.lock, self.engine.begin() as db:
                db.exec_driver_sql("BEGIN IMMEDIATE")
                if receipt_id:
                    previous_id=db.execute(select(receipts.c.file_id).where(receipts.c.id==receipt_id)).scalar()
                    if previous_id:return [{"id":previous_id,"duplicate_of":None}]
                if workspace_id:
                    live=db.execute(select(workspaces.c.id).where(workspaces.c.id==workspace_id,workspaces.c.state=='open',workspaces.c.expires>time.time())).scalar()
                    if not live:raise ValueError('Workspace ended')
                now = time.time()
                condition = [batches.c.device == device]
                if external_batch:
                    condition += [batches.c.kind == "device", batches.c.external_id == external_batch]
                else:
                    condition += [batches.c.kind == "inferred", batches.c.last_at >= now - batch_interval]
                previous = db.execute(select(batches).where(*condition).order_by(batches.c.last_at.desc()).limit(1)).mappings().first()
                batch_id = previous["id"] if previous else uuid.uuid4().hex
                if previous:
                    db.execute(update(batches).where(batches.c.id == batch_id).values(last_at=now))
                else:
                    db.execute(batches.insert().values(id=batch_id, name=datetime.now().strftime("扫描 %m-%d %H:%M"),
                        kind="device" if external_batch else "inferred", device=device,
                        external_id=external_batch, created_at=now, last_at=now))
                for item in prepared:
                    file_id = uuid.uuid4().hex
                    relative = Path("uploads") / datetime.now(timezone.utc).strftime("%Y/%m/%d") / file_id
                    target = self.root / relative
                    target.parent.mkdir(parents=True, exist_ok=True)
                    os.replace(item["temp"], target)
                    moved.append(target)
                    digest = hashlib.sha256()
                    with target.open("rb") as source:
                        while chunk := source.read(65536):
                            digest.update(chunk)
                    sha = digest.hexdigest()
                    duplicate = db.execute(select(files.c.id).where(files.c.sha256 == sha).limit(1)).scalar()
                    row = dict(id=file_id, name=clean_name(item["name"]), path=relative.as_posix(),
                        size=target.stat().st_size, mime=item["mime"], sha256=sha, uploaded_at=time.time(),
                        device=device, qr=qr, extra=extra, batch_id=batch_id, width=item["width"],
                        height=item["height"], rotation=0, deleted_at=None,workspace_id=workspace_id)
                    db.execute(files.insert().values(**row))
                    records.append({"id": file_id, "duplicate_of": duplicate})
                if receipt_id:db.execute(receipts.insert().values(id=receipt_id,file_id=records[0]["id"]))
                self.bump(db)
            return records
        except BaseException:
            for target in moved:
                target.unlink(missing_ok=True)
            raise

    def thumbnail(self, row):
        target = self.root / "thumbnails" / f"{row['id']}.jpg"
        if not target.exists():
            # Thumbnail errors never remove an original or its database row.
            with Image.open(self.root / row["path"]) as image:
                image = ImageOps.exif_transpose(image)
                image.thumbnail((600, 600))
                image.convert("RGB").save(target, "JPEG", quality=82)
        return target

    def reconcile(self):
        """Report missing files and orphans without deleting user data."""
        with self.engine.connect() as db:
            known = set(db.execute(select(files.c.path)).scalars())
            import json
            queued = {path for job in db.execute(select(purge_jobs.c.paths)).scalars()
                      for path in json.loads(job)}
        actual = {p.relative_to(self.root).as_posix() for p in (self.root / "uploads").rglob("*") if p.is_file()}
        return {"missing": sorted(known - actual), "orphaned": sorted(actual - known - queued)}

    def purge(self, ids):
        """Durable delete queue: commit metadata removal before unlink; retry after crashes."""
        import json
        with self.lock, self.engine.begin() as db:
            rows = db.execute(select(files).where(files.c.id.in_(ids))).mappings().all()
            if len(rows) != len(set(ids)) or any(row["deleted_at"] is None for row in rows):
                raise ValueError("只允许永久删除回收站文件，部分文件不存在或已恢复")
            for row in rows:
                db.execute(purge_jobs.insert().values(id=row["id"], paths=json.dumps(
                    [row["path"], f"thumbnails/{row['id']}.jpg"])))
            db.execute(delete(files).where(files.c.id.in_(ids)))
            self.bump(db)
        return self.finish_purges()

    def finish_purges(self):
        import json
        pending = 0
        with self.lock, self.engine.connect() as db:
            jobs = db.execute(select(purge_jobs)).mappings().all()
        for job in jobs:
            try:
                for relative in json.loads(job["paths"]):
                    path = (self.root / relative).resolve()
                    if not path.is_relative_to(self.root.resolve()):
                        raise OSError("Invalid purge path")
                    path.unlink(missing_ok=True)
                with self.engine.begin() as db:
                    db.execute(delete(purge_jobs).where(purge_jobs.c.id == job["id"]))
            except OSError:
                pending += 1
        return pending

    def cleanup(self, now=None):
        """Retention is opt-in. Snapshot trash before soft-deleting old inbox files."""
        now = time.time() if now is None else now
        options = self.options()
        file_days, trash_days = options.get("file_retention_days", 0), options.get("trash_retention_days", 0)
        with self.lock, self.engine.begin() as db:
            expired = list(db.execute(select(files.c.id).where(files.c.deleted_at.is_not(None),
                files.c.deleted_at < now - trash_days * 86400)).scalars()) if trash_days else []
            moved = 0
            if file_days:
                result = db.execute(update(files).where(files.c.deleted_at.is_(None),
                    files.c.uploaded_at < now - file_days * 86400).values(deleted_at=now))
                moved = result.rowcount
                if moved:
                    self.bump(db)
        # Recheck trash status inside purge; never remove a concurrently restored file.
        purged = 0
        for start in range(0, len(expired), 500):
            ids = expired[start:start + 500]
            try:
                self.purge(ids)
                purged += len(ids)
            except ValueError:
                continue
        pending = self.finish_purges()
        return {"moved_to_trash": moved, "purged": purged, "pending": pending}
