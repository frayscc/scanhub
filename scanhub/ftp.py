"""Upload-only FTP with durable completion queue and idempotent ingestion."""
import asyncio
import hashlib
import hmac
import json
import logging
import os
from pathlib import Path
import shutil
import tempfile
import threading
import time
import uuid

from pyftpdlib.authorizers import AuthenticationFailed, DummyAuthorizer
from pyftpdlib.handlers import DTPHandler, FTPHandler
from pyftpdlib.ioloop import IOLoop
from pyftpdlib.servers import FTPServer

from .storage import clean_name, inspect_file, upload_logs

log = logging.getLogger("uvicorn.error")


class FTPReceiver:
    def __init__(self, store, cfg):
        self.store, self.cfg = store, cfg
        self.root = store.root / "ftp"
        for name in ("drop", "queue", "failed"):
            (self.root / name).mkdir(parents=True, exist_ok=True)
        self.server = self.thread = None

    def record(self, status, size=0,workspace_id=None):
        with self.store.engine.begin() as db:
            db.execute(upload_logs.insert().values(id=uuid.uuid4().hex,received_at=time.time(),
                status=status,bytes=size,file_count=1 if status==200 else 0,device=os.getenv("FTP_DEVICE_NAME","FTP扫描仪"),workspace_id=workspace_id))
            old = __import__('sqlalchemy').select(upload_logs.c.id).order_by(upload_logs.c.received_at.desc()).offset(1000)
            db.execute(upload_logs.delete().where(upload_logs.c.id.in_(old)))

    def start(self):
        password = os.getenv("FTP_PASSWORD", "")
        if not password or os.getenv("FTP_ENABLED", "true").lower() != "true":
            log.info("FTP not configured; set FTP_PASSWORD to enable scanner reception")
            return
        receiver = self
        from .workspaces import WorkspaceService
        self.workspaces=WorkspaceService(self.store)
        digest = hashlib.sha256(password.encode()).digest()

        class Authorizer(DummyAuthorizer):
            def validate_authentication(self, username, password, handler):
                if username not in self.user_table or not hmac.compare_digest(hashlib.sha256(password.encode()).digest(), digest):
                    raise AuthenticationFailed("Authentication failed")

        auth = Authorizer()
        # No read, delete, rename, or append permission. Originals/DB are outside this root.
        auth.add_user(os.getenv("FTP_USERNAME","scanner"), "", str(self.root / "drop"), perm="elwm")

        class LimitedDTP(DTPHandler):
            def handle_read(self):
                super().handle_read()
                if self.tot_bytes_received > receiver.cfg.max_bytes:
                    self.transfer_finished = False
                    self._resp=("552 File exceeds configured limit.",log.warning)
                    self.close()
            handle_read_event=handle_read

        class Handler(FTPHandler):
            authorizer = auth
            dtp_handler = LimitedDTP
            banner = "ScanHub FTP ready"
            encoding = "utf-8"
            max_login_attempts = 3

            def ftp_STOR(self, file, mode="w"):
                if mode != "w" or self._restart_position:
                    self._restart_position = 0
                    self.respond("550 Append and resume are not supported; upload a complete file.")
                    return
                workspace_id=receiver.workspaces.active()
                if not workspace_id:
                    self.respond('450 Open the ScanHub page and click Start scanning first.')
                    return
                receipt = uuid.uuid4().hex
                target = receiver.root / "drop" / (receipt + ".part")
                metadata = {"id":receipt,"workspace_id":workspace_id,"name":clean_name(file),"device":os.getenv("FTP_DEVICE_NAME","FTP扫描仪")}
                (receiver.root / "queue" / (receipt + ".json")).write_text(json.dumps(metadata),encoding="utf-8")
                return super().ftp_STOR(str(target))

            def ftp_SIZE(self, path):
                # Some scanner clients verify SIZE after STOR in the same session.
                sizes = getattr(self,"completed_sizes",{})
                name=clean_name(path)
                if name in sizes:
                    self.respond("213 " + str(sizes[name]))
                else:
                    return super().ftp_SIZE(path)

            def on_file_received(self, path):
                source=Path(path)
                receipt=source.stem
                meta_path=receiver.root/"queue"/(receipt+".json")
                try:meta=json.loads(meta_path.read_text(encoding="utf-8"))
                except FileNotFoundError:
                    source.unlink(missing_ok=True)
                    return
                if not receiver.workspaces.live(meta.get('workspace_id','')):
                    source.unlink(missing_ok=True);meta_path.unlink(missing_ok=True)
                    return
                if not hasattr(self,"completed_sizes"): self.completed_sizes={}
                self.completed_sizes[meta["name"]]=source.stat().st_size
                source.replace(receiver.root/"queue"/(receipt+".ready"))
                log.info("FTP transfer completed receipt=%s bytes=%d",receipt,self.completed_sizes[meta["name"]])

            def on_incomplete_file_received(self, path):
                source=Path(path)
                if source.exists(): source.replace(receiver.root/"failed"/source.name)
                log.warning("FTP incomplete transfer; retained outside inbox")

            def log_transfer(self, cmd, filename, receive, completed, elapsed, bytes):
                pass  # Never log original names or content.

            def log_cmd(self, cmd, arg, respcode, respstr):
                # Log protocol progress, never arguments (passwords, names, paths).
                command=cmd if cmd in self.proto_cmds else 'UNKNOWN'
                log.info('FTP command peer=%s:%s cmd=%s code=%s',self.remote_ip,self.remote_port,command,respcode)

            def respond(self, resp, logfun=None):
                # Data-channel errors may arrive asynchronously after PASV/PORT.
                if resp[:3] in ('421','425','426'):
                    log.warning('FTP connection peer=%s:%s code=%s',self.remote_ip,self.remote_port,resp[:3])
                if logfun is None:return super().respond(resp)
                return super().respond(resp,logfun)

        Handler.masquerade_address = os.getenv("FTP_PUBLIC_HOST") or None
        minimum,maximum=int(os.getenv("FTP_PASSIVE_MIN","21000")),int(os.getenv("FTP_PASSIVE_MAX","21010"))
        if not 1024 <= minimum <= maximum <= 65535: raise ValueError("Invalid FTP passive port range")
        Handler.passive_ports=range(minimum,maximum+1)
        self.loop=IOLoop()
        self.server=FTPServer((os.getenv("FTP_BIND","0.0.0.0"),int(os.getenv("FTP_PORT","2121"))),Handler,ioloop=self.loop)
        self.server.max_cons=16
        self.server.max_cons_per_ip=4
        self.thread=threading.Thread(target=self.server.serve_forever,kwargs={"timeout":.2,"handle_exit":False},daemon=True)
        self.thread.start()
        log.info("FTP scanner reception started on port %s",self.server.socket.getsockname()[1])
        log.info('FTP data configuration public_host=%s passive_ports=%d-%d',Handler.masquerade_address or 'auto',minimum,maximum)

    def close(self):
        if self.server:
            self.loop.call_later(0,self.server.close_all)
            self.thread.join(timeout=5)
            self.loop.close()

    def import_one(self, ready):
        ready=Path(ready)
        receipt=ready.stem
        meta_path=ready.with_suffix(".json")
        meta=json.loads(meta_path.read_text(encoding="utf-8"))
        from .workspaces import WorkspaceService
        if not WorkspaceService(self.store).live(meta.get('workspace_id','')):
            ready.unlink(missing_ok=True);meta_path.unlink(missing_ok=True)
            return
        if ready.stat().st_size > self.cfg.max_bytes:
            raise ValueError("FTP file exceeds configured limit")
        mime,width,height=inspect_file(ready)
        size=ready.stat().st_size
        with tempfile.TemporaryDirectory(dir=self.store.root/"tmp",prefix="ftp-") as directory:
            staged=Path(directory)/"source"
            shutil.copyfile(ready,staged)
            result=self.store.ingest([{"temp":staged,"name":meta["name"],"mime":mime,"width":width,"height":height}],
                meta["device"],"",'{}',batch_interval=self.store.options().get("batch_interval",30),receipt_id=receipt,workspace_id=meta["workspace_id"])
        # Receipt and file metadata commit together; crash here is safe to retry.
        ready.unlink(missing_ok=True)
        meta_path.unlink(missing_ok=True)
        self.record(200,size,meta['workspace_id'])
        log.info("FTP imported receipt=%s id=%s",receipt,result[0]["id"])

    def sweep(self):
        from .workspaces import WorkspaceService
        service=WorkspaceService(self.store)
        for meta in (self.root/'queue').glob('*.json'):
            try:
                data=json.loads(meta.read_text(encoding='utf-8'))
                if service.live(data.get('workspace_id','')):continue
                receipt=meta.stem
                for folder,suffix in [('drop','.part'),('queue','.ready'),('failed','.ready'),('failed','.part')]:
                    (self.root/folder/(receipt+suffix)).unlink(missing_ok=True)
                meta.unlink(missing_ok=True)
            except (OSError,ValueError):continue

    async def process(self, work):
        while True:
            for ready in sorted((self.root/"queue").glob("*.ready")):
                try:
                    await work.run(self.import_one,ready)
                except ValueError:
                    try:
                        owner=json.loads(ready.with_suffix('.json').read_text(encoding='utf-8')).get('workspace_id')
                        ready.replace(self.root/"failed"/ready.name)
                        self.record(415,workspace_id=owner)
                        log.warning("FTP file rejected; original retained until workspace cleanup")
                    except FileNotFoundError:
                        pass  # The owner ended while validation was running.
                except Exception:
                    log.error("FTP ingestion deferred; completed original retained for retry")
            await asyncio.sleep(1)
