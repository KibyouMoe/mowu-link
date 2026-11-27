import {spawn} from 'node:child_process';
import {lstatSync} from 'node:fs';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import readline from 'node:readline';
import {privateWindowsEnvironment} from './dsh_host/workspace-engine.mjs';

const helper = fileURLToPath(new URL('../windows_workspace_files.py',import.meta.url));

async function bounded(operation,milliseconds) {
  let timer;
  try {
    return await Promise.race([operation,new Promise((_,reject) => {
      timer = setTimeout(()=>reject(new Error('assignment_workspace_unconfirmed')),milliseconds);
    })]);
  } finally {
    clearTimeout(timer);
  }
}

export function windowsWorkspaceFiles(runtime,python) {
  if (process.platform !== 'win32' || typeof python !== 'string' || !/^[A-Za-z]:[\\/].+\.exe$/i.test(python) ||
      !lstatSync(python,{throwIfNoEntry:false})?.isFile()) throw new Error('private_workspace_runtime_required');
  const env = privateWindowsEnvironment(runtime);

  let child, reader, exited;
  let tail = Promise.resolve();
  let failed = false, closing = false;

  async function read() {
    const {value,done} = await bounded(reader.next(),15000);
    if (done) throw new Error('assignment_workspace_unconfirmed');
    return JSON.parse(value);
  }

  function call(operation,parameter,data) {
    if (closing) return Promise.reject(new Error('assignment_workspace_unconfirmed'));
    const request = tail.then(async ()=>{
      if (failed) throw new Error('assignment_workspace_unconfirmed');
      let response;
      try {
        if (!child) {
          child = spawn(python,['-I','-B','-S',helper,runtime],{
            cwd:runtime,env,windowsHide:true,stdio:['pipe','pipe','ignore'],
          });
          child.on('error',()=>{ failed=true; });
          child.stdin.on('error',()=>{ failed=true; });
          exited = new Promise(resolve=>child.once('close',resolve));
          reader = readline.createInterface({input:child.stdout,crlfDelay:Infinity})[Symbol.asyncIterator]();
          if ((await read()).ready !== true) throw new Error('assignment_workspace_unconfirmed');
        }
        child.stdin.write(JSON.stringify({operation,parameter,data})+'\n');
        response = await read();
      } catch {
        failed = true;
        child?.kill();
        throw new Error('assignment_workspace_unconfirmed');
      }
      if (!Object.hasOwn(response,'value')) throw new Error('assignment_workspace_unconfirmed');
      return response.value;
    });
    tail = request.catch(()=>{});
    return request;
  }

  return {
    async prepare() { await call('prepare'); },
    async read(filename) { return call('read',path.basename(filename)); },
    async write(filename,record) { await call('write',path.basename(filename),JSON.stringify(record)); },
    async pending(session) {
      return (await call('pending')).filter(row=>row.session===session)
        .map(({task_id,assignment_id})=>({task_id,assignment_id}));
    },
    async seed(filename) { return Buffer.from(await call('seed',filename),'base64'); },
    async close() {
      closing = true;
      await tail;
      if (!child) return;
      child.stdin.end();
      try { await bounded(exited,3000); }
      catch { child.kill(); await exited; }
    },
  };
}
