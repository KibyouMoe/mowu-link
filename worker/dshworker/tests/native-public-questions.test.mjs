import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { DesktopManager, PROTOCOL } from "../desktop-manager.mjs";
import { publicJournalItem } from "../local-public-journal.mjs";
import { projectPublicQuestion } from "../interface/public-questions.mjs";

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

const rawQuestion = () => ({type:"AgentMessage",id:"call_native_question",phase:"final_answer",delivery:"async",
 content:[{type:"Text",text:"选择同步目标？\\n- 当前仓库\\n- 另一个仓库"}],
 questions:[{title:"选择同步目标？",options:["当前仓库","另一个仓库"]}]});
const project = x => publicJournalItem({type:"event_msg",timestamp:"2026-11-12T02:16:49Z",
 payload:{type:"item_completed",thread_id:"thread",turn_id:"native-turn",item:x}},"thread");
function ui(f) {
 const cards=[];
 f.manager.deliveryState=async()=>({turns:[{id:"native-turn",status:"inProgress",items:[]}]});
 f.manager.read=async()=>({turns:[{id:"native-turn",status:"inProgress",items:[]}]});
 f.notifier.sendQuestionCard=async(card,options)=>{cards.push({card,options});return {code:0,data:{message_id:"om_real_question_card"}};};
 f.notifier.updateQuestionCard=async()=>({code:0});
 return cards;
}
const buttons = card => card.body.elements.flatMap(e=>e.columns??[]).flatMap(e=>e.elements??[]).filter(e=>e.tag==="button");
async function observe(f) {
 const item=project(rawQuestion()).item;
 await f.manager.serial(()=>f.manager.emit("item.completed",{targetThreadId:"thread",source:"codex"},
  {text:item.text,snapshot:item.text,itemKey:"native-turn/"+item.id,phase:item.phase,complete:true,publicQuestion:item.publicQuestion},{turnId:"native-turn"}));
 return item.publicQuestion;
}

test("the real native async AgentMessage projects explicit options and stable native question identities",()=>{
 const a=project(rawQuestion()),b=project(rawQuestion());
 assert.deepEqual(a,b);assert.deepEqual(a.item.publicQuestion.questions,rawQuestion().questions);
 assert.equal(a.item.publicQuestion.sourceTurnId,"native-turn");
 assert.deepEqual(a.item.publicQuestion.sourceQuestionItemIds,[JSON.stringify(["request_user_input_async","call_native_question",0])]);
 const x=rawQuestion();x.questions[0].secret="not public";x.reasoning="private";
 assert.ok(!JSON.stringify(project(x)).includes("private"));assert.ok(!JSON.stringify(project(x)).includes("not public"));
});

test("prose questions, private phases, approvals and malformed options cannot manufacture interactive public questions",()=>{
 const x=rawQuestion();
 for(const item of [{...x,delivery:undefined},{...x,type:"CommandExecution"},{...x,phase:"analysis"},{...x,questions:[{title:"?",options:["only one"]}]},{...x,questions:[{title:"?",options:[{},{}]}]}])
  assert.equal(projectPublicQuestion(item,{threadId:"thread",turnId:"native-turn"}),null);
 const prose=project({...x,delivery:undefined});assert.equal(prose.item.publicQuestion,undefined);
 assert.equal(project({...x,phase:"analysis"}),null);
});

test("native question output creates a question-titled button card instead of an ordinary reply, once",async t=>{
 const f=await fixture(t),cards=ui(f),q=await observe(f);
 const record=(await f.manager.messageJournal.list()).records.find(r=>r.kind==="question");
 assert.deepEqual(record.publicQuestion,q);
 const entry=Object.values(f.manager.state.publicTextOutbox).find(e=>e.publicQuestion);
 assert.ok(entry.title.endsWith("-提问"));assert.deepEqual(entry.publicQuestion.questions,q.questions);
 f.manager.handoffPaused=false;await f.manager.drainPublicTexts();
 assert.equal(cards.length,1);assert.ok(cards[0].card.header.title.content.includes("-提问"));
 assert.ok(!cards[0].card.header.title.content.includes("-回复"));
 assert.deepEqual(buttons(cards[0].card).map(b=>b.text.content),["1）当前仓库","2）另一个仓库"]);
 assert.equal(f.notices.length,0);
 await observe(f);await f.manager.drainPublicTexts();assert.equal(cards.length,1);
 const delivery=await f.manager.messageJournal.receipt(record.id);
 assert.equal(delivery.state,"sent");
 assert.ok(Object.values(delivery.pages).some(p=>p.messageId==="om_real_question_card"));
});

