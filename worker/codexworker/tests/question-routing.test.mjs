import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createHash, randomUUID } from "node:crypto";
import { DesktopManager, PROTOCOL, publicItems } from "../desktop-manager.mjs";
import { publicJournalItem } from "../local-public-journal.mjs";
import { projectPublicQuestion } from "../interface/public-questions.mjs";
import { requestInitiator, rememberTurnInitiator, questionInitiator } from "../question-origin.mjs";
import { waitForTaskClarification } from "../router-question-wait.mjs";
import { SKIPPED_QUESTION_ANSWER } from "../interface/public-questions.mjs";

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


const question = () => projectPublicQuestion({type:"AgentMessage",id:"call_router_question",phase:"final_answer",delivery:"async",
 questions:[{title:"使用哪个配置？",options:["配置甲","配置乙"]}]},{threadId:"thread",turnId:"task-turn"});
async function routing(t,{hermes=true,notice,taskState="running"}={}) {
 const f=await fixture(t),cards=[],patches=[],notifications=[];
 f.manager.config.port=0;f.manager.config.host="127.0.0.1";f.manager.config.nativeBusyDeliveryVerified=true;
 const turn={id:"task-turn",status:"inProgress",items:[]};
 f.manager.deliveryState=async()=>({turns:[structuredClone(turn)]});
 f.manager.read=async()=>({thread:{id:"thread",cwd:f.root},turns:[structuredClone(turn)]});
 f.session.turnInitiators={"task-turn":hermes?{kind:"hermes",taskId:"t-routing"}:{kind:"trainer"}};
 f.manager.routerClient={notice:async body=>{notifications.push(body);return notice?notice(body):{ok:true,event_id:"notice-confirmed"};},getTask:async()=>({task_id:"t-routing",state:taskState})};
 f.notifier.sendQuestionCard=async(card,opts)=>{cards.push({card,opts});return {code:0,data:{message_id:"om_origin_question"}};};
 f.notifier.updateQuestionCard=async(id,card)=>{patches.push({id,card});return {code:0};};
 return {...f,cards,patches,notifications,turn,q:question()};
}
const buttonCount=card=>JSON.stringify(card).split('"tag":"button"').length-1;
const answer=(f,extra={})=>({actor:"hermes",threadId:"thread",taskId:"t-routing",nonce:f.manager.questions.state.entries[f.q.questionId].nonce,answers:["配置乙"],...extra});

test("origin comes from the initiating dispatch, not a text prefix or a later trainer interjection",async()=>{
 const session={turnMessages:{"task-turn":["start","later"]}};
 const router={messageId:"start",source:"hermes",origin:"router",text:"[router t-routing] task"};
 rememberTurnInitiator(session,"task-turn",router);
 rememberTurnInitiator(session,"task-turn",{messageId:"later",source:"trainer",origin:"feishu"});
 assert.deepEqual(session.turnInitiators["task-turn"],{kind:"hermes",taskId:"t-routing",messageId:"start"});
 assert.equal(requestInitiator({source:"trainer",origin:"api",text:"[router t-forged]"}).kind,"trainer");
 const m={state:{receipts:{}},session:()=>({turnMessages:{"old":["start"]}}),receiptArchive:{get:async()=>({request:router})}};
 assert.equal((await questionInitiator(m,"thread","old")).taskId,"t-routing");
});

test("a router task asks Hermes by event and mirrors a read-only question card to the trainer",async t=>{
 const f=await routing(t);await f.manager.questions.publish(f.q);
 assert.equal(f.notifications.length,1);
 assert.equal(f.notifications[0].kind,"worker.question");assert.equal(f.notifications[0].urgency,"urgent");
 assert.ok(f.notifications[0].text.includes(f.q.questionId));assert.ok(f.notifications[0].text.length<=100);
 const template=f.manager.questions.get(f.q.questionId);
 assert.deepEqual(template.questions[0].options,["配置甲","配置乙"]);assert.equal(template.answerRequest.actor,"hermes");
 assert.equal(template.answerRequest.nonce,f.manager.questions.state.entries[f.q.questionId].nonce);
 assert.equal(buttonCount(f.cards[0].card),0);assert.ok(JSON.stringify(f.cards[0].card).includes("等待溪流回复"));
 const state=f.manager.questions.get(f.q.questionId);assert.equal(state.recipient.kind,"hermes");assert.equal(state.routerDelivery,"sent");
 await f.manager.questions.publish(f.q);assert.equal(f.notifications.length,1);assert.equal(f.cards.length,1);
});

