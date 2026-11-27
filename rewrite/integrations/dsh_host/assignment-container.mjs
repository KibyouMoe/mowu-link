import {createHash, randomUUID} from 'node:crypto';
import {closeSync, constants, fstatSync, fsyncSync, lstatSync, mkdirSync, openSync,
  readFileSync, readdirSync, renameSync, writeFileSync} from 'node:fs';
import path from 'node:path';
import {guid} from './session-preparation.mjs';
import {workspaceEngine} from './workspace-engine.mjs';
import {maximumSeedBytes, packProject, seedProject, workspaceSeed} from './workspace-seed.mjs';

const LABEL = 'io.mowulink.assignment';
const PROCESS_LIMIT = 64;
const MEMORY_LIMIT = 256 * 1024 * 1024;
const NANO_CPU_LIMIT = 1_000_000_000;
const TMPFS = {'/workspace':'rw,nosuid,nodev,size=64m,mode=1777','/tmp':'rw,nosuid,nodev,size=16m,mode=1777'};
const EXECUTABLE_TMPFS = {...TMPFS,'/workspace':'rw,nosuid,nodev,exec,size=64m,mode=1777'};

function mountOptions(record) {
  return record.workspace_exec===true ? EXECUTABLE_TMPFS : TMPFS;
}
const containerID = value => /^[a-f0-9]{64}$/.test(value);
const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex');

const MAX_ARTIFACT_BYTES = 16 * 1024 * 1024;
const readArtifact = String.raw`
import os
import stat
import sys

limit = int(sys.argv[1])
parts = sys.argv[2].split("/")
directory = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
fd = os.open("/workspace", directory)
try:
    for name in parts[:-1]:
        child = os.open(name, directory, dir_fd=fd)
        os.close(fd)
        fd = child
    with os.fdopen(os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd), "rb") as source:
        before = os.fstat(source.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > limit:
            raise ValueError("invalid artifact")
        data = source.read(limit + 1)
        after = os.fstat(source.fileno())
        if len(data) != before.st_size or (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise ValueError("artifact changed")
        sys.stdout.buffer.write(data)
finally:
    os.close(fd)
`;


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

