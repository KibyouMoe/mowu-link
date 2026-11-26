// Real DSH AgentLoop and persistence in an isolated directory. The only model provider is an in-process test stream.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {writeFileSync} from 'node:fs';
import {createRequire} from 'node:module';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {createInterface} from 'node:readline';
import * as adapter from './index.mjs';

const [root,directory,session,mode='stop'] = process.argv.slice(2);
const dependency = createRequire(path.join(root,'probe.cjs'));
const sdk = name=>import(pathToFileURL(dependency.resolve('@deepseek-ai/'+name)));
const {Context,Service} = await sdk('cordis');
const {LlmAdapter,LlmRuntime} = await sdk('dsh-llm');
const entered = Promise.withResolvers();
const secondRound = Promise.withResolvers();
const formalRound = Promise.withResolvers();
const compactEntered = Promise.withResolvers();
const compactReleased = Promise.withResolvers();
const foregroundQuestion = Promise.withResolvers();
const foregroundAnswer = Promise.withResolvers();
const isCold = mode.startsWith('cold-resume');
const isManaged = mode.startsWith('managed-sessions');
const usesCleanup = mode.startsWith('cleanup-');
const standalone = mode==='standalone-trainer-question';
const foregroundRelay = mode.startsWith('foreground-relay');
const usesForeground = foregroundRelay || mode==='foreground-question';
const usesGoals = ['goals','goal-driver','formal-goal','cold-resume-goal'].includes(mode);
const usesMedia = ['media','peer-media','handoff'].includes(mode);
const usesApprovals = ['command-approvals','command-approvals-reopen'].includes(mode);
const modelRequests = [];
const catalogSessions = [];
const privateStops = new Map(['one','two','three'].map(tag=>[tag,Promise.withResolvers()]));
const raceRelease = Promise.withResolvers();
const mediaRequests = [];
const standaloneAnswers = [];
let foregroundProviderCloses = 0;

