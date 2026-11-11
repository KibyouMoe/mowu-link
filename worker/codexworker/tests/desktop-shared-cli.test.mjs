import test from "node:test";
import assert from "node:assert/strict";
import {mkdtemp,rm,writeFile,chmod} from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import {desktopServerOptions,desktopRequest,loadDesktopConnection} from "../interface/transport/desktop-shared-cli.mjs";
test("Desktop startup config becomes native per-thread config without losing existing overrides",()=>{
 const opts=desktopServerOptions(["app-server","--analytics-default-enabled","-c","features.code_mode_host=true","-c","plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled=true"]);
 const original={id:1,method:"thread/resume",params:{threadId:"original",config:{"features.code_mode_host":false,approval_policy:"on-request"}}};
 const out=desktopRequest(original,opts.config);
 assert.equal(out.params.threadId,"original");assert.equal(out.params.config["features.code_mode_host"],false);
 assert.equal(out.params.config.approval_policy,"on-request");
 assert.equal(out.params.config["plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled"],true);
 assert.equal(original.params.config["plugins.codex-app-tools@openai-bundled.mcp_servers.codex_app.enabled"],undefined);
});
test("observations and model input are forwarded exactly, with no synthetic conversation text",()=>{
 const config={feature:true};
 for(const method of ["initialize","thread/read","thread/loaded/list","turn/start","turn/steer","config/read"]){
  const message={id:9,method,params:{threadId:"original",input:[{type:"text",text:"desktop input"}]}};
  assert.equal(desktopRequest(message,config),message);
 }
});
test("auth, version and schema commands use the native CLI; unsupported startup options fail closed",()=>{
 for(const args of [["--version"],["login"],["app-server","daemon","version"],["app-server","--help"],["app-server","--print-startup-config"]])
  assert.equal(desktopServerOptions(args),null);
 for(const args of [["app-server","--listen","ws://127.0.0.1:9000"],["app-server","-c","bad"],["app-server","-c","permissions=invalid-toml"],["app-server","--unknown"]])
  assert.throws(()=>desktopServerOptions(args));
 assert.deepEqual(desktopServerOptions(["-c","approval_policy=\"on-request\"","app-server","--stdio"]).config,{approval_policy:"on-request"});
});
test("connection settings are machine-local and cannot be group-writable",async t=>{
 const root=await mkdtemp(path.join(os.tmpdir(),"shared-desktop-config-")),file=path.join(root,"settings.json");
 t.after(()=>rm(root,{recursive:true,force:true}));
 await writeFile(file,JSON.stringify({version:1,realCli:"/Applications/native/codex",socket:"/tmp/native.sock"}),{mode:0o600});
 assert.equal((await loadDesktopConnection({CODEXWORKER_SHARED_DESKTOP_CONFIG:file})).socket,"/tmp/native.sock");
 await chmod(file,0o666);await assert.rejects(loadDesktopConnection({CODEXWORKER_SHARED_DESKTOP_CONFIG:file}),/not_private/);
});
