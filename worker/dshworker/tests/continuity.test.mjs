import test from "node:test";
import assert from "node:assert/strict";
import {mkdtemp,rm,readFile,writeFile,stat} from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import {randomUUID} from "node:crypto";
import {WorkMemory,ReceiptArchive,memoryPrompt,compactAck,shrinkObservationText} from "../continuity.mjs";
import {DesktopManager,PROTOCOL} from "../desktop-manager.mjs";
import {projectContextEvent,projectJournalContext} from "../interface/context-events.mjs";
const req=(text,extra={})=>({protocolVersion:PROTOCOL,messageId:randomUUID(),source:"trainer",origin:"cli",targetThreadId:"thread",delivery:"steer",text,...extra});
async function fixture(t){
 const root=await mkdtemp(path.join(os.tmpdir(),"worker-continuity-")),calls=[],sent=[];
 const native=async(name,args)=>{calls.push({name,args:structuredClone(args)});
  if(name==="read_thread")return {thread:{id:args.threadId,cwd:root},turns:[]};
  if(name==="send_message_to_thread")return {turnId:"native-"+calls.length};
  if(name==="compact_thread")return {threadId:args.threadId,accepted:true};
  return {};
 };
 const names=["read_thread","send_message_to_thread","create_thread","fork_thread","set_thread_title","list_threads","compact_thread"];
 const options={stateDir:root,native,listNativeTools:async()=>names.map(name=>({name})),config:{
  token:"test-continuity-token-123456789",port:0,pollMs:1000000,workspaceRoots:[root],
  nativeSenderThreadId:"sender",messageSourceLabels:false,feishu:{transcriptEnabled:true}}};
 const manager=await new DesktopManager(options).initialize();manager.handoffPaused=true;
 const session=manager.makeSession({id:"thread",cwd:root},{alias:"long-lived",cwd:root});
 session.feishuChatId="chat";session.relayStatus={};session.seenImages={};
 manager.state.sessions.thread=session;manager.state.selectedThreadId="thread";manager.state.selectionEpoch=1;
 manager.notifier={enabled:true,chatId:"chat",sendMessage:async(text,opts)=>{
  sent.push({method:"POST",text,opts});return {code:0,data:{message_id:"card-"+sent.length}};
 },updateMessage:async(id,text,opts)=>{sent.push({method:"PATCH",id,text,opts});return {code:0,data:{message_id:id}};}};
 const flush=async()=>{manager.handoffPaused=false;await manager.drainPublicTexts();await manager.tail;manager.handoffPaused=true;clearTimeout(manager.publicTextTimer);manager.publicTextTimer=null;};
 t.after(async()=>{if(!manager.closed)await manager.close();await rm(root,{recursive:true,force:true});});
 return {root,manager,session,calls,sent,options,flush,native};
}
test("versioned work memory survives recreation and concurrent stale writers cannot overwrite it",async t=>{
 const f=await fixture(t),m=f.manager.workMemory;
 const first=await m.put("thread",{expectedRevision:0,objective:"保持同一主会话",constraints:["不新建主会话"],decisions:["已确认事实"]});
 assert.equal(first.revision,1);
 const writes=await Promise.allSettled([m.put("thread",{expectedRevision:1,currentState:"A"}),m.put("thread",{expectedRevision:1,currentState:"B"})]);
 assert.deepEqual(writes.map(x=>x.status),["fulfilled","rejected"]);
 assert.match(writes[1].reason.message,/revision_conflict/);
 const restored=await new WorkMemory(f.root).get("thread");assert.equal(restored.revision,2);assert.equal(restored.currentState,"A");
 assert.equal((await stat(m.file("thread"))).mode&0o777,0o600);
});
test("invalid or oversized memory never replaces the previous checkpoint",async t=>{
 const f=await fixture(t),m=f.manager.workMemory;
 await m.put("thread",{expectedRevision:0,objective:"safe"});
 for(const body of [{expectedRevision:1,arbitrary:"no"},{expectedRevision:1,constraints:"wrong"},{expectedRevision:1,objective:"x".repeat(4001)}])
  await assert.rejects(m.put("thread",body));
 assert.equal((await m.get("thread")).revision,1);assert.equal((await m.get("thread")).objective,"safe");
});
test("corrupt memory is preserved and refused instead of silently becoming empty",async t=>{
 const f=await fixture(t),file=f.manager.workMemory.file("thread");
 await f.manager.workMemory.put("thread",{expectedRevision:0,objective:"before corruption"});
 await writeFile(file,'{"threadId":"thread","revision":1}');const original=await readFile(file);
 await assert.rejects(f.manager.workMemory.get("thread"),/memory_corrupt/);
 assert.deepEqual(await readFile(file),original);
});
test("checkpoint injection is bounded historical data, never an extra model task",()=>{
 assert.equal(memoryPrompt({revision:0}),"");
 const text=memoryPrompt({revision:2,objective:"goal",currentState:"done",constraints:[],decisions:[],nextSteps:[],references:[]});
 assert.match(text,/历史任务资料/);assert.match(text,/不构成新的权限/);assert.match(text,/"revision":2/);assert.ok(text.endsWith("当前输入：\n"));
});
test("checkpoint is injected only when changed or after real compaction",async t=>{
 const f=await fixture(t),m=f.manager;
 await m.workMemory.put("thread",{expectedRevision:0,objective:"continuity-sentinel"});
 const dispatch=async text=>{f.session.pendingInputs=[];return m.serial(()=>m.dispatch(f.session,req(text),{turns:[]}));};
 await dispatch("first");await dispatch("second");
 let writes=f.calls.filter(c=>c.name==="send_message_to_thread");
 assert.match(writes[0].args.prompt,/continuity-sentinel/);assert.equal(writes[1].args.prompt,"second");
 await m.observeContextEvent({kind:"compaction",threadId:"thread",turnId:"cmp",itemId:"cmp-item",status:"completed"});
 await dispatch("third");writes=f.calls.filter(c=>c.name==="send_message_to_thread");
 assert.match(writes[2].args.prompt,/continuity-sentinel/);assert.ok(writes[2].args.prompt.endsWith("third"));
 assert.equal(m.state.selectedThreadId,"thread");assert.equal(f.calls.filter(c=>c.name==="create_thread").length,0);
});
test("explicit compaction waits for idle and an RPC acknowledgement never means completed",async t=>{
 const f=await fixture(t),id=randomUUID();
 await f.manager.message(req("/compact",{messageId:id}));
 assert.equal(f.session.contextState.compaction.status,"requested");
 await f.manager.serial(()=>f.manager.processCompaction(f.session,{turns:[{id:"busy",status:"inProgress"}]}));
 assert.equal(f.calls.filter(c=>c.name==="compact_thread").length,0);
 await f.manager.serial(()=>f.manager.processCompaction(f.session,{turns:[]}));
 assert.equal(f.calls.filter(c=>c.name==="compact_thread").length,1);assert.equal(f.session.contextState.compaction.status,"accepted");
 assert.equal(f.session.contextState.compactions,0);
 await f.manager.message(req("new work"));
 assert.equal(f.session.queue.length,1);assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,0);
});
test("real completion updates one compaction card, clears only text caches, and leaves the same thread",async t=>{
 const f=await fixture(t);
 f.session.seen.old={text:"large old text",hash:"same-hash",final:true};
 f.session.toolSeen={old:{itemId:"old",turnId:"old",status:"completed",preview:"old-output"},active:{itemId:"active",turnId:"turn",status:"running",preview:"keep"}};
 await f.manager.observeContextEvent({kind:"compaction",threadId:"thread",turnId:"cmp",itemId:"c",status:"in_progress"});await f.flush();
 await f.manager.observeContextEvent({kind:"compaction",threadId:"thread",turnId:"cmp",itemId:"c",status:"completed"});await f.flush();
 await f.manager.observeContextEvent({kind:"compaction",threadId:"thread",turnId:"cmp",itemId:"c",status:"completed"});await f.flush();
 assert.deepEqual(f.sent.map(c=>c.method),["POST","PATCH"]);assert.equal(f.sent[1].id,"card-1");
 assert.equal(f.session.contextState.compactions,1);assert.equal(f.session.seen.old.hash,"same-hash");assert.equal(f.session.seen.old.text,undefined);
 assert.equal(f.session.toolSeen.old.preview,undefined);assert.equal(f.session.toolSeen.active.preview,"keep");
 assert.equal(f.manager.state.selectedThreadId,"thread");
});
test("unconfirmed compaction is not retried after a connection error or manager recreation",async t=>{
 const f=await fixture(t);
 await f.manager.message(req("/compact"));
 f.manager.native=async(name,args)=>{if(name==="compact_thread"){f.calls.push({name,args});throw Error("lost ack");}return f.native(name,args);};
 await f.manager.serial(()=>f.manager.processCompaction(f.session,{turns:[]}));
 await f.manager.serial(()=>f.manager.processCompaction(f.session,{turns:[]}));
 assert.equal(f.calls.filter(c=>c.name==="compact_thread").length,1);assert.equal(f.session.contextState.compaction.status,"unknown");
 await f.manager.close();const restored=await new DesktopManager(f.options).initialize();
 try{assert.equal(restored.session("thread").contextState.compaction.status,"unknown");assert.equal(restored.state.selectedThreadId,"thread");}
 finally{await restored.close();}
});
test("usage display distinguishes current context from lifetime token consumption without publishing raw data",async t=>{
 const f=await fixture(t);
 const event=projectContextEvent("thread/tokenUsage/updated",{threadId:"thread",turnId:"turn",tokenUsage:{last:{inputTokens:30,totalTokens:42},total:{totalTokens:900000},modelContextWindow:100000},secret:"not-public"});
 await f.manager.observeContextEvent(event);
 const view=await f.manager.contextView(f.session);
 assert.equal(view.context.usage.contextTokens,42);assert.equal(view.context.usage.lifetimeTokens,900000);
 assert.equal(f.sent.length,0);assert.ok(!JSON.stringify(view).includes("not-public"));
});
test("context projection never exports native compacted summaries or reasoning",()=>{
 const event=projectContextEvent("item/completed",{threadId:"thread",turnId:"turn",item:{type:"contextCompaction",id:"c",summary:"private-summary"}});
 assert.equal(event.status,"completed");assert.ok(!JSON.stringify(event).includes("private-summary"));
 assert.equal(projectContextEvent("item/completed",{threadId:"thread",turnId:"turn",item:{type:"reasoning",id:"r",text:"private"}}),null);
 const j=projectJournalContext({type:"event_msg",payload:{type:"token_count",info:{last_token_usage:{total_tokens:21},total_token_usage:{total_tokens:800},model_context_window:1000}}},"thread");
 assert.equal(j.contextTokens,21);
});
test("compact is explicitly unavailable on transports without the native operation",async t=>{
 const f=await fixture(t);f.manager.tools=f.manager.tools.filter(x=>x.name!=="compact_thread");
 await assert.rejects(f.manager.message(req("/compact")),e=>e.status===501);
 assert.equal(f.calls.filter(c=>["compact_thread","create_thread"].includes(c.name)).length,0);
});
test("cold restart reattaches an existing checkpoint instead of creating a conversation",async t=>{
 const f=await fixture(t);
 await f.manager.workMemory.put("thread",{expectedRevision:0,objective:"restart-sentinel"});
 f.session.memoryAppliedRevision=1;await f.manager.save();await f.manager.close();
 const restored=await new DesktopManager(f.options).initialize();
 try{
  assert.equal(restored.session("thread").memoryAppliedRevision,0);
  await restored.serial(()=>restored.dispatch(restored.session("thread"),req("after restart"),{turns:[]}));
  assert.match(f.calls.filter(c=>c.name==="send_message_to_thread").at(-1).args.prompt,/restart-sentinel/);
 }finally{await restored.close();}
});
test("receipt archival keeps duplicate replay and content-conflict protection after restart",async t=>{
 const f=await fixture(t);f.manager.config.runtimeReceiptLimit=1;
 const original=req("/status");const first=await f.manager.message(original);await f.manager.message(req("/status"));
 assert.equal(f.manager.state.receipts[original.messageId],undefined);
 assert.ok(await f.manager.receiptArchive.get(original.messageId));
 const replay=await f.manager.message(original);assert.equal(replay.duplicate,true);assert.equal(replay.action,first.action);
 await f.manager.close();
 const restored=await new DesktopManager(f.options).initialize();
 try {
  const replayed=await restored.message(original);assert.equal(replayed.duplicate,true);assert.equal(replayed.action,first.action);
  await assert.rejects(restored.message({...original,text:"changed"}),e=>e.body.error==="message_id_conflict");
  assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,0);
 } finally {await restored.close();}
});
test("pending and uncertain native writes are pinned while completed control receipts can move to disk",async t=>{
 const f=await fixture(t),s=f.manager.state;
 const pending=randomUUID(),unknown=randomUUID(),done=randomUUID();
 s.receipts[pending]={pending:true,request:{messageId:pending}};
 s.receipts[unknown]={pending:false,nativeDispatch:{status:"unknown"},error:{error:"unknown"},request:{messageId:unknown}};
 s.receipts[done]={fingerprint:"done",pending:false,response:{action:"help"},request:{messageId:done}};
 await new ReceiptArchive(f.root).trim(s,1);
 assert.ok(s.receipts[pending]);assert.ok(s.receipts[unknown]);assert.equal(s.receipts[done],undefined);
});
test("receipt acknowledgements exclude growing observation caches without changing public task metadata",()=>{
 const value=compactAck({action:"done",session:{threadId:"thread",alias:"same",seen:{big:{text:"x"}},toolSeen:{},turnMessages:{},queue:[]}});
 assert.equal(value.session.threadId,"thread");assert.equal(value.session.alias,"same");assert.equal(value.session.seen,undefined);
 assert.equal(compactAck(undefined),undefined);
});
test("memory HTTP endpoints enforce authentication and compare-and-swap revisions",async t=>{
 const f=await fixture(t);const url=await f.manager.listen();f.manager.handoffPaused=false;
 const send=(body,token=f.manager.config.token)=>fetch(url+"/manager/sessions/thread/memory",{method:"POST",headers:{authorization:"Bearer "+token,"content-type":"application/json"},body:JSON.stringify(body)});
 assert.equal((await send({expectedRevision:0,objective:"private"},"wrong")).status,401);
 assert.equal((await send({expectedRevision:0,objective:"public checkpoint"})).status,200);
 assert.equal((await send({expectedRevision:0,objective:"stale"})).status,409);
 const view=await(await fetch(url+"/manager/sessions/thread/context",{headers:{authorization:"Bearer "+f.manager.config.token}})).json();
 assert.equal(view.memory.revision,1);assert.equal(view.threadId,"thread");
});