class LocalModel extends LlmAdapter {
  submitted = new Map();
  async listModels(provider) {
    return [await this.resolveModel(provider,'local')];
  }
  async resolveModel(provider, model) {
    return {provider,id:model,name:'Local test model',reasoning:{
      efforts:[{id:'low',name:'Low'},{id:'high',name:'High'}],defaultEffort:'low'},
      inputModalities:usesMedia ? ['text','image'] : ['text']};
  }
  async *stream({provider,model,reasoningEffort,messages,signal,tools,purpose}) {
    if (purpose==='compaction') {
      modelRequests.push({command:'compaction',provider,model});
      compactEntered.resolve();
      await compactReleased.promise;
      signal.throwIfAborted();
      yield {type:'block-start',index:0,blockType:'text'};
      yield {type:'text-delta',index:0,text:'Retained fixture history.'};
      yield {type:'finish',reason:{kind:'stop'}};
      return;
    }
    const message = messages.findLast(entry=>entry.role==='user'
      && (!usesApprovals || entry.source.kind!=='runtime-context'));
    const text = message.content.find(block=>block.type==='text')?.text ?? '';
    const payload = text.startsWith('魔物链接投递\n') ? JSON.parse(text.split('\n')[1]) : null;
    const goal = message.source.kind==='goal' ? message.source : null;
    const command = goal ? 'goal' : payload?.kind ?? 'private';
    modelRequests.push({command,provider,model,reasoningEffort});
    if (usesCleanup && (command==='task.cancel' || command==='task.transfer_out')) {
      const tag = 'cleanup-report-'+payload.assignment_id;
      if (!this.submitted.has(tag)) {
        this.submitted.set(tag,true);
        const args = {task_id:payload.task_id,assignment_id:payload.assignment_id,summary:'Old assignment stopped',
          outcome:command==='task.cancel' ? 'cancel_confirmed' : 'transfer_confirmed'};
        yield {type:'block-start',index:0,blockType:'tool-call'};
        yield {type:'block-end',index:0,block:{type:'tool-call',id:tag,name:'worker_report',arguments:JSON.stringify(args)}};
        yield {type:'finish',reason:{kind:'tool-calls'}};
        return;
      }
    }
    if (mode==='cleanup-forbidden-tool' && command==='task' && !this.submitted.has('forbidden')) {
      this.submitted.set('forbidden',true);
      assert.ok(tools.some(tool=>tool.name==='fixture_write_workspace'));
      yield {type:'block-start',index:0,blockType:'tool-call'};
      yield {type:'block-end',index:0,block:{type:'tool-call',id:'cleanup-forbidden-tool',name:'fixture_write_workspace',
        arguments:'{}'}};
      yield {type:'finish',reason:{kind:'tool-calls'}};
      return;
    }
    if (usesApprovals && text.startsWith('DSH approval fixture ') && !this.submitted.has(message.id)) {
      this.submitted.set(message.id,true);
      const variant = text.substring('DSH approval fixture '.length);
      const args = {description:'Read the isolated working directory',command:'pwd'};
      if (variant==='unverifiable') args.workdir = 'absent-approval-directory';
      else if (variant==='relative') args.workdir = 'approval-child';
      else if (variant==='physical-parent') args.workdir = 'approval-link/..';
      else if (!['implicit','waiting-change','dispatch-change','after-pin-change'].includes(variant)) args.workdir = directory;
      assert.ok(tools?.some(tool=>tool.name==='bash'));
      yield {type:'block-start',index:0,blockType:'tool-call'};
      yield {type:'block-end',index:0,block:{type:'tool-call',id:'fixture-bash-'+variant,name:'bash',
        arguments:JSON.stringify(args)}};
      yield {type:'finish',reason:{kind:'tool-calls'}};
      return;
    }
    if (mode==='peer-source' && text.startsWith('Send peer fixture\n') && !this.submitted.has('outbound')) {
      this.submitted.set('outbound',true);
      const args = JSON.parse(text.split('\n')[1]);
      assert.ok(tools?.some(tool=>tool.name==='worker_send_peer'));
      yield {type:'block-start',index:0,blockType:'tool-call'};
      yield {type:'block-end',index:0,block:{type:'tool-call',id:'peer-native-send',name:'worker_send_peer',
        arguments:JSON.stringify(args)}};
      yield {type:'finish',reason:{kind:'tool-calls'}};
      return;
    }
    if (standalone && text.startsWith('Standalone trainer fixture\n') && !this.submitted.has(message.id)) {
      this.submitted.set(message.id,true);
      assert.ok(tools.some(tool=>tool.name==='worker_ask_trainer'));
      yield {type:'block-start',index:0,blockType:'tool-call'};
      yield {type:'block-end',index:0,block:{type:'tool-call',id:'standalone-'+message.id,name:'worker_ask_trainer',
        arguments:text.split('\n')[1]}};
      yield {type:'finish',reason:{kind:'tool-calls'}};
      return;
    }
    if (standalone && command==='trainer_answer') standaloneAnswers.push(payload);
    if (standalone && text==='Hold another isolated turn') await waitForStop(signal);
    if (usesMedia) {
      const store = ctx.get('attachments');
      const images = [];
      for (const part of message.content.filter(block=>block.type==='image')) {
        const image = await store.readImage(part.attachment);
        images.push({sha256:createHash('sha256').update(image.data).digest('hex'),size:image.data.length});
      }
      mediaRequests.push({caption:text,images,partTypes:message.content.map(part=>part.type)});
      if (mode==='peer-media' && command==='peer' && !this.submitted.has('peer-history')) {
        this.submitted.set('peer-history',true);
        assert.ok(tools?.some(tool=>tool.name==='worker_peer_inbox'));
        yield {type:'block-start',index:0,blockType:'tool-call'};
        yield {type:'block-end',index:0,block:{type:'tool-call',id:'peer-native-history',name:'worker_peer_inbox',
          arguments:JSON.stringify({view:'history'})}};
        yield {type:'finish',reason:{kind:'tool-calls'}};
        return;
      }
      if (mode==='peer-media' && command==='peer' && !this.submitted.has('peer-directory')) {
        this.submitted.set('peer-directory',true);
        assert.ok(tools?.some(tool=>tool.name==='worker_list_peers'));
        yield {type:'block-start',index:0,blockType:'tool-call'};
        yield {type:'block-end',index:0,block:{type:'tool-call',id:'peer-native-directory',name:'worker_list_peers',
          arguments:JSON.stringify({online:true})}};
        yield {type:'finish',reason:{kind:'tool-calls'}};
        return;
      }
      if (mode==='peer-media' && command==='peer' && !this.submitted.has(payload.message_id)) {
        this.submitted.set(payload.message_id,true);
        assert.ok(tools?.some(tool=>tool.name==='worker_send_peer'));
        const args = {to_worker:payload.from_worker,reply_to:payload.message_id,text:'Native reply to the original message'};
        yield {type:'block-start',index:0,blockType:'tool-call'};
        yield {type:'block-end',index:0,block:{type:'tool-call',id:'peer-native-reply',name:'worker_send_peer',
          arguments:JSON.stringify(args)}};
        yield {type:'finish',reason:{kind:'tool-calls'}};
        return;
      }
      const statusKey = payload?.message_id+'-status';
      if (mode==='peer-media' && command==='peer' && !this.submitted.has(statusKey)) {
        this.submitted.set(statusKey,true);
        assert.ok(tools?.some(tool=>tool.name==='worker_peer_status'));
        yield {type:'block-start',index:0,blockType:'tool-call'};
        yield {type:'block-end',index:0,block:{type:'tool-call',id:'peer-native-status',name:'worker_peer_status',
          arguments:JSON.stringify({message_id:payload.message_id})}};
        yield {type:'finish',reason:{kind:'tool-calls'}};
        return;
      }
      if (mode!=='handoff' || command!=='task') {
        yield {type:'block-start',index:0,blockType:'text'};
        yield {type:'text-delta',index:0,text:`Local fixture received ${images.length} image(s).`};
        yield {type:'finish',reason:{kind:'stop'}};
        return;
      }
    }
    if (goal?.round===1 && mode!=='formal-goal') await waitForStop(signal);
    if (goal?.round===2) secondRound.resolve();
    if (mode==='material' && command==='task') this.candidateFile = payload.instructions;
    if (mode==='material' && command==='task.shelved' && !this.submitted.has('material')) {
      const result = messages.findLast(entry=>entry.toolCallId==='candidate-stage');
      const name = result ? 'worker_submit_material' : 'worker_stage_artifacts';
      assert.ok(tools?.some(tool=>tool.name===name));
      const args = result ? {task_id:payload.task_id,assignment_id:payload.assignment_id,
        artifact_ids:JSON.parse(result.content[0].text).artifact_ids} : {attachment_ids:[this.candidateFile]};
      if (result) this.submitted.set('material',true);
      yield {type:'block-start',index:0,blockType:'tool-call'};
      yield {type:'block-end',index:0,block:{type:'tool-call',id:result ? 'native-candidate' : 'candidate-stage',name,
        arguments:JSON.stringify(args)}};
      yield {type:'finish',reason:{kind:'tool-calls'}};
      return;
    }
    if (mode==='formal-goal') {
      let action;
      if (command==='task') action = ['create_goal',{objective:'Complete the isolated formal objective',max_goal_rounds:1}];
      if (goal?.round===1) {
        formalRound.resolve();
        action = ['update_goal',{goal_id:goal.goalId,revision:goal.revision,action:'complete'}];
      }
      if (command==='task_probe') action = ['worker_report',{task_id:payload.task_id,assignment_id:payload.assignment_id,
        summary:'Verified in the separate goal continuation',outcome:'completed'}];
      if (action && !this.submitted.has(action[0])) {
        const [name,args] = action;
        this.submitted.set(name,true);
        assert.ok(tools?.some(tool=>tool.name===name));
        yield {type:'block-start',index:0,blockType:'tool-call'};
        yield {type:'block-end',index:0,block:{type:'tool-call',id:'native-formal-goal-'+name,name,arguments:JSON.stringify(args)}};
        yield {type:'finish',reason:{kind:'tool-calls'}};
        return;
      }
    }
    if (['private-stop','admission-race'].includes(mode) && text.startsWith('Hold private ')) {
      privateStops.get(text.split(' ').at(-1)).resolve();
      if (mode==='admission-race') await Promise.race([raceRelease.promise,waitForStop(signal)]);
      else await waitForStop(signal);
    }
    if ((mode==='questions' && /^Question fixture (choice|text|skip|multi|native)$/.test(text)) ||
        (usesForeground && text==='Question fixture foreground')) {
      const choice = text.split(' ').at(-1);
      if (!this.submitted.has(text)) {
        this.submitted.set(text,true);
        assert.ok(tools?.some(tool=>tool.name==='ask_user_question'));
        const options = [{label:'A'},{label:'B'}];
        const question = {id:'primary',question:'Pick '+choice+'?',options,multi_select:choice==='multi'};
        const questions = [question];
        if (choice==='multi') {
          questions.push({id:'note',question:'What should the agent remember?',header:'备注'},
            {id:'optional',question:'Optional follow-up?',options:[{label:'A',description:'Follow the same route'}]});
        }
        yield {type:'block-start',index:0,blockType:'tool-call'};
        yield {type:'block-end',index:0,block:{type:'tool-call',id:'native-question-'+choice,name:'ask_user_question',
          arguments:JSON.stringify({questions})}};
        yield {type:'finish',reason:{kind:'tool-calls'}};
        return;
      }
    }
    if (mode==='work-question' && ['task','question_answer','task_update'].includes(command) &&
        !this.submitted.has(command)) {
      this.submitted.set(command,true);
      const initial = command==='task';
      const name = initial ? 'worker_ask' : 'worker_report';
      assert.ok(tools?.some(tool=>tool.name===name));
      if (command==='question_answer') {
        assert.equal(payload.answer.mode,'choice');
        assert.equal(payload.answer.option_id,'go');
        assert.ok(payload.question_id);
      }
      const args = initial ? {text:'Use the first route?',options:[{id:'go',label:'继续'}]} : {
        task_id:payload.task_id,assignment_id:payload.assignment_id,
        outcome:command==='question_answer' ? 'continue_requested' : 'completed',
        summary:command==='question_answer' ? 'Will continue the original assignment' : 'Finished after the confirmed answer',
      };
      yield {type:'block-start',index:0,blockType:'tool-call'};
      yield {type:'block-end',index:0,block:{type:'tool-call',id:'native-w05-'+command,name,arguments:JSON.stringify(args)}};
      yield {type:'finish',reason:{kind:'tool-calls'}};
      return;
    }
    if (command==='task') {
      if (mode==='formal-artifacts') {
        const attempt = this.submitted.get(payload.assignment_id) ?? 0;
        if (attempt<3) {
          this.submitted.set(payload.assignment_id,attempt+1);
          const staging = attempt<2;
          const name = staging ? 'worker_stage_artifacts' : 'worker_report';
          assert.ok(tools?.some(tool=>tool.name===name));
          const previous = messages.findLast(entry=>entry.toolCallId==='artifact-fixture-1');
          const args = staging ? {attachment_ids:[payload.instructions]} : {
            task_id:payload.task_id,assignment_id:payload.assignment_id,summary:'Native fixture with an artifact',
            artifact_ids:JSON.parse(previous.content[0].text).artifact_ids,
          };
          yield {type:'block-start',index:0,blockType:'tool-call'};
          yield {type:'block-end',index:0,block:{type:'tool-call',id:'artifact-fixture-'+attempt,
            name,arguments:JSON.stringify(args)}};
          yield {type:'finish',reason:{kind:'tool-calls'}};
          return;
        }
      } else if (isCold || isManaged || ['formal','handoff'].includes(mode)) {
        const attempt = this.submitted.get(payload.assignment_id) ?? 0;
        if (attempt<2) {
          this.submitted.set(payload.assignment_id,attempt+1);
          assert.ok(tools?.some(tool=>tool.name==='worker_report'));
          yield {type:'block-start',index:0,blockType:'tool-call'};
          yield {type:'block-end',index:0,block:{type:'tool-call',id:'native-w04-fixture-'+attempt,name:'worker_report',
            arguments:JSON.stringify({task_id:payload.task_id,assignment_id:payload.assignment_id,
              summary:'DSH native tool fixture result'})}};
          yield {type:'finish',reason:{kind:'tool-calls'}};
          return;
        }
      } else if (!['work-question','formal-goal','admission-race','material'].includes(mode)) {
        await waitForStop(signal);
      }
    }
    yield {type:'block-start',index:0,blockType:'text'};
    yield {type:'text-delta',index:0,text:'isolated fixture response'};
    yield {type:'finish',reason:{kind:'stop'}};
  }
}

