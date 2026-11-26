import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { isDeepStrictEqual } from 'node:util';

const guid = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;

function absoluteWorkdir(args) {
  return typeof args?.workdir==='string' && path.isAbsolute(args.workdir);
}

function origin(events,callId) {
  const start = events.findLastIndex(event=>event.type==='turn/start' || event.type==='turn/end');
  if (start<0 || events[start].type!=='turn/start') return null;
  const turn = events[start];
  const history = events.slice(start+1);
  const calls = history.flatMap(event=>event.type==='assistant/message' && event.data.message?.source?.kind==='model'
    ? event.data.message.content.filter(item=>item.type==='tool-call' && item.id===callId && item.name==='bash') : []);
  const settled = new Set(history.filter(event=>event.type==='approval/decided').map(event=>event.data.id));
  const asked = history.filter(event=>event.type==='approval/asked' && event.data.toolName==='bash'
    && event.data.callId===callId && !settled.has(event.data.id));
  if (calls.length!==1 || asked.length!==1 || !guid.test(asked[0].data.id)) return null;
  const args = typeof calls[0].arguments==='string' ? JSON.parse(calls[0].arguments) : calls[0].arguments;
  if (!args || typeof args.command!=='string' || !args.command.trim() || args.command.length>8192) return null;
  return {id:asked[0].data.id,seq:asked[0].seq,turn:turn.seq+'/'+turn.data.turn,args};
}

export function approvalProof(events,approvalId,turnId) {
  if (typeof approvalId!=='string' || !guid.test(approvalId)) return null;
  const start = events.findIndex(event=>event.type==='turn/start' && event.seq+'/'+event.data.turn===turnId);
  if (start<0) return null;
  const end = events.findIndex((event,index)=>index>start && ['turn/start','turn/end'].includes(event.type));
  const window = events.slice(start+1,end<0 ? events.length : end);
  const asked = window.filter(event=>event.type==='approval/asked' && event.data.id===approvalId && event.data.toolName==='bash');
  const decided = window.filter(event=>event.type==='approval/decided' && event.data.id===approvalId);
  if (asked.length!==1 || decided.length!==1 || asked[0].seq>=decided[0].seq) return null;
  return {approval_id:approvalId,turn_id:turnId,asked_seq:asked[0].seq,
    decided_seq:decided[0].seq,outcome:decided[0].data.outcome};
}