test("one-option router questions reach Hermes once and accept the original native reply", async t => {
 const f = await routing(t);
 f.q.questions[0].options = ["已解锁，继续验收"];
 await f.manager.questions.publish(f.q);
 await f.manager.questions.publish(f.q);
 assert.equal(f.notifications.length, 1);
 assert.equal(f.cards.length, 1);
 assert.ok(f.cards[0].card.body.elements.some(e=>e.content?.includes("已解锁，继续验收")));
 await f.manager.questions.acceptAgentAnswer(f.q.questionId, answer(f, { answers: ["已解锁，继续验收"] }));
 f.manager.handoffPaused = false;
 await f.manager.questions.process();
 await f.manager.questions.process();
 const writes = f.calls.filter(call => call.name === "send_message_to_thread");
 assert.equal(writes.length, 1);
 assert.ok(writes[0].args.prompt.includes(f.q.sourceQuestionItemIds[0].replaceAll('"', '\\"')));
 assert.equal(f.manager.questions.get(f.q.questionId).status, "answered");
});

test("trainer-initiated questions keep their buttons and never notify Hermes",async t=>{
 const f=await routing(t,{hermes:false});await f.manager.questions.publish(f.q);
 assert.equal(f.notifications.length,0);assert.equal(buttonCount(f.cards[0].card),4);
});
test("the trainer cannot accidentally answer a Hermes-owned question through card callbacks",async t=>{
 const f=await routing(t);await f.manager.questions.publish(f.q);
 const a=f.manager.questions.state.entries[f.q.questionId];
 const result=await f.manager.questions.acceptCard({header:{event_type:"card.action.trigger",event_id:"wrong-recipient"},event:{
  operator:{open_id:"trainer"},context:{open_message_id:"om_origin_question",open_chat_id:"chat"},
  action:{value:{bridgeQuestion:f.q.questionId,nonce:a.nonce,action:"select",questionIndex:0,optionIndex:0}}}});
 assert.ok(result.toast.content.includes("溪流"));assert.equal(a.status,"open");
 assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,0);
});
test("Hermes answer admission rejects another actor, thread, nonce or placeholder",async t=>{
 const f=await routing(t);await f.manager.questions.publish(f.q);
 for(const extra of [{actor:"trainer"},{threadId:"other"},{nonce:"wrong"}])
  await assert.rejects(f.manager.questions.acceptAgentAnswer(f.q.questionId,answer(f,extra)),e=>e.status===403);
 await assert.rejects(f.manager.questions.acceptAgentAnswer(f.q.questionId,answer(f,{answers:["<填写回答>"]})),e=>e.status===400);
 assert.equal(f.manager.questions.get(f.q.questionId).status,"open");
});
test("the authenticated agent answer endpoint returns to the same native chat once, updating only the original question card",async t=>{
 const f=await routing(t);const base=await f.manager.listen();f.manager.handoffPaused=false;
 f.manager.config.messageSourceLabels=true;
 await f.manager.questions.publish(f.q);const body=answer(f);
 const send=async(token="isolated-post-test-token",value=body)=>{
  const r=await fetch(base+"/bridge/questions/"+f.q.questionId+"/answer",{method:"POST",headers:{authorization:"Bearer "+token,"content-type":"application/json"},body:JSON.stringify(value)});
  return {status:r.status,body:await r.json()};
 };
 assert.equal((await send("wrong")).status,401);
 const first=await send();assert.equal(first.status,200);assert.equal(first.body.status,"answer_pending");
 assert.equal((await send()).body.duplicate,true);
 assert.equal((await send("isolated-post-test-token",{...body,answers:["冲突答案"]})).status,409);
 await f.manager.questions.running;await f.manager.questions.process();
 const writes=f.calls.filter(c=>c.name==="send_message_to_thread");assert.equal(writes.length,1);
 assert.ok(writes[0].args.prompt.includes("配置乙"));
 assert.match(writes[0].args.prompt,/^<send_user_message_question_reply>\n/);
 const nativeReplies=JSON.parse(writes[0].args.prompt.slice("<send_user_message_question_reply>".length,-"</send_user_message_question_reply>".length));
 assert.deepEqual(nativeReplies,[{questionItemId:JSON.stringify(["request_user_input_async","call_router_question",0]),question:"使用哪个配置？",answer:"配置乙"}]);

 const a=f.manager.questions.state.entries[f.q.questionId];assert.equal(a.status,"answered");
 assert.equal(f.manager.state.receipts[a.nativeMessageId].request.source,"hermes");
 assert.equal(f.manager.state.receipts[a.nativeMessageId].request.taskId,"t-routing");
 const record=(await f.manager.messageJournal.list()).records.find(r=>r.messageId===a.nativeMessageId&&r.direction==="input");
 assert.equal(record.mirror,false);
 assert.ok(!Object.values(f.manager.state.publicTextOutbox??{}).some(e=>e.key==="input:"+a.nativeMessageId));
 assert.ok(f.patches.some(p=>p.id==="om_origin_question"&&JSON.stringify(p.card).includes("溪流的回答")));
 assert.equal(f.notifications.length,1);
 f.manager.routerClient.getTask=async()=>({state:"done"});
 assert.equal((await f.manager.questions.acceptAgentAnswer(f.q.questionId,body)).duplicate,true);
});
test("Hermes answers an exact question after the originating task ends or is cancelled",async t=>{
 const f=await routing(t);await f.manager.questions.publish(f.q);
 f.manager.routerClient.getTask=async()=>({state:"cancelled"});
 const result=await f.manager.questions.acceptAgentAnswer(f.q.questionId,answer(f,{actor:"Hermes",taskId:"t-new-sender"}));
 assert.equal(result.status,"answer_pending");
 f.manager.handoffPaused=false;await f.manager.questions.process();
 assert.equal(f.manager.questions.get(f.q.questionId).status,"answered");
 assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,1);
});
for(const taskState of ["done","failed","cancelled"]) test(`an async question survives a ${taskState} source task and a later native turn`,async t=>{
 const f=await routing(t,{taskState});f.turn.status="completed";
 await f.manager.questions.publish(f.q);
 f.manager.deliveryState=async()=>({turns:[{id:"later-native-turn",status:"inProgress"}]});
 f.manager.config.nativeBusyDeliveryVerified=true;
 f.manager.handoffPaused=false;await f.manager.questions.process();
 assert.equal(f.manager.questions.get(f.q.questionId).status,"open");
 const request=f.manager.questions.get(f.q.questionId).answerRequest;
 assert.equal(request.taskId,undefined);
 await f.manager.questions.acceptAgentAnswer(f.q.questionId,{...request,actor:"Hermes",answers:["配置甲"]});
 await f.manager.questions.running;
 assert.equal(f.manager.questions.get(f.q.questionId).status,"answered");
 assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,1);
});
for(const mode of ["option","skip","free-text"]) test(`Hermes ${mode} answer updates the original card and preserves its author on native echo`,async t=>{
 const f=await routing(t);await f.manager.questions.publish(f.q);
 const request=f.manager.questions.get(f.q.questionId).answerRequest;
 const expected=mode==="skip"?SKIPPED_QUESTION_ANSWER:mode==="option"?"配置甲":"由溪流给出的自由文本";
 const body=mode==="skip"?{...request,action:"skip"}:{...request,answers:[expected]};
 await f.manager.questions.acceptAgentAnswer(f.q.questionId,body);
 f.manager.handoffPaused=false;await f.manager.questions.process();
 const a=f.manager.questions.state.entries[f.q.questionId];
 assert.equal(a.status,mode==="skip"?"skipped":"answered");assert.equal(a.answerActor,"hermes");
 assert.equal(f.manager.questions.get(f.q.questionId).answeredBy,"hermes");
 assert.equal((await f.manager.questions.acceptAgentAnswer(f.q.questionId,body)).duplicate,true);
 const wire=f.calls.find(c=>c.name==="send_message_to_thread").args.prompt;
 await f.manager.questions.observeNativeReply({threadId:"thread",text:wire,messageId:randomUUID(),turnId:"native-echo"});
 await f.manager.questions.process();
 assert.equal(a.answerActor,"hermes");assert.equal(a.nativePanelSync.status,"accepted");
 assert.ok(f.patches.some(p=>JSON.stringify(p.card).includes(mode==="skip"?"溪流已跳过":"溪流的回答")));
 assert.equal(f.cards.length,1);assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,1);
});
test("two workers cannot consume each other's answer nonce even with identical question IDs",async t=>{
 const first=await routing(t),second=await routing(t);
 await Promise.all([first.manager.questions.publish(first.q),second.manager.questions.publish(second.q)]);
 const body=first.manager.questions.get(first.q.questionId).answerRequest;
 await assert.rejects(second.manager.questions.acceptAgentAnswer(second.q.questionId,{...body,answers:["配置甲"]}),e=>e.status===403);
 assert.equal(second.manager.questions.get(second.q.questionId).status,"open");
 assert.equal(second.calls.filter(c=>c.name==="send_message_to_thread").length,0);
});
test("router notifications retain the full central question URL inside a 100-character preview",async t=>{
 const f=await routing(t);f.manager.routerClient.url="http://192.168.3.102:8790";f.manager.routerClient.name="Rudolf";
 await f.manager.questions.publish(f.q);
 const text=f.notifications[0].text;
 assert.ok(text.length<=100);assert.ok(text.endsWith(`/questions/Rudolf/${f.q.questionId}`));
 assert.deepEqual(f.manager.questions.get(f.q.questionId).questions,f.q.questions);
});
test("a correlated native answer echo confirms panel synchronization while retaining Hermes as its author",async t=>{
 const f=await routing(t);await f.manager.questions.publish(f.q);
 await f.manager.questions.acceptAgentAnswer(f.q.questionId,answer(f));
 f.manager.handoffPaused=false;await f.manager.questions.process();
 const action=f.manager.questions.state.entries[f.q.questionId];
 assert.equal(action.nativePanelSync,undefined);
 const receipt=f.manager.state.receipts[action.nativeMessageId];
 const messageId=await f.manager.observeNativeInput(f.session,{key:"real-turn/answer-echo",turnId:"real-turn",text:receipt.request.text});
 assert.equal(messageId,action.nativeMessageId);
 assert.equal(action.answerActor,"hermes");assert.equal(action.nativePanelSync.status,"accepted");
 assert.equal(action.nativePanelSync.via,"native-observation");
 assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,1);
});
test("an unknown router notification outcome is not resent and does not turn into a trainer question",async t=>{
 const f=await routing(t,{notice:async()=>{throw Error("lost response");}});
 await f.manager.questions.publish(f.q);await f.manager.questions.process();await f.manager.questions.publish(f.q);
 assert.equal(f.notifications.length,1);assert.equal(f.manager.questions.get(f.q.questionId).routerDelivery,"unknown");
 assert.equal(buttonCount(f.cards[0].card),0);
});
test("long questions retain their complete worker record while router notices remain below the router limit",async t=>{
 const f=await routing(t);f.q.questions=Array.from({length:3},(_,i)=>({title:String(i)+"题".repeat(7998),options:["甲".repeat(1000),"乙".repeat(1000)]}));
 f.q.sourceQuestionItemIds=f.q.questions.map((_,i)=>JSON.stringify(["request_user_input_async","call_router_question",i]));
 await f.manager.questions.publish(f.q);assert.ok(f.notifications[0].text.length<=8000);
 assert.ok(f.notifications[0].text.includes("GET http://"));
 assert.equal(f.manager.questions.get(f.q.questionId).questions[0].title.length,7999);
});
test("task-terminal capture routes a clarification even without a Feishu notifier",async t=>{
 const f=await routing(t);f.manager.notifier=null;f.turn.status="completed";
 await f.manager.captureTaskQuestions("thread",{...f.turn,items:[{type:"agentMessage",id:"call_router_question",phase:"final_answer",delivery:"async",text:"需要确认配置",questions:f.q.questions}]});
 assert.equal(f.notifications.length,1);assert.equal(f.manager.questions.get(f.q.questionId).recipient.kind,"hermes");
});
function waiting(status="open") {
 const question={questionId:"q",threadId:"thread",recipient:{kind:"hermes",taskId:"t-routing"},createdAt:"1"};
 const action={status,routerDelivery:{status:"sent"},nativeMessageId:"answer"};
 const manager={state:{publicQuestions:{q:question},receipts:{answer:{nativeTurnId:"continued-turn",nativeDispatch:{status:"accepted"}}}},questions:{state:{entries:{q:action}}}};
 return {manager,action,taskId:"t-routing",threadId:"thread"};
}
test("the task wait follows the answer turn and has no implicit elapsed-time deadline",async()=>{
 const f=waiting();let ticks=0,time=0;
 const result=await waitForTaskClarification({...f,now:()=>time,sleep:async()=>{time+=3600000;if(++ticks===2)f.action.status="answered";},router:{getTask:async()=>({state:"running"})}});
 assert.equal(result.messageId,"answer");assert.equal(result.turnId,"continued-turn");assert.equal(time,7200000);
 assert.equal(await waitForTaskClarification({...f,consumed:new Set(["q"])}),null);
});
test("clarification waits respect cancellation and uncertain answer states while estimates remain advisory",async()=>{
 const f=waiting();
 await assert.rejects(waitForTaskClarification({...f,router:{getTask:async()=>({state:"cancelled"})}}),/cancelled/);
 const afterEstimate=waiting();
 const continued=await waitForTaskClarification({...afterEstimate,deadline:1,now:()=>2,
   sleep:async()=>{afterEstimate.action.status="answered";}});
 assert.equal(continued.turnId,"continued-turn");
 f.action.routerDelivery.status="unavailable";await assert.rejects(waitForTaskClarification(f),/通道不可用/);
 f.action.routerDelivery.status="sent";f.action.status="answer_unknown";await assert.rejects(waitForTaskClarification(f),/澄清未完成/);
});

