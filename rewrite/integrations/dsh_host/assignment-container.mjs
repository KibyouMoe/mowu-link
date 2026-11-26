import {execFile} from 'node:child_process';
import {createHash, randomUUID} from 'node:crypto';
import {closeSync, constants, fstatSync, fsyncSync, lstatSync, mkdirSync, openSync,
  readFileSync, renameSync, writeFileSync} from 'node:fs';
import path from 'node:path';
import {promisify} from 'node:util';
import {guid} from './session-preparation.mjs';

const exec = promisify(execFile);
const LABEL = 'io.mowulink.assignment';
const TMPFS = {'/workspace':'rw,nosuid,nodev,size=64m,mode=1777','/tmp':'rw,nosuid,nodev,size=16m,mode=1777'};
const containerID = value => /^[a-f0-9]{64}$/.test(value);
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');

function check(test,code='native_cleanup_unconfirmed') {
  if (!test) throw new Error(code);
}

function privateDirectory(directory) {
  try { mkdirSync(directory,{mode:0o700}); }
  catch (error) { if (error.code!=='EEXIST') throw error; }
  const info = lstatSync(directory);
  check(info.isDirectory() && info.uid===process.getuid() && !(info.mode&0o077),'private_runtime_required');
}

function readRecord(filename) {
  let fd;
  try {
    fd = openSync(filename,constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
    const info = fstatSync(fd);
    check(info.isFile() && info.uid===process.getuid() && !(info.mode&0o077) && info.size<=4096);
    return JSON.parse(readFileSync(fd,'utf8'));
  } catch (error) {
    if (error.code==='ENOENT') return null;
    throw error;
  } finally {
    if (fd!==undefined) closeSync(fd);
  }
}

function writeRecord(filename,value) {
  const temp = filename+'.'+randomUUID();
  const fd = openSync(temp,'wx',0o600);
  try {
    writeFileSync(fd,JSON.stringify(value),{encoding:'utf8'});
    fsyncSync(fd);
  } finally { closeSync(fd); }
  renameSync(temp,filename);
  const parent = openSync(path.dirname(filename),'r');
  try { fsyncSync(parent); }
  finally { closeSync(parent); }
}

export function assignmentContainers(runtime,session,config) {
  const {docker,socket,image} = config;
  check(typeof docker==='string' && path.isAbsolute(docker)
    && typeof socket==='string' && path.isAbsolute(socket) && lstatSync(socket).isSocket()
    && typeof image==='string' && /^sha256:[a-f0-9]{64}$/.test(image),'invalid_assignment_container');
  const directory = path.join(runtime,'assignment-containers');
  const cliConfig = path.join(directory,'docker-config');
  privateDirectory(directory);
  privateDirectory(cliConfig);

  const locks = new Map();
  const filename = aid=>path.join(directory,hash([session,aid])+'.json');
  const save = entry=>writeRecord(filename(entry.assignment_id),entry);
  const invoke = (args,options={})=>exec(docker,['--config',cliConfig,'--host','unix://'+socket,...args],{
    cwd:directory,env:{PATH:'/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'},timeout:7000,
    maxBuffer:131072,...options,
  });

  async function daemon() {
    const {stdout} = await invoke(['info','--format','{{.OSType}} {{.ID}}']);
    const id = /^linux ([a-zA-Z0-9:-]+)$/.exec(stdout.trim())?.[1];
    check(id);
    return id;
  }

  async function scope(tid,aid) {
    check(guid(tid) && guid(aid));
    const owner = await daemon();
    let record = readRecord(filename(aid));
    if (!record) {
      record = {session,task_id:tid,assignment_id:aid,daemon:owner,image,
        token:randomUUID(),phase:'unused',container_id:null};
      save(record);
    }
    check(record.session===session && record.task_id===tid && record.assignment_id===aid &&
      record.daemon===owner && record.image===image && guid(record.token) &&
      ['unused','issuing','active','sealing','sealed'].includes(record.phase) &&
      (record.container_id===null || containerID(record.container_id)) &&
      (record.phase!=='active' || record.container_id!==null));
    return record;
  }

  function serial(aid,operation) {
    const work = (locks.get(aid) ?? Promise.resolve()).catch(()=>{}).then(operation);
    locks.set(aid,work);
    return work.finally(()=>{ if (locks.get(aid)===work) locks.delete(aid); });
  }

  async function owned(record) {
    const {stdout} = await invoke(['container','ls','--all','--no-trunc','--filter',
      'label='+LABEL+'='+record.token,'--format','{{.ID}}']);
    const ids = stdout.trim().split('\n').filter(Boolean);
    if (!ids.length) return null;
    check(ids.length===1 && containerID(ids[0]) && (!record.container_id || record.container_id===ids[0]));
    const listing = await invoke(['container','inspect',ids[0]]);
    const [container] = JSON.parse(listing.stdout);
    const host = container?.HostConfig;
    check(container?.Id===ids[0] && container.Image===image &&
      container.Config?.Labels?.[LABEL]===record.token && container.Config.User==='65534:65534' &&
      host?.NetworkMode==='none' && host.PidMode==='' && host.IpcMode==='private' &&
      host.ReadonlyRootfs===true && host.Privileged===false && !host.Binds?.length &&
      host.CapDrop?.includes('ALL') && host.SecurityOpt?.includes('no-new-privileges') &&
      Object.keys(host.Tmpfs ?? {}).length===2 && Object.entries(TMPFS).every(([key,value])=>host.Tmpfs[key]===value) &&
      container.Mounts?.every(mount=>mount.Type==='tmpfs' && Object.hasOwn(TMPFS,mount.Destination)));
    return container;
  }

  async function prepare(record) {
    check(['unused','issuing','active'].includes(record.phase));
    let container = await owned(record);
    if (!container) {
      check(record.phase==='unused');
      const imageInfo = await invoke(['image','inspect',image]);
      const [base] = JSON.parse(imageInfo.stdout);
      check(base?.Id===image && base.Os==='linux' && !Object.keys(base.Config?.Volumes ?? {}).length,
        'invalid_assignment_image');
      record.phase = 'issuing';
      save(record);
      await invoke(['container','create','--pull','never','--name','mowulink-'+record.token,
        '--label',LABEL+'='+record.token,'--network','none','--ipc','private','--user','65534:65534',
        '--cap-drop','ALL','--security-opt','no-new-privileges','--read-only','--restart','no',
        '--tmpfs','/workspace:'+TMPFS['/workspace'],'--tmpfs','/tmp:'+TMPFS['/tmp'],
        '--pids-limit','64','--memory','256m','--cpus','1','--no-healthcheck','--workdir','/workspace',
        '--entrypoint','/bin/sleep',image,'infinity']);
      container = await owned(record);
      check(container);
    }
    record.container_id = container.Id;
    record.phase = 'active';
    save(record);
    if (container.State.Status==='created') {
      await invoke(['container','start',container.Id]);
      container = await owned(record);
    }
    check(container?.State?.Running===true && !container.State.Paused && !container.State.Restarting);
    return container.Id;
  }

  return {
    async run(tid,aid,command,signal) {
      check(typeof command==='string' && command.trim() && command.length<=8192 && !command.includes('\0'),
        'invalid_assignment_command');
      const {result} = await serial(aid,async()=>{
        signal?.throwIfAborted();
        const id = await prepare(await scope(tid,aid));
        signal?.throwIfAborted();
        const result = invoke(['container','exec',id,'/bin/sh','-c',command],{timeout:30000,signal})
          .then(({stdout,stderr})=>({exit_code:0,stdout,stderr}),error=>{
            if (typeof error.code==='number' && !error.killed) {
              return {exit_code:error.code,stdout:error.stdout,stderr:error.stderr};
            }
            throw new Error('assignment_command_unconfirmed',{cause:error});
          });
        return {result};
      });
      return result;
    },
    async seal(tid,aid) {
      return serial(aid,async()=>{
        const record = await scope(tid,aid);
        const container = await owned(record);
        // A create with an uncertain response but no observed identity cannot prove absence.
        check(!(record.phase==='issuing' && !container));
        if (record.phase==='sealed') {
          check(!container);
        } else {
          if (container) record.container_id = container.Id;
          record.phase = 'sealing';
          save(record);
          if (container) await invoke(['container','rm','--force',record.container_id]);
          check(await daemon()===record.daemon && !await owned(record));
          record.phase = 'sealed';
          save(record);
        }
        return {container_id:record.container_id,receipt_id:hash(record),state:'removed'};
      });
    },
  };
}