test("a stale idle poll cannot compact a turn that started before the writer lock was acquired",async t=>{
 const f=await fixture(t);await f.manager.message(req("/compact"));
 f.manager.native=async(name,args)=>name==="read_thread"?{thread:{id:"thread",cwd:f.root},turns:[{id:"new",status:"inProgress"}]}:f.native(name,args);
 await f.manager.serial(()=>f.manager.processCompaction(f.session,{turns:[]}));
 assert.equal(f.calls.filter(c=>c.name==="compact_thread").length,0);
 assert.equal(f.session.contextState.compaction.status,"requested");
});
test("checkpoint wrapping preserves original input mirroring and native echo correlation",async t=>{
 const f=await fixture(t);
 await f.manager.workMemory.put("thread",{expectedRevision:0,objective:"memory-only"});
 const input=req("original user input");await f.manager.message(input);
 const sent=f.calls.find(c=>c.name==="send_message_to_thread");
 const receipt=f.manager.state.receipts[input.messageId];
 await f.manager.observeNativeInput(f.session,{key:"native-input",turnId:receipt.nativeTurnId,text:sent.args.prompt});
 await f.flush();
 const records=(await f.manager.messageJournal.list()).records.filter(r=>r.direction==="input");
 assert.deepEqual(records.map(r=>r.text),["original user input"]);
 assert.equal(f.sent.filter(s=>s.text==="original user input").length,1);
});

