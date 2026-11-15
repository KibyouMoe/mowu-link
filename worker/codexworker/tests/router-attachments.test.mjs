import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createHash } from "node:crypto";
import { sendRouterImage, uploadRouterImage, imageType } from "../router-attachments.mjs";

const messageId="d4ce4b62-576b-4e58-a0ea-168b72928d58";
const png=Buffer.from("89504e470d0a1a0a00000000","hex");
const sha=createHash("sha256").update(png).digest("hex");
async function fixture(t, fetchImpl, bytes=png) {
  const dir=await mkdtemp(path.join(os.tmpdir(),"router-image-"));
  t.after(()=>rm(dir,{recursive:true,force:true}));
  const imagePath=path.join(dir,"attachment.png");
  await writeFile(imagePath,bytes);
  return {imagePath,messageId,text:"look at image",worker:"OguriCap",
          url:"https://router.example.test",token:"test-token",fetchImpl};
}
function uploaded(overrides={}) {
  return {ok:true,status:201,json:async()=>({ok:true,attachment:{
    id:sha,sha256:sha,size:png.length,media_type:"image/png",...overrides}})};
}

test("upload-only returns a stable image ID without a notice, local path or binary receipt", async t => {
  const calls=[];
  const options=await fixture(t,async(url,request)=>{
    calls.push({url,request});
    return uploaded({untrustedBody:"data:image/png;base64,private"});
  });
  const first=await uploadRouterImage({...options,messageId:undefined,worker:undefined});
  const retry=await uploadRouterImage(options);
  assert.deepEqual(first,retry); assert.equal(first.id,sha);
  assert.ok(calls.every(c=>c.url.endsWith("/attachments")));
  assert.equal(calls.length,2);
  assert.deepEqual(calls[0].request.body,png);
  assert.ok(!JSON.stringify(first).includes(options.imagePath));
  assert.ok(!JSON.stringify(first).includes("base64"));
});

test("actual bytes upload then descriptor-only notify; retry keeps message identity",async t=>{
  const calls=[];
  const options=await fixture(t,async(url,request)=>{
    calls.push({url,request});
    if(url.endsWith("/attachments"))return uploaded();
    return {ok:true,status:200,json:async()=>({ok:true,event_id:"test-event",level:"notify"})};
  });
  const result=await sendRouterImage(options);
  assert.equal(result.messageId,messageId);
  assert.deepEqual(calls[0].request.body,png);
  assert.equal(calls[0].request.headers["X-Attachment-SHA256"],sha);
  assert.equal(calls[0].request.redirect,"error");
  const notice=JSON.parse(calls[1].request.body);
  assert.equal(notice.message_id,messageId);
  assert.equal(notice.urgency,"normal");
  assert.deepEqual(notice.attachments,[{id:sha,name:"attachment.png"}]);
  assert.ok(!calls[1].request.body.includes(options.imagePath));
  assert.ok(!calls[1].request.body.includes(options.token));
});

test("bad MIME or missing explicit message ID never sends",async t=>{
  let calls=0;
  const options=await fixture(t,()=>{calls++;},Buffer.from("private text"));
  await assert.rejects(sendRouterImage(options),/Unsupported image/);
  await assert.rejects(sendRouterImage({...options,messageId:undefined}),/explicit UUID/);
  assert.equal(calls,0);
});

test("upload failure never sends notification",async t=>{
  let calls=0;
  const options=await fixture(t,async()=>{calls++;return {ok:false,status:413};});
  await assert.rejects(sendRouterImage(options),/413/);
  assert.equal(calls,1);
});

test("mismatched upload receipt never sends notification",async t=>{
  let calls=0;
  const options=await fixture(t,async()=>{calls++;return uploaded({id:"0".repeat(64)});});
  await assert.rejects(sendRouterImage(options),/receipt mismatch/);
  assert.equal(calls,1);
});

test("unknown notice result is not automatically re-sent",async t=>{
  let calls=0;
  const options=await fixture(t,async(url)=>{
    calls++;
    if(url.endsWith("/attachments"))return uploaded();
    throw new Error("connection lost after posting");
  });
  await assert.rejects(sendRouterImage(options),/connection lost/);
  assert.equal(calls,2);
});

test("receiver event mode is explicit and reference has no source path",async t=>{
  let body;
  const options=await fixture(t,async(url,request)=>{
    if(url.endsWith("/attachments"))return uploaded();
    body=JSON.parse(request.body);
    return {ok:true,status:200,json:async()=>({ok:true,level:"event"})};
  });
  await sendRouterImage({...options,urgency:"urgent"});
  assert.equal(body.urgency,"urgent");
  assert.ok(!body.attachments[0].imagePath);
});

test("URL credentials are refused before uploading authorized bytes",async t=>{
  let called=false;
  const options=await fixture(t,()=>{called=true;});
  await assert.rejects(sendRouterImage({...options,url:"https://name:password@router.example.test"}),/embedded credentials/);
  assert.equal(called,false);
});

test("supported raster signatures are recognized; SVG is refused",()=>{
  assert.equal(imageType(png),"image/png");
  assert.equal(imageType(Buffer.from([255,216,255])),"image/jpeg");
  assert.equal(imageType(Buffer.from("GIF89a")),"image/gif");
  assert.equal(imageType(Buffer.from("RIFF0000WEBP")),"image/webp");
  assert.throws(()=>imageType(Buffer.from("<svg/>")),/Unsupported/);
});
