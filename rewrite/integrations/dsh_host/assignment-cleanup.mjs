import {createHash} from 'node:crypto';
import {closeSync, constants, fstatSync, fsyncSync, lstatSync, mkdirSync, openSync, readFileSync, writeFileSync} from 'node:fs';
import path from 'node:path';
import {isDeepStrictEqual} from 'node:util';
import {guid} from './session-preparation.mjs';

const taskInputs = new Set(['task','task_update','task_probe','question_answer']);
const safeTools = new Set(['worker_report','worker_ask','worker_list_peers','worker_peer_status','worker_peer_inbox']);
const nativeID = value => typeof value==='string' && /^mowulink-[a-f0-9]{64}$/.test(value);
const hash = text => createHash('sha256').update(text).digest('hex');

function check(value) {
  if (!value) throw new Error('native_cleanup_unconfirmed');
}

function scopeInput(message) {
  const prefix = '魔物链接投递\n';
  const text = message?.content?.find(item=>item.type==='text' && typeof item.text==='string'
    && item.text.startsWith(prefix))?.text;
  if (!text) return null;
  const raw = text.slice(prefix.length);
  try {
    return {payload:JSON.parse(raw),digest:hash(raw)};
  } catch {
    return null;
  }
}

function recordFile(directory,id) {
  check(nativeID(id));
  return path.join(directory,id+'.json');
}