export function assignmentContainers(runtime,session,config,files=null) {
  const {docker,socket,image,execute_workspace_files=false} = config;
  check(typeof image==='string' && /^sha256:[a-f0-9]{64}$/.test(image)
    && typeof execute_workspace_files==='boolean','invalid_assignment_container');
  const seed = workspaceSeed(config.seed,files);
  const directory = path.join(runtime,'assignment-containers');
  const cliConfig = path.join(directory,'docker-config');
  const {invoke,daemon} = workspaceEngine(docker,socket,cliConfig);
  if (!files) {
    privateDirectory(directory);
    privateDirectory(cliConfig);
  }

  const locks = new Map();
  const filename = aid=>path.join(directory,hash([session,aid])+'.json');
  const save = entry=>files ? files.write(filename(entry.assignment_id),entry)
                            : writeRecord(filename(entry.assignment_id),entry);
  async function scope(tid,aid,{existing=false}={}) {
    check(guid(tid) && guid(aid));
    const owner = await daemon();
    let record = files ? await files.read(filename(aid)) : readRecord(filename(aid));
    if (!record) {
      check(!existing,'assignment_workspace_unavailable');
      record = {session,task_id:tid,assignment_id:aid,daemon:owner,image,
        token:randomUUID(),phase:'unused',container_id:null,
        ...(seed ? {seed_sha256:seed.sha256} : {}),...(execute_workspace_files ? {workspace_exec:true} : {})};
      await save(record);
    }
    check(record.session===session && record.task_id===tid && record.assignment_id===aid &&
      record.daemon===owner && record.image===image && guid(record.token) &&
      ['unused','issuing','seeding','active','sealing','sealed'].includes(record.phase)
      && (record.seed_sha256===undefined || /^[a-f0-9]{64}$/.test(record.seed_sha256)) &&
      (record.workspace_exec===undefined || record.workspace_exec===true) &&
      (record.container_id===null || containerID(record.container_id)) &&
      (!['seeding','active'].includes(record.phase) || record.container_id!==null) &&
      (record.started_at===undefined || typeof record.started_at==='string') &&
      (record.phase!=='active' || !record.seed_sha256 || record.started_at));
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
    const ids = stdout.trim().split(/\r?\n/).filter(Boolean);
    if (!ids.length) return null;
    check(ids.length===1 && containerID(ids[0]) && (!record.container_id || record.container_id===ids[0]));
    const listing = await invoke(['container','inspect',ids[0]]);
    const [container] = JSON.parse(listing.stdout);
    const host = container?.HostConfig;
    const tmpfs = mountOptions(record);
    check(container?.Id===ids[0] && container.Image===image &&
      container.Config?.Labels?.[LABEL]===record.token && container.Config.User==='65534:65534' &&
      host?.NetworkMode==='none' && host.PidMode==='' && host.IpcMode==='private' &&
      host.ReadonlyRootfs===true && host.Privileged===false && !host.Binds?.length &&
      host.CapDrop?.includes('ALL') && !host.CapAdd?.length && host.SecurityOpt?.includes('no-new-privileges') &&
      !host.Devices?.length && !host.DeviceRequests?.length && host.RestartPolicy?.Name==='no' &&
      host.PidsLimit===PROCESS_LIMIT && host.Memory===MEMORY_LIMIT && host.NanoCpus===NANO_CPU_LIMIT &&
      Object.keys(host.Tmpfs ?? {}).length===2 && Object.entries(tmpfs).every(([key,value])=>host.Tmpfs[key]===value) &&
      container.Mounts?.every(mount=>mount.Type==='tmpfs' && Object.hasOwn(tmpfs,mount.Destination)));
    return container;
  }

  function running(container) {
    const state = container?.State;
    check(state?.Running===true && !state.Paused && !state.Restarting &&
      typeof state.StartedAt==='string' && state.StartedAt,'assignment_workspace_unavailable');
    return state.StartedAt;
  }

  async function ready(record,container) {
    const startedAt = running(container);
    check(record.started_at===undefined || record.started_at===startedAt,'assignment_workspace_restarted');
    if (record.started_at===undefined) {
      record.started_at = startedAt;
      await save(record);
    }
  }

  async function prepare(record,signal) {
    check(['unused','issuing','seeding','active'].includes(record.phase));
    check(record.seed_sha256===seed?.sha256,'workspace_seed_changed');
    check((record.workspace_exec===true)===execute_workspace_files,'workspace_execution_policy_changed');
    let container = await owned(record);
    if (record.phase==='active') {
      // Prior releases recorded active before the first start and had no start timestamp.
      if (record.started_at===undefined && container?.State?.Status==='created') {
        await invoke(['container','start',record.container_id]);
        container = await owned(record);
      }
      await ready(record,container);
      return record.container_id;
    }
    const seedBytes = await seed?.bytes();
    signal?.throwIfAborted();
    if (!container) {
      check(record.phase==='unused');
      const imageInfo = await invoke(['image','inspect',image]);
      const [base] = JSON.parse(imageInfo.stdout);
      check(base?.Id===image && base.Os==='linux' && !Object.keys(base.Config?.Volumes ?? {}).length,
        'invalid_assignment_image');
      record.phase = 'issuing';
      await save(record);
      const tmpfs = mountOptions(record);
      await invoke(['container','create','--pull','never','--name','mowulink-'+record.token,
        '--label',LABEL+'='+record.token,'--network','none','--ipc','private','--user','65534:65534',
        '--cap-drop','ALL','--security-opt','no-new-privileges','--read-only','--restart','no',
        '--tmpfs','/workspace:'+tmpfs['/workspace'],'--tmpfs','/tmp:'+tmpfs['/tmp'],
        '--pids-limit',String(PROCESS_LIMIT),'--memory',String(MEMORY_LIMIT),
        '--cpus',String(NANO_CPU_LIMIT/1_000_000_000),'--no-healthcheck','--workdir','/workspace',
        '--entrypoint','/bin/sleep',image,'infinity']);
      container = await owned(record);
      check(container);
    }
    record.container_id = container.Id;
    await save(record);
    if (container.State.Status==='created') {
      await invoke(['container','start',container.Id]);
      container = await owned(record);
    }
    const startedAt = running(container);
    if (seed) {
      record.phase = 'seeding';
      await save(record);
      await invoke(['container','exec','-i',container.Id,'python3','-I','-c',seedProject,
        String(maximumSeedBytes),seed.sha256],{stdin:seedBytes,timeout:20000,signal})
        .catch(()=>{ throw new Error('workspace_seed_unconfirmed'); });
      check(running(await owned(record))===startedAt,'assignment_workspace_restarted');
    }
    record.started_at = startedAt;
    record.phase = 'active';
    await save(record);
    return container.Id;
  }

  return {
    seeded:seed!==null,
    executeWorkspaceFiles:execute_workspace_files,
    async confirmEngine() { await daemon(); },
    async run(tid,aid,command,signal) {
      check(typeof command==='string' && command.trim() && command.length<=8192 && !command.includes('\0'),
        'invalid_assignment_command');
      const {result} = await serial(aid,async()=>{
        signal?.throwIfAborted();
        const id = await prepare(await scope(tid,aid),signal);
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
    async artifact(tid,aid,relative,signal) {
      check(typeof relative==='string' && Buffer.byteLength(relative)<=1024 &&
        !/[\\\x00-\x1f\x7f]/.test(relative),'invalid_workspace_artifact_path');
      const parts = relative.split('/');
      check(parts.every(part=>part && part!=='.' && part!=='..'),'invalid_workspace_artifact_path');
      return serial(aid,async()=>{
        signal?.throwIfAborted();
        const record = await scope(tid,aid);
        check(record.phase==='active' && record.container_id,'assignment_workspace_unavailable');
        const container = await owned(record);
        await ready(record,container);
        const {stdout:bytes} = await invoke(['container','exec',container.Id,'python3','-I','-c',readArtifact,
          String(MAX_ARTIFACT_BYTES),relative],{encoding:'buffer',maxBuffer:MAX_ARTIFACT_BYTES,signal})
          .catch(()=>{ throw new Error('workspace_artifact_unreadable'); });
        check(bytes.length<=MAX_ARTIFACT_BYTES,'workspace_artifact_too_large');
        return {name:parts.at(-1),size_bytes:bytes.length,
          sha256:createHash('sha256').update(bytes).digest('hex'),bytes};
      });
    },
    async checkpoint(tid,aid) {
      return serial(aid,async()=>{
        const record = await scope(tid,aid,{existing:true});
        check(record.phase==='active' && record.started_at,'assignment_workspace_unavailable');
        const container = await owned(record);
        check(running(container)===record.started_at,'assignment_workspace_restarted');
        const {stdout:archive} = await invoke(['container','exec',container.Id,'python3','-I','-c',
          packProject,String(maximumSeedBytes)],{encoding:'buffer',maxBuffer:maximumSeedBytes,timeout:30000})
          .catch(error=>{
            if (error.code===73) throw new Error('assignment_workspace_has_live_processes');
            throw new Error('workspace_project_checkpoint_failed');
          });
        check(running(await owned(record))===record.started_at,'assignment_workspace_restarted');
        check(await daemon()===record.daemon && archive.length>0);
        return {archive,sha256:createHash('sha256').update(archive).digest('hex'),
          container_id:record.container_id,started_at:record.started_at,image:record.image};
      });
    },
    pending() {
      if (files) return files.pending(session);
      const pending = [];
      for (const name of readdirSync(directory)) {
        if (!/^[a-f0-9]{64}\.json$/.test(name)) continue;
        const record = readRecord(path.join(directory,name));
        if (record?.session!==session || record.phase==='sealed') continue;
        check(guid(record.task_id) && guid(record.assignment_id) &&
          filename(record.assignment_id)===path.join(directory,name));
        pending.push({task_id:record.task_id,assignment_id:record.assignment_id});
      }
      return pending;
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
          await save(record);
          if (container) await invoke(['container','rm','--force',record.container_id]);
          check(await daemon()===record.daemon && !await owned(record));
          record.phase = 'sealed';
          await save(record);
        }
        return {container_id:record.container_id,receipt_id:hash(record),state:'removed'};
      });
    },
  };
}
