// Real DSH AgentLoop and persistence in an isolated directory. The only model provider is an in-process test stream.
import assert from 'node:assert/strict';
import {createHash} from 'node:crypto';
import {createRequire} from 'node:module';
import path from 'node:path';
import {pathToFileURL} from 'node:url';
import {bindHost} from './index.mjs';

const [root,directory,session] = process.argv.slice(2);
const dependency = createRequire(path.join(root,'probe.cjs'));
const sdk = name=>import(pathToFileURL(dependency.resolve('@deepseek-ai/'+name)));
const {Context,Service} = await sdk('cordis');
const {LlmAdapter,LlmRuntime} = await sdk('dsh-llm');
const entered = Promise.withResolvers();
const modelRequests = [];

class LocalModel extends LlmAdapter {
  async listModels(provider) {
    return [await this.resolveModel(provider,'local')];
  }
  async resolveModel(provider, model) {
    return {provider,id:model,name:'Local test model',reasoning:{
      efforts:[{id:'low',name:'Low'},{id:'high',name:'High'}],defaultEffort:'low'}};
  }
  async *stream({provider,model,reasoningEffort,messages,signal}) {
    const text = messages.findLast(message=>message.role==='user').content.find(block=>block.type==='text').text;
    const command = text.startsWith('魔物链接投递\n') ? JSON.parse(text.split('\n')[1]).kind : 'private';
    modelRequests.push({command,provider,model,reasoningEffort});
    if (command==='task') {
      const stop = Promise.withResolvers();
      signal.addEventListener('abort',()=>stop.resolve(),{once:true});
      entered.resolve();
      if (!signal.aborted) await stop.promise;
      signal.throwIfAborted();
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

try {
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
  const host = await bindHost(ctx,session,directory);
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
  const reader = await ctx.get('sessionPersistence').open(session,'read');
  let events;
  try {
    ({events} = await reader.read(0));
  } finally {
    await reader.close();
  }
  const admitted = events.filter(event=>event.type==='user/message' && event.data.source.kind==='user').map(e=>e.data.id);
  assert.deepEqual(admitted,[original.native_id,'keep-private',proved.native_id]);
  const turns = events.filter(e=>e.type==='turn/end').map(e=>[e.data.turn,e.data.reason.kind,e.data.reason.reason?.kind ?? null]);
  assert.deepEqual(turns,[[1,'aborted','user'],[2,'completed',null],[3,'completed',null]]);
  assert.deepEqual(modelRequests.map(r=>r.command),['task','private','task.cancel']);
  assert.ok(modelRequests.every(r=>r.reasoningEffort==='high'));
  console.log(JSON.stringify({session,assignment,original,control:proved,turns,admitted,modelRequests}));
} finally {
  for (const handle of plugins.reverse()) await handle.dispose();
}
