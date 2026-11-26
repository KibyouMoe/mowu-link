import { createHash } from 'node:crypto';
import { readFileSync, readdirSync, lstatSync, realpathSync, constants } from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import { admitMedia } from './media.mjs';
import { baseline, displayImage, nextDisplay } from './display.mjs';
import { changeModel, modelView } from './model-view.mjs';
import { changeGoal, goalView } from './goal-view.mjs';
import { compaction } from './compaction.mjs';
import { foregroundRecords, installForegroundQuestions, questionRequest } from './questions.mjs';
import { browseSessions, catalogSessions } from './catalog.mjs';
import { approvalProof, installCommandApprovals } from './command-approvals.mjs';
import { guid, prepareSession, selection } from './session-preparation.mjs';
import { assignmentCleanup } from './assignment-cleanup.mjs';
import { assignmentContainers } from './assignment-container.mjs';

const tools = JSON.parse(readFileSync(new URL('../../agent-toolset.json', import.meta.url)));
const sha = value => createHash('sha256').update(value).digest('hex');
const limit = 1024 * 1024;

function check(condition, code) {
  if (!condition) throw new Error(code);
}

async function input(request, ctx, agent, directory) {
  const {session, delivery_id, payload_json, digest} = request;
  check(typeof payload_json === 'string' && sha(payload_json) === digest, 'delivery_digest_mismatch');
  check(typeof delivery_id === 'string' && delivery_id.length > 0, 'invalid_delivery_id');
  const payload = JSON.parse(payload_json);
  const id = 'mowulink-' + sha(JSON.stringify([session,delivery_id,digest]));
  if (payload.kind === 'queued_chat') check(payload.session === session, 'queued_session_mismatch');
  if (payload.kind === 'trainer_answer') check(payload.session === session, 'wrong_native_session');
  const text = ['chat','queued_chat'].includes(payload.kind) ? payload.text : '魔物链接投递\n' + payload_json;
  const media = payload.attachments;
  check(typeof text === 'string' && (text.trim() || media?.length), 'empty_input');
  const parts = text ? [{type:'text',text}] : [];
  const content = media?.length ? await admitMedia(ctx,agent,directory,media,parts,request.operation==='deliver') : parts;
  return {id, role:'user', content, source:{kind:'user'}};
}

function turns(events) {
  const result = [];
  let current;
  for (const event of events) {
    if (event.type === 'turn/start') {
      current = {id:String(event.seq) + '/' + event.data.turn, number:event.data.turn, users:[], calls:[], open:true};
      result.push(current);
    } else if (current && event.type === 'turn/end') {
      current.open = false;
      current.end = {seq:event.seq,reason:event.data.reason};
      current = undefined;
    } else if (current && event.type === 'user/message' && event.data.source?.kind !== 'runtime-context') {
      current.users.push(event.data);
    } else if (current && event.type === 'assistant/message' && event.data.turn === current.number
               && event.data.message?.role === 'assistant' && event.data.message.source?.kind === 'model') {
      current.calls.push(...event.data.message.content.filter(part => part.type === 'tool-call'));
    }
  }
  return result;
}

async function sessionEvents(ctx, agent) {
  check(await ctx.get('sessions').flush(agent.session), 'native_persistence_unavailable');
  const handle = await ctx.get('sessionPersistence').open(agent.session.id, 'read');
  try {
    check(handle.header.id === agent.session.id, 'wrong_native_session');
    const {events} = await handle.read(0);
    const inherited = handle.inheritedEventCount;
    check(Number.isSafeInteger(inherited) && inherited >= 0 && inherited <= events.length, 'unknown_history_boundary');
    return {events,inherited};
  } finally {
    await handle.close();
  }
}

async function ownEvents(ctx,agent) {
  const {events,inherited} = await sessionEvents(ctx,agent);
  return events.slice(inherited);
}

async function persisted(ctx,agent) {
  return turns(await ownEvents(ctx,agent));
}

