"""Synthetic FTP scanner; no real user documents are read."""
import argparse,base64,io,os,uuid
from ftplib import FTP
from concurrent.futures import ThreadPoolExecutor
PNG=base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAoAAAAUCAIAAAA7jDsBAAAAHUlEQVR4nGP8//8/A27AhEeOYVQaB2DCJcEwoqUBagcDJbd2MKEAAAAASUVORK5CYII=")
def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('--host',default='127.0.0.1')
    parser.add_argument('--port',type=int,default=21)
    parser.add_argument('--username',default='scanner')
    parser.add_argument('--count',type=int,default=10)
    parser.add_argument('--workers',type=int,default=1)
    parser.add_argument('--confirm-test-target',action='store_true',required=True)
    args=parser.parse_args()
    password=os.environ.get('SCANHUB_TEST_FTP_PASSWORD')
    if not password:parser.error('Set SCANHUB_TEST_FTP_PASSWORD')
    run_id=uuid.uuid4().hex[:8]
    def send(index):
        with FTP() as ftp:
            ftp.connect(args.host,args.port,timeout=30)
            ftp.login(args.username,password)
            ftp.storbinary(f'STOR test-{run_id}-{index}.png',io.BytesIO(PNG))
            print(f'FTP upload {index}: complete')
    with ThreadPoolExecutor(max_workers=args.workers) as pool:list(pool.map(send,range(args.count)))
if __name__=='__main__':main()
