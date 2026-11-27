// Run the old Worker manager against an in-memory native session.
import assert from "node:assert/strict";
import {readFile} from "node:fs/promises";
import {randomUUID} from "node:crypto";
const [legacy,rule,root]=process.argv.slice(2);
const {DesktopManager,PROTOCOL}=await import(legacy);
const {subscriptionMessageId,subscriptionMarker}=await import(rule);
const event={source:"mc_chat",kind:"chat",id:3,text:"observed 订阅 🍃 \nline two"};
const mid=subscriptionMessageId(event);
const target="fixture-host",sender="fixture-sender";
const task={thread:{id:target,title:target,cwd:root,hostId:"local",status:{type:"idle"}},turns:[]};
let injected=0;
const native=async(method,args)=>{
  if(method==="list_threads") return {pinnedThreads:[],threads:[{...task.thread,kind:"codex"}]};
  if(method==="list_projects") return {projects:[{projectId:"fixture-project",path:root,hostId:"local"}]};
  if(method==="read_thread") return structuredClone(task);
  if(method==="send_message_to_thread"){
    injected++;
    const item={id:"fixture-input",type:"functionCallOutput",name:"send_message_to_thread",
      output:{text:"<codex_delegation><source_thread_id>"+sender+"</source_thread_id><input>"+args.prompt+"</input></codex_delegation>"}};
    task.turns.push({id:"fixture-turn",status:"inProgress",items:[item]});
    return {threadId:target};
  }
  if(method==="set_thread_title") return {updated:true};
  throw new Error(method);
};
const listNativeTools=async()=>["read_thread","list_threads","list_projects","send_message_to_thread",
    "create_thread","set_thread_title","fork_thread"].map(name=>({name}));
const manager=await new DesktopManager({native,listNativeTools,stateDir:root,
    config:{token:"test-only-token-for-readback-fixture-123",port:0,pollMs:1000000,
    projectId:"fixture-project",workspaceRoots:[root],nativeSenderThreadId:sender,feishu:{}},
    fetchImpl:async()=>{throw Error("external fetch denied");}}).initialize();
try {
  await manager.register({protocolVersion:PROTOCOL,messageId:randomUUID(),source:"trainer",threadId:target});
  const request={protocolVersion:PROTOCOL,messageId:mid,source:"hermes",origin:"subscription",
    delivery:"steer",targetThreadId:target,text:subscriptionMarker(event)+"\n"+event.text};
  await manager.message(request);
  const before=JSON.parse(await readFile(manager.statePath,"utf8"));
  await manager.scan();
  const after=JSON.parse(await readFile(manager.statePath,"utf8"));
  await manager.message(request);
  assert.equal(injected,1);
  for(const state of [before,after]) console.log(JSON.stringify({state,event,mid}));
} finally { await manager.close(); }