test("pending structured question survives archive-only recovery and still renders buttons",async t=>{
 const f=await fixture(t);ui(f);await observe(f);
 f.manager.state.publicTextOutbox={};await f.manager.save();await f.manager.close();
 const restored=await new DesktopManager(f.options).initialize();restored.handoffPaused=true;
 restored.notifier=f.notifier;const next={...f,manager:restored},cards=ui(next);
 try{
  assert.ok(Object.values(restored.state.publicTextOutbox).some(e=>e.publicQuestion));
  restored.handoffPaused=false;await restored.drainPublicTexts();
  assert.equal(cards.length,1);assert.equal(buttons(cards[0].card).length,2);
  assert.ok(cards[0].card.header.title.content.includes("-提问"));
 }finally{await restored.close();}
});

test("a native question card uses the existing authorized callback and persists the selected answer before native dispatch",async t=>{
 const f=await fixture(t),cards=ui(f),q=await observe(f);
 f.manager.handoffPaused=false;await f.manager.drainPublicTexts();f.manager.handoffPaused=true;
 const card=cards[0].card,button=buttons(card)[1];
 const click={header:{event_type:"card.action.trigger",event_id:"test-native-question-click"},event:{
  operator:{open_id:"trainer"},context:{open_message_id:"om_real_question_card",open_chat_id:"chat"},
  action:{value:button.behaviors[0].value}}};
 const reply=await f.manager.questions.acceptCard(click);
 assert.equal(reply.toast.type,"success");
 const saved=JSON.parse(await readFile(f.manager.questions.path,"utf8")).entries[q.questionId];
 assert.equal(saved.status,"answer_pending");assert.deepEqual(saved.answers,["另一个仓库"]);
 assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,0);
 f.manager.config.nativeBusyDeliveryVerified=true;f.manager.handoffPaused=false;
 await f.manager.questions.process();await f.manager.questions.process();
 const writes=f.calls.filter(c=>c.name==="send_message_to_thread");
 assert.equal(writes.length,1);
 assert.ok(writes[0].args.prompt.includes("另一个仓库"));
 assert.equal(f.manager.questions.get(q.questionId).status,"answered");
});

test("native async choices remain actionable after their own turn completes and deliver to the same idle chat",async t=>{
 const f=await fixture(t),cards=ui(f);
 f.manager.deliveryState=async()=>({turns:[{id:"native-turn",status:"completed",items:[]}]});
 f.manager.read=async()=>({turns:[{id:"native-turn",status:"completed",items:[]}]});
 const q=await observe(f);f.manager.handoffPaused=false;await f.manager.drainPublicTexts();
 assert.equal(buttons(cards[0].card).length,2);
 const button=buttons(cards[0].card)[0];
 await f.manager.questions.acceptCard({header:{event_type:"card.action.trigger",event_id:"completed-source-click"},event:{
  operator:{open_id:"trainer"},context:{open_message_id:"om_real_question_card",open_chat_id:"chat"},
  action:{value:button.behaviors[0].value}}});
 await f.manager.questions.process();
 assert.equal(f.manager.questions.get(q.questionId).status,"answered");
 assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,1);
});
test("native async choices cannot deliver into a newer task after their own turn completes",async t=>{
 const f=await fixture(t),cards=ui(f),q=await observe(f);
 f.manager.handoffPaused=false;await f.manager.drainPublicTexts();
 const button=buttons(cards[0].card)[0];
 await f.manager.questions.acceptCard({header:{event_type:"card.action.trigger",event_id:"new-task-click"},event:{
  operator:{open_id:"trainer"},context:{open_message_id:"om_real_question_card",open_chat_id:"chat"},
  action:{value:button.behaviors[0].value}}});
 f.manager.deliveryState=async()=>({turns:[{id:"native-turn",status:"completed"},{id:"new-task",status:"inProgress"}]});
 await f.manager.questions.process();
 assert.equal(f.manager.questions.get(q.questionId).status,"expired");
 assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,0);
});
