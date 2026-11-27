import {execFile} from 'node:child_process';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';

const exec = promisify(execFile);
const helper = fileURLToPath(new URL('../windows_workspace_files.py',import.meta.url));

export function windowsWorkspaceFiles(runtime,python) {
  if (process.platform !== 'win32' || !path.isAbsolute(python ?? '') ||
      !/^[A-Za-z]:[\\/]/.test(runtime)) throw new Error('private_workspace_runtime_required');

  async function call(operation,args=[],input,options={}) {
    const run = exec(python,['-I','-B','-S',helper,runtime,operation,...args],{
      cwd:runtime,windowsHide:true,timeout:15000,maxBuffer:1048576,...options,
    });
    if (input !== undefined) {
      run.child.stdin.on('error',()=>{});
      run.child.stdin.end(input);
    }
    try { return (await run).stdout; }
    catch { throw new Error('assignment_workspace_unconfirmed'); }
  }

  return {
    async prepare() { await call('prepare'); },
    async read(filename) { return JSON.parse(await call('read',[path.basename(filename)])); },
    async write(filename,record) { await call('write',[path.basename(filename)],JSON.stringify(record)); },
    async pending(session) {
      return JSON.parse(await call('pending')).filter(row=>row.session===session)
        .map(({task_id,assignment_id})=>({task_id,assignment_id}));
    },
    async seed(filename) { return call('seed',[filename],undefined,{encoding:'buffer',maxBuffer:24*1024*1024}); },
  };
}
