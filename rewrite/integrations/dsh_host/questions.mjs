import {createHash} from 'node:crypto';
import {closeSync,constants,fstatSync,fsyncSync,openSync,readFileSync,writeFileSync} from 'node:fs';
import path from 'node:path';
import {isDeepStrictEqual} from 'node:util';
import {openPrivateState} from './local-state.mjs';

export const questionStateFile = 'dsh-questions.sqlite3';

export async function sqliteQuestionMarks(directory) {
  const db = await openPrivateState(directory,questionStateFile,'native_question_unconfirmed');
  try {
    db.exec(`CREATE TABLE IF NOT EXISTS foreground_questions (
      digest TEXT PRIMARY KEY CHECK(length(digest)=64)
    ) STRICT, WITHOUT ROWID`);
    const find = db.prepare('SELECT 1 FROM foreground_questions WHERE digest=?');
    const insert = db.prepare('INSERT INTO foreground_questions VALUES (?) ON CONFLICT(digest) DO NOTHING');
    return {close:()=>db.close(),has:id=>Boolean(find.get(id)),record:id=>insert.run(id)};
  } catch (error) {
    db.close();
    throw error;
  }
}

export function foregroundRecords(directory,session,marks) {
  function key(call) {
    const {callId,turn,step,name,arguments:args} = call.data;
    return createHash('sha256').update(JSON.stringify([session,call.seq,callId,turn,step,name,args])).digest('hex');
  }
  if (marks) return {has:call=>marks.has(key(call)),record:call=>marks.record(key(call))};
  if (marks===null) return {has:()=>false};
  function filename(id) {
    return path.join(directory,'question-'+id+'.open');
  }
  function has(call) {
    const id = key(call);
    let fd;
    try {
      fd = openSync(filename(id),constants.O_RDONLY|constants.O_NOFOLLOW|constants.O_NONBLOCK);
      const info = fstatSync(fd);
      if (!info.isFile() || info.uid!==process.getuid() || (info.mode&0o077) || info.size!==id.length) return false;
      return readFileSync(fd,'utf8')===id;
    } catch (error) {
      if (error.code==='ENOENT') return false;
      throw error;
    } finally {
      if (fd!==undefined) closeSync(fd);
    }
  }
  return {has,record(call) {
    const id = key(call);
    let fd;
    try {
      fd = openSync(filename(id),'wx',0o600);
      writeFileSync(fd,id,'utf8');
      fsyncSync(fd);
      const parent = openSync(directory,'r');
      try { fsyncSync(parent); }
      finally { closeSync(parent); }
    } catch (error) {
      if (error.code!=='EEXIST' || !has(call)) throw error;
    } finally {
      if (fd!==undefined) closeSync(fd);
    }
  }};
}

function json(content) {
  try { return JSON.parse(content?.find(part=>part.type==='text')?.text); }
  catch { return null; }
}

export function pendingCall(event) {
  if (event.type!=='tool/result' || event.data.error) return null;
  const message = event.data?.message;
  const result = json(message?.content);
  return !message?.isError && typeof message?.toolCallId==='string' && result?.pending===true &&
    result.callId===message.toolCallId ? message.toolCallId : null;
}

function form(session,start,call) {
  let args;
  try { args = JSON.parse(call.data.arguments); }
  catch { return null; }
  if (!Array.isArray(args?.questions) || !args.questions.length || args.questions.length>6) return null;
  const fields = args.questions.map(questionField);
  if (fields.some(field=>!field) || new Set(fields.map(field=>field.id)).size!==fields.length) return null;
  const question = {session,turn_id:start.seq+'/'+start.data.turn,native_question_id:call.data.callId};
  if (fields.length===1 && !fields[0].multiple) {
    const {text,options} = fields[0];
    Object.assign(question,{text,options:options.map(({id,label})=>({id,label}))});
  } else {
    question.questions = fields;
  }
  return {question,fields};
}

