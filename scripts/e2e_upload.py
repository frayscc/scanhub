"""Send synthetic fixtures to the browser-test FTP instance only."""
from ftplib import FTP
import io
from pathlib import Path
import sys
import json
import urllib.request

opener=urllib.request.build_opener(urllib.request.ProxyHandler({}))
with opener.open('http://127.0.0.1:18767/api/health',timeout=5) as response:
    assert json.load(response)['instance']=='acceptance'
data=(Path(__file__).resolve().parents[1]/'frontend/e2e/scan.png').read_bytes()
with FTP() as ftp:
    ftp.connect('127.0.0.1',18768,timeout=10)
    ftp.login('scanner','isolated-browser-ftp-password')
    for name in sys.argv[1:]:ftp.storbinary('STOR '+name,io.BytesIO(data))
