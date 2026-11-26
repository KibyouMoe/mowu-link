// Isolated SDK driver. The scripted agent supplies messages; no provider or account is loaded.
import {createRequire} from 'node:module';
import {createHash} from 'node:crypto';
import {pathToFileURL} from 'node:url';
import {createInterface} from 'node:readline';
import path from 'node:path';
import * as adapter from './index.mjs';

const [root, directory, id] = process.argv.slice(2);
const req = createRequire(path.join(root,'probe.cjs'));
const sdk = name => import(pathToFileURL(req.resolve('@deepseek-ai/'+name)));
const {Context, Service} = await sdk('cordis');
const {createScope} = await sdk('dsh-scope');
const {AgentModel,modelSelectionProjectionDefinition:selectionProjection} = await sdk('dsh-agent');
const {executeToolBatch, ToolExecutionPipeline} = await sdk('dsh-tools/pipeline');
const ctx = new Context();
const fibers = [];
async function install(plugin,options) {
  const fiber = ctx.plugin(plugin,options);
  fibers.push(fiber);
  await fiber.await();
}
await install((await sdk('dsh-session')).SessionStore);
await install(await sdk('dsh-session-persistence-jsonl'),{root:path.join(directory,'sessions')});
await install((await sdk('dsh-system-prompt')).SystemPrompt);
await install((await sdk('dsh-tools')).ToolsService);
await install((await sdk('dsh-attachment-local')).default,{dshHome:directory});
const header = {version:4,id,createdAt:Date.now(),isSeeded:false,delegationDepth:0};
const writer = await ctx.get('sessionPersistence').create(header);
const session = ctx.get('sessions').create(id,{meta:header});
let number = 0;
let step = 1;
let received = 0;
const stops = [];
let loseAdmission = false;
let lastMessage;
const model = new AgentModel(
  ()=>session.snapshotEvents().reduce(selectionProjection.apply,selectionProjection.init()),
  session,{provider:'fixture',model:'fixture'});
function endTurn(reason) {
  session.append('step/end',{turn:number,step});
  session.append('turn/end',{turn:number,reason});
  agent.status = 'idle';
}
const agent = {session,status:'idle',model,cancel(cause,options) {
  stops.push({turn:number,cause,options});
  endTurn({kind:'aborted',reason:cause});
},followup(message) {
  lastMessage = message;
  received++;
  if (agent.status === 'idle') {
    agent.status = 'running';
    step = 1;
    session.append('turn/start',{turn:++number});
    session.append('step/start',{turn:number,step});
  }
  session.append('user/message',message,{surfaceOp:'append'});
  if (loseAdmission) {
    loseAdmission = false;
    throw new Error('fixture_ack_lost');
  }
}};
const scope = createScope(ctx,agent);
agent.ctx = scope.ctx;
class Controller extends Service {
  constructor(c) { super(c,'sessionController'); }
  resolveAgent(target) { return target === id ? {agent} : {error:'unknown'}; }
}
await install(Controller);
class LiveAgents extends Service {
  constructor(c) { super(c,'agents'); }
  get(target) { return target === id ? agent : undefined; }
}
await install(LiveAgents);
class Routes extends Service {
  constructor(c) { super(c,'llm'); }
  listProviders() { return [{id:'fixture'},{id:'unavailable'}]; }
  listModels(provider) {
    if (provider==='unavailable') throw new Error('private provider details');
    return ['fixture','text-only',...Array.from({length:10},(_,i)=>'route-'+(i+1))]
      .map(id=>({id,name:id}));
  }
  resolveModelInfo(provider, id) {
    if (provider!=='fixture') throw new Error('unknown_provider');
    return {inputModalities:id==='text-only' ? ['text'] : ['text','image'],
      reasoning:['fixture','route-1'].includes(id)
        ? {efforts:[{id:'low',name:'低'},{id:'high',name:'高'}],defaultEffort:'low'} : undefined};
  }
  async resolveCallConfig(choice) {
    if (!this.listModels(choice.provider).some(model=>model.id===choice.model)) throw new Error('unavailable_model');
    const info = this.resolveModelInfo(choice.provider,choice.model);
    if (choice.reasoningEffort && !info.reasoning?.efforts.some(item=>item.id===choice.reasoningEffort)) {
      throw new Error('unsupported_effort');
    }
    return {...choice,reasoningEffort:choice.reasoningEffort ?? info.reasoning?.defaultEffort};
  }
}
await install(Routes);
await install(adapter,{session:id,directory});
const registry = ctx.get('tools');
const names = [...registry.view(agent).visible.keys()];
const versions = Object.fromEntries(['dsh-session','dsh-tools','dsh-session-persistence-jsonl']
  .map(name=>[name,req('@deepseek-ai/'+name+'/package.json').version]));