function findQuestion(session,events,turnId,callId,records) {
  const start = events.find(e=>e.type==='turn/start' && e.seq+'/'+e.data.turn===turnId);
  if (!start) return null;
  const calls = events.filter(e=>e.type==='tool/call' && e.data.callId===callId);
  const [call] = calls;
  if (calls.length!==1 || call.seq<=start.seq || call.data.turn!==start.data.turn ||
      call.data.name!=='ask_user_question') return null;
  const end = events.find(e=>e.type==='turn/end' && e.data.turn===start.data.turn && e.seq>call.seq);
  const window = events.filter(e=>e.data?.turn===start.data.turn && e.seq>call.seq && (!end || e.seq<end.seq));
  const results = window.filter(e=>e.type==='tool/result' && e.data.message?.toolCallId===callId &&
    e.data.message.source?.kind==='tool' && e.data.message.source.callId===callId);
  if (results.length>1) return null;
  const [result] = results;
  const pending = result && pendingCall(result)===callId ? result.seq : null;
  const foreground = records.has(call);
  if (!pending && !foreground) return null;
  const found = form(session,start,call);
  return found && {...found,pending,foreground,result,end};
}

function nonempty(value,max) {
  return typeof value==='string' && value.trim() && value.length<=max;
}

function questionField(q) {
  if (!q || !nonempty(q.id,200) || !nonempty(q.question,3000) || 'intent' in q || 'detail' in q ||
      (q.multi_select!==undefined && typeof q.multi_select!=='boolean') ||
      (q.options!==undefined && (!Array.isArray(q.options) || q.options.length>8)) ||
      (q.header!==undefined && typeof q.header!=='string')) return null;
  const options = q.options ?? [];
  const labels = options.map(option=>option?.label);
  if (new Set(labels).size!==labels.length ||
      labels.some(label=>!nonempty(label,200)) || q.header?.length>200 ||
      options.some(option=>(option.description!==undefined && typeof option.description!=='string') ||
        option.description?.length>500)) return null;
  const field = {id:q.id,text:q.question,multiple:q.multi_select===true,custom:true,
    options:options.map(({label,description},index)=>({id:String(index+1),label,
      ...(description?.trim() ? {description} : {})}))};
  if (q.header?.trim()) field.header = q.header;
  return field;
}

function proofFromAnswers(found,body,receiptId) {
  const callId = found.question.native_question_id;
  if (typeof receiptId!=='string' || !receiptId || !Array.isArray(body?.answers) ||
      body.answers.length!==found.fields.length) throw new Error('native_question_unconfirmed');
  const answers = found.fields.map(field=>{
    const matching = body.answers.filter(item=>item?.id===field.id);
    const [native] = matching;
    if (matching.length!==1 || !Array.isArray(native.selected) ||
        native.selected.length!==new Set(native.selected).size ||
        native.selected.some(label=>!field.options.some(option=>option.label===label))) {
      throw new Error('native_question_unconfirmed');
    }
    const value = {id:field.id,option_ids:field.options.filter(option=>native.selected.includes(option.label)).map(option=>option.id)};
    if (native.custom!==undefined) value.text = native.custom;
    nativeAnswer(field,value);
    return value;
  });
  let answer = {mode:'batch',answers};
  if (!found.question.questions) {
    const [single] = answers;
    answer = {mode:'skip'};
    if (single.option_ids.length) answer = {mode:'choice',option_id:single.option_ids[0]};
    if ('text' in single) answer = {mode:'text',text:single.text};
  }
  return {closed:true,answer,receipt_id:receiptId,session:found.question.session,
    turn_id:found.question.turn_id,native_question_id:callId};
}

function acceptedAnswer(found,events) {
  const callId = found.question.native_question_id;
  if (found.pending!==null) {
    const replies = events.filter(e=>e.type==='user/message' && e.seq>found.pending &&
      e.data.source?.kind==='user-question-reply' && e.data.source.callId===callId
      && e.data.source.outcome==='answered');
    if (!replies.length) return null;
    if (replies.length!==1) throw new Error('native_question_unconfirmed');
    const [event] = replies;
    const body = json(event.data.content);
    if (body?.kind!=='answer_to_pending_question' || body.callId!==callId) throw new Error('native_question_unconfirmed');
    return proofFromAnswers(found,body,event.data.id);
  }
  const message = found.result?.data.message;
  if (message && !found.result.data.error && !message.isError) {
    return proofFromAnswers(found,json(message.content),message.id);
  }
  if (!found.end) return null;
  const {session,turn_id,native_question_id} = found.question;
  return {session,turn_id,native_question_id,closed:true,answer:null,state:'ended_unanswered',
    receipt_id:'dsh-turn-end:'+found.end.seq};
}