function contextUsage(session,events) {
  const count = n => Number.isSafeInteger(n) && n>=0 ? n : null;
  let context, latest = null;
  for (const {type,data} of events) {
    if (type==='request/context') context=data;
    if (type!=='assistant/message') continue;
    const source = data.message?.source;
    if (data.message?.role!=='assistant' || source?.kind!=='model') continue;
    latest = {turn:data.turn,step:data.step,provider:source.provider,model:source.model,
      input_tokens:count(data.usage?.inputTokens),total_tokens:count(data.usage?.totalTokens),
      window_tokens:context?.provider===source.provider && context?.model===source.model
        ? count(context?.contextWindow) : null};
  }
  return {session, latest};
}

function receipt(request, expected, history) {
  const found = history.flatMap(turn => turn.users.filter(message => message.id === expected.id)
    .map(message => ({turn,message})));
  if (!found.length) return null;
  check(found.length === 1, 'duplicate_native_input');
  const {turn,message} = found[0];
  check(message.role === 'user' && message.source?.kind === 'user'
    && isDeepStrictEqual(message.content,expected.content), 'native_input_mismatch');
  const result = {session:request.session, delivery_id:request.delivery_id, digest:request.digest,
    native_id:message.id, turn_id:turn.id};
  const media = JSON.parse(request.payload_json).attachments;
  if (media?.length) result.media_sha256 = media.map(item => item.sha256);
  return result;
}

function stopBoundTurn(agent, request, history, before) {
  check(Array.isArray(request.bindings) && request.bindings.length<=256, 'invalid_control_bindings');
  const payload = JSON.parse(request.payload_json);
  check(['task.cancel','task.transfer_out'].includes(payload.kind), 'invalid_control_delivery');
  const turn = history.at(-1);
  // A session-wide abort is safe only while the single admitted input and its turn both match the old assignment.
  if (!turn?.open || turn.users.length!==1 || agent.status!=='running' ||
      !Number.isSafeInteger(before) || agent.session.seq!==before ||
      !request.bindings.some(ref=>ref?.native_id===turn.users[0].id && ref.turn_id===turn.id)) return;
  if (typeof agent.cancel==='function') agent.cancel({kind:'user'}, {keepInbox:true});
}

async function stopPrivateTurn(ctx,agent,compacting,bound) {
  const session = agent.session.id;
  if (compacting()) throw new Error('native_stop_not_private');
  if (agent.status==='idle') return {session,state:'idle'};
  check(typeof agent.cancel==='function','native_stop_unavailable');
  const before = agent.session.seq;
  const turn = (await persisted(ctx,agent)).at(-1);
  const user = turn?.users.length===1 ? turn.users[0] : null;
  check(turn?.open && user?.source?.kind==='user' &&
    !user.content.some(part=>part.type==='text' && part.text.startsWith('魔物链接投递\n')), 'native_stop_not_private');
  bound();
  check(agent.status==='running' && agent.session.seq===before && !compacting(), 'native_stop_changed');
  agent.cancel({kind:'user'},{keepInbox:true});
  // Confirm the exact selected turn, even if preserved inbox messages start another one.
  for (let attempt=0;attempt<15;attempt++) {
    const ended = (await persisted(ctx,agent)).find(item=>item.id===turn.id)?.end;
    if (ended) {
      check(ended.reason?.kind==='aborted' && ended.reason.reason?.kind==='user','native_stop_unconfirmed');
      return {session,state:'stopped',turn_id:turn.id,end_seq:ended.seq};
    }
    await new Promise(resolve=>setTimeout(resolve,50));
  }
  throw new Error('native_stop_unconfirmed');
}

