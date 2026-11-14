import test from 'node:test';
import assert from 'node:assert/strict';
import { mkdtemp, readFile, rm } from 'node:fs/promises';
import os from 'node:os';
import path from 'node:path';
import { DesktopManager, PROTOCOL } from '../desktop-manager.mjs';
import { routerMessageId } from '../router-tasks.mjs';

async function fixture(t) {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), 'router-control-'));
  t.after(() => rm(stateDir, { recursive: true, force: true }));
  const calls = [];
  const manager = new DesktopManager({ stateDir, listNativeTools: async () => [],
    native: async (name, args) => { calls.push({ name, args }); return { ok: true, state: 'cancelled', executionStopped: true, pendingCommands: 0 }; } });
  const taskId = 't-exact', messageId = routerMessageId(taskId), threadId = 'original-thread';
  const request = { source: 'hermes', origin: 'router', messageId, targetThreadId: threadId,
    delivery: 'queue', text: '[router t-exact] actual original task' };
  manager.state.sessions[threadId] = { threadId, queue: [], steerQueue: [], turnMessages: {} };
  manager.state.receipts[messageId] = { request, nativeTargetThreadId: threadId,
    nativeDispatch: { status: 'accepted' } };
  manager.tools = [{ name: 'cancel_task' }];
  return { manager, taskId, messageId, threadId, request, calls };
}

test('queued cancellation removes only the original task and persists its terminal receipt', async t => {
  const f = await fixture(t), session = f.manager.state.sessions[f.threadId];
  session.queue.push(f.request, { messageId: 'other-request' });
  const result = await f.manager.cancelRouterTask(f.taskId);
  assert.equal(result.state, 'cancelled'); assert.equal(f.calls.length, 0);
  assert.deepEqual(session.queue, [{ messageId: 'other-request' }]);
  const saved = JSON.parse(await readFile(f.manager.statePath, 'utf8'));
  assert.equal(saved.receipts[f.messageId].nativeDispatch.status, 'canceled');
  assert.equal((await f.manager.cancelRouterTask(f.taskId)).state, 'cancelled');
  assert.equal(f.calls.length, 0);
});

test('pre-dispatch cancellation survives reopening and prevents a late task from executing', async t => {
  const f = await fixture(t);
  delete f.manager.state.receipts[f.messageId];
  assert.equal((await f.manager.cancelRouterTask(f.taskId, { notDispatched: true })).state, 'cancelled');
  const reopened = new DesktopManager({ stateDir: f.manager.stateDir, native: () => assert.fail('cancelled tasks must never execute') });
  reopened.state = JSON.parse(await readFile(f.manager.statePath, 'utf8'));
  const result = await reopened.message({ ...f.request, protocolVersion: PROTOCOL });
  assert.equal(result.action, 'task_cancelled');
  assert.equal((await reopened.cancelRouterTask(f.taskId)).state, 'cancelled');
  assert.equal(reopened.state.receipts[f.messageId], undefined);
});

test('a complete native task marker binds cancellation to its actual turn', async t => {
  const f = await fixture(t);
  await f.manager.confirmRouterTaskTurn(f.taskId, f.threadId, { id: 'real-turn',
    items: [{ type: 'userMessage', text: f.request.text }] });
  assert.equal(f.manager.state.receipts[f.messageId].nativeTurnId, 'real-turn');
  assert.equal(f.manager.state.sessions[f.threadId].turnInitiators['real-turn'].taskId, f.taskId);
  assert.equal((await f.manager.cancelRouterTask(f.taskId)).state, 'cancelled');
  assert.deepEqual(f.calls, [{ name: 'cancel_task', args: { taskId: f.taskId,
    messageId: f.messageId, threadId: f.threadId, turnId: 'real-turn' } }]);
});

test('unavailable native cancellation remains explicit and cannot stop a different turn', async t => {
  const f = await fixture(t);
  const result = await f.manager.cancelRouterTask(f.taskId);
  assert.equal(result.ok, false); assert.equal(result.state, 'cancelling');
  assert.equal(f.calls.length, 0);
  await f.manager.confirmRouterTaskTurn(f.taskId, f.threadId, { id: 'unrelated-turn',
    items: [{ type: 'userMessage', text: 'another message' }] });
  assert.equal(f.manager.state.receipts[f.messageId].nativeTurnId, undefined);
});

test('legacy active-turn cancellation is revalidated while confirmed cleanup stays idempotent', async t => {
  const f = await fixture(t);
  f.manager.state.receipts[f.messageId].nativeTurnId = 'real-turn';
  f.manager.state.routerCancellations = { [f.taskId]: { ok: true, state: 'cancelled', executionStopped: true, turn_id: 'real-turn' } };
  assert.equal((await f.manager.cancelRouterTask(f.taskId)).executionStopped, true);
  assert.equal(f.calls.length, 1);
  assert.equal((await f.manager.cancelRouterTask(f.taskId)).executionStopped, true);
  assert.equal(f.calls.length, 1);
});
