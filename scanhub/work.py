"""Bound CPU/memory intensive work and keep the ASGI event loop responsive."""
import threading
import asyncio
import time

from fastapi import HTTPException
from starlette.concurrency import run_in_threadpool


class BoundedWorker:
    def __init__(self):
        self._lock = threading.Lock()

    async def run(self, function, *args, _wait_timeout=10, **kwargs):
        deadline = time.monotonic() + _wait_timeout
        while not self._lock.acquire(blocking=False):
            if time.monotonic() >= deadline:
                raise HTTPException(503, "正在处理图片或导出任务，请稍后重试")
            await asyncio.sleep(.01)
        try:
            # Keep the lease until the thread has really stopped. Otherwise cancellation
            # could release memory capacity and delete temp files while a job still uses them.
            task = asyncio.create_task(run_in_threadpool(function, *args, **kwargs))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                while not task.done():
                    try:
                        await asyncio.shield(task)
                    except asyncio.CancelledError:
                        continue
                task.result()
                raise
        finally:
            self._lock.release()