export async function bindHost(ctx, session, directory, catalog=[], selected=null, readOnly=false, foreground=false, workspaceConfig=null) {
  check(typeof session === 'string' && session.length > 0, 'missing_native_session');
  const controller = ctx.get('sessionController');
  const agent = selected ?? (await controller.resolveAgent(session)).agent;
  const agents = ctx.get('agents');
  check(agent?.session?.id === session && agents?.get(session) === agent, 'wrong_native_session');
  const displayStart = await baseline(ctx,agent);
  const compact = compaction(ctx,agent,()=>ownEvents(ctx,agent));
  const workspace = workspaceConfig ? assignmentContainers(directory,session,workspaceConfig) : null;
  const cleanup = readOnly || workspace ? assignmentCleanup(ctx,agent,directory,workspace) : null;
  const imageCache = {};
  function bound() {
    // Resolving through SessionController here could reopen a closed session.
    check(agents.get(session) === agent && agent.session.id === session
      && ['idle','running'].includes(agent.status), 'native_agent_unavailable');
  }
  bound();
  const questionRecords = foregroundRecords(directory,session);
  const questions = foreground ? installForegroundQuestions(ctx,agent,questionRecords,bound) : null;
  return {
    agent, checkBinding:bound, cleanup, workspace,
    async handle(request) {
      check(request.session === session, 'wrong_native_session');
      bound();
      if (request.operation === 'display-start') return displayStart;
      if (request.operation === 'display-next') return nextDisplay(ctx,agent,request,questionRecords,foreground);
      if (request.operation === 'display-image') return displayImage(ctx,agent,request,imageCache);
      if (request.operation === 'activity') return {busy:agent.status !== 'idle' || compact.inProgress,
        ...(workspace ? {workspace_pending:workspace.pending().length>0} : {})};
      if (request.operation === 'cleanup-evidence') {
        check(cleanup,'native_cleanup_unconfirmed');
        const {events,inherited} = await sessionEvents(ctx,agent);
        bound();
        return {proof:await cleanup.proof(request,turns(events.slice(inherited)),events)};
      }
      if (request.operation === 'capabilities') return {compaction:compact.supported,
        session_catalog:typeof ctx.get('sessionQuery')?.inspectSession==='function',
        stop_private:typeof agent.cancel==='function'};
      if (request.operation === 'stop-private') return stopPrivateTurn(ctx,agent,()=>compact.inProgress,bound);
      if (request.operation === 'session-catalog') return browseSessions(ctx,session,catalog);
      if (request.operation === 'approval-proof') {
        return {proof:approvalProof(await ownEvents(ctx,agent),request.approval_id,request.turn_id)};
      }
      if (['question-open','question-answer','question-proof'].includes(request.operation)) {
        return questionRequest(ctx,agent,request,()=>ownEvents(ctx,agent),questions,questionRecords);
      }
      if (['compact-start','compact-status'].includes(request.operation)) {
        return compact.request(request.operation,request.command_id);
      }
      if (request.operation === 'context-usage') return contextUsage(session,await ownEvents(ctx,agent));
      if (request.operation === 'goal-view') return goalView(ctx,agent);
      if (request.operation === 'goal-change') {
        check(['pause','resume','create'].includes(request.action) &&
          (request.action==='create' ? typeof request.objective==='string' && request.objective.trim()
            : request.objective===undefined),'invalid_goal_action');
        return changeGoal(ctx,agent,request.action,request.objective,()=>ownEvents(ctx,agent));
      }
      if (request.operation === 'model-view') {
        check(['models','efforts'].includes(request.kind), 'invalid_model_view');
        return modelView(ctx,agent,request.kind,await ownEvents(ctx,agent));
      }
      if (request.operation === 'model-change' || request.operation === 'model-change-if-route') {
        check(!compact.inProgress, 'model_change_busy');
        check(['models','efforts'].includes(request.kind) && typeof request.value === 'string'
          && request.value.length>0 && request.value.length<=401, 'invalid_model_change');
        if (request.operation === 'model-change-if-route') {
          check(request.kind==='efforts' && typeof request.expected_route==='string'
            && request.expected_route.length>0 && request.expected_route.length<=401, 'invalid_model_change');
        } else {
          check(request.expected_route===undefined, 'invalid_model_change');
        }
        return changeModel(ctx,agent,request,bound,()=>ownEvents(ctx,agent));
      }
      check(['deliver','lookup','deliver-control'].includes(request.operation), 'unsupported_operation');
      const expected = await input(request,ctx,agent,directory);
      const before = agent.session.seq;
      const history = await persisted(ctx,agent);
      const seen = receipt(request,expected,history);
      if (seen || request.operation === 'lookup') return {receipt:seen};
      // Only the adapter which first persisted its write intent can request delivery.
      bound();
      if (request.operation==='deliver' && JSON.parse(request.payload_json).kind==='task') {
        check(agent.status==='idle' && !compact.inProgress,'native_task_not_admitted_busy');
      }
      if (request.operation==='deliver' && JSON.parse(request.payload_json).kind==='trainer_answer') {
        check(agent.status==='idle' && !compact.inProgress,'native_answer_not_admitted_busy');
      }
      try {
        if (request.operation==='deliver-control') {
          const sameTurn = agent.session.seq===before;
          cleanup?.withdraw(request);
          stopBoundTurn(agent,request,history,sameTurn ? agent.session.seq : before);
          cleanup?.stopWorkspace(request);
        }
        cleanup?.admit(request,expected);
        agent.followup(expected);
        return {receipt:receipt(request,expected,await persisted(ctx,agent))};
      } catch (cause) {
        throw new Error('native_write_unconfirmed',{cause});
      }
    },
    async tool(name, args, exec, notify) {
      bound();
      check(exec?.agent === agent && typeof exec.callId === 'string' && exec.callId, 'wrong_native_caller');
      const history = await persisted(ctx,agent);
      const current = history.at(-1);
      check(current?.open && current.users.length === 1, 'ambiguous_native_turn');
      const calls = history.flatMap(turn => turn.calls.filter(call => call.id === exec.callId)
        .map(call => ({turn,call})));
      check(calls.length === 1 && calls[0].turn === current && calls[0].call.name === name,
        'native_call_unconfirmed');
      const raw = calls[0].call.arguments;
      check(isDeepStrictEqual(typeof raw === 'string' ? JSON.parse(raw) : raw,args), 'native_arguments_mismatch');
      const user = current.users[0];
      if (name==='worker_workspace_shell' || name==='worker_workspace_export') {
        check(workspace,'assignment_container_unavailable');
        const scope = cleanup.workspaceScope(user);
        const state = await workspaceState(directory,scope);
        check(state==='active','assignment_workspace_inactive');
        if (name==='worker_workspace_shell') {
          return workspace.run(scope.task_id,scope.assignment_id,args.command,exec.signal);
        }
        const {bytes,...metadata} = await workspace.artifact(scope.task_id,scope.assignment_id,args.path,exec.signal);
        return callback(directory,{...metadata,session,turn_id:current.id,native_id:user.id,call_id:exec.callId},
          '/workspace-artifact',bytes);
      }
      const direct = name==='worker_ask_trainer' && user.source?.kind==='user' && !user.id.startsWith('mowulink-')
        && !user.content.some(item=>item.type==='text' && item.text.startsWith('魔物链接投递\n'));
      return notify({session,turn_id:current.id,native_id:user.id,
        call_id:exec.callId,tool:name,arguments:args,...(direct ? {native_direct:true} : {})});
    }
  };
}

