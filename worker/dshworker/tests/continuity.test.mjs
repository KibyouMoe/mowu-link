import test from "node:test";
import assert from "node:assert/strict";
import {mkdtemp,mkdir,rm,readFile,writeFile} from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import {createHash,randomUUID} from "node:crypto";
import {ReceiptArchive,compactAck,shrinkObservationText} from "../continuity.mjs";
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
async function legacyMemory(root,text='{"objective":"must-not-be-injected","revision":2}'){
 const dir=path.join(root,"work-memory");await mkdir(dir,{recursive:true});
 const file=path.join(dir,createHash("sha256").update("thread").digest("hex")+".json");
 await writeFile(file,text);return file;
}
test("legacy Worker memory is ignored, never read or injected into model input",async t=>{
 const f=await fixture(t),file=await legacyMemory(f.root);
 await f.manager.message(req("actual user input"));
 assert.equal(f.calls.find(c=>c.name==="send_message_to_thread").args.prompt,"actual user input");
 assert.equal(f.manager.workMemory,undefined);
 assert.equal(await readFile(file,"utf8"),'{"objective":"must-not-be-injected","revision":2}');
 assert.equal(f.manager.state.events.filter(e=>e.type==="memory.notice").length,0);
});

test("corrupt legacy memory cannot block native compaction or message delivery",async t=>{
 const f=await fixture(t),file=await legacyMemory(f.root,"{broken");
 await f.manager.message(req("/compact"));
 await f.manager.serial(()=>f.manager.processCompaction(f.session,{turns:[]}));
 await f.manager.observeContextEvent({kind:"compaction",threadId:"thread",turnId:"cmp",itemId:"c",status:"completed"});
 await f.manager.message(req("continue normally"));
 assert.equal(f.calls.filter(c=>c.name==="compact_thread").length,1);
 assert.equal(f.calls.find(c=>c.name==="send_message_to_thread").args.prompt,"continue normally");
 assert.equal(await readFile(file,"utf8"),"{broken");
});



test("native compaction preserves the thread and never adds a Worker summary to later inputs",async t=>{
 const f=await fixture(t),m=f.manager;
 const dispatch=async text=>{f.session.pendingInputs=[];return m.serial(()=>m.dispatch(f.session,req(text),{turns:[]}));};
 await dispatch("first");await dispatch("second");
 await m.observeContextEvent({kind:"compaction",threadId:"thread",turnId:"cmp",itemId:"cmp-item",status:"completed"});
 await dispatch("third");
 assert.deepEqual(f.calls.filter(c=>c.name==="send_message_to_thread").map(c=>c.args.prompt),["first","second","third"]);
 assert.equal(m.state.selectedThreadId,"thread");assert.equal(f.calls.filter(c=>c.name==="create_thread").length,0);
 assert.ok(!(await m.contextView(f.session)).memory);
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
test("cold restart keeps the native thread and retires old Worker memory bookkeeping",async t=>{
 const f=await fixture(t);await legacyMemory(f.root,"{broken");
 f.session.memoryAppliedRevision=2;await f.manager.save();await f.manager.close();
 const restored=await new DesktopManager(f.options).initialize();
 try{
  assert.equal(restored.session("thread").memoryAppliedRevision,undefined);
  await restored.serial(()=>restored.dispatch(restored.session("thread"),req("after restart"),{turns:[]}));
  assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").at(-1).args.prompt,"after restart");
  assert.equal(f.calls.filter(c=>c.name==="create_thread").length,0);
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
test("only native context is exposed; Worker memory APIs and command are retired",async t=>{
 const f=await fixture(t);const url=await f.manager.listen();f.manager.handoffPaused=false;
 const headers={authorization:"Bearer "+f.manager.config.token,"content-type":"application/json"};
 assert.equal((await fetch(url+"/manager/sessions/thread/context")).status,401);
 const view=await(await fetch(url+"/manager/sessions/thread/context",{headers})).json();
 assert.equal(view.threadId,"thread");assert.equal(view.memory,undefined);
 assert.equal(f.manager.health().capabilities.workMemory,undefined);
 for(const method of ["GET","POST"]){
  const response=await fetch(url+"/manager/sessions/thread/memory",{method,headers,...(method==="POST"?{body:JSON.stringify({expectedRevision:0,objective:"retired"})}:{})});
  assert.equal(response.status,404);
 }
 await assert.rejects(f.manager.message(req("/memory")),e=>e.body.error==="unknown_command");
 const help=await f.manager.message(req("/help"));
 assert.ok(!help.text.includes("/memory"));
});

test("a stale idle poll cannot compact a turn that started before the writer lock was acquired",async t=>{
 const f=await fixture(t);await f.manager.message(req("/compact"));
 f.manager.native=async(name,args)=>name==="read_thread"?{thread:{id:"thread",cwd:f.root},turns:[{id:"new",status:"inProgress"}]}:f.native(name,args);
 await f.manager.serial(()=>f.manager.processCompaction(f.session,{turns:[]}));
 assert.equal(f.calls.filter(c=>c.name==="compact_thread").length,0);
 assert.equal(f.session.contextState.compaction.status,"requested");
});
test("original model input preserves mirroring and native echo correlation",async t=>{
 const f=await fixture(t);
 await legacyMemory(f.root);
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
