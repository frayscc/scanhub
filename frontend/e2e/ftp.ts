import { execFile } from 'node:child_process'
import { promisify } from 'node:util'
import path from 'node:path'
export async function uploadFiles(names:string[]){
 const python=path.resolve(process.platform==='win32'?'../.venv/Scripts/python.exe':'../.venv/bin/python')
 await promisify(execFile)(python,[path.resolve('../scripts/e2e_upload.py'),...names])
}