async function waitForStop(signal) {
  const stop = Promise.withResolvers();
  signal.addEventListener('abort',()=>stop.resolve(),{once:true});
  entered.resolve();
  if (!signal.aborted) await stop.promise;
  signal.throwIfAborted();
}

const ctx = new Context();
const plugins = [];
let originalHandle;
let resumedHandle;
let controllerResolutions = 0;
async function install(plugin,options) {
  const handle = ctx.plugin(plugin,options);
  plugins.push(handle);
  await handle.await();
}

function envelope(operation,id,payload) {
  const payload_json = JSON.stringify(payload);
  const digest = createHash('sha256').update(payload_json).digest('hex');
  return {operation,session,delivery_id:id,payload_json,digest};
}

async function setup() {
  await install((await sdk('dsh-session')).SessionStore);
  await install((await sdk('dsh-session-projection')).SessionProjectionRegistry);
  await install((await sdk('dsh-agent')).AgentRegistry);
  if (usesGoals) await install((await sdk('dsh-goal')).GoalService,mode==='goal-driver' ? {defaultMaxGoalRounds:2} : {});
  await install(await sdk('dsh-session-persistence-jsonl'),{root:path.join(directory,'sessions')});
  if (mode==='catalog' || isCold || isManaged) {
    await install((await sdk('dsh-session-query')).SessionQueryEngine);
    await install((await sdk('dsh-session-title')).SessionTitleService,
      {fallbackMaxWords:12,fallbackMaxBytes:120,maxTitleBytes:300});
  }
  await install((await sdk('dsh-system-prompt')).SystemPrompt);
  if (usesMedia) await install((await sdk('dsh-attachment-local')).default,{dshHome:directory});
  await install((await sdk('dsh-tools')).ToolsService);
  if (mode==='cleanup-forbidden-tool') {
    ctx.get('tools').register({name:'fixture_write_workspace',description:'Write only in the isolated fixture directory',
      parameters:{type:'object',additionalProperties:false},output:{schema:{type:'object'},render:()=>[]},
      execute:()=>{
        writeFileSync(path.join(directory,'workspace-write.txt'),'fixture proof');
        return {written:true};
      }});
  }
  if (usesApprovals) {
    await install((await sdk('dsh-user-approval')).ApprovalService,{policy:'ask'});
    await install((await sdk('dsh-fs-local')).LocalFileSystem,{cwd:directory});
    await install((await sdk('dsh-working-directory')).WorkingDirectoryService,{defaultDirectory:directory});
    await install((await sdk('dsh-shell-env')).ShellEnvRegistry,{dshHome:directory});
    await install((await sdk('dsh-subprocess-local')).LocalSubprocessRuntime);
    await install((await sdk('dsh-bash-local')).LocalBashExecutor,{cwd:directory});
    await install(await sdk('dsh-tool-bash'),{enableRunInBackground:false,promoteOnTimeout:false});
    ctx.on('tools/pre-execute',(exec,next)=>exec.name==='bash'
      ? Promise.resolve({kind:'ask',reason:'Isolated Bash approval test'}) : next(),{prepend:true});
    ctx.on('approval/request',(request,next)=>request.toolName==='bash'
      ? Promise.resolve('rejected') : next());
  }
  if (mode==='questions' || usesForeground) {
    await install((await sdk('dsh-user-questions')).UserQuestionService);
    const options = foregroundRelay && mode!=='foreground-relay-timed' ? {} :
      {mode:'timed',timeout:mode==='questions' ? 1 : mode==='foreground-relay-timed' ? 3 : -1};
    await install(await sdk('dsh-tool-ask-user'),options);
  }
  if (usesForeground) {
    ctx.on('user-questions/request',(request,next)=>{
      if (request.agent?.session.id!==session) return next();
      foregroundQuestion.resolve(request);
      return new Promise((resolve,reject)=>{
        const abort = ()=>{
          foregroundProviderCloses++;
          reject(request.signal.reason);
        };
        request.signal?.addEventListener('abort',abort,{once:true});
        foregroundAnswer.promise.then(resolve,reject).finally(()=>request.signal?.removeEventListener('abort',abort));
      });
    });
  }
  if (mode==='formal-goal') await install(await sdk('dsh-tool-goal'));
  await install(await sdk('dsh-tool-executor-native'));
  await install(LlmRuntime);
  ctx.get('llm').registerAdapter(['fixture'],new LocalModel());
  if (mode==='compact') {
    await install((await sdk('dsh-token-meter')).TokenMeter);
    await install((await sdk('dsh-compaction-basic')).BasicCompactionEngine,{auto:false});
  }
  await install((await sdk('dsh-agent-loop')).AgentLoop,{});
  if (mode==='goal-driver' || mode==='formal-goal') await install(await sdk('dsh-goal-round-driver'));
  const agentOptions = {provider:'fixture',model:'local'};
  const registry = ctx.get('agents');
  if (!['cold-resume-reopen','managed-sessions-reopen'].includes(mode)) {
    originalHandle = ['command-approvals-reopen','foreground-relay-reopen'].includes(mode)
      ? await registry.resume({resumeSessionId:session,agentOptions})
      : await registry.create({sessionId:session,agentOptions,...(isManaged ? {meta:{cwd:directory}} : {})});
  }
  const agent = originalHandle?.agent;
  if (isManaged && agent) {
    agent.followup({id:'seed-managed-dsh',role:'user',source:{kind:'user'},
      content:[{type:'text',text:'Original DSH source history'}]});
    await agent.whenIdle();
    assert.equal(await ctx.get('sessions').flush(agent.session),true);
  }
  if (isCold && agent) {
    agent.followup({id:'seed-cold-dsh',role:'user',source:{kind:'user'},
      content:[{type:'text',text:'Seed the isolated historical session'}]});
    await agent.whenIdle();
    if (mode==='cold-resume-goal') ctx.get('goals').create(agent,{objective:'Preserve this isolated active goal'});
    assert.equal(await ctx.get('sessions').flush(agent.session),true);
    await originalHandle.dispose();
  }
  if (mode==='catalog') {
    const titles = ctx.get('sessionTitle');
    titles.rename(agent.session,'已绑定的会话');
    for (let index=0;index<12;index++) {
      const id = 'mowulink-test-catalog-'+index;
      const handle = await ctx.get('agents').create({sessionId:id,agentOptions:{provider:'fixture',model:'local'}});
      titles.rename(handle.agent.session,index===11 ? '仅宿主可见' : '目录中的会话 '+index);
      await ctx.get('sessions').flush(handle.agent.session);
      if (index!==11) catalogSessions.push(id);
      await handle.dispose();
    }
    catalogSessions.push('mowulink-test-catalog-unreadable');
  }
  class Controller extends Service {
    constructor(c) { super(c,'sessionController'); }
    async resolveAgent(target) {
      if (target!==session && !isManaged) return {error:'unknown'};
      controllerResolutions++;
      const live = ctx.get('agents').get(target);
      if (live || mode!=='binding-close' && !isCold && !isManaged) return {agent:live};
      resumedHandle = await ctx.get('agents').resume({resumeSessionId:target,agentOptions});
      return resumedHandle;
    }
  }
  await install(Controller);
  return agent;
}