function nativeAnswer(field,value) {
  const fields = ['id','option_ids'];
  if (Object.hasOwn(value ?? {},'text')) fields.push('text');
  if (!value || !isDeepStrictEqual(Object.keys(value).sort(),fields) || value.id!==field.id ||
      !Array.isArray(value.option_ids)) throw new Error('invalid_native_question');
  const options = field.options.filter(option=>value.option_ids.includes(option.id));
  if (!isDeepStrictEqual(options.map(option=>option.id),value.option_ids) ||
      (!field.multiple && (options.length>1 || (options.length && 'text' in value)))) {
    throw new Error('invalid_native_option');
  }
  const answer = {id:field.id,selected:options.map(option=>option.label)};
  if ('text' in value) {
    if (!nonempty(value.text,1000)) throw new Error('invalid_native_question');
    answer.custom = value.text;
  }
  return answer;
}

function nativeAnswers(found,answer) {
  if (found.question.questions) {
    if (answer?.mode!=='batch' || !isDeepStrictEqual(Object.keys(answer).sort(),['answers','mode']) ||
        !Array.isArray(answer.answers) || answer.answers.length!==found.fields.length) {
      throw new Error('invalid_native_question');
    }
    return found.fields.map((field,index)=>nativeAnswer(field,answer.answers[index]));
  }
  const expectedKeys = {choice:['mode','option_id'],text:['mode','text'],skip:['mode']};
  if (!isDeepStrictEqual(Object.keys(answer ?? {}).sort(),expectedKeys[answer?.mode])) {
    throw new Error('invalid_native_question');
  }
  const [field] = found.fields;
  const value = {id:field.id,option_ids:answer.mode==='choice' ? [answer.option_id] : []};
  if (answer.mode==='text') value.text = answer.text;
  return [nativeAnswer(field,value)];
}

function liveForm(agent,request,events) {
  const start = events.findLast(e=>e.type==='turn/start'||e.type==='turn/end');
  if (start?.type!=='turn/start') return null;
  const finished = new Set(events.filter(e=>e.type==='tool/result').map(e=>e.data.message?.toolCallId));
  const calls = events.filter(e=>e.type==='tool/call' && e.data.name==='ask_user_question'
    && e.seq>start.seq && e.data.turn===start.data.turn && !finished.has(e.data.callId)
    && (!request.wait || e.data.callId===request.wait.callId));
  if (calls.length!==1) return null;
  const [call] = calls;
  const found = form(agent.session.id,start,call);
  if (!found || events.filter(e=>e.type==='tool/call' && e.data.callId===call.data.callId).length!==1) return null;
  const fields = request.questions.map(q=>{
    // Never mirror requests belonging to permission flows or plan reviews.
    if (q.intent!==undefined || q.detail!==undefined) return null;
    const {multiSelect,...rest} = q;
    return questionField({...rest,...(multiSelect===undefined ? {} : {multi_select:multiSelect})});
  });
  return isDeepStrictEqual(fields,found.fields) ? {...found,call} : null;
}

export function awaitingQuestionProvider(agent,call) {
  const events = agent.session.snapshotEvents();
  const start = events.findLast(event=>event.type==='turn/start'||event.type==='turn/end');
  return start?.type==='turn/start' && start.seq<call.seq && form(agent.session.id,start,call)!==null
    && !events.some(event=>event.type==='tool/result' && event.seq>call.seq
      && event.data.message?.toolCallId===call.data.callId);
}