function admission(directory,id) {
  let fd;
  try {
    fd = openSync(recordFile(directory,id),constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
    const info = fstatSync(fd);
    check(info.isFile() && info.uid===process.getuid() && !(info.mode & 0o077) && info.size<=4096);
    return JSON.parse(readFileSync(fd,'utf8'));
  } catch {
    throw new Error('native_cleanup_unconfirmed');
  } finally {
    if (fd!==undefined) closeSync(fd);
  }
}

function saveAdmission(directory,entry) {
  let fd;
  try {
    fd = openSync(recordFile(directory,entry.native_id),'wx',0o600);
    writeFileSync(fd,JSON.stringify(entry),{encoding:'utf8'});
    fsyncSync(fd);
    const parent = openSync(directory,'r');
    try { fsyncSync(parent); }
    finally { closeSync(parent); }
  } catch (error) {
    if (error.code!=='EEXIST') throw error;
    check(isDeepStrictEqual(admission(directory,entry.native_id),entry));
  } finally {
    if (fd!==undefined) closeSync(fd);
  }
}

function readOnlyDirectory(directory) {
  const dir = path.join(directory,'read-only-admissions');
  try { mkdirSync(dir,{mode:0o700}); }
  catch (error) { if (error.code!=='EEXIST') throw error; }
  const info = lstatSync(dir);
  check(info.isDirectory() && info.uid===process.getuid() && !(info.mode & 0o077));
  return dir;
}

function verifyAdmission(admissions,message,session,aid,ref) {
  check(message.id===ref.native_id);
  const record = admissions.read(ref.native_id);
  check(isDeepStrictEqual(record,{session,native_id:ref.native_id,delivery_id:ref.delivery_id,digest:ref.digest,
    assignment_id:aid,task_id:ref.task_id,kind:ref.kind}));
  const envelope = scopeInput(message);
  const origin = envelope?.payload;
  check(envelope?.digest===ref.digest && message.id==='mowulink-'+hash(JSON.stringify([session,ref.delivery_id,ref.digest]))
    && origin?.kind===ref.kind && origin?.task_id===ref.task_id && origin?.assignment_id===aid);
}

function inboxMessages(agent) {
  check(agent.inbox && Array.isArray(agent.inbox.nextTurn) && Array.isArray(agent.inbox.nextStep));
  return [...agent.inbox.nextTurn,...agent.inbox.nextStep];
}

function canceledInputs(events,wanted) {
  const queues = {'next-turn':[],'next-step':[]};
  const removed = new Map();
  for (const event of events) {
    if (event.type!=='agent/inbox/spliced') continue;
    const {target,start,inserted,removedCount=0,outcome} = event.data;
    const items = queues[target];
    check(Array.isArray(items) && Array.isArray(inserted) && Number.isSafeInteger(start) && start>=0
      && Number.isSafeInteger(removedCount) && removedCount>=0 && start+removedCount<=items.length);
    const changes = items.splice(start,removedCount,...inserted);
    if (outcome==='canceled') {
      for (const message of changes) {
        if (!wanted.has(message.id)) continue;
        check(!removed.has(message.id));
        removed.set(message.id,{message,seq:event.seq});
      }
    }
  }
  return removed;
}

function fileAdmissions(runtimeDir) {
  const directory = readOnlyDirectory(runtimeDir);
  return {
    read: id => admission(directory,id),
    save: entry => saveAdmission(directory,entry),
  };
}

export function assignmentCleanup(ctx,agent,runtimeDir,workspace=null,admissions=fileAdmissions(runtimeDir)) {
  const session = agent.session.id;
  const registered = new Map();
  const allowed = workspace ? new Set([...safeTools,'worker_workspace_shell','worker_workspace_export']) : safeTools;
  ctx.on('tools/pre-execute',(exec,next)=>{
    if (exec.agent!==agent) return next();
    if (registered.has(exec.name) && ctx.get('tools').get(exec.name,agent)===registered.get(exec.name)) return next();
    return Promise.resolve({kind:'reject',reason:workspace ? '这个独立工位仅执行已登记的工具。' : '这个独立只读工位仅执行已登记的查询与工单回报工具。'});
  },{prepend:true});
  function workspaceScope(message) {
    const origin = scopeInput(message)?.payload;
    check(workspace && nativeID(message.id) && taskInputs.has(origin?.kind));
    const ref = admissions.read(message.id);
    verifyAdmission(admissions,message,session,origin.assignment_id,ref);
    return {session,task_id:origin.task_id,assignment_id:origin.assignment_id};
  }
  return {
    workspaceScope,
    bindTools() {
      for (const name of allowed) {
        const tool = ctx.get('tools').get(name,agent);
        if (tool) registered.set(name,tool);
      }
    },
    admit(request,expected) {
      const {kind,assignment_id,task_id} = JSON.parse(request.payload_json);
      if (!taskInputs.has(kind) && !['task.cancel','task.transfer_out'].includes(kind)) return;
      check(guid(task_id) && guid(assignment_id));
      admissions.save({session,native_id:expected.id,delivery_id:request.delivery_id,
        digest:request.digest,assignment_id,task_id,kind});
    },
    withdraw(request) {
      const {assignment_id:aid,task_id:tid} = JSON.parse(request.payload_json);
      for (const message of inboxMessages(agent)) {
        const envelope = scopeInput(message);
        const origin = envelope?.payload;
        if (!nativeID(message.id) || !taskInputs.has(origin?.kind)
            || origin.task_id!==tid || origin.assignment_id!==aid) continue;
        try {
          verifyAdmission(admissions,message,session,aid,admissions.read(message.id));
        } catch {
          continue;
        }
        check(typeof agent.inbox.remove==='function' && agent.inbox.remove(message.id));
      }
    },
    stopWorkspace(request) {
      if (!workspace) return;
      const {task_id,assignment_id} = JSON.parse(request.payload_json);
      // Notify the original session even if the container engine cannot yet confirm cleanup.
      workspace.seal(task_id,assignment_id).catch(()=>{});
    },
    async proof(request,history,events) {
      const {assignment_id:aid,kind,bindings} = request;
      check(guid(aid) && ['task.cancel','task.transfer_out'].includes(kind) && Array.isArray(bindings));
      const controls = bindings.filter(ref=>ref.kind===kind);
      const originals = bindings.filter(ref=>taskInputs.has(ref.kind));
      check(controls.length && originals.filter(ref=>ref.kind==='task').length===1
        && controls.length+originals.length===bindings.length);
      const sources = new Map(bindings.map(ref=>[ref.native_id,ref]));
      check(sources.size===bindings.length && bindings.every(ref=>ref.task_id===originals[0].task_id));
      const scoped = history.filter(turn=>turn.users.some(user=>{
        const payload = scopeInput(user)?.payload;
        return payload?.assignment_id===aid && (taskInputs.has(payload.kind) || payload.kind===kind);
      }));
      const completedInputs = new Set();
      check(scoped.every(turn=>{
        const ref = sources.get(turn.users[0].id);
        if (!ref || !Array.isArray(turn.calls) || turn.calls.some(call=>!allowed.has(call.name))) return false;
        check(turn.users.length===1 && turn.id===ref.turn_id && !completedInputs.has(ref.native_id));
        verifyAdmission(admissions,turn.users[0],session,aid,ref);
        completedInputs.add(ref.native_id);
        return true;
      }));
      const last = scoped.at(-1);
      const control = last && sources.get(last.users[0].id);
      check(control?.kind===kind);
      const finished = scoped.filter(turn=>taskInputs.has(sources.get(turn.users[0].id).kind));
      const boundary = Number(last.id.split('/')[0]);
      check(finished.every(turn=>!turn.open && Number.isSafeInteger(turn.end?.seq) && turn.end.seq < boundary));
      check(!inboxMessages(agent).some(row=>scopeInput(row)?.payload?.assignment_id===aid));
      const pending = bindings.filter(ref=>!completedInputs.has(ref.native_id));
      const removed = pending.length ? canceledInputs(events,new Set(pending.map(ref=>ref.native_id))) : new Map();
      const withdrawn_inputs = pending.map(ref=>{
        const found = removed.get(ref.native_id);
        check(taskInputs.has(ref.kind) && ref.turn_id===null && found && found.seq<boundary);
        verifyAdmission(admissions,found.message,session,aid,ref);
        return {native_id:ref.native_id,delivery_id:ref.delivery_id,discard_seq:found.seq};
      });
      const stopped_turns = finished.map(turn=>({turn_id:turn.id,end_seq:turn.end.seq,reason:turn.end.reason?.kind}));
      const removedWorkspace = workspace ? await workspace.seal(control.task_id,aid).catch(()=>{
        throw new Error('native_cleanup_unconfirmed');
      }) : null;
      const proofKey = [session,aid,kind,control.native_id,stopped_turns,withdrawn_inputs];
      if (removedWorkspace) proofKey.push(removedWorkspace.receipt_id);
      return {session,assignment_id:aid,kind,stopped:true,cleaned:true,scope:workspace?'assignment_container':'read_only',
        receipt_id:hash(JSON.stringify(proofKey)),
        ...(removedWorkspace ? {workspace:removedWorkspace} : {}),
        control_turn_id:last.id,stopped_turns,withdrawn_inputs};
    },
  };
}
