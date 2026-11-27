// Called by the offline operator command while it holds the source ledger locks.
import {assignmentContainers} from './dsh_host/assignment-container.mjs';

try {
  let input = '';
  for await (const part of process.stdin.setEncoding('utf8')) {
    input += part;
    if (input.length > 16384) throw new Error('invalid_workspace_checkpoint');
  }
  const {runtime,session,task_id,assignment_id,engine} = JSON.parse(input);
  const {archive,...receipt} = await assignmentContainers(runtime,session,engine).checkpoint(task_id,assignment_id);
  process.stdout.write(JSON.stringify(receipt)+'\n');
  process.stdout.write(archive);
} catch (error) {
  const allowed = ['assignment_workspace_unavailable','assignment_workspace_has_live_processes',
    'assignment_workspace_restarted','workspace_project_checkpoint_failed','native_cleanup_unconfirmed'];
  process.stderr.write((allowed.includes(error.message) ? error.message : 'workspace_checkpoint_unconfirmed')+'\n');
  process.exitCode = 1;
}
