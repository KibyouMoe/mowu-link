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
  if (!Array.isArray(args?.questions) || !args.questions.length || args.questions.length>6) return null;
  const fields = args.questions.map(questionField);
  if (fields.some(field=>!field) || new Set(fields.map(field=>field.id)).size!==fields.length) return null;
  const question = {session,turn_id:turnId,native_question_id:callId};
  if (fields.length===1 && !fields[0].multiple) {
    const {text,options} = fields[0];
    Object.assign(question,{text,options:options.map(({id,label})=>({id,label}))});
  } else {
    question.questions = fields;
  }
  return {question,fields,pending:replies[0].seq};
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

function acceptedAnswer(found, events) {
  const callId = found.question.native_question_id;
  const replies = events.filter(e=>e.type==='user/message' && e.seq>found.pending &&
    e.data.source?.kind==='user-question-reply' && e.data.source.callId===callId
    && e.data.source.outcome==='answered');
  if (!replies.length) return null;
  if (replies.length!==1) throw new Error('native_question_unconfirmed');
  const [event] = replies;
  const body = json(event.data.content);
  if (body?.kind!=='answer_to_pending_question' || body.callId!==callId || !Array.isArray(body.answers) ||
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
  return {closed:true,answer,receipt_id:event.data.id,session:found.question.session,
    turn_id:found.question.turn_id,native_question_id:callId};
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
  try {
    service.answer(agent,native_question_id,{answers:nativeAnswers(found,answer)});
  } catch (error) {
    if (error.code!=='REPLY_QUEUED') throw error;
  }
  events = await readEvents();
  return {proof:acceptedAnswer(found,events)};
}
