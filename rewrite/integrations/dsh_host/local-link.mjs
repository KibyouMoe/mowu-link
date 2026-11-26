import {execFileSync} from 'node:child_process';
import {lstatSync,readFileSync} from 'node:fs';
import http from 'node:http';
import https from 'node:https';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const names = ['dsh-link.json','dsh-link.crt','dsh-link.key'];

export function privateDir(directory) {
  const info = lstatSync(directory);
  if (!path.isAbsolute(directory) || !info.isDirectory() || info.uid!==process.getuid() || (info.mode & 0o077)) {
    throw new Error('private_runtime_required');
  }
}

function verifiedCredentials(directory) {
  if (process.platform==='win32') {
    if (!/^[A-Za-z]:[\\/]/.test(directory)) throw new Error('private_runtime_required');
    const system = process.env.SystemRoot;
    if (!system || !path.isAbsolute(system)) throw new Error('private_runtime_required');
    const powershell = path.join(system,'System32','WindowsPowerShell','v1.0','powershell.exe');
    const check = fileURLToPath(new URL('./check-local-link.ps1',import.meta.url));
    try {
      execFileSync(powershell,['-NoProfile','-NonInteractive','-File',check,directory],
        {windowsHide:true,stdio:'ignore',timeout:20000});
    } catch (error) {
      if (error.code==='ETIMEDOUT') throw new Error('dsh_link_acl_check_timeout');
      throw new Error(error.status===3 ? 'private_dsh_link_required' : 'dsh_link_acl_check_unavailable');
    }
  } else {
    privateDir(directory);
  }
  return names.map(name=>{
    const file = path.join(directory,name);
    const info = lstatSync(file);
    if (!info.isFile() || (process.platform!=='win32' &&
        (info.uid!==process.getuid() || (info.mode & 0o077)))) throw new Error('private_dsh_link_required');
    return readFileSync(file);
  });
}

export function localLink(directory,useTls) {
  if (!useTls && process.platform==='win32') throw new Error('dsh_local_tls_required');
  if (!useTls) {
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
  const [data,cert,key] = verifiedCredentials(directory);
  const cfg = JSON.parse(data);
  if (Object.keys(cfg??{}).sort().join(',')!=='host_port,worker_port' ||
      !Object.values(cfg).every(port=>Number.isInteger(port) && port>=1024 && port<=65535) ||
      cfg.host_port===cfg.worker_port) throw new Error('invalid_dsh_local_ports');

  const tls = {ca:cert,cert,key,rejectUnauthorized:true,minVersion:'TLSv1.2'};
  return {
    server: handler=>https.createServer({...tls,requestCert:true,handshakeTimeout:8000},handler),
    listen: (server,ready)=>server.listen(cfg.host_port,'127.0.0.1',ready),
    request: (options,ready)=>https.request({...options,...tls,hostname:'127.0.0.1',port:cfg.worker_port,agent:false},ready),
  };
}
