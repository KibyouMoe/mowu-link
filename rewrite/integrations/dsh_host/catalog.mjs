export function catalogSessions(values) {
  if (!Array.isArray(values) || values.length>199 ||
      values.some(id=>typeof id!=='string' || !/^[A-Za-z0-9_-]{1,200}$/.test(id))) {
    throw new Error('invalid_catalog_sessions');
  }
  return values;
}

export async function browseSessions(ctx, current, configured) {
  const query = ctx.get('sessionQuery');
  if (typeof query?.inspectSession!=='function') throw new Error('native_catalog_unavailable');
  const ids = [...new Set([current,...configured])];
  const signal = AbortSignal.timeout(3000);
  const items = [];
  let unavailable = 0;
  for (let index=0; index<ids.length; index+=6) {
    signal.throwIfAborted();
    const reads = ids.slice(index,index+6).map(id=>query.inspectSession(id,session=>{
      if (session.id!==id || session.header.origin==='subagent') return null;
      const events = session.snapshotEvents();
      const title = events.findLast(event=>event.type==='session/title')?.data.title;
      const lastInput = events.findLast(event=>event.type==='user/message' && event.data.source?.kind==='user');
      const updated = lastInput?.time ?? session.header.createdAt;
      const clean = typeof title==='string' ? title.replace(/[\p{Cc}\p{Cs}\u2028-\u202e\u2066-\u2069]/gu,' ').trim() : '';
      return {id,title:[...clean].slice(0,160).join(''),
        updated_at:Number.isSafeInteger(updated) && updated>=0 ? updated : 0};
    },signal));
    for (const result of await Promise.allSettled(reads)) {
      if (result.status==='fulfilled' && result.value) items.push(result.value);
      else unavailable += 1;
    }
  }
  signal.throwIfAborted();
  items.sort((a,b)=>b.updated_at-a.updated_at || a.id.localeCompare(b.id));
  return {session:current,items:items.map(({id,title})=>({id,title})),unavailable};
}
