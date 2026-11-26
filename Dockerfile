FROM node:22-alpine AS frontend
WORKDIR /ui
COPY frontend/package*.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim AS wheels
WORKDIR /build
COPY requirements.txt requirements.lock ./
RUN pip wheel --no-cache-dir --wheel-dir /wheels -r requirements.txt

FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DATA_DIR=/data
WORKDIR /app
COPY --from=wheels /wheels /wheels
RUN pip install --no-cache-dir /wheels/* && rm -r /wheels && useradd --uid 10001 --create-home scanhub && mkdir /data && chown scanhub:scanhub /data
COPY scanhub ./scanhub
COPY --from=frontend /ui/dist ./scanhub/static
USER scanhub
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/api/ready', timeout=4)"
CMD ["uvicorn", "scanhub.app:app", "--host", "0.0.0.0", "--port", "8080", "--no-access-log", "--timeout-keep-alive", "5", "--limit-concurrency", "16"]
