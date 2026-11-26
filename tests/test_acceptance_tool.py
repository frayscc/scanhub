"""Real HTTP + process restart + offline backup/restore on an isolated data directory."""
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time

import pytest

from scanhub.maintenance import backup, restore
from scripts.acceptance import Client, prepare


def test_full_acceptance_restart_restore_and_cleanup(tmp_path):
    env_file=tmp_path/"acceptance.env"
    prepare(env_file)
    values=dict(line.split("=",1) for line in env_file.read_text().splitlines())
    environment=dict(os.environ)|values|{"DATA_DIR":str(tmp_path/"data"),"SCANHUB_INSTANCE":"acceptance"}
    with socket.socket() as sock:
        sock.bind(("127.0.0.1",0))
        port=sock.getsockname()[1]
    url=f"http://127.0.0.1:{port}"
    script=Path(__file__).resolve().parents[1]/"scripts"/"acceptance.py"
    report=tmp_path/"report.json"
    def start(data):
        environment["DATA_DIR"]=str(data)
        process=subprocess.Popen([sys.executable,"-m","uvicorn","scanhub.app:app","--host","127.0.0.1",
            "--port",str(port),"--no-access-log"],env=environment,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
        client=Client(url,values)
        for _ in range(100):
            try:
                if client.json("/api/ready")["status"]=="ready": return process
            except Exception:
                pass
            if process.poll() is not None: break
            time.sleep(.05)
        process.terminate()
        process.wait(timeout=10)
        raise AssertionError("Isolated test service failed to start")
    def command(action):
        result=subprocess.run([sys.executable,str(script),action,"--env-file",str(env_file),"--url",url,
            "--report",str(report),"--requests","6","--workers","2","--confirm-isolated"],
            capture_output=True,text=True,timeout=60)
        assert result.returncode==0,result.stdout
        assert json.loads(result.stdout)["ok"]
    original=tmp_path/"data"
    process=start(original)
    try:
        command("run")
    finally:
        process.terminate();process.wait(timeout=10)
    process=start(original)
    try:
        command("verify")
    finally:
        process.terminate();process.wait(timeout=10)
    archive=tmp_path/"offline.zip"
    backup(original,archive,True)
    recovered=tmp_path/"recovered"
    restore(archive,recovered,True)
    process=start(recovered)
    try:
        command("verify")
        command("cleanup")
    finally:
        process.terminate();process.wait(timeout=10)


def test_acceptance_refuses_production_before_login(monkeypatch):
    client=Client("http://unused",{"ADMIN_USERNAME":"scanhub-acceptance"})
    calls=[]
    def json_response(path,*args,**kwargs):
        calls.append(path)
        return {"instance":"production"}
    monkeypatch.setattr(client,"json",json_response)
    with pytest.raises(ValueError): client.login()
    assert calls==["/api/health"]