function privateDir(dir) {
  const info = lstatSync(dir);
  check(path.isAbsolute(dir) && info.isDirectory() && info.uid === process.getuid()
    && !(info.mode & 0o077), 'private_runtime_required');
}

async function readJson(stream) {
  let size = 0;
  const chunks = [];
  for await (const chunk of stream) {
    size += chunk.length;
    check(size <= limit, 'request_too_large');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8'));
}

function callback(dir, envelope, route='/tool', bytes=null) {
  privateDir(dir);
  const encoded = JSON.stringify(envelope);
  const body = bytes ?? Buffer.from(encoded);
  const headers = bytes===null ? {'Content-Type':'application/json'} : {
    'Content-Type':'application/octet-stream','X-Mowu-Workspace':Buffer.from(encoded).toString('base64url'),
  };
  return new Promise((resolve,reject) => {
    const req = http.request({socketPath:path.join(dir,'worker.sock'),path:route,method:'POST',
      headers:{...headers,'Content-Length':body.length},timeout:bytes===null ? 8000 : 45000}, async res => {
      try {
        const body = await readJson(res);
        check(res.statusCode === 200, body.error || 'worker_unconfirmed');
        resolve(body);
      } catch (error) { reject(error); }
    });
    req.on('error',reject).on('timeout',() => req.destroy(new Error('worker_timeout')));
    req.end(body);
  });
}

async function workspaceState(directory,scope) {
  const result = await callback(directory,scope,'/workspace-state');
  check(Object.entries(scope).every(([key,value])=>result[key]===value) &&
    ['active','inactive','released'].includes(result.state),'assignment_workspace_unconfirmed');
  return result.state;
}

function watchWorkspaceReleases(ctx,port,directory) {
  const session = port.agent.session.id;
  ctx.effect(()=>{
    let stopped = false;
    let timer;
    async function sweep() {
      try {
        for (const assignment of port.workspace.pending()) {
          if (stopped) break;
          try {
            if (await workspaceState(directory,{session,...assignment})==='released') {
              if (stopped) break;
              await port.workspace.seal(assignment.task_id,assignment.assignment_id);
            }
          } catch { /* Unconfirmed release stays in the durable workspace journal. */ }
        }
      } catch { /* Leave the journal intact if it cannot be read. */ }
      finally {
        if (!stopped) timer = setTimeout(sweep,1500);
      }
    }
    timer = setTimeout(sweep,1500);
    return ()=>{ stopped=true; clearTimeout(timer); };
  });
}

export const name = 'mowulink-dsh-host';
export const inject = ['sessionController','agents','sessions','sessionPersistence','tools'];

async function inspectColdSession(ctx,session) {
  const query = ctx.get('sessionQuery');
  check(typeof query?.inspectSession==='function','native_cold_inspection_unavailable');
  const view = await query.inspectSession(session,native=>{
    if (native.id!==session || native.header.origin==='subagent') return null;
    const event = native.snapshotEvents().findLast(item=>item.type==='goal/change');
    return {goal:event?.data ?? null};
  },AbortSignal.timeout(3000));
  check(view,'native_cold_thread_unconfirmed');
  const goal = view.goal;
  if (!goal) return;
  check(goal.kind==='goal/change' && goal.version===1,'native_cold_goal_unconfirmed');
  if (goal.operation==='clear') return;
  const phase = goal.goal?.phase;
  check(['active','paused','blocked','complete'].includes(phase),'native_cold_goal_unconfirmed');
  check(phase!=='active','native_cold_goal_active');
}

function installBoundTools(ctx,port,directory,relay_command_approvals) {
  if (relay_command_approvals) {
    installCommandApprovals(ctx,port.agent,()=>ownEvents(ctx,port.agent),(route,body)=>callback(directory,body,route));
  }
  const available = port.workspace ? [...tools,{
    name:'worker_workspace_shell',
    description:'Run a shell command inside the private workspace of the current work order.',
    inputSchema:{type:'object',required:['command'],additionalProperties:false,properties:{command:{type:'string'}}},
  },{
    name:'worker_workspace_export',
    description:'Stage a regular file (up to 16 MiB) from this work order’s private workspace. Include the returned artifact IDs in worker_report to share the file.',
    inputSchema:{type:'object',required:['path'],additionalProperties:false,properties:{path:{type:'string'}}},
  }] : tools;
  for (const tool of available) {
    const unregister = port.agent.ctx.get('tools').register({name:tool.name,description:tool.description,
      parameters:tool.inputSchema,output:{schema:{type:'object'},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},
      execute:(args,exec) => port.tool(tool.name,args,exec,body=>callback(directory,body))});
    ctx.effect(() => unregister);
  }
  port.cleanup?.bindTools();
  if (port.workspace) watchWorkspaceReleases(ctx,port,directory);
}

export async function apply(ctx, {session, directory, catalog_sessions=[], relay_command_approvals=false,
                                 resume_on_handoff=false, managed_sessions_dir=null, new_session_selection=null,
                                 read_only_assignments=false, relay_foreground_questions=false, assignment_container=null}) {
  privateDir(directory);
  check(typeof session==='string' && session.length>0,'missing_native_session');
  check(typeof resume_on_handoff==='boolean','invalid_native_activation');
  check(typeof read_only_assignments==='boolean' &&
    (!assignment_container || (!read_only_assignments && !relay_command_approvals)),
    'invalid_native_cleanup_configuration');
  check(typeof relay_foreground_questions==='boolean','invalid_native_question_configuration');
  const managed = managed_sessions_dir!==null;
  check(!managed || (!resume_on_handoff && typeof managed_sessions_dir==='string'),'invalid_managed_session_configuration');
  if (resume_on_handoff || managed) {
    check(typeof ctx.get('sessionQuery')?.inspectSession==='function','native_cold_inspection_unavailable');
  }
  if (managed) privateDir(managed_sessions_dir);
  const defaultSelection = new_session_selection ? selection(new_session_selection) : null;
  const catalog = catalogSessions(catalog_sessions);
  const scopes = new Map([[session,{port:null,selected:null,opening:null}]]);
  function scopeFor(target) {
    if (target!==session) {
      check(managed && guid(target),'wrong_native_session');
      privateDir(managed_sessions_dir);
      const filename = path.join(managed_sessions_dir,target+'.json');
      let info;
      try { info = lstatSync(filename); } catch { throw new Error('wrong_native_session'); }
      check(info.isFile() && info.uid===process.getuid() && !(info.mode & 0o077),'private_session_registry_required');
      const record = JSON.parse(readFileSync(filename,{encoding:'utf8',flag:constants.O_RDONLY|constants.O_NOFOLLOW}));
      check(record?.session===target && guid(record.operation_id) && typeof record.source==='string'
        && record.source!==target,'wrong_native_session');
      if (!scopes.has(target)) scopes.set(target,{port:null,selected:null,opening:null});
    }
    return scopes.get(target);
  }
  async function resolveCold(target,scope) {
    await inspectColdSession(ctx,target);
    const agents = ctx.get('agents');
    scope.selected ??= agents.get(target) ?? (await ctx.get('sessionController').resolveAgent(target)).agent;
    check(scope.selected?.session?.id===target && agents.get(target)===scope.selected,'native_agent_unavailable');
    check(scope.selected.status==='idle','native_cold_thread_unconfirmed');
    const opened = await bindHost(ctx,target,directory,catalog,scope.selected,read_only_assignments,relay_foreground_questions,assignment_container);
    installBoundTools(ctx,opened,directory,relay_command_approvals);
    scope.port = opened;
  }
  async function activate(request,scope) {
    check(resume_on_handoff || managed,'native_activation_unavailable');
    const operation_id = request.operation_id;
    check(guid(operation_id),'invalid_native_activation');
    if (!scope.port) {
      scope.opening ??= resolveCold(request.session,scope).finally(()=>{ scope.opening=undefined; });
      await scope.opening;
    }
    scope.port.checkBinding();
    return {session:request.session,operation_id,state:'loaded'};
  }
  if (!resume_on_handoff && (!managed || !readdirSync(managed_sessions_dir).some(name=>name.endsWith('.json')))) {
    const port = await bindHost(ctx,session,directory,catalog,null,read_only_assignments,relay_foreground_questions,assignment_container);
    installBoundTools(ctx,port,directory,relay_command_approvals);
    scopes.get(session).port = port;
  }
  const server = http.createServer(async (req,res) => {
    try {
      check(req.method === 'POST' && req.url === '/host','unknown_route');
      const request = await readJson(req);
      const scope = scopeFor(request.session);
      const {port} = scope;
      let result;
      if (request.operation==='managed-state' && managed && request.session===session) {
        result = {session,directory:realpathSync(managed_sessions_dir)};
      }
      else if (request.operation==='activate') result = await activate(request,scope);
      else if (request.operation==='load-state' && (resume_on_handoff || managed)) {
        port?.checkBinding();
        result = {session:request.session,state:port ? 'loaded' : 'cold'};
      }
      else if (!port && request.operation==='capabilities') result = {};
      else if (managed && ['plan-thread','prepare-thread'].includes(request.operation)) {
        check(port,'native_session_cold');
        result = await prepareSession(ctx,port,request,defaultSelection);
      }
      else {
        check(port,'native_session_cold');
        result = await port.handle(request);
      }
      res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify(result));
    } catch (error) {
      const code = /^[a-z_]{1,80}$/.test(error.message) ? error.message : 'dsh_host_unconfirmed';
      const transient = ['native_write_unconfirmed','native_agent_unavailable','native_persistence_unavailable',
                         'dsh_host_unconfirmed','model_change_unconfirmed','goal_change_unconfirmed',
                         'compact_unconfirmed','native_stop_unconfirmed','native_session_cold',
                         'native_cold_thread_unconfirmed','native_cold_goal_unconfirmed','native_creation_unconfirmed',
                         'native_preparation_source_unconfirmed'].includes(code);
      res.writeHead(transient ? 503 : 409,{'Content-Type':'application/json'}).end(JSON.stringify({error:code}));
    }
  });
  server.requestTimeout = 10000;
  await new Promise((resolve,reject) => {
    server.once('error',reject).listen(path.join(directory,'dsh.sock'),resolve);
  });
  ctx.effect(() => () => new Promise(resolve=>server.close(resolve)));
}