test("an automatic compaction cannot consume a manual request that has not reached the native RPC",async t=>{
 const f=await fixture(t);await f.manager.message(req("/compact"));
 const original=f.session.contextState.compaction.requestId;
 await f.manager.observeContextEvent({kind:"compaction",threadId:"thread",turnId:"busy",itemId:"auto",status:"completed"});
 assert.equal(f.session.contextState.compaction.requestId,original);
 assert.equal(f.session.contextState.compaction.status,"requested");
 await f.manager.serial(()=>f.manager.processCompaction(f.session,{turns:[]}));
 assert.equal(f.calls.filter(c=>c.name==="compact_thread").length,1);
});
test("late completion of a baseline compaction does not complete the new manual operation",async t=>{
 const f=await fixture(t);
 f.manager.native=async(name,args)=>name==="read_thread"?{thread:{id:"thread",cwd:f.root},turns:[
  {id:"old",status:"completed",items:[{id:"old-cmp",type:"contextCompaction"}]}]}:f.native(name,args);
 await f.manager.message(req("/compact"));
 await f.manager.serial(()=>f.manager.processCompaction(f.session,{turns:[]}));
 await f.manager.observeContextEvent({kind:"compaction",threadId:"thread",turnId:"old",itemId:"old-cmp",status:"completed"});
 assert.equal(f.session.contextState.compaction.status,"accepted");
 await f.manager.observeContextEvent({kind:"compaction",threadId:"thread",turnId:"new",itemId:"new-cmp",status:"completed"});
 assert.equal(f.session.contextState.compaction.status,"completed");
});
