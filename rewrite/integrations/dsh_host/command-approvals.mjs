import path from 'node:path';
import { setTimeout as delay } from 'node:timers/promises';
import { isDeepStrictEqual } from 'node:util';

const guid = /^[a-f0-9]{8}(?:-[a-f0-9]{4}){3}-[a-f0-9]{12}$/;

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
  const session = agent.session.id;
  const bound = () => ctx.get('agents').get(session)===agent && agent.session.id===session;

  function requestContext(native,reason) {
    const workdir = native.args.workdir;
    // Bash resolves a relative override against the session directory after the prompt.
    // A delegated grant needs an explicit absolute directory in the original call.
    if (typeof workdir!=='string' || !path.isAbsolute(workdir)) return null;
    return {session,turn_id:native.turn,item_id:native.id,command:native.args.command,cwd:workdir,
      environment_id:null,arguments:native.args,reason:typeof reason==='string' ? reason : '',
      decisions:['accept','cancel']};
  }

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
    let native,offer;
    try {
      const live = origin(agent.session.snapshotEvents(),request.callId);
      if (!live) return next();
      if (active.has(live.id)) return 'unavailable';
      native = origin(await readEvents(),request.callId);
      if (!isDeepStrictEqual(live,native)) return next();
      offer = requestContext(native,request.reason);
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
              || !isDeepStrictEqual(requestContext(native,request.reason),offer)) break;
          await recordAttempt(entry,{source:'operator',decision:reply.decision});
          signal.throwIfAborted();
          if (!bound() || Date.now()>=reply.expires_at*1000
              || !isDeepStrictEqual(origin(agent.session.snapshotEvents(),request.callId),native)
              || !isDeepStrictEqual(requestContext(native,request.reason),offer)) break;
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
    };
  });
}
