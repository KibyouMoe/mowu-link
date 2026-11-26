// Private stdio bridge to the same container and journal implementation used by DSH.
import readline from 'node:readline';
import {assignmentContainers} from './dsh_host/assignment-container.mjs';

const runtime = process.argv[2];
const sessions = new Map();
const operations = new Set();
let config;

async function dispatch(method,params) {
  const {session,task_id:task,assignment_id:assignment} = params;
  if (typeof session !== 'string' || !/^[A-Za-z0-9_.:-]{1,200}$/.test(session)) {
    throw new Error('invalid_workspace_session');
  }
  if (method === 'open' && config === undefined) config = params.config;
  if (config === undefined) throw new Error('assignment_workspace_unavailable');
  if (!sessions.has(session)) sessions.set(session,assignmentContainers(runtime,session,config));
  const workspace = sessions.get(session);
  if (method === 'open') return {opened:true};
  if (method === 'run') return workspace.run(task,assignment,params.command);
  if (method === 'pending') return workspace.pending();
  if (method === 'seal') return workspace.seal(task,assignment);
  if (method === 'artifact') {
    const {bytes,...meta} = await workspace.artifact(task,assignment,params.path);
    return {...meta,data:bytes.toString('base64')};
  }
  throw new Error('invalid_workspace_operation');
}

for await (const line of readline.createInterface({input:process.stdin,terminal:false})) {
  const {id,method,params} = JSON.parse(line);
  const operation = dispatch(method,params).then(value=>({value}),error=>({
    error:/^[a-z][a-z_]{1,80}$/.test(error.message) ? error.message : 'assignment_workspace_unconfirmed',
  })).then(result=>process.stdout.write(JSON.stringify({id,result})+'\n'));
  operations.add(operation);
  operation.finally(()=>operations.delete(operation));
}
await Promise.allSettled(operations);
