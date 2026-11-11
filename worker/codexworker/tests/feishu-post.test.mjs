import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { DesktopManager, PROTOCOL } from "../desktop-manager.mjs";
import { parseFeishuPost } from "../feishu-post.mjs";

const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1WkAAAAASUVORK5CYII=", "base64");
const content = keys => JSON.stringify({title:"图片问题",content:[...keys.map(image_key=>[{tag:"img",image_key}]),[{tag:"text",text:"这个是啥呀"}]]});
const event = (body=content(["img_first"]),extra={}) => ({message_id:"om_post_test",message_type:"post",open_id:"trainer",chat_type:"p2p",sender_type:"user",chat_id:"chat",content:body,...extra});
async function fixture(t){
 const root=await mkdtemp(path.join(os.tmpdir(),"worker-feishu-post-")),calls=[],downloads=[],notices=[];
 const native=async(name,args)=>{
  calls.push({name,args:structuredClone(args)});
  if(name==="read_thread")return {thread:{id:args.threadId,cwd:root},turns:[]};
  if(name==="send_message_to_thread")return {turnId:"real-turn"};
  return {};
 };
 const options={native,listNativeTools:async()=>["read_thread","send_message_to_thread","list_threads","create_thread","fork_thread","set_thread_title"].map(name=>({name})),stateDir:root,
  config:{token:"isolated-post-test-token",pollMs:1000000,workspaceRoots:[root],messageSourceLabels:false,nativeSenderThreadId:"manager",
   feishu:{trainerOpenIdWhitelist:["trainer"],transcriptEnabled:true}}};
 const manager=await new DesktopManager(options).initialize();
 manager.handoffPaused=true;
 const session=manager.makeSession({id:"thread",cwd:root},{alias:"main",cwd:root});
 session.feishuChatId="chat";session.relayStatus={};session.seenImages={};
 manager.state.sessions.thread=session;manager.state.selectedThreadId="thread";manager.state.selectionEpoch=1;
 const notifier={enabled:true,chatId:"chat",addReaction:async()=>null,removeReaction:async()=>true,
  sendMessage:async text=>{notices.push(text);return {code:0,data:{message_id:"card-"+notices.length}};},
  updateMessage:async()=>({code:0}),downloadImage:async(mid,key)=>{downloads.push({mid,key});return png;}};
 manager.notifier=notifier;
 t.after(async()=>{if(!manager.closed)await manager.close();await rm(root,{recursive:true,force:true});});
 return {root,manager,session,calls,downloads,notices,notifier,options};
}
test("real Feishu post shape preserves the caption and does not double content_v2 images",()=>{
 const raw=JSON.parse(content(["img_first"]));raw.content_v2=raw.content;
 const parsed=parseFeishuPost(JSON.stringify(raw));
 assert.deepEqual(parsed.images,["img_first"]);assert.equal(parsed.lines.at(-1)[0].text,"这个是啥呀");
});
test("localized rich text and plain text posts preserve readable content",async t=>{
 const f=await fixture(t);
 const body=JSON.stringify({zh_cn:{title:"标题",content:[[{tag:"text",text:"你好"},{tag:"a",text:"链接",href:"https://example.org"}]]}});
 const result=await f.manager.receiveFeishu(event(body));
 assert.equal(result.action,"dispatch_pending");assert.equal(f.downloads.length,0);
 assert.equal(f.calls.find(c=>c.name==="send_message_to_thread").args.prompt,"标题\n你好链接 (https://example.org)");
});
test("mixed post downloads every image and dispatches its caption once with stable deduplication",async t=>{
 const f=await fixture(t),e=event(content(["img_first","img_second"]));
 const first=await f.manager.receiveFeishu(e),repeat=await f.manager.receiveFeishu(e);
 assert.equal(repeat.duplicate,true);assert.deepEqual(f.downloads,[{mid:e.message_id,key:"img_first"},{mid:e.message_id,key:"img_second"}]);
 const paths=f.manager.state.feishuReceipts[first.messageId].imagePaths;
 assert.equal(Object.keys(paths).length,2);
 for(const file of Object.values(paths))assert.deepEqual(await readFile(file),png);
 const sends=f.calls.filter(c=>c.name==="send_message_to_thread");assert.equal(sends.length,1);
 assert.ok(sends[0].args.prompt.includes("这个是啥呀"));assert.ok(sends[0].args.prompt.includes("view_image"));
 assert.ok(sends[0].args.prompt.indexOf(paths.img_first)<sends[0].args.prompt.indexOf(paths.img_second));
 assert.ok(!sends[0].args.prompt.includes("img_first"));
 const records=(await f.manager.messageJournal.list()).records.filter(r=>r.direction==="input");
 assert.equal(records.length,1);assert.equal(records[0].source,"feishu");
});
test("durable rich-text inbox pins the accepted target and cannot download after a selection change",async t=>{
 const f=await fixture(t),run=f.manager.runInbox;f.manager.runInbox=()=>{};
 await f.manager.enqueueFeishu(event());
 const saved=JSON.parse(await readFile(f.manager.inboxPath,"utf8")).entries.om_post_test;
 assert.equal(saved.bridgeImageThreadId,"thread");
 const other=f.manager.makeSession({id:"other",cwd:f.root},{alias:"other",cwd:f.root});
 f.manager.state.sessions.other=other;f.manager.state.selectedThreadId="other";f.manager.state.selectionEpoch++;
 await assert.rejects(f.manager.receiveFeishu(saved),/no fallback/);
 assert.equal(f.downloads.length,0);assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,0);
 f.manager.runInbox=run;
});
test("post authorization and remote-image guards run before resource downloads",async t=>{
 const f=await fixture(t);
 assert.equal(await f.manager.receiveFeishu(event(undefined,{open_id:"stranger"})),undefined);
 f.session.hostId="remote-device";
 await assert.rejects(f.manager.receiveFeishu(event()),/暂不能/);
 assert.equal(f.downloads.length,0);assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,0);
});
test("invalid posts and failed resources never dispatch a partial or placeholder input",async t=>{
 const f=await fixture(t);
 await assert.rejects(f.manager.receiveFeishu(event("{broken")),/JSON/);
 f.notifier.downloadImage=async()=>{throw Error("resource unavailable");};
 await assert.rejects(f.manager.receiveFeishu(event(content(["img_first"]),{message_id:"om_download_failed"})),/resource unavailable/);
 assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,0);
 assert.throws(()=>parseFeishuPost(content(Array.from({length:11},(_,i)=>"img_"+i))),/最多接收/);
});
test("accepted rich text remains deduplicated after manager recreation",async t=>{
 const f=await fixture(t),e=event();
 await f.manager.receiveFeishu(e);await f.manager.close();
 const restored=await new DesktopManager(f.options).initialize();restored.handoffPaused=true;restored.notifier=f.notifier;
 try{
  const result=await restored.receiveFeishu(e);assert.equal(result.duplicate,true);
  assert.equal(f.downloads.length,1);assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,1);
 }finally{await restored.close();}
});
test("ordinary image messages keep their original cache and view_image dispatch",async t=>{
 const f=await fixture(t);
 const first=await f.manager.receiveFeishu(event(JSON.stringify({image_key:"img_single"}),{message_type:"image"}));
 const record=f.manager.state.feishuReceipts[first.messageId];assert.ok(record.imagePath);
 assert.deepEqual(await readFile(record.imagePath),png);
 assert.ok(f.calls.find(c=>c.name==="send_message_to_thread").args.prompt.includes(record.imagePath));
});
test("internal adoption stays an operation event rather than a fabricated Hermes /adopt input",async t=>{
 const f=await fixture(t),req={protocolVersion:PROTOCOL,messageId:randomUUID(),source:"hermes",threadId:"adopted"};
 await f.manager.register(req);await f.manager.register(req);
 await assert.rejects(f.manager.register({...req,messageId:randomUUID()}),e=>e.body.error==="adopt_conflict");
 const records=(await f.manager.messageJournal.list()).records;
 assert.equal(records.filter(r=>r.direction==="input").length,0);
 assert.ok(f.manager.state.events.some(e=>e.type==="operation.accepted"));
 assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,0);
});
