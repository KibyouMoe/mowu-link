import {createHash} from 'node:crypto';
import path from 'node:path';
import {isDeepStrictEqual} from 'node:util';

export const guid = value=>typeof value==='string' && /^[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}$/.test(value);

function check(value, code) {
  if (!value) throw new Error(code);
}

export function selection(value) {
  check(value && typeof value.provider==='string' && value.provider.trim()
    && typeof value.model==='string' && value.model.trim()
    && (value.effort==null || typeof value.effort==='string' && value.effort.trim()),
    'native_preparation_model_unconfirmed');
  return {provider:value.provider,model:value.model,effort:value.effort ?? null};
}

function canonical(value) {
  if (Array.isArray(value)) return value.map(canonical);
  if (value && typeof value==='object') {
    return Object.fromEntries(Object.keys(value).sort().map(key=>[key,canonical(value[key])]));
  }
  return value;
}

function digest(events) {
  return createHash('sha256').update(JSON.stringify(canonical(events))).digest('hex');
}

function projection(request,created_at,agent,defaultSelection,events) {
  const {session:source_session,target_session:session,operation_id,kind} = request;
  const cwd = agent.session.header.cwd;
  check(typeof cwd==='string' && path.isAbsolute(cwd),'native_preparation_workspace_unconfirmed');
  if (events.length) {
    const start = events.findLastIndex(event=>event.type==='turn/start');
    const end = events.findLastIndex(event=>event.type==='turn/end');
    check(start<=end,'native_preparation_source_busy');
  }
  const route = agent.model.current();
  const current = selection({provider:route?.provider,model:route?.model,effort:route?.reasoningEffort});
  const plan = {source_session,session,operation_id,kind,cwd,created_at,source_count:events.length,
    source_digest:digest(events),selection:kind==='new' && defaultSelection ? defaultSelection : current};
  return plan;
}

function selectionEvent(plan,seq) {
  const {provider,model,effort} = plan.selection;
  return {type:'model/selection',seq,time:plan.created_at,
    data:{provider,model,...(effort===null ? {} : {reasoningEffort:effort})}};
}

function seedEnd(events) {
  return {type:'session/end-seed',seq:events.length,time:events.at(-1).time,data:{inherited:true}};
}

async function readBack(persistence,plan) {
  const reader = await persistence.open(plan.session,'read');
  try {
    const header = reader.header;
    const {events} = await reader.read(0);
    const count = plan.source_count;
    const prefix = events.slice(0,count);
    const parent = plan.kind==='fork' ? plan.source_session : null;
    const own = count ? [seedEnd(prefix),selectionEvent(plan,count+1)] : [selectionEvent(plan,0)];
    const stored = events.slice(count);
    if (plan.kind==='new') {
      // The native store appends a barrier after a seeded non-fork session.
      const barrier = stored.pop();
      check(barrier?.type==='session/end-seed' && barrier.seq===1 && Number.isSafeInteger(barrier.time)
        && isDeepStrictEqual(barrier.data,{}),'native_creation_unconfirmed');
    }
    check(header.id===plan.session && header.cwd===plan.cwd && header.createdAt===plan.created_at
      && (header.parentSession ?? null)===parent && header.isSeeded===(plan.kind==='fork')
      && reader.inheritedEventCount===count && digest(prefix)===plan.source_digest
      && isDeepStrictEqual(stored,own),'native_creation_unconfirmed');
    return {operation_id:plan.operation_id,source_session:plan.source_session,session:plan.session,kind:plan.kind,
      parent_session:parent,state:'prepared',selection:plan.selection};
  } finally {
    await reader.close();
  }
}

async function sourceEvents(ctx,agent,kind) {
  check(await ctx.get('sessions').flush(agent.session),'native_persistence_unavailable');
  const persistence = ctx.get('sessionPersistence');
  check(await persistence.stat(agent.session.id),'native_preparation_source_unconfirmed');
  if (kind!=='fork') return [];
  const reader = await persistence.open(agent.session.id,'read');
  try {
    const {events} = await reader.read(0);
    check(reader.header.id===agent.session.id && digest(events)===digest(agent.session.snapshotEvents()),
      'native_preparation_source_changed');
    return events;
  } finally {
    await reader.close();
  }
}

export async function prepareSession(ctx,port,request,defaultSelection) {
  check(guid(request.operation_id) && guid(request.target_session) && request.target_session!==request.session
    && ['new','fork'].includes(request.kind),'invalid_native_preparation');
  const {agent} = port;
  const sessions = ctx.get('sessions');
  const persistence = ctx.get('sessionPersistence');
  const requested = request.plan;
  if (request.operation==='prepare-thread') {
    check(requested && requested.operation_id===request.operation_id && requested.source_session===request.session
      && requested.session===request.target_session && requested.kind===request.kind
      && Number.isSafeInteger(requested.created_at) && requested.created_at>=0,'invalid_native_preparation');
    if (await persistence.stat(requested.session)) {
      return readBack(persistence,requested);
    }
  }
  check((await port.handle({operation:'activity',session:request.session})).busy===false,'native_preparation_source_busy');
  const events = await sourceEvents(ctx,agent,request.kind);
  port.checkBinding();
  check(agent.status==='idle','native_preparation_source_busy');
  const plan = projection(request,requested?.created_at ?? Date.now(),agent,defaultSelection,events);
  if (request.operation==='plan-thread') {
    if (plan.kind==='new' && defaultSelection) {
      const {provider,model,effort} = plan.selection;
      try {
        await agent.ctx.get('llm').resolveCallConfig({provider,model,...(effort===null ? {} : {reasoningEffort:effort})});
      } catch {
        throw new Error('native_preparation_model_unconfirmed');
      }
    }
    return plan;
  }
  check(isDeepStrictEqual(plan,requested),'native_preparation_source_changed');
  const nativeEvents = events.slice();
  if (events.length) nativeEvents.push(seedEnd(events));
  nativeEvents.push(selectionEvent(plan,nativeEvents.length));
  const branch = plan.kind==='fork';
  const native = sessions.prepare(plan.session,{seed:nativeEvents,inheritedEventCount:events.length,
    meta:{cwd:plan.cwd,createdAt:plan.created_at,isSeeded:branch,...(branch ? {parentSession:plan.source_session} : {})}});
  try {
    const writer = await persistence.create(native.header,{inheritedEventCount:native.inheritedEventCount});
    try {
      await writer.append(native.snapshotEvents());
      await writer.flush();
    } finally {
      await writer.close();
    }
  } catch {
    throw new Error('native_creation_unconfirmed');
  }
  return readBack(persistence,plan);
}