export function installCommandApprovals(ctx,agent,readEvents,notify) {
  const lifecycle = new AbortController();
  const active = new Set();
  const preparing = new Map();
  const approved = new WeakMap();
  const session = agent.session.id;
  const bound = () => ctx.get('agents').get(session)===agent && agent.session.id===session;

  async function resolveDirectory(args,signal) {
    const workingDirectory = ctx.get('workingDirectory');
    const fs = ctx.get('fs');
    const base = workingDirectory?.get(agent.session);
    if (typeof base!=='string' || !path.isAbsolute(base)
        || (args.workdir!==undefined && typeof args.workdir!=='string')
        || typeof fs?.processPath!=='function') return null;
    // Preserve the native spelling before realpath so parent traversal through links has Bash's meaning.
    const candidate = args.workdir===undefined ? base : base+path.sep+args.workdir;
    const target = await fs.resolve(candidate,{signal});
    const cwd = fs.processPath(target);
    if (typeof cwd!=='string' || !path.isAbsolute(cwd) || (await fs.stat(target,signal))?.type!=='directory'
        || workingDirectory.get(agent.session)!==base) return null;
    return {base,cwd};
  }

  function requestContext(native,reason,cwd) {
    return {session,turn_id:native.turn,item_id:native.id,command:native.args.command,cwd,
      environment_id:null,arguments:native.args,reason:typeof reason==='string' ? reason : '',
      decisions:['accept','cancel']};
  }

  const watchPrepare = ctx.on('tools/pre-execute',async (exec,next)=>{
    const gate = await next();
    if (exec.agent===agent && exec.name==='bash' && gate.kind==='ask' && !absoluteWorkdir(exec.arguments)) {
      preparing.set(exec.callId,exec);
    }
    return gate;
  },{prepend:true});

  const pinAtDispatch = ctx.on('tools/execute',async (exec,next)=>{
    const grant = approved.get(exec);
    if (!grant) return next();
    if (grant.dispatched) throw new Error('DSH Bash approval has already been used');
    grant.dispatched = true;
    const {native,directory} = grant;
    const actual = await resolveDirectory(native.args,exec.signal);
    const proof = approvalProof(agent.session.snapshotEvents(),native.id,native.turn);
    if (!bound() || !isDeepStrictEqual(exec.arguments,native.args)
        || !isDeepStrictEqual(actual,directory) || proof?.outcome!=='allowed-once') {
      throw new Error('DSH Bash approval no longer matches the working directory');
    }
    exec.signal.throwIfAborted();
    const original = exec.arguments;
    // The native tool may resolve the session directory again. Keep this one approved call absolute.
    exec.arguments = Object.freeze({...original,workdir:directory.cwd});
    try {
      return await next();
    } finally {
      exec.arguments = original;
    }
  });

  const stopWatching = ctx.on('tools/result',exec=>{
    if (preparing.get(exec.callId)===exec) preparing.delete(exec.callId);
    approved.delete(exec);
  });

  async function readOffer(entry) {
    const reply = await notify('/approval',{approval_id:entry.native.id,data:entry.offer});
    if (reply?.approval_id!==entry.native.id || !isDeepStrictEqual(reply.request,entry.offer)
        || !/^[a-f0-9]{64}$/.test(reply.request_digest)
        || (entry.digest && entry.digest!==reply.request_digest)) throw new Error('native_approval_receipt_mismatch');
    entry.digest = reply.request_digest;
    return reply;
  }

  async function recordAttempt(entry,response) {
    const reply = await notify('/approval/attempt',{approval_id:entry.native.id,data:{
      session,request_digest:entry.digest,attempted_response:response}});
    if (reply?.approval_id!==entry.native.id || !isDeepStrictEqual(reply.attempted_response,response)) {
      throw new Error('native_approval_attempt_unconfirmed');
    }
  }

  const unsubscribe = ctx.on('approval/request',async (request,next)=>{
    if (request.agent!==agent || request.toolName!=='bash' || typeof request.callId!=='string' || !bound()) return next();
    let native,offer,execution,directory;
    try {
      const live = origin(agent.session.snapshotEvents(),request.callId);
      if (!live) return next();
      if (active.has(live.id)) return 'unavailable';
      native = origin(await readEvents(),request.callId);
      if (!isDeepStrictEqual(live,native)) return next();
      if (absoluteWorkdir(native.args)) {
        offer = requestContext(native,request.reason,native.args.workdir);
      } else {
        execution = preparing.get(request.callId);
        // A request from inside the tool body, including sandbox escalation, has no prepare-stage pin.
        if (!execution || execution.signal!==request.signal || !isDeepStrictEqual(execution.arguments,native.args)
            || native.args.sandbox_permissions!==undefined) return next();
        directory = await resolveDirectory(native.args,request.signal);
        if (!directory) return next();
        offer = requestContext(native,request.reason,directory.cwd);
      }
    } catch {
      return next();
    }
    if (!offer) return next();
    if (active.has(native.id)) return 'unavailable';
    active.add(native.id);
    const entry = {native,offer,digest:null};
    const signals = [lifecycle.signal,AbortSignal.timeout(610_000)];
    if (request.signal) signals.push(request.signal);
    const signal = AbortSignal.any(signals);
    const sameDirectory = async () => !directory
      || isDeepStrictEqual(await resolveDirectory(native.args,signal),directory);
    try {
      while (bound() && !signal.aborted) {
        let reply;
        try {
          reply = await readOffer(entry);
        } catch (error) {
          if (!entry.digest && error.message==='approval_relay_unavailable') {
            return next();
          }
          await delay(250,null,{signal});
          continue;
        }
        signal.throwIfAborted();
        if (!bound() || reply.state==='disconnected' || reply.state==='native_closed') break;
        if (reply.state==='expired') {
          await recordAttempt(entry,{source:'expiry',decision:'cancel'});
          signal.throwIfAborted();
          return 'rejected';
        }
        if (reply.state==='decided' && typeof reply.expires_at==='number' && Date.now()<reply.expires_at*1000) {
          if (!offer.decisions.includes(reply.decision)) break;
          const current = origin(await readEvents(),request.callId);
          signal.throwIfAborted();
          if (!bound() || !isDeepStrictEqual(current,native)
              || !await sameDirectory() || !isDeepStrictEqual(requestContext(native,request.reason,offer.cwd),offer)) break;
          await recordAttempt(entry,{source:'operator',decision:reply.decision});
          signal.throwIfAborted();
          if (!bound() || Date.now()>=reply.expires_at*1000
              || !isDeepStrictEqual(origin(agent.session.snapshotEvents(),request.callId),native)
              || !await sameDirectory() || !isDeepStrictEqual(requestContext(native,request.reason,offer.cwd),offer)) break;
          signal.throwIfAborted();
          if (!bound() || Date.now()>=reply.expires_at*1000) break;
          if (reply.decision==='accept' && directory) {
            execution.signal = AbortSignal.any([execution.signal,lifecycle.signal]);
            approved.set(execution,{native,directory});
          }
          return reply.decision==='accept' ? 'allowed-once' : 'rejected';
        }
        await delay(250,null,{signal});
      }
      return 'unavailable';
    } catch {
      return request.signal?.aborted ? 'cancelled' : 'unavailable';
    } finally {
      active.delete(native.id);
    }
  },{prepend:true});
  ctx.effect(() => {
    return () => {
      lifecycle.abort();
      unsubscribe();
      watchPrepare();
      pinAtDispatch();
      stopWatching();
      preparing.clear();
    };
  });
}
