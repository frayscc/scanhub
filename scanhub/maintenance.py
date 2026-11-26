"""Offline backup/restore and read-only acceptance checks. No server dependencies required."""
import argparse
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile

FORMAT = 1
SCHEMA = 2
MANIFEST = "scanhub-backup.json"


def readonly_database(path):
    return sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=10)


def contained_file(root, relative):
    path = PurePosixPath(relative)
    if path.is_absolute() or not path.parts or any(p in (".", "..") or ":" in p or "\\" in p for p in path.parts):
        raise ValueError("备份或数据库包含不安全路径")
    candidate = root.joinpath(*path.parts)
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise ValueError("路径越过数据目录")
    current = candidate
    while current != root:
        if current.is_symlink():
            raise ValueError("数据目录中不允许符号链接")
        current = current.parent
    return candidate


def digest_file(path):
    digest = hashlib.sha256()
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def inspect_data(root, verify_hashes=False):
    """Never creates/migrates a database or executes retention/purge tasks."""
    root = Path(root).resolve()
    issues, count, version = [], 0, None
    database = root / "scanhub.db"
    if not database.is_file():
        return {"ok": False, "schema": None, "files": 0, "issues": ["数据库不存在"]}
    try:
        with closing(readonly_database(database)) as db:
            check = db.execute("PRAGMA quick_check").fetchall()
            if check != [("ok",)]:
                issues.append("SQLite 一致性检查失败")
            version = db.execute("PRAGMA user_version").fetchone()[0]
            if version not in (1, SCHEMA):
                issues.append("不支持的数据库版本")
            for file_id, relative, size, sha in db.execute("SELECT id,path,size,sha256 FROM files"):
                count += 1
                try:
                    path = contained_file(root, relative)
                    if not path.is_file():
                        issues.append(f"原件缺失 id={file_id}")
                    elif path.stat().st_size != size:
                        issues.append(f"原件大小不符 id={file_id}")
                    elif verify_hashes and digest_file(path) != sha:
                        issues.append(f"原件摘要不符 id={file_id}")
                except (OSError, ValueError):
                    issues.append(f"原件路径或读取异常 id={file_id}")
    except (OSError, sqlite3.Error):
        issues.append("数据库不可读或结构异常")
    return {"ok": not issues, "schema": version, "files": count, "issues": issues}


