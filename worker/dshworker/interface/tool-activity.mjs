// Project native schemas into the shared public lifecycle contract.
import { safeToolPreview } from "../tool-progress.mjs";
const known = new Map([["CommandExecution","terminal"],["commandExecution","terminal"],
 ["McpToolCall","mcp"],["mcpToolCall","mcp"],["DynamicToolCall","dynamic"],["dynamicToolCall","dynamic"],
 ["FileChange","patch"],["fileChange","patch"],["WebSearch","web_search"],["webSearch","web_search"],
 ["ImageGeneration","image_generation"],["imageGeneration","image_generation"]]);
const identity = v => typeof v === "string" && v.length > 0 && v.length <= 256;
const label = v => String(v ?? "").replace(/[^a-zA-Z0-9_.:/-]/g,"_").slice(0,100);
export function projectToolActivity(item, {threadId,turnId,completed=false,timestamp}={}) {
 const category=known.get(item?.type);
 if (!category || !identity(threadId) || !identity(turnId) || !identity(item.id)) return null;
 let toolName=category,preview="";
 if(category==="terminal") preview=typeof item.command==="string"?item.command:
  Array.isArray(item.command)&&item.command.every(x=>typeof x==="string")?item.command.join(" "):"";
 else if(category==="mcp"||category==="dynamic") {
  toolName=[label(item.server),label(item.tool??item.name)].filter(Boolean).join("/")||"tool";
  const args=item.arguments;
  if(args&&typeof args==="object") preview=["title","query","url","path","file_path"].map(k=>args[k]).find(v=>typeof v==="string")??"";
 } else if(category==="patch") preview=Array.isArray(item.changes)?item.changes.length+" file(s)":"";
 else if(category==="web_search") preview=item.query??item.action?.query??"";
 const raw=String(item.status??"").toLowerCase(),exitCode=Number.isInteger(item.exitCode??item.exit_code)?item.exitCode??item.exit_code:null;
 const failed=["failed","error","declined","denied"].includes(raw)||exitCode!=null&&exitCode!==0||item.isError===true||item.result?.isError===true;
 const status=failed?"failed":["interrupted","cancelled","canceled"].includes(raw)?"interrupted":
  completed||["completed","complete","succeeded","done"].includes(raw)?"completed":"running";
 const duration=item.durationMs??item.duration;
 const ms=typeof duration==="number"?duration:duration&&typeof duration.secs==="number"?duration.secs*1000+(duration.nanos??0)/1e6:null;
 return {threadId,turnId,itemId:item.id,toolName:label(toolName),category,preview:safeToolPreview(preview),
  status,exitCode,durationMs:Number.isFinite(ms)&&ms>=0?Math.round(ms):null,
  timestamp:typeof timestamp==="string"?timestamp:new Date().toISOString()};
}
