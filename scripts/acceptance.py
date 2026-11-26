"""Standard-library acceptance client. Refuses production instances and non-test accounts."""
import argparse
from ftplib import FTP
from urllib.parse import urlsplit
import base64
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar
import hashlib
import io
import json
from pathlib import Path
import secrets
import sys
import time
import urllib.error
import urllib.request
import uuid
import zipfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAoAAAAUCAIAAAA7jDsBAAAAHUlEQVR4nGP8//8/A27AhEeOYVQaB2DCJcEwoqUBagcDJbd2MKEAAAAASUVORK5CYII=")


def prepare(path):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    values={"FTP_USERNAME":"scanner","FTP_PASSWORD":secrets.token_urlsafe(32),"FTP_PORT":"2121"}
    # Never replace existing credentials; do not print them to terminal or report.
    with path.open("x", encoding="utf-8") as out:
        for key, value in values.items():
            out.write(f"{key}={value}\n")
    path.chmod(0o600)
    return {"ok": True, "prepared": True}


class Client:
    def __init__(self, url, values):
        self.url = url.rstrip("/")
        self.values = values
        self.csrf = ""
        self.opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
                                                 urllib.request.HTTPCookieProcessor(CookieJar()))

    def request(self, path, payload=None, method="GET", data=None, headers=None):
        headers = dict(headers or {})
        if payload is not None:
            data = json.dumps(payload).encode()
            headers["Content-Type"] = "application/json"
        if self.csrf:
            headers["X-Workspace-Token"] = self.csrf
        req = urllib.request.Request(self.url + path, data=data, headers=headers, method=method)
        with self.opener.open(req, timeout=120) as response:
            return response.read(), response.headers

    def json(self, path, payload=None, method="GET"):
        raw=self.request(path,payload,method)[0]
        return json.loads(raw) if raw else {}

    def login(self):
        health = self.json("/api/health")
        if health.get("instance")!='acceptance':raise ValueError('Isolated acceptance instance required')
        self.json('/api/ready')
        self.csrf=self.json('/api/workspaces',{},'POST')['token']
        self.json('/api/workspaces/start',{},'POST')



def run(client, count, workers):
    if not 1 <= count <= 50 or not 1 <= workers <= 4:
        raise ValueError("Requests must be 1..50, workers 1..4")
    run_id = uuid.uuid4().hex
    def upload(index):
        start=time.perf_counter()
        with FTP() as ftp:
            ftp.connect(urlsplit(client.url).hostname,int(client.values.get('TEST_FTP_PORT',client.values.get('FTP_PORT','2121'))),timeout=30)
            ftp.login(client.values['FTP_USERNAME'],client.values['FTP_PASSWORD'])
            ftp.storbinary(f'STOR acceptance-{run_id}-{index}.png',io.BytesIO(PNG))
        return time.perf_counter()-start
    with ThreadPoolExecutor(max_workers=workers) as pool:
        durations=list(pool.map(upload,range(count)))
    for _ in range(300):
        listing=client.json('/api/files?q=acceptance-'+run_id+'&limit=200')
        if listing['total']==count:break
        time.sleep(.1)
    else:raise ValueError('FTP import did not complete')
    ids=[row['id'] for row in listing['items']]
    changed = ids[0]
    client.json("/api/files/batch", {"ids": [changed], "action": "right"}, "POST")
    pdf, _ = client.request("/api/files/pdf", {"pages": [{"id": file_id} for file_id in ids[:2]]}, "POST")
    if not pdf.startswith(b"%PDF-"):
        raise ValueError("Invalid PDF")
    zipped, _ = client.request("/api/files/zip", {"ids": ids[:2], "name": "acceptance.zip"}, "POST")
    with zipfile.ZipFile(io.BytesIO(zipped)) as archive:
        if len(archive.namelist()) != len(ids[:2]) or any(archive.read(name) != PNG for name in archive.namelist()):
            raise ValueError("Invalid ZIP contents")
    durations = sorted(durations)
    report = {"ok": True, "workspace_token":client.csrf, "run_id": run_id, "ids": ids, "rotated_id": changed,
              "sha256": hashlib.sha256(PNG).hexdigest(), "requests": count, "workers": workers,
              "upload_p50_seconds": durations[len(durations)//2],
              "upload_p95_seconds": durations[min(len(durations)-1, int(len(durations)*.95))]}
    verify(client, report)
    return report


def verify(client, report):
    run_id, ids = report["run_id"], set(report["ids"])
    rows = client.json("/api/files?q=acceptance-" + run_id + "&limit=200")["items"]
    if {row["id"] for row in rows} != ids or any(not row["name"].startswith("acceptance-"+run_id) for row in rows):
        raise ValueError("Test records changed or missing")
    for row in rows:
        if row["rotation"] != (90 if row["id"] == report["rotated_id"] else 0):
            raise ValueError("Rotation did not persist")
        data, _ = client.request(f"/api/files/{row['id']}/original")
        if hashlib.sha256(data).hexdigest() != report["sha256"]:
            raise ValueError("Original contents changed")
    return {"ok": True, "verified_originals": len(ids), "rotation_persisted": True}


def main():
    parser = argparse.ArgumentParser(description="ScanHub 独立测试实例验收，不用于正式实例")
    parser.add_argument("command", choices=["prepare", "run", "verify", "cleanup"])
    parser.add_argument("--env-file", default="test-results/acceptance.env")
    parser.add_argument("--url", default="http://127.0.0.1:18686")
    parser.add_argument("--report", default="test-results/acceptance-report.json")
    parser.add_argument("--requests", type=int, default=20)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--confirm-isolated", action="store_true")
    args = parser.parse_args()
    try:
        if args.command == "prepare":
            result = prepare(args.env_file)
        else:
            if not args.confirm_isolated:
                raise ValueError("Explicit isolated target acknowledgement required")
            values = dict(line.split("=", 1) for line in Path(args.env_file).read_text().splitlines() if "=" in line)
            client = Client(args.url, values)
            if args.command=="run":client.login()
            else:
                if client.json("/api/health").get("instance")!="acceptance":raise ValueError("Isolated target required")
            if args.command == "run":
                if Path(args.report).exists():
                    raise ValueError("Report exists: use a new report path to retain prior test IDs")
                result = run(client, args.requests, args.workers)
                Path(args.report).parent.mkdir(parents=True, exist_ok=True)
                Path(args.report).write_text(json.dumps(result, indent=2), encoding="utf-8")
                Path(args.report).chmod(0o600)
            else:
                report = json.loads(Path(args.report).read_text(encoding="utf-8"))
                client.csrf=report["workspace_token"]
                result = verify(client, report)
                if args.command == "cleanup":
                    client.json('/api/workspaces/end',{},'POST')
                    result = {"ok": True, "removed_test_files": len(report["ids"])}
        # Keep credentials and original filenames out of the summary.
        print(json.dumps({key: value for key, value in result.items() if key not in ("ids", "sha256", "workspace_token")}, ensure_ascii=False))
    except Exception:
        print('{"ok":false,"error":"验收失败；核对实例角色、测试凭据、服务状态及报告路径"}')
        raise SystemExit(1)


if __name__ == "__main__":
    main()