def backup(root, destination, offline_confirmed=False):
    if not offline_confirmed:
        raise ValueError("须先停止 ScanHub，再传 --offline-confirmed；文件与数据库必须来自同一静止状态")
    root, destination = Path(root).resolve(), Path(destination).resolve()
    if destination.is_relative_to(root) or destination.exists():
        raise ValueError("备份目标必须位于数据目录之外，且不能覆盖已有文件")
    report = inspect_data(root, verify_hashes=True)
    if not report["ok"]:
        raise ValueError("数据完整性检查未通过，拒绝生成可恢复备份")
    destination.parent.mkdir(parents=True, exist_ok=True)
    # mkstemp gives private permissions; publish only a fully closed archive.
    handle, partial = tempfile.mkstemp(prefix=".scanhub-backup-", suffix=".partial", dir=destination.parent)
    os.close(handle)
    try:
        with tempfile.TemporaryDirectory(prefix=".scanhub-snapshot-", dir=destination.parent) as directory:
            snapshot = Path(directory) / "scanhub.db"
            with closing(readonly_database(root / "scanhub.db")) as source:
                with closing(sqlite3.connect(snapshot)) as target:
                    source.backup(target)
            entries = []
            with zipfile.ZipFile(partial, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                def add(path, relative):
                    # Hash bytes as they are copied, never load an entire original into memory.
                    digest, size = hashlib.sha256(), 0
                    with path.open("rb") as source, archive.open(relative, "w", force_zip64=True) as target:
                        while chunk := source.read(1024 * 1024):
                            target.write(chunk)
                            digest.update(chunk)
                            size += len(chunk)
                    entries.append({"path": relative, "size": size, "sha256": digest.hexdigest()})
                add(snapshot, "scanhub.db")
                for parent, directories, names in os.walk(root, followlinks=False):
                    base = Path(parent)
                    directories[:] = sorted(d for d in directories if not (base == root and d == "tmp"))
                    for name in directories:
                        if (base / name).is_symlink():
                            raise ValueError("备份拒绝符号链接目录")
                    for name in sorted(names):
                        path = base / name
                        relative = path.relative_to(root).as_posix()
                        if base == root and name in ("scanhub.db", "scanhub.db-wal", "scanhub.db-shm"):
                            continue
                        if relative == MANIFEST:
                            raise ValueError("数据目录中存在保留的备份清单名称")
                        path = contained_file(root, relative)
                        if not stat.S_ISREG(path.stat().st_mode):
                            raise ValueError("备份只支持普通文件")
                        add(path, relative)
                archive.writestr(MANIFEST, json.dumps({"format": FORMAT, "schema": report["schema"],
                    "created_at": time.time(), "files": entries}, ensure_ascii=False))
        # Link refuses to overwrite a destination created concurrently; output is on the same filesystem.
        os.link(partial, destination)
        return {"ok": True, "originals": report["files"], "archive_bytes": destination.stat().st_size}
    finally:
        Path(partial).unlink(missing_ok=True)


def restore(archive_path, destination, offline_confirmed=False):
    if not offline_confirmed:
        raise ValueError("须先停止 ScanHub，再传 --offline-confirmed")
    destination = Path(destination).absolute()
    # Never merge into existing files, never replace a symlink, and never replace a live data directory.
    if destination.exists() or destination.is_symlink():
        raise ValueError("恢复目标必须是尚不存在的新目录；已有目录不做任何修改")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".scanhub-restore-", dir=destination.parent) as directory:
        stage = Path(directory)
        with zipfile.ZipFile(archive_path) as archive:
            infos = archive.infolist()
            manifest_infos = [info for info in infos if info.filename == MANIFEST]
            if len(manifest_infos) != 1 or manifest_infos[0].file_size > 32 * 1024 * 1024:
                raise ValueError("备份清单缺失、重复或过大")
            manifest = json.loads(archive.read(MANIFEST))
            if manifest.get("format") != FORMAT or manifest.get("schema") not in (1, SCHEMA):
                raise ValueError("不支持的备份格式或数据库版本")
            expected = {}
            for entry in manifest["files"]:
                name = entry["path"]
                contained_file(stage, name)
                if name in expected or name == MANIFEST or not isinstance(entry["size"], int) or entry["size"] < 0:
                    raise ValueError("备份清单包含重复或无效记录")
                expected[name] = entry
            if "scanhub.db" not in expected:
                raise ValueError("备份没有数据库")
            payload = [info for info in infos if info.filename != MANIFEST]
            if len(payload) != len(expected) or {i.filename for i in payload} != set(expected):
                raise ValueError("备份内容与清单不符")
            total = sum(entry["size"] for entry in expected.values())
            if total + 64 * 1024 * 1024 > shutil.disk_usage(stage).free:
                raise ValueError("恢复所需磁盘空间不足")
            seen = set()
            for info in payload:
                entry = expected[info.filename]
                mode = info.external_attr >> 16
                if info.filename in seen or info.is_dir() or stat.S_ISLNK(mode) or info.file_size != entry["size"]:
                    raise ValueError("备份包含重复、链接或大小不符的文件")
                seen.add(info.filename)
                target = contained_file(stage, info.filename)
                target.parent.mkdir(parents=True, exist_ok=True)
                digest, size = hashlib.sha256(), 0
                with archive.open(info) as source, target.open("xb") as output:
                    while chunk := source.read(1024 * 1024):
                        size += len(chunk)
                        if size > entry["size"]:
                            raise ValueError("解压内容超出清单大小")
                        output.write(chunk)
                        digest.update(chunk)
                if size != entry["size"] or digest.hexdigest() != entry["sha256"]:
                    raise ValueError("备份校验失败")
        report = inspect_data(stage, verify_hashes=True)
        if not report["ok"]:
            raise ValueError("恢复后的数据完整性检查未通过")
        # Revoke copied browser sessions. Preserve upload token digest and user settings.
        with closing(sqlite3.connect(stage / "scanhub.db")) as db:
            db.execute("DELETE FROM sessions")
            db.commit()
        # Destination is published only after every file and DB reference has been verified.
        stage.rename(destination)
        return {"ok": True, "originals": report["files"], "sessions_revoked": True}


def main():
    parser = argparse.ArgumentParser(description="ScanHub 离线维护工具；备份包含敏感文件，请妥善保存")
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("check")
    check.add_argument("--data-dir", required=True)
    check.add_argument("--verify-hashes", action="store_true")
    create = sub.add_parser("backup")
    create.add_argument("--data-dir", required=True)
    create.add_argument("--output", required=True)
    create.add_argument("--offline-confirmed", action="store_true")
    recover = sub.add_parser("restore")
    recover.add_argument("--archive", required=True)
    recover.add_argument("--data-dir", required=True)
    recover.add_argument("--offline-confirmed", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "check":
            report = inspect_data(args.data_dir, args.verify_hashes)
        elif args.command == "backup":
            report = backup(args.data_dir, args.output, args.offline_confirmed)
        else:
            report = restore(args.archive, args.data_dir, args.offline_confirmed)
    except (ValueError, KeyError, TypeError, OSError, sqlite3.Error, zipfile.BadZipFile):
        # No sensitive filenames, DB values, credentials or archive payload in stderr.
        print(json.dumps({"ok": False, "error": "维护失败；请核对命令、离线状态、目录权限、备份清单及磁盘空间"}, ensure_ascii=False))
        raise SystemExit(1)
    print(json.dumps(report, ensure_ascii=False))
    raise SystemExit(0 if report["ok"] else 1)


if __name__ == "__main__":
    main()
