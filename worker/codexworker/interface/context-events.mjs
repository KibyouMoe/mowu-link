// Engine-specific event/schema mapping; no compaction summaries are exported.
const n=x=>Number.isSafeInteger(x)&&x>=0?x:null;
export function projectContextEvent(method,p){
 if(typeof p?.threadId!=="string")return null;
 if(method==="thread/tokenUsage/updated"){
  const u=p.tokenUsage??{};
  return {kind:"usage",threadId:p.threadId,turnId:p.turnId,
   inputTokens:n(u.last?.inputTokens),contextTokens:n(u.last?.totalTokens),
   lifetimeTokens:n(u.total?.totalTokens),modelContextWindow:n(u.modelContextWindow),timestamp:new Date().toISOString()};
 }
 if(["item/started","item/completed"].includes(method)&&p.item?.type==="contextCompaction"&&typeof p.item.id==="string"&&typeof p.turnId==="string")
  return {kind:"compaction",threadId:p.threadId,turnId:p.turnId,itemId:p.item.id,
   status:method==="item/started"?"in_progress":"completed",timestamp:new Date().toISOString()};
 if(method==="turn/completed")return {kind:"turn_end",threadId:p.threadId,turnId:p.turn?.id,status:p.turn?.status,timestamp:new Date().toISOString()};
 return null;
}
export function projectJournalContext(record,threadId){
 if(record?.type!=="event_msg")return null;const p=record.payload??{};
 if(p.type==="token_count"){
  const u=p.info??{};
  return {kind:"usage",source:"journal",threadId,turnId:p.turn_id??null,inputTokens:n(u.last_token_usage?.input_tokens),
   contextTokens:n(u.last_token_usage?.total_tokens),lifetimeTokens:n(u.total_token_usage?.total_tokens),
   modelContextWindow:n(u.model_context_window),timestamp:record.timestamp};
 }
 if(p.type==="item_completed"&&p.thread_id===threadId&&p.item?.type==="ContextCompaction")
  return {kind:"compaction",source:"journal",threadId,turnId:p.turn_id,itemId:p.item.id,status:"completed",timestamp:record.timestamp};
 return null;
}
