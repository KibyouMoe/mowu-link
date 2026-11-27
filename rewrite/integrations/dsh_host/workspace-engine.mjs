import {execFile} from 'node:child_process';
import {lstatSync} from 'node:fs';
import path from 'node:path';
import {promisify} from 'node:util';

const exec = promisify(execFile);
const windowsHost = 'npipe:////./pipe/docker_engine';

function localHost(socket) {
  if (process.platform === 'win32') {
    // The documented Docker Desktop pipe preserves the engine's normal isolation policy.
    if (socket === windowsHost) return socket;
  } else if (typeof socket === 'string' && path.isAbsolute(socket) && lstatSync(socket,{throwIfNoEntry:false})?.isSocket()) {
    return 'unix://' + socket;
  }
  throw new Error('invalid_assignment_container');
}

export function workspaceEngine(docker,socket,cliConfig) {
  if (typeof docker !== 'string' || !path.isAbsolute(docker)) {
    throw new Error('invalid_assignment_container');
  }
  const host = localHost(socket);
  const windows = process.platform === 'win32';
  let env = {PATH:'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'};
  if (windows) {
    const system = process.env.SystemRoot;
    if (!/^[A-Za-z]:[\\/].+\.exe$/i.test(docker) || !lstatSync(docker,{throwIfNoEntry:false})?.isFile() ||
        !system || !/^[A-Za-z]:[\\/]/.test(system) || !/^[A-Za-z]:[\\/]/.test(cliConfig)) {
      throw new Error('invalid_assignment_container');
    }
    // libuv restores a missing USERPROFILE and home drive/path from the parent on Windows.
    env = {SystemRoot:system,PATH:path.join(system,'System32'),USERPROFILE:cliConfig,
           HOMEDRIVE:cliConfig.slice(0,2),HOMEPATH:cliConfig.slice(2),HOME:cliConfig,
           APPDATA:cliConfig,LOCALAPPDATA:cliConfig,TEMP:cliConfig,TMP:cliConfig};
  }

  function invoke(args,{stdin,...options}={}) {
    const result = exec(docker,['--config',cliConfig,'--host',host,...args],{
      cwd:path.dirname(cliConfig),env,windowsHide:true,timeout:7000,maxBuffer:131072,...options,
    });
    if (stdin !== undefined) {
      // A closed CLI confirms nothing. The container initializer checks all stdin before publishing.
      result.child.stdin.on('error',()=>{});
      result.child.stdin.end(stdin);
    }
    return result;
  }

  async function daemon() {
    const {stdout} = await invoke(['info','--format','{{.OSType}} {{.ID}}']);
    const id = /^linux ([a-zA-Z0-9:-]+)$/.exec(stdout.trim())?.[1];
    if (!id) throw new Error('native_cleanup_unconfirmed');
    return id;
  }

  return {invoke,daemon};
}