export function installForegroundQuestions(ctx,agent,records,bound) {
  const active = new Map();
  const lifetime = new AbortController();
  const unsubscribe = ctx.on('user-questions/request',async (request,next)=>{
    const originalSignal = request.signal;
    if (request.agent!==agent || !originalSignal || originalSignal.aborted) return next();
    const found = liveForm(agent,request,agent.session.snapshotEvents());
    if (!found) return next();
    bound();
    const signal = AbortSignal.any([originalSignal,lifetime.signal]);
    signal.throwIfAborted();
    const callId = found.question.native_question_id;
    if (active.has(callId)) return next();
    // Register synchronously before yielding to a native answerer. The display path independently
    // validates this record against the exact tool call in the persisted native Session.
    try { records.record(found.call); }
    catch { return next(); }
    const waiting = Promise.withResolvers();
    const provider = new AbortController();
    const stop = ()=>waiting.reject(signal.reason);
    const entry = {question:found.question,offer(answer) {
      if (signal.aborted || active.get(callId)!==entry) return;
      active.delete(callId);
      waiting.resolve(answer);
    }};
    active.set(callId,entry);
    signal.addEventListener('abort',stop,{once:true});
    try {
      // The original answerer still gets this exact native question. Its view closes when the
      // same waterfall accepts the Feishu answer or the native lifetime ends.
      request.signal = AbortSignal.any([signal,provider.signal]);
      const desktop = Promise.resolve().then(next).catch(error=>{
        if (error?.code==='NO_PROVIDER') return waiting.promise;
        throw error;
      });
      return await Promise.race([waiting.promise,desktop]);
    } finally {
      active.delete(callId);
      signal.removeEventListener('abort',stop);
      provider.abort();
    }
  },{prepend:true});
  ctx.effect(()=>()=>{
    unsubscribe();
    lifetime.abort();
  });
  return active;
}

export async function questionRequest(ctx,agent,request,readEvents,foreground,records) {
  const service = ctx.get('userQuestions');
  if (!service) return {question:null,proof:null};
  const {session,turn_id,native_question_id,operation,original,answer} = request;
  if (typeof turn_id!=='string' || typeof native_question_id!=='string' ||
      !turn_id || !native_question_id) throw new Error('invalid_native_question');
  let events = await readEvents();
  const found = findQuestion(session,events,turn_id,native_question_id,records);
  if (!found) return {question:null,proof:null};
  const waiting = foreground?.get(native_question_id);
  const active = () => found.pending!==null && service.continued(agent).some(q=>q.callId===native_question_id);
  if (operation==='question-open') return {question:found.foreground || active() ? found.question : null};
  const saved = acceptedAnswer(found,events);
  if (operation==='question-proof') return {proof:saved};
  if (!isDeepStrictEqual(original,found.question)) throw new Error('native_question_conflict');
  if (saved) {
    if (saved.state==='ended_unanswered') throw new Error('native_question_ended');
    if (!isDeepStrictEqual(saved.answer,answer)) throw new Error('native_question_already_answered');
    return {proof:saved};
  }
  if (waiting) {
    if (!isDeepStrictEqual(waiting.question,found.question)) throw new Error('native_question_conflict');
    waiting.offer({answers:nativeAnswers(found,answer)});
    // The SDK tool, rather than the answerer, writes the result. A lost reply is recovered
    // by reading the original tool result using this same turn and native call ID.
    for (let attempt=0;attempt<10;attempt++) {
      const observed = await readEvents();
      const current = findQuestion(session,observed,turn_id,native_question_id,records);
      const proof = current && acceptedAnswer(current,observed);
      if (proof) {
        if (proof.state==='ended_unanswered') throw new Error('native_question_ended');
        if (!isDeepStrictEqual(proof.answer,answer)) throw new Error('native_question_already_answered');
        return {proof};
      }
      if (current?.pending!=null) break;
      await new Promise(resolve=>setTimeout(resolve,20));
    }
    return {proof:null};
  }
  if (!active()) return {proof:null};
  try {
    service.answer(agent,native_question_id,{answers:nativeAnswers(found,answer)});
  } catch (error) {
    if (error.code!=='REPLY_QUEUED') throw error;
  }
  events = await readEvents();
  return {proof:acceptedAnswer(found,events)};
}
