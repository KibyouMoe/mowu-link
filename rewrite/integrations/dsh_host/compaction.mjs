// The native session log is the authority for completed compaction.
function observe(events, commandId) {
  const records = events.filter(event=>event.data?.sourceCommandId===commandId);
  const starts = records.filter(event=>event.type==='compaction/start' && event.data.turn===null);
  if (!starts.length) return null;
  if (starts.length!==1) throw new Error('compact_unconfirmed');
  const start = starts[0];
  const id = start.data.compactionId;
  const ends = records.filter(event=>event.type==='compaction/end' && event.data.compactionId===id);
  if (!ends.length) return {state:'running'};
  if (ends.length!==1 || ends[0].seq<=start.seq) throw new Error('compact_unconfirmed');
  const [end] = ends;
  if (end.data.error) return {state:'failed'};
  const summaries = records.filter(event=>event.type==='compaction/summary' && event.data.compactionId===id);
  const checkpoints = events.filter(event=>event.type==='user/message' && event.data.source?.kind==='compact-checkpoint'
    && event.data.source.sourceCommandId===commandId && event.data.source.compactionId===id);
  if (summaries.length!==1 || checkpoints.length!==1) throw new Error('compact_unconfirmed');
  const [summary] = summaries;
  const [checkpoint] = checkpoints;
  if (!(start.seq<summary.seq && summary.seq<checkpoint.seq && checkpoint.seq<end.seq)
      || checkpoint.surfaceOp?.op!=='replace'
      || checkpoint.surfaceOp.startSeq!==summary.data.shadowedRange?.start
      || checkpoint.surfaceOp.endSeq!==summary.data.shadowedRange?.end
      || !Array.isArray(summary.data.shadowedSeqs) || !summary.data.shadowedSeqs.length
      || !Number.isSafeInteger(summary.data.shadowedTokenCount) || summary.data.shadowedTokenCount<0) {
    throw new Error('compact_unconfirmed');
  }
  return {state:'completed',compaction_id:id,end_seq:end.seq,summary_seq:summary.seq,
    items:summary.data.shadowedSeqs.length,tokens:summary.data.shadowedTokenCount};
}

export function compaction(ctx, agent, readEvents) {
  // No-history and pre-admission refusals have no native log record. Cache only
  // the latest local observation, with a durable Worker intent preventing resend.
  let latest;
  const engine = () => ctx.get('compaction');
  return {
    get supported() { return typeof engine()?.compactNow==='function'; },
    get inProgress() { return latest?.state==='running'; },
    async request(operation,commandId) {
      if (typeof commandId!=='string' || !/^mowulink-[a-f0-9]{64}$/.test(commandId)) {
        throw new Error('invalid_compact_request');
      }
      const result = value=>({session:agent.session.id,command_id:commandId,...value});
      const known = observe(await readEvents(), commandId);
      if (known) return result(known);
      if (latest?.id===commandId) return result({state:latest.state});
      if (operation==='compact-status') return result({state:'missing'});
      if (!this.supported) return result({state:'unavailable'});
      if (agent.status!=='idle' || this.inProgress) return result({state:'busy'});
      const current = {id:commandId,state:'running'};
      latest = current;
      try {
        Promise.resolve(engine().compactNow(agent,AbortSignal.timeout(240000),commandId)).then(
          value=>{ current.state = value===null ? 'empty' : 'unknown'; },
          error=>{ current.state = error?.code==='busy' ? 'busy' : 'unknown'; });
      } catch (error) {
        current.state = error?.code==='busy' ? 'busy' : 'unknown';
      }
      return result({state:current.state});
    },
  };
}
