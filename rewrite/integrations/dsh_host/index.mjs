import { createHash } from 'node:crypto';
import { readFileSync, lstatSync } from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { isDeepStrictEqual } from 'node:util';
import { admitMedia } from './media.mjs';
import { baseline, displayImage, nextDisplay } from './display.mjs';
import { changeModel, modelView } from './model-view.mjs';
import { changeGoal, goalView } from './goal-view.mjs';
import { compaction } from './compaction.mjs';
import { questionRequest } from './questions.mjs';

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

async function ownEvents(ctx, agent) {
  check(await ctx.get('sessions').flush(agent.session), 'native_persistence_unavailable');
  const handle = await ctx.get('sessionPersistence').open(agent.session.id, 'read');
  try {
    check(handle.header.id === agent.session.id, 'wrong_native_session');
    const {events} = await handle.read(0);
    const inherited = handle.inheritedEventCount;
    check(Number.isSafeInteger(inherited) && inherited >= 0 && inherited <= events.length, 'unknown_history_boundary');
    return events.slice(inherited);
  } finally {
    await handle.close();
  }
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

export async function bindHost(ctx, session, directory) {
  check(typeof session === 'string' && session.length > 0, 'missing_native_session');
  const controller = ctx.get('sessionController');
  const {agent} = await controller.resolveAgent(session);
  check(agent?.session?.id === session, 'wrong_native_session');
  const displayStart = await baseline(ctx,agent);
  const compact = compaction(ctx,agent,()=>ownEvents(ctx,agent));
  const imageCache = {};
  async function bound() {
    const live = await controller.resolveAgent(session);
    check(live.agent === agent && agent.session.id === session
      && ['idle','running'].includes(agent.status), 'native_agent_unavailable');
  }
  return {
    agent,
    async handle(request) {
      check(request.session === session, 'wrong_native_session');
      await bound();
      if (request.operation === 'display-start') return displayStart;
      if (request.operation === 'display-next') return nextDisplay(ctx,agent,request);
      if (request.operation === 'display-image') return displayImage(ctx,agent,request,imageCache);
      if (request.operation === 'activity') return {busy:agent.status !== 'idle' || compact.inProgress};
      if (request.operation === 'capabilities') return {compaction:compact.supported};
      if (['question-open','question-answer','question-proof'].includes(request.operation)) {
        return questionRequest(ctx,agent,request,()=>ownEvents(ctx,agent));
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
      await bound();
      try {
        if (request.operation==='deliver-control') stopBoundTurn(agent,request,history,before);
        agent.followup(expected);
        return {receipt:receipt(request,expected,await persisted(ctx,agent))};
      } catch (cause) {
        throw new Error('native_write_unconfirmed',{cause});
      }
    },
    async tool(name, args, exec, notify) {
      await bound();
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
      return notify({session,turn_id:current.id,native_id:current.users[0].id,
        call_id:exec.callId,tool:name,arguments:args});
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

function callback(dir, envelope) {
  privateDir(dir);
  return new Promise((resolve,reject) => {
    const req = http.request({socketPath:path.join(dir,'worker.sock'),path:'/tool',method:'POST',
      headers:{'Content-Type':'application/json'}, timeout:8000}, async res => {
      try {
        const body = await readJson(res);
        check(res.statusCode === 200, body.error || 'worker_unconfirmed');
        resolve(body);
      } catch (error) { reject(error); }
    });
    req.on('error',reject).on('timeout',() => req.destroy(new Error('worker_timeout')));
    req.end(JSON.stringify(envelope));
  });
}

export const name = 'mowulink-dsh-host';
export const inject = ['sessionController','sessions','sessionPersistence','tools'];

export async function apply(ctx, {session, directory}) {
  privateDir(directory);
  const port = await bindHost(ctx,session,directory);
  for (const tool of tools) {
    const unregister = port.agent.ctx.get('tools').register({name:tool.name,description:tool.description,
      parameters:tool.inputSchema,output:{schema:{type:'object'},render:(_args,value)=>[{type:'text',text:JSON.stringify(value)}]},
      execute:(args,exec) => port.tool(tool.name,args,exec,body=>callback(directory,body))});
    ctx.effect(() => unregister);
  }
  const server = http.createServer(async (req,res) => {
    try {
      check(req.method === 'POST' && req.url === '/host','unknown_route');
      const result = await port.handle(await readJson(req));
      res.writeHead(200,{'Content-Type':'application/json'}).end(JSON.stringify(result));
    } catch (error) {
      const code = /^[a-z_]{1,80}$/.test(error.message) ? error.message : 'dsh_host_unconfirmed';
      const transient = ['native_write_unconfirmed','native_agent_unavailable','native_persistence_unavailable',
                         'dsh_host_unconfirmed','model_change_unconfirmed','goal_change_unconfirmed','compact_unconfirmed'].includes(code);
      res.writeHead(transient ? 503 : 409,{'Content-Type':'application/json'}).end(JSON.stringify({error:code}));
    }
  });
  server.requestTimeout = 10000;
  await new Promise((resolve,reject) => {
    server.once('error',reject).listen(path.join(directory,'dsh.sock'),resolve);
  });
  ctx.effect(() => () => new Promise(resolve=>server.close(resolve)));
}
