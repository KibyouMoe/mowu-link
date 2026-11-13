import test from "node:test";
import assert from "node:assert/strict";
import http from "node:http";
import {once} from "node:events";
import {createHash} from "node:crypto";
import {mkdtemp,chmod,rm} from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import {connectUnixWebSocket} from "../interface/desktop-connection/unix-websocket.mjs";

const unixTest = (name, run) => test(name, {skip: process.platform !== "darwin"}, run);
const GUID="258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
const packet=(opcode,body="",fin=true)=>{
 const bytes=Buffer.from(body),head=Buffer.alloc(bytes.length<126?2:4);head[0]=(fin?128:0)|opcode;
 if(bytes.length<126)head[1]=bytes.length;else{head[1]=126;head.writeUInt16BE(bytes.length,2);}
 return Buffer.concat([head,bytes]);
};
async function server(t,upgrade){
 const root=await mkdtemp(path.join(os.tmpdir(),"codex-ws-")),file=path.join(root,"rpc.sock"),sockets=new Set();
 const srv=http.createServer();let connections=0;
 srv.on("connection",s=>{connections++;sockets.add(s);s.on("close",()=>sockets.delete(s));});
 srv.on("upgrade",(req,socket)=>{
  const accept=createHash("sha1").update(req.headers["sec-websocket-key"]+GUID).digest("base64");
  const ok=()=>socket.write("HTTP/1.1 101 Switching Protocols\r\nConnection: Upgrade\r\nUpgrade: websocket\r\nSec-WebSocket-Accept: "+accept+"\r\n\r\n");
  upgrade({req,socket,ok});
 });
 srv.listen(file);await once(srv,"listening");await chmod(file,0o600);
 t.after(async()=>{for(const s of sockets)s.destroy();await new Promise(r=>srv.close(r));await rm(root,{recursive:true,force:true});});
 return {file,srv,count:()=>connections};
}
function clientFrames(socket,onFrame){
 let buffer=Buffer.alloc(0);
 socket.on("data",chunk=>{
  buffer=Buffer.concat([buffer,chunk]);
  for(;;){
   if(buffer.length<2)return;
   const opcode=buffer[0]&15;assert.ok(buffer[1]&128,"client frames must be masked");
   let n=buffer[1]&127,at=2;
   if(n===126){if(buffer.length<4)return;n=buffer.readUInt16BE(2);at=4;}
   if(n===127){if(buffer.length<10)return;n=Number(buffer.readBigUInt64BE(2));at=10;}
   if(buffer.length<at+4+n)return;
   const mask=buffer.subarray(at,at+4),body=Buffer.from(buffer.subarray(at+4,at+4+n));
   for(let i=0;i<n;i++)body[i]^=mask[i%4];
   buffer=buffer.subarray(at+4+n);onFrame(opcode,body);
  }
 });
}
unixTest("Unix WebSocket preserves RPC framing, masked requests, fragmented Unicode and ping/pong",async t=>{
 let request,pong;
 const f=await server(t,({socket,ok})=>{
  ok();clientFrames(socket,(opcode,body)=>{
   if(opcode===1){
    request=JSON.parse(body);
    const reply=JSON.stringify({id:request.id,result:{text:"桌面与飞书同一实例"}});
    const bytes=Buffer.from(reply),at=bytes.indexOf(Buffer.from("桌"))+1;
    const first=Buffer.concat([Buffer.from([1,at]),bytes.subarray(0,at)]);
    const remaining=bytes.subarray(at);
    socket.write(Buffer.concat([first,packet(9,"ping"),Buffer.concat([Buffer.from([128,remaining.length]),remaining])]));
   }else if(opcode===10)pong=body.toString();
  });
 });
 const c=await connectUnixWebSocket(f.file),got=once(c.stdout,"data");
 c.stdin.write(JSON.stringify({id:7,method:"thread/read",params:{caption:"x".repeat(200)}})+"\n");
 assert.equal(JSON.parse((await got)[0]).result.text,"桌面与飞书同一实例");
 for(let i=0;i<20&&!pong;i++)await new Promise(r=>setTimeout(r,5));
 assert.equal(pong,"ping");assert.equal(request.params.caption.length,200);
 const exited=once(c,"exit");c.kill();await exited;
 assert.equal(f.srv.listening,true);
});
unixTest("shared clients can disconnect and reconnect without ending the listener",async t=>{
 const f=await server(t,({socket,ok})=>{ok();clientFrames(socket,(op,body)=>{if(op===8)socket.end(packet(8,body));});});
 for(let i=0;i<2;i++){const c=await connectUnixWebSocket(f.file);const exit=once(c,"exit");c.kill();await exit;}
 assert.equal(f.count(),2);assert.equal(f.srv.listening,true);
});
unixTest("wrong upgrade authentication is rejected rather than falling back",async t=>{
 const f=await server(t,({socket})=>socket.end("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Accept: invalid\r\n\r\n"));
 await assert.rejects(connectUnixWebSocket(f.file),/handshake_rejected/);assert.equal(f.count(),1);
});
unixTest("insecure or absent sockets cannot be used as shared runtime endpoints",async t=>{
 const f=await server(t,({ok})=>ok());await chmod(f.file,0o666);
 await assert.rejects(connectUnixWebSocket(f.file),/socket_not_private/);assert.equal(f.count(),0);
 await assert.rejects(connectUnixWebSocket(f.file+"-missing"),e=>e.code==="ENOENT");
});
unixTest("a stalled handshake has a bounded connection timeout",async t=>{
 const f=await server(t,()=>{});
 await assert.rejects(connectUnixWebSocket(f.file,{timeoutMs:30}),/handshake_timeout/);
});
unixTest("oversized incoming messages fail before allocating their declared payload",async t=>{
 let send;
 const f=await server(t,({socket,ok})=>{ok();send=()=>{const h=Buffer.alloc(10);h[0]=129;h[1]=127;h.writeBigUInt64BE(16777217n,2);socket.write(h);};});
 const c=await connectUnixWebSocket(f.file),error=once(c,"error");send();
 assert.match((await error)[0].message,/message_too_large/);
});
