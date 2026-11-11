// Hermes: all+accumulate, edits >=1.5s, 40-character previews, (×N) repeats.
export const TOOL_EDIT_INTERVAL_MS=1500;
const fence=String.fromCharCode(96).repeat(3);
export function safeToolPreview(value) {
 if(typeof value!=="string") return "";
 let text=value.replace(/\x1b\[[0-9;]*m/g,"").replace(/[\u0000-\u0008\u000b-\u001f\u007f]/g,"");
 text=text.replace(/\bBearer\s+[a-z0-9._~+/-]+=*/ig,"Bearer [redacted]")
  .replace(/((?:password|passwd|secret|token|api[_-]?key|authorization)\s*[:=]\s*)(?:"[^"]*"|'[^']*'|[^\s,;]+)/ig,"$1[redacted]")
  .replace(/(--(?:password|secret|token|api-key)\s+)(?:"[^"]*"|'[^']*'|\S+)/ig,"$1[redacted]")
  .replace(/https?:\/\/[^/\s:@]+:[^@\s/]+@/ig,"https://[redacted]@")
  .replace(/\b(?:sk-|gh[pousr]_|AIza)[a-z0-9_-]{12,}\b/ig,"[redacted]");
 text=text.replace(/\b[a-zA-Z0-9_+/-]{32,}={0,2}\b/g,"[redacted]");
 const lines=text.trim().split(/\r?\n/),chars=Array.from(lines[0]??"");
 return chars.length>40?chars.slice(0,37).join("")+"...":chars.join("")+(lines.length>1?" ...":"");
}
export function toolProgressLine(a,omitTerminalHeader=false) {
 const preview=safeToolPreview(a.preview);
 if(a.category==="terminal"&&preview) return (omitTerminalHeader?"":"💻 terminal\n")+fence+"\n"+
  preview.replaceAll(fence,String.fromCharCode(39).repeat(3))+"\n"+fence;
 const emoji={terminal:"💻",patch:"🔧",web_search:"🔍",image_generation:"🎨"}[a.category]??"⚙️";
 return emoji+" "+a.toolName+(preview?': "'+preview.replace(/["<>]/g,"")+'"':"...");
}
export function renderToolBubble(b) {
 return b.lines.map(l=>l.count===1?l.text:l.text+(l.text.endsWith(fence)?"\n":" ")+"(×"+l.count+")").join("\n");
}
export function addToolToBubble(previous,a) {
 let b=previous?.turnId===a.turnId?structuredClone(previous):{turnId:a.turnId,lines:[],lastTerminal:false};
 const text=toolProgressLine(a,b.lastTerminal);
 b.lastTerminal=a.category==="terminal"&&Boolean(a.preview);
 const last=b.lines.at(-1);if(last?.text===text) last.count++;else b.lines.push({text,count:1});
 if(renderToolBubble(b).length>7600) b={turnId:a.turnId,lines:[{text:toolProgressLine(a),count:1}],
  lastTerminal:a.category==="terminal"&&Boolean(a.preview),rollover:true};
 return b;
}
