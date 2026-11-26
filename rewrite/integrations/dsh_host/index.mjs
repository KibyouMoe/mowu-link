import { createHash } from 'node:crypto';
import { readFileSync, lstatSync } from 'node:fs';
import http from 'node:http';
import path from 'node:path';
import { isDeepStrictEqual } from 'node:util';

const tools = JSON.parse(readFileSync(new URL('../../agent-toolset.json', import.meta.url)));
const sha = value => createHash('sha256').update(value).digest('hex');
const limit = 1024 * 1024;

function check(condition, code) {
  if (!condition) throw new Error(code);
}

function input(request) {
  const {session, delivery_id, payload_json, digest} = request;
  check(typeof payload_json === 'string' && sha(payload_json) === digest, 'delivery_digest_mismatch');
  check(typeof delivery_id === 'string' && delivery_id.length > 0, 'invalid_delivery_id');
  const payload = JSON.parse(payload_json);
  check(!payload.attachments?.length, 'native_media_input_unavailable');
  const id = 'mowulink-' + sha(JSON.stringify([session,delivery_id,digest]));
  const text = payload.kind === 'chat' ? payload.text : '魔物链接投递\n' + payload_json;
  check(typeof text === 'string' && text.trim(), 'empty_input');
  return {id, role:'user', content:[{type:'text',text}], source:{kind:'user'}};
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

async function persisted(ctx, agent) {
  check(await ctx.get('sessions').flush(agent.session), 'native_persistence_unavailable');
  const handle = await ctx.get('sessionPersistence').open(agent.session.id, 'read');
  try {
    check(handle.header.id === agent.session.id, 'wrong_native_session');
    const {events} = await handle.read(0);
    const inherited = handle.inheritedEventCount;
    check(Number.isSafeInteger(inherited) && inherited >= 0 && inherited <= events.length, 'unknown_history_boundary');
    return turns(events.slice(inherited));
  } finally {
    await handle.close();
  }
}

function receipt(request, expected, history) {
  const found = history.flatMap(turn => turn.users.filter(message => message.id === expected.id)
    .map(message => ({turn,message})));
  if (!found.length) return null;
  check(found.length === 1, 'duplicate_native_input');
  const {turn,message} = found[0];
  check(message.role === 'user' && message.source?.kind === 'user'
    && isDeepStrictEqual(message.content,expected.content), 'native_input_mismatch');
  return {session:request.session, delivery_id:request.delivery_id, digest:request.digest,
    native_id:message.id, turn_id:turn.id};
}

export async function bindHost(ctx, session) {
  check(typeof session === 'string' && session.length > 0, 'missing_native_session');
  const controller = ctx.get('sessionController');
  const {agent} = await controller.resolveAgent(session);
  check(agent?.session?.id === session, 'wrong_native_session');
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
      if (request.operation === 'activity') return {busy:agent.status !== 'idle'};
      check(['deliver','lookup'].includes(request.operation), 'unsupported_operation');
      const expected = input(request);
      const seen = receipt(request,expected,await persisted(ctx,agent));
      if (seen || request.operation === 'lookup') return {receipt:seen};
      // Only the adapter which first persisted its write intent can request delivery.
      agent.followup(expected);
      return {receipt:receipt(request,expected,await persisted(ctx,agent))};
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
  const port = await bindHost(ctx,session);
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
      res.writeHead(409,{'Content-Type':'application/json'}).end(JSON.stringify({error:code}));
    }
  });
  server.requestTimeout = 10000;
  await new Promise((resolve,reject) => {
    server.once('error',reject).listen(path.join(directory,'dsh.sock'),resolve);
  });
  ctx.effect(() => () => new Promise(resolve=>server.close(resolve)));
}
