import {createHash} from 'node:crypto';

const digest = e => createHash('sha256').update(JSON.stringify(e)).digest('hex');

function textOf(blocks) {
  return blocks.filter(b=>b.type==='text' && typeof b.text==='string').map(b=>b.text).join('');
}

function visible(event) {
  const d = event.data;
  if (event.type==='turn/start') return {kind:'start',turn:d.turn};
  if (event.type==='turn/end') return {kind:'end',turn:d.turn,completed:d.reason?.kind==='completed'};
  if (event.type==='user/message' && d.role==='user' && d.source?.kind==='user') {
    const text = textOf(d.content);
    const internal = /^mowulink-[a-f0-9]{64}$/.test(d.id) && text.startsWith('魔物链接投递\n');
    return {kind:'user',text:internal ? '' : text};
  }
  if (event.type==='assistant/message' && d.message?.role==='assistant' && d.message.source?.kind==='model') {
    const parts = [];
    for (const [index,block] of d.message.content.entries()) {
      if (block.type==='text' && typeof block.text==='string' && block.text.length) {
        const last = parts.at(-1);
        if (last?.type==='text') last.text+=block.text;
        else parts.push({type:'text',text:block.text,index});
      }
      if (block.type==='image') {
        const ref = block.attachment;
        if (!ref || !/^sha256:[0-9a-f]{64}$/.test(ref.attachmentId) || !Number.isSafeInteger(ref.bytes)
            || ref.bytes<=0 || ref.bytes>10*1024*1024) throw new Error('unsupported_display_image');
        parts.push({type:'image',index,sha256:ref.attachmentId.slice(7),size:ref.bytes});
      }
    }
    return {kind:'model',turn:d.turn,parts,
            intermediate:!!d.interrupted || d.message.content.some(b=>b.type==='tool-call')};
  }
  if (event.type==='tool/call') {
    // Show the operation name. Only publish recognizable command verbs, never raw tool arguments.
    let preview = '';
    if (['terminal','shell','exec_command'].includes(d.name)) {
      try {
        const cmd = JSON.parse(d.arguments);
        const match = /^(git (?:status|diff|log)|npm (?:test|run)|python3? -m unittest|ls|pwd)(?:\s|$)/
          .exec(typeof cmd.command==='string' ? cmd.command : cmd.cmd);
        if (match) preview = match[1];
      } catch { /* A tool with opaque arguments still has a public name. */ }
    }
    return {kind:'tool',turn:d.turn,name:d.name,preview};
  }
  return {kind:'other'};
}

async function withLog(ctx, agent, read) {
  if (!await ctx.get('sessions').flush(agent.session)) throw new Error('native_persistence_unavailable');
  const handle = await ctx.get('sessionPersistence').open(agent.session.id,'read');
  try {
    if (handle.id!==agent.session.id || handle.header.id!==agent.session.id) throw new Error('wrong_native_session');
    return await read(handle);
  } finally {
    await handle.close();
  }
}

export function baseline(ctx,agent) {
  return withLog(ctx,agent,async log=>{
    const {events} = await log.read(0);
    const boundary = events.slice(log.inheritedEventCount).findLast(e=>e.type==='turn/start'||e.type==='turn/end');
    const current = boundary?.type==='turn/start' ? {number:boundary.data.turn,id:boundary.seq+'/'+boundary.data.turn} : null;
    return {offset:events.length,anchor:events.length ? digest(events.at(-1)) : null,current};
  });
}

export function nextDisplay(ctx,agent,{offset,anchor}) {
  return withLog(ctx,agent,async log=>{
    if (!Number.isSafeInteger(offset) || offset<log.inheritedEventCount) throw new Error('unknown_display_cursor');
    const {events} = await log.read(Math.max(0,offset-1),offset ? 17 : 16);
    if (offset) {
      const previous = events.shift();
      if (previous?.seq!==offset-1 || digest(previous)!==anchor) throw new Error('display_history_changed');
    } else if (anchor!==null) throw new Error('unknown_display_cursor');
    let bytes = 0;
    const rows = [];
    for (const event of events) {
      if (event.seq!==offset+rows.length) throw new Error('display_history_changed');
      const row = {seq:event.seq,anchor:digest(event),...visible(event)};
      const length = Buffer.byteLength(JSON.stringify(row));
      if (length>800000) throw new Error('display_event_too_large');
      if (bytes+length>800000) break;
      rows.push(row);
      bytes+=length;
    }
    return {rows};
  });
}

export function displayImage(ctx,agent,{seq,index,anchor,offset},cache) {
  return withLog(ctx,agent,async log=>{
    if (!Number.isSafeInteger(seq) || seq<log.inheritedEventCount || !Number.isSafeInteger(index) || index<0)
      throw new Error('invalid_display_image');
    const [event] = (await log.read(seq,1)).events;
    if (!event || digest(event)!==anchor) throw new Error('display_history_changed');
    const row = visible(event);
    const part = row.parts?.find(p=>p.index===index && p.type==='image');
    if (row.kind!=='model' || !part || !Number.isSafeInteger(offset) || offset<0 || offset>=part.size)
      throw new Error('invalid_display_image');
    const ref = event.data.message.content[index].attachment;
    if (cache.sha!==part.sha256) {
      const {data} = await ctx.get('attachments').readImage(ref);
      if (data.length!==part.size || createHash('sha256').update(data).digest('hex')!==part.sha256)
        throw new Error('display_image_digest_mismatch');
      cache.sha=part.sha256;
      cache.data=data;
    }
    const data = cache.data;
    const block = Buffer.from(data.buffer,data.byteOffset+offset,Math.min(256*1024,data.length-offset));
    return {seq,index,offset,sha256:part.sha256,data:block.toString('base64')};
  });
}
