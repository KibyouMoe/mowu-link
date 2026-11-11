import test from "node:test";
import assert from "node:assert/strict";
import os from "node:os";
import path from "node:path";
import {appServerConnection} from "../interface/transport/app-server-connection.mjs";
import {AppServerClient} from "../interface/transport/app-server.mjs";

test("standalone compatibility is explicit and shared mode selects only the documented Unix WebSocket endpoint",()=>{
 assert.deepEqual(appServerConnection({}),{mode:"standalone",args:["app-server"],socket:null,ownsRuntime:true});
 const shared=appServerConnection({CODEXWORKER_APP_SERVER_MODE:"shared",CODEX_HOME:"/private/codex"});
 assert.equal(shared.args,null);assert.equal(shared.socket,"/private/codex/app-server-control/app-server-control.sock");
 assert.equal(shared.ownsRuntime,false);
 assert.equal(appServerConnection({CODEXWORKER_APP_SERVER_MODE:"shared"}).socket,path.join(os.homedir(),".codex/app-server-control/app-server-control.sock"));
 assert.equal(appServerConnection({CODEXWORKER_APP_SERVER_MODE:"shared",CODEXWORKER_APP_SERVER_SOCKET:"/tmp/custom.sock"}).socket,"/tmp/custom.sock");
});
test("invalid shared configuration cannot silently start an independent writer",()=>{
 for(const env of [
  {CODEXWORKER_APP_SERVER_MODE:"other"},
  {CODEXWORKER_APP_SERVER_SOCKET:"/tmp/shared.sock"},
  {CODEXWORKER_APP_SERVER_MODE:"shared",CODEXWORKER_APP_SERVER_SOCKET:"relative.sock"},
  {CODEXWORKER_APP_SERVER_MODE:"shared",CODEX_HOME:"relative-home"}
 ])assert.throws(()=>appServerConnection(env));
});
test("closing a shared connection owns and terminates only its proxy child",async()=>{
 const c=new AppServerClient(),calls=[];
 c.connection={mode:"shared",ownsRuntime:false};
 const child={stdin:{end:()=>calls.push("proxy stdin end")},kill:signal=>calls.push(signal),once:(_,done)=>done()};
 c.child=child;c.ready=true;
 await c.shutdown();
 assert.deepEqual(calls,["proxy stdin end","SIGTERM"]);
 assert.equal(c.child,null);assert.equal(c.ready,false);
});
test("a request failure is not a reason to create another connection or runtime",async()=>{
 const c=new AppServerClient();
 c.connection={mode:"shared",ownsRuntime:false};
 const pending=c.request("thread/read",{threadId:"original"},100);
 await assert.rejects(pending,e=>e.code==="app_server_not_running");
 assert.equal(c.child,null);assert.equal(c.connection.mode,"shared");
});

test("observing an existing native thread subscribes with metadata only and starts no model turn",async()=>{
 const c=new AppServerClient(),calls=[];
 c.request=async(method,params)=>{calls.push({method,params});return {thread:{id:"original",cwd:"/work"}};};
 await c.ensureThreadLoaded("original");await c.ensureThreadLoaded("original");
 assert.deepEqual(calls,[{method:"thread/resume",params:{threadId:"original",excludeTurns:true}}]);
 assert.equal(c.loadedThreads.has("original"),true);
});