test("a closed question without a native acceptance receipt cannot finish the router task",async()=>{
 const f=waiting("answered");delete f.manager.state.receipts.answer.nativeDispatch;
 await assert.rejects(waitForTaskClarification(f),/原生接收回执/);
});
test("an answer racing with compaction waits for its original queued input rather than reporting native acceptance early",async t=>{
 const f=await routing(t);await f.manager.questions.publish(f.q);
 await f.manager.questions.acceptAgentAnswer(f.q.questionId,answer(f));
 f.manager.handoffPaused=false;const original=f.manager.message.bind(f.manager);let attempts=0;
 f.manager.message=async req=>{attempts++;f.manager.state.receipts[req.messageId]={response:{action:"message_queued"}};return {action:"message_queued"};};
 await f.manager.questions.process();await f.manager.questions.process();
 const a=f.manager.questions.state.entries[f.q.questionId];assert.equal(a.status,"answer_pending");assert.equal(attempts,1);
 f.manager.state.receipts[a.nativeMessageId].nativeDispatch={status:"accepted"};
 await f.manager.questions.process();assert.equal(a.status,"answered");assert.equal(attempts,1);
 f.manager.message=original;
});

test("legacy transcript-disabled mode also updates only the original Hermes question card",async t=>{
 const f=await routing(t);await f.manager.questions.publish(f.q);
 f.manager.config.feishu.transcriptEnabled=false;f.manager.handoffPaused=false;
 await f.manager.questions.acceptAgentAnswer(f.q.questionId,answer(f));await f.manager.questions.process();
 assert.equal(f.manager.questions.get(f.q.questionId).status,"answered");
 assert.equal(f.notices.length,0);assert.ok(f.patches.some(p=>p.id==="om_origin_question"));
});

