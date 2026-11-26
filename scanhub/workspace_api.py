"""Public temporary workspaces; every file access is scoped to a per-page capability."""
import asyncio
from contextlib import asynccontextmanager
import json
import logging
import os
from pathlib import Path
import tempfile

from fastapi import HTTPException,Request
from fastapi.responses import FileResponse,Response,StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel,Field
from sqlalchemy import select,func,update
from sqlalchemy.exc import SQLAlchemyError
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool

from .storage import Store,files,clean_name,upload_logs
from .workspaces import WorkspaceService
from .work import BoundedWorker
from .media import preview_bytes,thumbnail_bytes,rotated_png,write_zip
from .schemas import PDFRequest


class Action(BaseModel):
    ids:list[str]=Field(min_length=1,max_length=500)
    action:str


class Archive(BaseModel):
    ids:list[str]=Field(min_length=1,max_length=500)
    name:str=Field(default='扫描文件.zip',max_length=180)


def attach_workspace(app,cfg):
    worker=BoundedWorker();app.state.work=worker
    @app.exception_handler(OSError)
    async def storage_error(request,exc):
        return Response('{"detail":"存储操作失败，请检查磁盘空间或重试"}',status_code=507,media_type='application/json')
    @app.exception_handler(SQLAlchemyError)
    async def database_error(request,exc):
        return Response('{"detail":"数据存储暂不可用"}',status_code=503,media_type='application/json')
    store=None
    def storage():
        nonlocal store
        if store is None:
            store=Store(cfg.data_dir);app.state.store=store
        return store
    def service():return WorkspaceService(storage())
    def auth(request):return service().auth(request.headers.get('x-workspace-token',''))
    def owned(request,ids):
        owner=auth(request)
        with storage().engine.connect() as db:
            rows=db.execute(select(files).where(files.c.workspace_id==owner,files.c.id.in_(set(ids)))).mappings().all()
        if len(rows)!=len(set(ids)):raise HTTPException(404,'文件不存在于当前工作台')
        return rows

    @asynccontextmanager
    async def lifespan(application):
        from .ftp import FTPReceiver
        receiver=FTPReceiver(storage(),cfg);receiver.start();app.state.ftp=receiver
        async def cleanup():
            while True:
                try:
                    await run_in_threadpool(service().cleanup)
                    await run_in_threadpool(receiver.sweep)
                except Exception:logging.getLogger('uvicorn.error').error('Workspace cleanup deferred')
                await asyncio.sleep(5)
        jobs=[asyncio.create_task(receiver.process(worker)),asyncio.create_task(cleanup())]
        try:yield
        finally:
            receiver.close()
            for job in jobs:job.cancel()
            for job in jobs:
                try:await job
                except asyncio.CancelledError:pass
            storage().engine.dispose()
    app.router.lifespan_context=lifespan

    @app.middleware('http')
    async def bounds(request,call_next):
        if request.method in ('POST','PUT','PATCH','DELETE'):
            body=bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body)>131072:return Response(status_code=413)
            request._body=bytes(body)
        if request.url.path.startswith('/api/') and request.url.path not in ('/api/health','/api/ready'):
            if not any(request.url.path.startswith(prefix) for prefix in ('/api/workspaces','/api/files','/api/events')):
                return Response(status_code=404)
        response=await call_next(request)
        response.headers['Cache-Control']='no-store'
        response.headers['X-Content-Type-Options']='nosniff'
        response.headers['Referrer-Policy']='no-referrer'
        response.headers['Content-Security-Policy']="default-src 'self'; img-src 'self' blob:; style-src 'self' 'unsafe-inline'; script-src 'self'; connect-src 'self'; frame-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'self'"
        return response

    @app.get('/api/ready')
    async def ready():
        from .readiness import check_ready
        try:
            ok=await run_in_threadpool(check_ready,storage())
            receiver=getattr(app.state,'ftp',None)
            if receiver and receiver.server:ok=ok and receiver.thread.is_alive()
        except Exception:ok=False
        return Response(json.dumps({'status':'ready' if ok else 'unavailable'}),status_code=200 if ok else 503,media_type='application/json')

    @app.post('/api/workspaces')
    async def create():return service().create()

    @app.post('/api/workspaces/start')
    async def start(request:Request):
        service().claim(auth(request));return {'active':True}

    @app.post('/api/workspaces/heartbeat')
    async def heartbeat(request:Request):
        owner=auth(request);service().heartbeat(owner)
        return {'active':service().active()==owner}

    @app.post('/api/workspaces/end')
    async def end(request:Request):
        token=request.headers.get('x-workspace-token','')
        if not token:
            try:token=(await request.json()).get('token','')
            except Exception:raise HTTPException(400,'Invalid close request')
        if not isinstance(token,str) or len(token)>128:raise HTTPException(400,'Invalid close request')
        try:owner=service().auth(token)
        except HTTPException:return Response(status_code=204)
        service().end(owner)
        await run_in_threadpool(storage().finish_purges)
        if hasattr(app.state,'ftp'):await run_in_threadpool(app.state.ftp.sweep)
        return Response(status_code=204)

    @app.get('/api/files')
    async def listing(request:Request,q:str='',sort:str='newest',offset:int=0,limit:int=100):
        owner=auth(request)
        conditions=[files.c.workspace_id==owner]
        if q:conditions.append(files.c.name.contains(q[:200],autoescape=True))
        order={'newest':files.c.uploaded_at.desc(),'oldest':files.c.uploaded_at,'name':files.c.name,'size':files.c.size.desc()}.get(sort,files.c.uploaded_at.desc())
        with storage().engine.connect() as db:
            total=db.execute(select(func.count()).select_from(files).where(*conditions)).scalar_one()
            rows=db.execute(select(files).where(*conditions).order_by(order).offset(max(0,offset)).limit(min(max(1,limit),200))).mappings().all()
            failures=db.execute(select(func.count()).select_from(upload_logs).where(upload_logs.c.workspace_id==owner,upload_logs.c.status>=400)).scalar_one()
        return {'total':total,'failed_files':failures,'items':[{k:v for k,v in row.items() if k not in ('path','extra','workspace_id','sha256')} for row in rows]}

    @app.post('/api/files/batch')
    async def action(request:Request,payload:Action):
        rows=owned(request,payload.ids)
        if payload.action=='delete':
            with storage().engine.begin() as db:db.execute(update(files).where(files.c.id.in_(payload.ids)).values(deleted_at=__import__('time').time()))
            storage().purge(payload.ids)
        elif payload.action in ('left','right'):
            if any(not row['mime'].startswith('image/') for row in rows):raise HTTPException(422,'请只选择图片进行旋转')
            with storage().engine.begin() as db:
                db.execute(update(files).where(files.c.id.in_(payload.ids)).values(rotation=(files.c.rotation+(270 if payload.action=='left' else 90))%360));storage().bump(db)
        else:raise HTTPException(422,'不支持此操作')
        return {'updated':len(rows)}

    @app.get('/api/files/{file_id}/{kind}')
    async def read_file(request:Request,file_id:str,kind:str):
        row=owned(request,[file_id])[0]
        if kind=='original':return FileResponse(storage().root/row['path'],media_type=row['mime'],filename=row['name'])
        if kind=='preview' and row['mime']=='application/pdf':return FileResponse(storage().root/row['path'],media_type='application/pdf',headers={'Content-Security-Policy':'sandbox'})
        if kind in ('preview','thumbnail') and row['mime'].startswith('image/'):
            data=await worker.run(preview_bytes,storage().root,row) if kind=='preview' else await worker.run(thumbnail_bytes,storage(),row)
            return Response(data,media_type='image/jpeg')
        if kind=='rotated' and row['mime'] in ('image/png','image/jpeg'):
            handle,target=tempfile.mkstemp(suffix='.png',dir=storage().root/'tmp');os.close(handle)
            try:await worker.run(rotated_png,storage().root,row,target)
            except BaseException:Path(target).unlink(missing_ok=True);raise
            return FileResponse(target,media_type='image/png',filename=Path(row['name']).stem+'-方向修正.png',background=BackgroundTask(Path(target).unlink,missing_ok=True))
        raise HTTPException(404,'此预览不可用')

    @app.post('/api/files/zip')
    async def archive(request:Request,payload:Archive):
        rows=owned(request,payload.ids)
        handle,target=tempfile.mkstemp(suffix='.zip',dir=storage().root/'tmp');os.close(handle)
        try:await worker.run(write_zip,storage().root,rows,target)
        except BaseException:Path(target).unlink(missing_ok=True);raise
        return FileResponse(target,media_type='application/zip',filename=clean_name(payload.name)+('' if payload.name.endswith('.zip') else '.zip'),background=BackgroundTask(Path(target).unlink,missing_ok=True))

    @app.post('/api/files/pdf')
    async def pdf(request:Request,payload:PDFRequest):
        from .pdf_export import build_pdf
        rows=owned(request,[p.id for p in payload.pages])
        handle,target=tempfile.mkstemp(suffix='.pdf',dir=storage().root/'tmp');os.close(handle)
        try:await worker.run(build_pdf,storage().root,{r['id']:r for r in rows},payload.pages,target,payload.a4)
        except ValueError as exc:Path(target).unlink(missing_ok=True);raise HTTPException(422,str(exc))
        except BaseException:Path(target).unlink(missing_ok=True);raise
        return FileResponse(target,media_type='application/pdf',filename=clean_name(payload.name)+('' if payload.name.endswith('.pdf') else '.pdf'),background=BackgroundTask(Path(target).unlink,missing_ok=True))

    @app.get('/api/events')
    async def events(request:Request):
        owner=auth(request)
        async def generate():
            previous=-1
            while not await request.is_disconnected():
                if not service().live(owner):return
                revision=storage().revision()
                if revision!=previous:yield f'data: {revision}\n\n';previous=revision
                else:yield ': keepalive\n\n'
                await asyncio.sleep(2)
        return StreamingResponse(generate(),media_type='text/event-stream',headers={'X-Accel-Buffering':'no'})

    static=Path(os.getenv('STATIC_DIR',Path(__file__).parent/'static'))
    if static.is_dir():app.mount('/',StaticFiles(directory=static,html=True),name='frontend')
