import test from "node:test";
import assert from "node:assert/strict";
import {AppServerClient,listTools} from "../interface/transport/app-server.mjs";
test("native compact calls the installed RPC on the original loaded thread and reports acceptance only",async()=>{
 const c=new AppServerClient(),calls=[];
 c.ensureStarted=async()=>{};c.ensureThreadLoaded=async id=>calls.push({loaded:id});
 c.request=async(method,params)=>{calls.push({method,params});return {};};
 const value=await c.execute("compact_thread",{threadId:"original"},5000);
 assert.deepEqual(calls,[{loaded:"original"},{method:"thread/compact/start",params:{threadId:"original"}}]);
 assert.equal(value.status,"accepted");assert.equal(value.threadId,"original");
 assert.ok((await listTools()).some(t=>t.name==="compact_thread"));
});
test("usage and compaction notifications have an independent nonblocking observer",async()=>{
 const c=new AppServerClient(),events=[];c.setLogger(()=>{});
 c.setContextHandler(e=>events.push(e));
 c.handleNotification("thread/tokenUsage/updated",{threadId:"t",turnId:"turn",tokenUsage:{last:{totalTokens:5},total:{totalTokens:500},modelContextWindow:1000}});
 c.handleNotification("item/completed",{threadId:"t",turnId:"turn",item:{type:"contextCompaction",id:"c",summary:"secret"}});
 assert.equal(events[0].contextTokens,5);assert.equal(events[1].status,"completed");assert.ok(!JSON.stringify(events).includes("secret"));
 c.setContextHandler(async()=>{throw Error("unavailable viewer");});
 assert.doesNotThrow(()=>c.handleNotification("item/started",{threadId:"t",turnId:"turn",item:{type:"contextCompaction",id:"c"}}));
 await new Promise(r=>setImmediate(r));
});