test("a canonical client answer observation can continue a router clarification without inventing a dispatch receipt",async()=>{
 const f=waiting("answered");delete f.manager.state.receipts.answer;
 f.action.nativeAnswerObservation={messageId:"answer",threadId:"thread",turnId:"native-client-answer-turn"};
 const reply=await waitForTaskClarification(f);
 assert.equal(reply.turnId,"native-client-answer-turn");assert.equal(reply.messageId,"answer");
 f.action.nativeAnswerObservation.threadId="different-thread";
 await assert.rejects(waitForTaskClarification(f),/原生接收回执/);
});

test("a skipped clarification still waits for and follows its accepted native continuation",async()=>{
 const f=waiting("skipped");
 assert.equal((await waitForTaskClarification(f)).turnId,"continued-turn");
 delete f.manager.state.receipts.answer.nativeDispatch;
 await assert.rejects(waitForTaskClarification(f),/原生接收回执/);
});
test("a trainer cannot skip a Hermes-owned clarification",async t=>{
 const f=await routing(t);await f.manager.questions.publish(f.q);
 const s=f.manager.questions.state.entries[f.q.questionId];
 const result=await f.manager.questions.acceptCard({header:{event_type:"card.action.trigger",event_id:"skip-other-actor"},event:{
  operator:{open_id:"trainer"},context:{open_message_id:"om_origin_question",open_chat_id:"chat"},
  action:{value:{bridgeQuestion:f.q.questionId,nonce:s.nonce,action:"skip"}}}});
 assert.ok(result.toast.content.includes("溪流"));assert.equal(s.status,"open");
 assert.equal(f.calls.filter(c=>c.name==="send_message_to_thread").length,0);
});
