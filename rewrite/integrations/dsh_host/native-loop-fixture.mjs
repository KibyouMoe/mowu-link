// Real DSH AgentLoop and persistence in an isolated directory. The only model provider is an in-process test stream.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
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
const modelRequests = [];

class LocalModel extends LlmAdapter {
  submitted = new Map();
  async listModels(provider) {
    return [await this.resolveModel(provider,'local')];
  }
  async resolveModel(provider, model) {
    return {provider,id:model,name:'Local test model',reasoning:{
      efforts:[{id:'low',name:'Low'},{id:'high',name:'High'}],defaultEffort:'low'}};
  }
  async *stream({provider,model,reasoningEffort,messages,signal,tools}) {
    const text = messages.findLast(message=>message.role==='user').content.find(block=>block.type==='text').text;
    const payload = text.startsWith('魔物链接投递\n') ? JSON.parse(text.split('\n')[1]) : null;
    const command = payload?.kind ?? 'private';
    modelRequests.push({command,provider,model,reasoningEffort});
    if (command==='task') {
      if (mode==='formal') {
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
      } else {
        const stop = Promise.withResolvers();
        signal.addEventListener('abort',()=>stop.resolve(),{once:true});
        entered.resolve();
        if (!signal.aborted) await stop.promise;
        signal.throwIfAborted();
      }
    }
    yield {type:'block-start',index:0,blockType:'text'};
    yield {type:'text-delta',index:0,text:'isolated fixture response'};
    yield {type:'finish',reason:{kind:'stop'}};
  }
}

const ctx = new Context();
const plugins = [];
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
  await install(await sdk('dsh-session-persistence-jsonl'),{root:path.join(directory,'sessions')});
  await install((await sdk('dsh-system-prompt')).SystemPrompt);
  await install((await sdk('dsh-tools')).ToolsService);
  await install(await sdk('dsh-tool-executor-native'));
  await install(LlmRuntime);
  ctx.get('llm').registerAdapter(['fixture'],new LocalModel());
  await install((await sdk('dsh-agent-loop')).AgentLoop,{});
  const {agent} = await ctx.get('agents').create({
    sessionId:session,agentOptions:{provider:'fixture',model:'local'}});
  class Controller extends Service {
    constructor(c) { super(c,'sessionController'); }
    resolveAgent(target) { return target===session ? {agent} : {error:'unknown'}; }
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
  await install(adapter,{session,directory});
  console.log(JSON.stringify({ready:true,session}));
  for await (const line of createInterface({input:process.stdin,terminal:false})) {
    if (line==='exit') break;
    assert.equal(line,'read');
    await agent.whenIdle();
    const events = await readEvents(agent);
    const inputs = events.filter(e=>e.type==='user/message' && e.data.source.kind==='user').map(e=>e.data.id);
    const calls = events.filter(e=>e.type==='tool/call').map(e=>e.data);
    const results = events.filter(e=>e.type==='tool/result').map(e=>e.data.message);
    const turns = events.filter(e=>e.type==='turn/end').map(e=>e.data);
    console.log(JSON.stringify({session,inputs,calls,results,turns,modelRequests}));
  }
}

try {
  const agent = await setup();
  if (mode==='formal') await formalScenario(agent);
  else await stopScenario(agent);
} finally {
  for (const handle of plugins.reverse()) await handle.dispose();
}
