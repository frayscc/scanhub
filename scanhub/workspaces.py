"""Anonymous per-page capability tokens and exclusive scanner leases."""
import hashlib
import json
import secrets
import time
import uuid

from fastapi import HTTPException
from sqlalchemy import select,update,delete,func

from .storage import workspaces,lease,files,purge_jobs,receipts,upload_logs

TTL=300


class WorkspaceService:
    def __init__(self,store):self.store=store

    def create(self):
        token=secrets.token_urlsafe(32)
        workspace_id=uuid.uuid4().hex
        with self.store.engine.begin() as db:
            db.exec_driver_sql('BEGIN IMMEDIATE')
            count=db.execute(select(func.count()).select_from(workspaces).where(workspaces.c.expires>time.time(),workspaces.c.state=='open')).scalar_one()
            if count>=64:raise HTTPException(503,'临时工作台已满，请稍后再试')
            db.execute(workspaces.insert().values(id=workspace_id,digest=hashlib.sha256(token.encode()).hexdigest(),expires=time.time()+TTL,state='open'))
        return {'token':token,'id':workspace_id,'timeout_seconds':TTL}

    def auth(self,token):
        with self.store.engine.connect() as db:
            row=db.execute(select(workspaces).where(workspaces.c.digest==hashlib.sha256(token.encode()).hexdigest(),
                workspaces.c.state=='open',workspaces.c.expires>time.time())).mappings().first()
        if not row:raise HTTPException(401,'临时工作台已结束，请重新打开页面')
        return row['id']

    def live(self,workspace_id):
        with self.store.engine.connect() as db:
            return db.execute(select(workspaces.c.id).where(workspaces.c.id==workspace_id,workspaces.c.state=='open',workspaces.c.expires>time.time())).scalar() is not None

    def active(self):
        with self.store.engine.connect() as db:
            return db.execute(select(workspaces.c.id).join(lease,lease.c.workspace_id==workspaces.c.id)
                .where(workspaces.c.state=='open',workspaces.c.expires>time.time())).scalar()

    def claim(self,workspace_id):
        with self.store.lock,self.store.engine.begin() as db:
            db.exec_driver_sql('BEGIN IMMEDIATE')
            current=db.execute(select(workspaces.c.id).join(lease,lease.c.workspace_id==workspaces.c.id)
                .where(workspaces.c.state=='open',workspaces.c.expires>time.time())).scalar()
            if current and current!=workspace_id:raise HTTPException(409,'扫描仪正在由其他人使用，请稍后再试')
            db.execute(update(lease).where(lease.c.id==1).values(workspace_id=workspace_id))
            db.execute(update(workspaces).where(workspaces.c.id==workspace_id).values(expires=time.time()+TTL))

    def heartbeat(self,workspace_id):
        with self.store.engine.begin() as db:
            db.execute(update(workspaces).where(workspaces.c.id==workspace_id,workspaces.c.state=='open').values(expires=time.time()+TTL))

    def end(self,workspace_id,expired_only=False):
        # Invalidate the token and persist all deletion jobs atomically before disk work.
        with self.store.lock,self.store.engine.begin() as db:
            db.exec_driver_sql('BEGIN IMMEDIATE')
            if expired_only:
                row=db.execute(select(workspaces).where(workspaces.c.id==workspace_id)).mappings().first()
                if row and row['state']=='open' and row['expires']>time.time():return
            db.execute(update(workspaces).where(workspaces.c.id==workspace_id).values(state='ended',expires=0))
            db.execute(update(lease).where(lease.c.workspace_id==workspace_id).values(workspace_id=None))
            rows=db.execute(select(files).where(files.c.workspace_id==workspace_id)).mappings().all()
            for row in rows:
                db.execute(purge_jobs.insert().values(id=row['id'],paths=json.dumps([row['path'],f"thumbnails/{row['id']}.jpg"])))
            db.execute(delete(files).where(files.c.workspace_id==workspace_id))
            if rows:db.execute(delete(receipts).where(receipts.c.file_id.in_([r['id'] for r in rows])))
            db.execute(delete(workspaces).where(workspaces.c.id==workspace_id))
            db.execute(delete(upload_logs).where(upload_logs.c.workspace_id==workspace_id))
            self.store.bump(db)

    def cleanup(self):
        with self.store.engine.connect() as db:
            ids=list(db.execute(select(workspaces.c.id).where((workspaces.c.expires<=time.time())|(workspaces.c.state=='ended'))).scalars())
        for workspace_id in ids:self.end(workspace_id,expired_only=True)
        self.store.finish_purges()
