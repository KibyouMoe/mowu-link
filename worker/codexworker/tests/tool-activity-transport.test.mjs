import test from "node:test";
import assert from "node:assert/strict";
import {AppServerClient} from "../interface/transport/app-server.mjs";
test("real native lifecycle notifications expose only sanitized projections without blocking RPC",async()=>{
 const c=new AppServerClient(),events=[];c.setLogger(()=>{});c.setActivityHandler(e=>events.push(e));
 const p={threadId:"thread",turnId:"turn",item:{id:"call",type:"commandExecution",command:"echo safe",status:"inProgress"}};
 c.handleNotification("item/started",p);
 c.handleNotification("item/completed",{...p,item:{...p.item,status:"completed",aggregatedOutput:"not-public",exitCode:0}});
 assert.deepEqual(events.map(e=>e.status),["running","completed"]);
 assert.ok(!JSON.stringify(events).includes("not-public"));
 c.setActivityHandler(async()=>{throw Error("consumer failed");});
 assert.doesNotThrow(()=>c.handleNotification("item/started",p));
 await new Promise(resolve=>setImmediate(resolve));
 assert.equal(c.threads.get("thread").turnMap.get("turn").items[0].id,"call");
});
test("unknown and reasoning notifications remain private and turn completion is observable",()=>{
 const c=new AppServerClient(),events=[];c.setActivityHandler(e=>events.push(e));
 c.handleNotification("item/started",{threadId:"thread",turnId:"turn",item:{id:"r",type:"reasoning",text:"private"}});
 c.handleNotification("turn/completed",{threadId:"thread",turn:{id:"turn",status:"interrupted",items:[]}});
 assert.equal(events.length,1);assert.equal(events[0].kind,"turn_end");
 assert.ok(!JSON.stringify(events).includes("private"));
 c.setActivityHandler(null);
 c.handleNotification("item/started",{threadId:"thread",turnId:"turn",item:{id:"c",type:"commandExecution",command:"echo ok"}});
 assert.equal(events.length,1);
});