async function readEvents(agent) {
  assert.equal(await ctx.get('sessions').flush(agent.session),true);
  const reader = await ctx.get('sessionPersistence').open(session,'read');
  try {
    return (await reader.read(0)).events;
  } finally {
    await reader.close();
  }
}

async function stopScenario(agent) {
  const host = await adapter.bindHost(ctx,session,directory);
  const selected = await host.handle({operation:'model-change',session,kind:'efforts',value:'high'});
  assert.equal(selected.changed,true);
  const assignment = {task_id:'test-order',assignment_id:'test-assignment'};
  const work = envelope('deliver','order-delivery',{kind:'task',...assignment});
  await host.handle(work);
  await Promise.race([entered.promise,agent.whenIdle().then(()=>{
    throw new Error("original request did not reach the fixture stream: "+
      JSON.stringify(agent.session.snapshotEvents().filter(event=>event.type==='turn/end').map(event=>event.data)));
  })]);
  const original = (await host.handle({...work,operation:'lookup'})).receipt;
  assert.ok(original?.native_id);
  agent.followup({id:'keep-private',role:'user',content:[{type:'text',text:'retained private message'}],source:{kind:'user'}});
  const control = {...envelope('deliver-control','cancel-delivery',{kind:'task.cancel',...assignment}),
    bindings:[{native_id:original.native_id,turn_id:original.turn_id}]};
  await host.handle(control);
  await agent.whenIdle();
  const proved = (await host.handle({...control,operation:'lookup'})).receipt;
  assert.ok(proved?.native_id);
  assert.notEqual(original.turn_id,proved.turn_id);
  assert.deepEqual((await host.handle(control)).receipt,proved);
  const events = await readEvents(agent);
  const admitted = events.filter(event=>event.type==='user/message' && event.data.source.kind==='user').map(e=>e.data.id);
  assert.deepEqual(admitted,[original.native_id,'keep-private',proved.native_id]);
  const turns = events.filter(e=>e.type==='turn/end').map(e=>[e.data.turn,e.data.reason.kind,e.data.reason.reason?.kind ?? null]);
  assert.deepEqual(turns,[[1,'aborted','user'],[2,'completed',null],[3,'completed',null]]);
  assert.deepEqual(modelRequests.map(r=>r.command),['task','private','task.cancel']);
  assert.ok(modelRequests.every(r=>r.reasoningEffort==='high'));
  console.log(JSON.stringify({session,assignment,original,control:proved,turns,admitted,modelRequests}));
}

