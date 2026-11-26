"""Read-only FTP control/passive-channel check using container configuration."""
from ftplib import FTP,parse227
import json
import os
import socket


def probe():
    result={'public_host':os.getenv('FTP_PUBLIC_HOST') or 'auto',
            'passive_range':os.getenv('FTP_PASSIVE_MIN','21000')+'-'+os.getenv('FTP_PASSIVE_MAX','21010')}
    stage='control'
    try:
        with FTP() as ftp:
            ftp.connect('127.0.0.1',int(os.getenv('FTP_PORT','2121')),timeout=5)
            stage='login'
            ftp.login(os.getenv('FTP_USERNAME','scanner'),os.getenv('FTP_PASSWORD',''))
            result['login']='ok'
            stage='root_directory'
            ftp.cwd('/')
            result['root_directory']='ok'
            stage='passive_offer'
            host,port=parse227(ftp.sendcmd('PASV'))
            result.update(passive_host=host,passive_port=port)
            stage='passive_connection'
            with socket.create_connection((host,port),timeout=5):pass
            result['passive_connection']='ok'
        result['ok']=True
    except Exception as exc:
        result.update(ok=False,failed_stage=stage,error_type=type(exc).__name__)
    return result


if __name__=='__main__':
    print(json.dumps(probe(),ensure_ascii=False))
