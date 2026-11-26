import {execFile} from 'node:child_process';
import {lstatSync,readFileSync} from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import path from 'node:path';
import {fileURLToPath} from 'node:url';
import {promisify} from 'node:util';

const run = promisify(execFile);

const names = ['dsh-link.json','dsh-link.crt','dsh-link.key'];

export function privateDir(directory) {
  const info = lstatSync(directory);
  if (!path.isAbsolute(directory) || !info.isDirectory() || info.uid!==process.getuid() || (info.mode & 0o077)) {
    throw new Error('private_runtime_required');
  }
}

export async function privateWindowsRuntime(directory,{admissions=false,questions=false,mediaDigests=[]}={}) {
  if (!/^[A-Za-z]:[\\/]/.test(directory)) throw new Error('private_runtime_required');
  const system = process.env.SystemRoot;
  if (!system || !path.isAbsolute(system)) throw new Error('private_runtime_required');
  const powershell = path.join(system,'System32','WindowsPowerShell','v1.0','powershell.exe');
  const check = fileURLToPath(new URL('./check-local-link.ps1',import.meta.url));
  try {
    const args = ['-NoProfile','-NonInteractive','-File',check,directory];
    if (admissions) args.push('-CheckAdmissions');
    if (questions) args.push('-CheckQuestions');
    if (mediaDigests.length) args.push('-MediaDigests',mediaDigests.join(','));
    await run(powershell,args,{windowsHide:true,timeout:mediaDigests.length ? 5000 : 15000,maxBuffer:65536});
  } catch (error) {
    if (error.code===5) throw new Error('invalid_media_object');
    if (error.killed) throw new Error('dsh_link_acl_check_timeout');
    if (error.code===4) throw new Error(admissions ? 'dsh_admissions_file_inheritance_required' :
      'dsh_questions_file_inheritance_required');
    throw new Error(error.code===3 ? 'private_dsh_link_required' : 'dsh_link_acl_check_unavailable');
  }
}

async function verifiedCredentials(directory,options) {
  if (process.platform==='win32') await privateWindowsRuntime(directory,options);
  else privateDir(directory);
  return names.map(name=>{
    const file = path.join(directory,name);
    const info = lstatSync(file);
    if (!info.isFile() || (process.platform!=='win32' &&
        (info.uid!==process.getuid() || (info.mode & 0o077)))) throw new Error('private_dsh_link_required');
    return readFileSync(file);
  });
}

export function unixLink(directory) {
  if (process.platform==='win32') throw new Error('dsh_local_tls_required');
  privateDir(directory);
  return {
    server: handler=>http.createServer(handler),
    listen: (server,ready)=>server.listen(path.join(directory,'dsh.sock'),ready),
    request: (options,ready)=>{
      privateDir(directory);
      return http.request({...options,socketPath:path.join(directory,'worker.sock')},ready);
    },
  };
}

function callbackPort(directory,configured) {
  if (configured!==0) return configured;
  const locator = path.join(directory,'dsh-worker-port.json');
  const info = lstatSync(locator);
  if (!info.isFile() || info.size>128 || (process.platform!=='win32' &&
        (info.uid!==process.getuid() || (info.mode & 0o077)))) throw new Error('dsh_callback_unconfirmed');
  const saved = JSON.parse(readFileSync(locator));
  if (Object.keys(saved??{}).join(',')!=='port' || !validPort(saved.port)) throw new Error('dsh_callback_unconfirmed');
  return saved.port;
}

const validPort = port => Number.isInteger(port) && port>=1024 && port<=65535;

export async function tlsLink(directory,options={}) {
  const [data,cert,key] = await verifiedCredentials(directory,options);
  const cfg = JSON.parse(data);
  if (Object.keys(cfg??{}).sort().join(',')!=='host_port,worker_port' ||
      !validPort(cfg.host_port) || (cfg.worker_port!==0 && !validPort(cfg.worker_port)) ||
      cfg.host_port===cfg.worker_port) throw new Error('invalid_dsh_local_ports');

  const tls = {ca:cert,cert,key,rejectUnauthorized:true,minVersion:'TLSv1.2'};
  return {
    server: handler=>https.createServer({...tls,requestCert:true,handshakeTimeout:8000},handler),
    listen: (server,ready)=>server.listen(cfg.host_port,'127.0.0.1',ready),
    request: (options,ready)=>https.request({...options,...tls,hostname:'127.0.0.1',port:callbackPort(directory,cfg.worker_port),
      agent:false},ready),
  };
}
