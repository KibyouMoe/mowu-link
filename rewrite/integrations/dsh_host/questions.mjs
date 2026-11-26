import {isDeepStrictEqual} from 'node:util';

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

function findQuestion(session, events, turnId, callId) {
  const starts = events.filter(e=>e.type==='turn/start' && e.seq+'/'+e.data.turn===turnId);
  if (starts.length!==1) return null;
  const [start] = starts;
  const calls = events.filter(e=>e.type==='tool/call' && e.data.callId===callId);
  if (calls.length!==1 || calls[0].seq<=start.seq || calls[0].data.turn!==start.data.turn ||
      calls[0].data.name!=='ask_user_question') return null;
  const replies = events.filter(e=>e.data?.turn===start.data.turn && e.seq>calls[0].seq && pendingCall(e)===callId);
  if (replies.length!==1) return null;
  let args;
  try { args = JSON.parse(calls[0].data.arguments); }
  catch { return null; }
  if (args?.questions?.length!==1) return null;
  const q = args.questions[0];
  if (!q || typeof q.id!=='string' || !q.id || typeof q.question!=='string' || !q.question.trim() ||
      q.question.length>3000 || q.multi_select || q.intent || q.detail ||
      q.options!==undefined && (!Array.isArray(q.options) || q.options.length>8)) return null;
  const labels = (q.options ?? []).map(option=>option?.label);
  if (new Set(labels).size!==labels.length ||
      labels.some(label=>typeof label!=='string' || !label.trim() || label.length>200)) return null;
  const question = {session,turn_id:turnId,native_question_id:callId,text:q.question,
    options:labels.map((label,index)=>({id:String(index+1),label}))};
  return {question,id:q.id,labels,pending:replies[0].seq};
}

function acceptedAnswer(found, events) {
  const callId = found.question.native_question_id;
  const replies = events.filter(e=>e.type==='user/message' && e.seq>found.pending &&
    e.data.source?.kind==='user-question-reply' && e.data.source.callId===callId
    && e.data.source.outcome==='answered');
  if (!replies.length) return null;
  if (replies.length!==1) throw new Error('native_question_unconfirmed');
  const [event] = replies;
  const body = json(event.data.content);
  const [item] = Array.isArray(body?.answers) ? body.answers : [];
  if (body?.kind!=='answer_to_pending_question' || body.callId!==callId || body.answers?.length!==1 ||
      item?.id!==found.id || !Array.isArray(item.selected)) throw new Error('native_question_unconfirmed');
  const [label] = item.selected;
  const index = found.labels.indexOf(label);
  let answer;
  if (item.selected.length===0 && item.custom===undefined) answer = {mode:'skip'};
  else if (item.selected.length===0 && typeof item.custom==='string' && item.custom.trim()) {
    answer = {mode:'text',text:item.custom};
  } else if (item.selected.length===1 && item.custom===undefined && index>=0) {
    answer = {mode:'choice',option_id:String(index+1)};
  } else throw new Error('native_question_unconfirmed');
  return {closed:true,answer,receipt_id:event.data.id,session:found.question.session,
    turn_id:found.question.turn_id,native_question_id:callId};
}

export async function questionRequest(ctx,agent,request,readEvents) {
  const service = ctx.get('userQuestions');
  if (!service) return {question:null,proof:null};
  const {session,turn_id,native_question_id,operation,original,answer} = request;
  if (typeof turn_id!=='string' || typeof native_question_id!=='string' ||
      !turn_id || !native_question_id) throw new Error('invalid_native_question');
  let events = await readEvents();
  const found = findQuestion(session,events,turn_id,native_question_id);
  if (!found) return {question:null,proof:null};
  const active = () => service.continued(agent).some(q=>q.callId===native_question_id);
  if (operation==='question-open') return {question:active() ? found.question : null};
  const saved = acceptedAnswer(found,events);
  if (operation==='question-proof') return {proof:saved};
  if (!isDeepStrictEqual(original,found.question)) throw new Error('native_question_conflict');
  if (saved) {
    if (!isDeepStrictEqual(saved.answer,answer)) throw new Error('native_question_already_answered');
    return {proof:saved};
  }
  if (!active()) return {proof:null};
  const expectedKeys = {choice:['mode','option_id'],text:['mode','text'],skip:['mode']};
  if (!isDeepStrictEqual(Object.keys(answer ?? {}).sort(),expectedKeys[answer?.mode])) {
    throw new Error('invalid_native_question');
  }
  const item = {id:found.id,selected:[]};
  if (answer.mode==='choice') {
    const option = found.question.options.find(value=>value.id===answer.option_id);
    if (!option) throw new Error('invalid_native_option');
    item.selected = [option.label];
  }
  if (answer.mode==='text') {
    if (typeof answer.text!=='string' || !answer.text.trim() || answer.text.length>1000) {
      throw new Error('invalid_native_question');
    }
    item.custom = answer.text;
  }
  try {
    service.answer(agent,native_question_id,{answers:[item]});
  } catch (error) {
    if (error.code!=='REPLY_QUEUED') throw error;
  }
  events = await readEvents();
  return {proof:acceptedAnswer(found,events)};
}