console.log(JSON.stringify({ready:true,versions,names,globalNames:[...registry.view().visible.keys()]}));
const pipeline = new ToolExecutionPipeline(ctx);
try {
  for await (const line of createInterface({input:process.stdin,terminal:false})) {
    try {
      const cmd = JSON.parse(line);
      if (cmd.action === 'exit') break;
      if (cmd.action === 'lose-admission') {
        loseAdmission = true;
      } else if (cmd.action === 'end') {
        endTurn(cmd.aborted ? {kind:'aborted',reason:{kind:'user'}} : {kind:'completed'});
      } else if (cmd.action === 'stop-attempts') {
        console.log(JSON.stringify({stops}));
        continue;
      } else if (cmd.action === 'tool') {
        const block = {type:'tool-call',id:cmd.call_id,name:cmd.name,arguments:JSON.stringify(cmd.args)};
        if (cmd.append !== false) {
          const message = {id:'assistant-'+cmd.call_id,role:'assistant',
            source:{kind:'model',provider:'fixture',model:'fixture'},content:[block]};
          session.append('assistant/message',{turn:number,step:1,message},{surfaceOp:'append'});
        }
        const result = await executeToolBatch({agent,calls:[block],signal:new AbortController().signal},pipeline,()=>1);
        console.log(JSON.stringify({received,result:result.results[0].result}));
        continue;
      } else if (cmd.action === 'output') {
        const message = {id:cmd.id,role:'assistant',source:{kind:'model',provider:'fixture',model:cmd.model ?? 'fixture'},
          content:cmd.content};
        session.append('assistant/message',{turn:number,step,message,...(cmd.usage ? {usage:cmd.usage} : {})},{surfaceOp:'append'});
      } else if (cmd.action === 'context') {
        session.append('request/context',{provider:'fixture',model:cmd.model,contextWindow:cmd.window});
      } else if (cmd.action === 'observed-tool') {
        session.append('tool/call',{turn:number,step,callId:cmd.id,name:cmd.name,arguments:JSON.stringify(cmd.args)});
        const message = {id:'result-'+cmd.id,role:'tool',source:{kind:'tool',callId:cmd.id},toolCallId:cmd.id,
          content:[{type:'text',text:'fixture tool result'}],isError:false};
        session.append('tool/result',{turn:number,step,message},{surfaceOp:'append'});
        session.append('step/end',{turn:number,step});
        session.append('step/start',{turn:number,step:++step});
      } else if (cmd.action === 'select-text-only') {
        model.select({provider:'fixture',model:'text-only'});
      } else if (cmd.action === 'select-model') {
        model.select({provider:'fixture',model:cmd.model,...(cmd.effort ? {reasoningEffort:cmd.effort} : {})});
      } else if (cmd.action === 'request-header') {
        session.append('request/header',{header:{config:{provider:'fixture',model:cmd.model,reasoningEffort:cmd.effort}},
          reason:'initial'});
      } else if (cmd.action === 'model-records') {
        await ctx.get('sessions').flush(session);
        const handle = await ctx.get('sessionPersistence').open(id,'read');
        const {events} = await handle.read(0);
        await handle.close();
        const records = events.filter(event=>['model/selection','request/header'].includes(event.type));
        console.log(JSON.stringify({selected:model.current(),records:records.map(event=>event.type)}));
        continue;
      } else if (cmd.action === 'inspect-media') {
        const content = [];
        const attachments = ctx.get('attachments');
        for (const block of lastMessage.content) {
          if (block.type === 'text') {
            content.push(block);
            continue;
          }
          const hash = createHash('sha256');
          if (block.type === 'image') {
            hash.update((await attachments.readImage(block.attachment)).data);
          } else {
            for await (const chunk of attachments.readFileStream(block.attachment)) hash.update(chunk);
          }
          content.push({type:block.type,ref:block.attachment,sha256:hash.digest('hex')});
        }
        console.log(JSON.stringify({id:lastMessage.id,content}));
        continue;
      } else if (cmd.action === 'manual') {
        agent.followup({role:'user',id:'manual',content:[{type:'text',text:'manual follow-up'}],source:{kind:'user'}});
      } else if (cmd.action !== 'count') {
        throw new Error('unknown test action');
      }
      console.log(JSON.stringify({received}));
    } catch (e) {
      console.log(JSON.stringify({error:String(e)}));
    }
  }
} finally {
  await fibers.pop().dispose();
  await writer.close();
  await scope.dispose();
  for (const fiber of fibers.reverse()) await fiber.dispose();
}