async function formalScenario(agent) {
  await install(adapter,{session,directory,catalog_sessions:catalogSessions,
    relay_command_approvals:usesApprovals,resume_on_handoff:isCold,
    read_only_assignments:usesCleanup && mode!=='cleanup-unscoped',
    relay_foreground_questions:foregroundRelay && mode!=='foreground-relay-reopen',
    ...(isManaged ? {managed_sessions_dir:path.join(directory,'enrolled'),
      new_session_selection:{provider:'fixture',model:'local',effort:'low'}} : {})});
  console.log(JSON.stringify({ready:true,session}));
  if (usesApprovals) ctx.on('tools/execute',async (exec,next)=>{
    if (exec.agent===agent && exec.callId==='fixture-bash-dispatch-change') {
      await ctx.get('workingDirectory').set(agent,path.join(directory,'approval-child'),exec.signal);
    }
    return next();
  },{prepend:true});
  if (usesApprovals) ctx.on('tools/execute',async (exec,next)=>{
    if (exec.agent===agent && exec.callId==='fixture-bash-after-pin-change') {
      assert.ok(path.isAbsolute(exec.arguments.workdir));
      await ctx.get('workingDirectory').set(agent,path.join(directory,'approval-child'),exec.signal);
    }
    return next();
  });
  for await (const line of createInterface({input:process.stdin,terminal:false})) {
    if (line==='exit') break;
    if (standalone && line==='hold-question-reply') {
      agent.followup({id:'hold-unrelated-native-turn',role:'user',source:{kind:'user'},
        content:[{type:'text',text:'Hold another isolated turn'}]});
      await entered.promise;
      console.log(JSON.stringify({busy:agent.status==='running'}));
      continue;
    }
    if (usesCleanup && line==='work-started') {
      await entered.promise;
      console.log(JSON.stringify({running:agent.status==='running'}));
      continue;
    }
    if (isManaged) {
      if (line==='status') {
        console.log(JSON.stringify({controllerResolutions,modelRequests,
          liveSessions:ctx.get('agents').list().map(agent=>agent.session.id)}));
      } else {
        assert.ok(line.startsWith('read '));
        const target = line.slice(5);
        const persistence = ctx.get('sessionPersistence');
        if (!await persistence.stat(target)) {
          console.log(JSON.stringify({session:target,exists:false}));
          continue;
        }
        const live = ctx.get('agents').get(target);
        if (live) {
          await live.whenIdle();
          assert.equal(await ctx.get('sessions').flush(live.session),true);
        }
        const reader = await persistence.open(target,'read');
        try {
          const {events} = await reader.read(0);
          const own = events.slice(reader.inheritedEventCount);
          const users = rows=>rows.filter(event=>event.type==='user/message' && event.data.source.kind==='user')
            .map(event=>event.data.id);
          console.log(JSON.stringify({session:reader.header.id,parent:reader.header.parentSession ?? null,
            inherited:reader.inheritedEventCount,allInputs:users(events),ownInputs:users(own),
            results:own.filter(event=>event.type==='tool/result').map(event=>event.data.message),
            selection:own.findLast(event=>event.type==='model/selection')?.data}));
        } finally {
          await reader.close();
        }
      }
      continue;
    }
    if (isCold) {
      if (line==='close') await resumedHandle?.dispose();
      agent = ctx.get('agents').get(session);
      if (line==='status' || line==='close') {
        console.log(JSON.stringify({controllerResolutions,live:!!agent,modelRequests}));
        continue;
      }
      assert.ok(agent,'cold fixture has not been resolved');
    }
    if (line==='approval-state' && usesApprovals) {
      console.log(JSON.stringify(agent.session.snapshotEvents()
        .filter(event=>['user/message','assistant/message','tool/call','tool/result','approval/asked','approval/decided','turn/end'].includes(event.type))
        .map(({type,data})=>({type,data}))));
      continue;
    }
    if (line.startsWith('set-approval-cwd ') && usesApprovals) {
      const cwd = await ctx.get('workingDirectory').set(agent,line.slice('set-approval-cwd '.length));
      console.log(JSON.stringify({cwd}));
      continue;
    }
    if (line==='foreground-question' && usesForeground) {
      const request = await foregroundQuestion.promise;
      const events = await readEvents(agent);
      const starts = events.filter(event=>event.type==='turn/start');
      const turn = starts.at(-1);
      console.log(JSON.stringify({session,status:agent.status,turn_id:turn.seq+'/'+turn.data.turn,
        call_id:request.wait?.callId ?? events.findLast(event=>event.type==='tool/call')?.data.callId,
        continued:ctx.get('userQuestions').continued(agent).length,
        timed:request.wait?.timed===true,question_ids:request.questions.map(item=>item.id)}));
      continue;
    }
    if (mode==='binding-close') {
      if (line==='close') await originalHandle.dispose();
      if (line==='reopen') {
        const {agent:reopened} = await ctx.get('sessionController').resolveAgent(session);
        assert.notEqual(reopened,agent);
      }
      const live = ctx.get('agents').get(session);
      console.log(JSON.stringify({controllerResolutions,live:!!live,sameAgent:live===agent,
        inputs:live?.session.snapshotEvents().filter(event=>event.type==='user/message').length ?? 0}));
      continue;
    }
    if (line==='first-round' && mode==='goal-driver') {
      await entered.promise;
      console.log(JSON.stringify({running:agent.status==='running'}));
      continue;
    }
    if (line==='first-compact' && mode==='compact') {
      await compactEntered.promise;
      console.log(JSON.stringify({compacting:true,status:agent.status}));
      continue;
    }
    if (line.startsWith('wait-private ') && ['private-stop','admission-race'].includes(mode)) {
      await privateStops.get(line.split(' ').at(-1)).promise;
      console.log(JSON.stringify({running:agent.status==='running',inbox:agent.inbox.nextTurn.map(message=>message.id)}));
      continue;
    }
    if (line==='finish-compact' && mode==='compact') compactReleased.resolve();
    else if (line==='wake-private-queue' && mode==='private-stop') {
      agent.followup({id:'fixture-next-private',role:'user',source:{kind:'user'},
        content:[{type:'text',text:'Continue the private conversation'}]});
    }
    else if (line==='end-race-private' && mode==='admission-race') raceRelease.resolve();
    else if (line==='answer-native' && mode==='questions') {
      assert.equal(ctx.get('userQuestions').answer(agent,'native-question-native',
        {answers:[{id:'primary',selected:['A']}]}),true);
    }
    else if (line==='answer-foreground' && usesForeground) {
      foregroundAnswer.resolve({answers:[{id:'primary',selected:['B']}]});
    }
    else if (line==='cancel-foreground' && foregroundRelay) agent.cancel({kind:'user'},{keepInbox:true});
    else if (line==='cancel-approval' && usesApprovals) agent.cancel({kind:'user'},{keepInbox:true});
    else if (line==='stop-unrelated-turn' && standalone) agent.cancel({kind:'user'},{keepInbox:true});
    else if (line.startsWith('direct-trainer ') && standalone) {
      agent.followup({id:'standalone-direct-origin',role:'user',source:{kind:'user'},
        content:[{type:'text',text:'Standalone trainer fixture\n'+line.slice('direct-trainer '.length)}]});
    }
    else if (line==='formal-goal-round' && mode==='formal-goal') await formalRound.promise;
    else assert.ok(line==='read' || line==='second-round' && mode==='goal-driver');
    if (line==='second-round') await secondRound.promise;
    await agent.whenIdle();
    const events = await readEvents(agent);
    const inputs = events.filter(e=>e.type==='user/message' && e.data.source.kind==='user').map(e=>e.data.id);
    const calls = events.filter(e=>e.type==='tool/call').map(e=>e.data);
    const results = events.filter(e=>e.type==='tool/result').map(e=>e.data.message);
    const turns = events.filter(e=>e.type==='turn/end').map(e=>e.data);
    const rounds = events.filter(e=>e.type==='user/message' && e.data.source.kind==='goal')
      .map(e=>({id:e.data.source.goalId,revision:e.data.source.revision,round:e.data.source.round}));
    const compactEvents = events.filter(e=>e.type.startsWith('compaction/') ||
      e.type==='user/message' && e.data.source.kind==='compact-checkpoint')
      .map(e=>({kind:e.type,seq:e.seq,compactionId:e.data.compactionId ?? e.data.source?.compactionId,
        sourceCommandId:e.data.sourceCommandId ?? e.data.source?.sourceCommandId}));
    const replies = events.filter(e=>e.type==='user/message' && e.data.source.kind==='user-question-reply')
      .map(e=>({id:e.data.id,call:e.data.source.callId,seq:e.seq,body:JSON.parse(e.data.content[0].text)}));
    const continued = mode==='questions' || usesForeground ? ctx.get('userQuestions').continued(agent).map(q=>q.callId) : [];
    const approvals = usesApprovals ? events.filter(event=>['approval/asked','approval/decided'].includes(event.type))
      .map(({type,seq,data})=>({type,seq,data})) : [];
    console.log(JSON.stringify({session,inputs,calls,results,turns,modelRequests,rounds,compactEvents,replies,continued,
      mediaRequests,approvals,foregroundProviderCloses,...(standalone ? {standaloneAnswers} : {}),
      inbox:mode==='private-stop' ? agent.inbox.nextTurn.map(message=>message.id) : [],
      liveSessions:mode==='catalog' ? ctx.get('agents').list().map(agent=>agent.session.id) : []}));
  }
}

try {
  const agent = await setup();
  if (['formal','formal-artifacts','compact','questions','foreground-question','catalog','private-stop','work-question','admission-race',
       'binding-close','material','peer-source'].includes(mode) || isCold || isManaged || usesGoals || usesMedia
       || usesApprovals || usesCleanup || foregroundRelay || standalone) {
    await formalScenario(agent);
  }
  else await stopScenario(agent);
} finally {
  for (const handle of plugins.reverse()) await handle.dispose();
}
