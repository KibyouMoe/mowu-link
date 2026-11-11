import test from "node:test";
import assert from "node:assert/strict";
import {mkdtemp,rm,readFile} from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import {randomUUID} from "node:crypto";
import {DesktopManager} from "../desktop-manager.mjs";
import {projectToolActivity} from "../interface/tool-activity.mjs";
import {safeToolPreview,toolProgressLine,addToolToBubble,renderToolBubble,TOOL_EDIT_INTERVAL_MS} from "../tool-progress.mjs";
import {publicJournalItem} from "../local-public-journal.mjs";
const fence=String.fromCharCode(96).repeat(3);
const activity=(id,extra={})=>({threadId:"thread",turnId:"turn",itemId:id,toolName:"terminal",category:"terminal",
 preview:"echo ok",status:"running",timestamp:new Date().toISOString(),...extra});
async function fixture(t) {
 const root=await mkdtemp(path.join(os.tmpdir(),"worker-tool-progress-")),nativeCalls=[],calls=[];
 const native=async(name,args)=>{nativeCalls.push({name,args});return name==="read_thread"?{thread:{id:args.threadId,cwd:root},turns:[]}:{};};
 const names=["read_thread","send_message_to_thread","create_thread","fork_thread","set_thread_title","list_threads"];
 const options={stateDir:root,native,listNativeTools:async()=>names.map(name=>({name})),
  config:{token:"test-only-tool-token-1234567890",pollMs:1000000,workspaceRoots:[root],feishu:{transcriptEnabled:true,toolProgressEnabled:true}}};
 const manager=await new DesktopManager(options).initialize();manager.handoffPaused=true;
 const session=manager.makeSession({id:"thread",cwd:root},{alias:"main",cwd:root});
 session.feishuChatId="chat";session.relayStatus={};session.seenImages={};
 manager.state.sessions.thread=session;manager.state.selectedThreadId="thread";manager.state.selectionEpoch=1;
 const notifier={enabled:true,chatId:"chat",sendMessage:async(text,opts)=>{
  calls.push({method:"POST",text,opts});return {code:0,data:{message_id:"card-"+calls.length}};
 },updateMessage:async(id,text,opts)=>{calls.push({method:"PATCH",id,text,opts});return {code:0,data:{message_id:id}};}};
 manager.notifier=notifier;
 const flush=async()=>{manager.handoffPaused=false;await manager.drainPublicTexts();await manager.tail;manager.handoffPaused=true;
  clearTimeout(manager.publicTextTimer);manager.publicTextTimer=null;};
 t.after(async()=>{if(!manager.closed)await manager.close();await rm(root,{recursive:true,force:true});});
 return {manager,session,calls,nativeCalls,flush,root,options,notifier};
}
test("Hermes terminal format has a first-line 40-character preview and balanced fences",()=>{
 const a=activity("1",{preview:"echo "+"abc ".repeat(20)+"\nsecond"});
 assert.equal(safeToolPreview(a.preview),"echo "+"abc ".repeat(8)+"...");
 assert.equal(toolProgressLine(a),"💻 terminal\n"+fence+"\n"+"echo "+"abc ".repeat(8)+"...\n"+fence);
 assert.equal(safeToolPreview("first\nsecond"),"first ...");
 assert.equal(TOOL_EDIT_INTERVAL_MS,1500);
});
test("Hermes grouping suppresses repeated terminal headers and keeps repeat counts outside fences",()=>{
 let b=null;
 for(let i=0;i<4;i++)b=addToolToBubble(b,activity(String(i)));
 const text=renderToolBubble(b);
 assert.equal(text.match(/💻 terminal/g).length,1);
 assert.ok(text.endsWith(fence+"\n(×3)"));
 assert.equal(text.split(fence).length%2,1);
});
test("consecutive identical generic tools become one line with the Hermes repeat counter",()=>{
 let b=null;
 for(let i=0;i<3;i++)b=addToolToBubble(b,activity(String(i),{category:"mcp",toolName:"browser/read",preview:""}));
 assert.equal(renderToolBubble(b),"⚙️ browser/read... (×3)");
});
test("native projection redacts secrets and drops raw arguments, tool results, and reasoning",()=>{
 const a=projectToolActivity({type:"mcpToolCall",id:"m",server:"s",tool:"search",arguments:{query:"token=private-value",api_key:"secret-value"},
  result:{content:"private-output"},status:"completed"},{threadId:"thread",turnId:"turn",completed:true});
 assert.equal(a.preview,"token=[redacted]");
 assert.ok(!JSON.stringify(a).includes("private-value"));assert.ok(!JSON.stringify(a).includes("secret-value"));assert.ok(!JSON.stringify(a).includes("private-output"));
 assert.equal(projectToolActivity({type:"Reasoning",id:"r",summary_text:"private"},{threadId:"thread",turnId:"turn"}),null);
 assert.equal(safeToolPreview("curl --token very-secret"),"curl --token [redacted]");
 assert.equal(safeToolPreview("Authorization: Bearer super-secret"),"Authorization: [redacted] [redacted]");
});
test("native started/completed events produce one visible call and two honest durable lifecycle states",async t=>{
 const f=await fixture(t);
 await f.manager.observeToolActivity(activity("call"));
 await f.flush();
 await f.manager.observeToolActivity(activity("call",{status:"completed",exitCode:0,durationMs:321}));
 await f.manager.observeToolActivity(activity("call"));
 await f.flush();
 assert.equal(f.calls.length,1);
 const page=await f.manager.messageJournal.list(),trace=page.records.filter(r=>r.kind==="tool_event");
 assert.deepEqual(trace.map(r=>r.tool.status),["running","completed"]);
 assert.equal(trace[1].tool.durationMs,321);assert.equal(trace[1].delivery.state,"local_only");
 assert.equal(f.nativeCalls.filter(c=>c.name==="send_message_to_thread").length,0);
});
test("same-tool parallel calls correlate by item ID even when completions arrive out of order",async t=>{
 const f=await fixture(t),base={category:"mcp",toolName:"s/read",preview:""};
 await Promise.all([f.manager.observeToolActivity(activity("a",base)),f.manager.observeToolActivity(activity("b",base))]);
 await f.manager.observeToolActivity(activity("b",{...base,status:"failed",exitCode:1}));
 await f.manager.observeToolActivity(activity("a",{...base,status:"completed",exitCode:0}));
 await f.flush();
 assert.equal(f.session.toolSeen["turn/a"].status,"completed");assert.equal(f.session.toolSeen["turn/b"].status,"failed");
 assert.equal(f.calls.length,1);assert.equal(f.calls[0].text,"⚙️ s/read... (×2)");
});
test("updates coalesce at 1.5 seconds and always PATCH the existing progress card",async t=>{
 const f=await fixture(t),original=Date.now;let now=10000;Date.now=()=>now;
 try {
  await f.manager.observeToolActivity(activity("a"));await f.flush();
  now=11000;await f.manager.observeToolActivity(activity("b",{preview:"echo second"}));await f.flush();
  assert.equal(f.calls.length,1);
  now=11500;await f.flush();
  assert.deepEqual(f.calls.map(c=>c.method),["POST","PATCH"]);assert.equal(f.calls[1].id,"card-1");
 } finally {Date.now=original;}
});
test("assistant prose closes the progress bubble and later tools open a new one",async t=>{
 const f=await fixture(t);
 await f.manager.observeToolActivity(activity("a"));await f.flush();
 await f.manager.publicReply({targetThreadId:"thread",messageId:randomUUID()},"我继续检查下一项");
 await f.manager.observeToolActivity(activity("b"));
 await f.flush();
 assert.deepEqual(f.calls.map(c=>c.method),["POST","POST","POST"]);
 assert.equal(f.calls[1].text,"我继续检查下一项");
 assert.notEqual(f.calls[0].opts.uuid,f.calls[2].opts.uuid);
});
test("progress delivery does not finish the trainer typing indicator or impersonate an assistant reply",async t=>{
 const f=await fixture(t),id=randomUUID();
 f.manager.state.feishuReceipts[id]={incomingMessageId:"original",cleanupRequested:false};
 await f.manager.observeToolActivity(activity("a"));await f.flush();
 assert.equal(f.manager.state.feishuReceipts[id].cleanupRequested,false);
 assert.equal(f.session.deliveredProgress?.["tool-progress:turn:1"],undefined);
 assert.equal((await f.manager.messageJournal.list()).records.find(r=>r.kind==="tool_progress").source,"tool");
});
test("turn end records interrupted tools once and does not invent completed status",async t=>{
 const f=await fixture(t);await f.manager.observeToolActivity(activity("a"));
 await f.manager.observeToolActivity({threadId:"thread",turnId:"turn",kind:"turn_end",status:"interrupted"});
 await f.flush();assert.equal(f.session.toolSeen["turn/a"].status,"interrupted");
 assert.equal(f.session.toolBubble,null);assert.equal(f.calls.length,1);
});
test("old journal tools and unmanaged sessions never replay progress on deployment",async t=>{
 const f=await fixture(t);
 assert.equal(await f.manager.observeToolActivity(activity("old",{timestamp:"2000-01-01T00:00:00Z"})),false);
 assert.equal(await f.manager.observeToolActivity(activity("other",{threadId:"unmanaged"})),false);
 await f.flush();assert.equal(f.calls.length,0);
});
test("completed journal projection is opt-in and contains public lifecycle metadata only",()=>{
 const id="01a17e53-76b1-7e63-bd85-2161ef913ce8";
 const r={type:"event_msg",timestamp:new Date().toISOString(),payload:{type:"item_completed",thread_id:id,turn_id:"turn",
  item:{type:"CommandExecution",id:"c",command:"echo ok",aggregated_output:"private-output",exit_code:0,duration:{secs:1,nanos:500000000}}}};
 assert.equal(publicJournalItem(r,id),null);
 const p=publicJournalItem(r,id,{includeTools:true});
 assert.equal(p.item.activity.status,"completed");assert.equal(p.item.activity.durationMs,1500);
 assert.ok(!JSON.stringify(p).includes("private-output"));
});
test("snapshot fallback seeds old history and only projects new tools",async t=>{
 const f=await fixture(t),snapshot={turns:[{id:"turn",status:"inProgress",items:[{type:"commandExecution",id:"old",command:"echo old",status:"completed"}]}]};
 await f.manager.serial(()=>f.manager.observeSnapshotTools(f.session,snapshot));
 snapshot.turns[0].items.push({type:"commandExecution",id:"new",command:"echo new",status:"inProgress"});
 await f.manager.serial(()=>f.manager.observeSnapshotTools(f.session,snapshot));await f.flush();
 assert.equal(f.calls.length,1);assert.ok(f.calls[0].text.includes("echo new"));assert.ok(!f.calls[0].text.includes("echo old"));
});
test("persisted progress retains its card and PATCHes it after manager restart",async t=>{
 const f=await fixture(t);await f.manager.observeToolActivity(activity("a"));await f.flush();await f.manager.close();
 const restored=await new DesktopManager(f.options).initialize();
 try {
  restored.handoffPaused=true;restored.notifier=f.notifier;
  assert.equal(restored.session("thread").toolSeen["turn/a"].status,"unknown");
  await restored.observeToolActivity(activity("b",{preview:"echo after restart"}));
  for(const e of Object.values(restored.state.publicTextOutbox))e.lastAttemptMs=0;
  restored.handoffPaused=false;await restored.drainPublicTexts();
  assert.deepEqual(f.calls.map(c=>c.method),["POST","PATCH"]);
  assert.equal(f.calls[1].id,"card-1");
 }finally{await restored.close();}
});
test("failed card creation is retained as unknown and is never retried with a new UUID",async t=>{
 const f=await fixture(t);f.notifier.sendMessage=async(text,opts)=>{f.calls.push({text,opts});return null;};
 await f.manager.observeToolActivity(activity("a"));await f.flush();
 await f.manager.observeToolActivity(activity("b"));await f.flush();
 assert.equal(f.calls.length,1);
 assert.equal(Object.values(f.manager.state.publicTextOutbox)[0].status,"unknown");
});

test("tool preview markup never authorizes uploading a local image",async t=>{
 const f=await fixture(t),images=[];
 f.manager.publicImageRelay.observe=(...args)=>images.push(args);
 await f.manager.observeToolActivity(activity("a",{category:"mcp",toolName:"s/query",preview:"![x](/tmp/private.png)"}));
 await f.flush();assert.equal(images.length,0);
});
