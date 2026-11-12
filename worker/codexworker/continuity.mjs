import {mkdir,open,readFile,rename,writeFile} from "node:fs/promises";
import path from "node:path";
import {randomUUID} from "node:crypto";
async function read(file,fallback=null){try{return JSON.parse(await readFile(file,"utf8"));}catch(e){if(e.code==="ENOENT")return fallback;throw e;}}
async function atomic(file,value){
 await mkdir(path.dirname(file),{recursive:true,mode:0o700});
 const temp=file+"."+randomUUID()+".tmp",f=await open(temp,"wx",0o600);
 try{await f.writeFile(JSON.stringify(value)+"\n");await f.sync();}finally{await f.close();}
 await rename(temp,file);
}
// Heavy observation caches are internal; keep receipt acknowledgements stable and small.
export function compactAck(result){
 if(!result||typeof result!=="object")return result;
 const copy=structuredClone(result);
 for(const key of ["session","currentSession"])if(copy[key])for(const k of
  ["seen","turnInitiators","toolSeen","seenImages","publicTextCards","turnMessages","inputMessages","observedTurns","completedMessages","publicJournalCheckpoint","toolBubble"])
  delete copy[key][k];
 return copy;
}
export function shrinkObservationText(session){
 for(const value of Object.values(session.seen??{}))delete value.text;
 for(const [id,value] of Object.entries(session.toolSeen??{}))
  if(value.status!=="running")session.toolSeen[id]={itemId:value.itemId,turnId:value.turnId,status:value.status};
}
export class ReceiptArchive {
 constructor(stateDir){this.root=path.join(stateDir,"receipt-archive");this.tail=Promise.resolve();}
 file(id){if(typeof id!=="string"||!/^[0-9a-f-]{36}$/i.test(id))throw Error("invalid_receipt_id");return path.join(this.root,id+".json");}
 async get(id){return read(this.file(id));}
 async trim(state,limit=500){
  limit=Number.isSafeInteger(limit)&&limit>0?limit:500;
  const operation=this.tail.then(async()=>{
   const entries=Object.entries(state.receipts),excess=entries.length-limit;if(excess<=0)return;
   const pinned=new Set();
   for(const s of Object.values(state.sessions))for(const p of [...s.queue??[],...s.steerQueue??[],...s.pendingInputs??[]])pinned.add(p.messageId);
   let removed=0;
   for(const [id,r] of entries){
    if(removed>=Math.min(excess,50))break;
    if(r.pending||pinned.has(id)||["unknown","dispatching"].includes(r.nativeDispatch?.status)||
       state.feishuReceipts?.[id]&&!state.feishuReceipts[id].cleanupRequested)continue;
    if(r.nativeDispatch?.status==="accepted"&&!Object.values(state.sessions).some(s=>s.completedMessages?.[id]))continue;
    if(!r.response&&!r.error)continue;
    const old=await this.get(id);
    if(old&&old.fingerprint!==r.fingerprint)throw Error("receipt_archive_conflict");
    if(!old)await atomic(this.file(id),r);
    delete state.receipts[id];removed++;
   }
  });this.tail=operation.catch(()=>{});return operation;
 }
}
