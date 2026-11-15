import test from 'node:test';
import assert from 'node:assert/strict';
import { InProcessTransport } from '../interface/inprocess.mjs';

function fixture(t) {
  const listeners = new Map(), records = [], activities = [], idleWaiters = [];
  const session = { id: 'native-main', seq: 0, eventAt: i => records[i] };
  const agent = { id: session.id, session, status: 'idle',
    whenIdle: () => agent.status === 'idle' ? Promise.resolve() : new Promise(resolve => idleWaiters.push(resolve)),
    steer: message => emit('user/message', message) };
  const registry = { list: () => [agent] };
  const ctx = { get: name => name === 'agents' ? registry : null,
    on(name, fn) { const list = listeners.get(name) ?? new Set(); list.add(fn); listeners.set(name, list); return () => list.delete(fn); } };
  const transport = new InProcessTransport({ ctx, selection: {}, cwd: '/tmp', stateDir: '/tmp/dsh-desktop-observer-tests-absent', log() {} });
  transport.save = async () => {};
  const thread = { id: 'worker-main', sessionId: session.id, turns: [], queue: [], running: false };
  transport.threads.set(thread.id, thread);
  transport.live.set(thread.id, { agent, handle: { dispose: async () => {} } });
  transport.publicEvents.attach({ observeToolActivity: e => activities.push(e) });
  t.after(() => transport.close());
  function publish(name, ...args) { for (const fn of listeners.get(name) ?? []) fn(...args); }
  function emit(type, data, subject = session) {
    const event = { type, data, seq: subject.seq++ }; records.push(event); publish('session/event', subject, event); return event;
  }
  function start() { agent.status = 'running'; emit('turn/start', { turn: 1 }); }
  function user(text, id = 'desktop-input', source = { kind: 'user' }) { emit('user/message', { id, source, content: [{ type: 'text', text }] }); }
  function stream(text, attemptId = 'a') { publish('agent/assistant-stream', { agent, frame: { type: 'chunk', attemptId, chunk: { type: 'text-delta', text } } }); }
  async function end(reason = { kind: 'completed' }) {
    emit('turn/end', { reason }); agent.status = 'idle'; for (const resolve of idleWaiters.splice(0)) resolve();
    await new Promise(resolve => setImmediate(resolve));
  }
  const snapshot = () => transport.readThread({ threadId: thread.id });
  return { transport, thread, agent, session, activities, publish, emit, start, user, stream, end, snapshot, listeners };
}

test('Desktop-only turn projects input, every public paragraph, tool lifecycle and final status', async t => {
  const f = fixture(t); f.start(); f.user('desktop hello'); f.stream('first', 'a'); f.stream('second', 'b');
  f.emit('tool/call', { callId: 'c', name: 'bash', arguments: '{"command":"echo ok"}' });
  f.emit('tool/result', { message: { toolCallId: 'c', content: [{ type: 'text', text: 'private tool output' }] } });
  assert.equal(f.snapshot().turns[0].items[0].text, 'desktop hello');
  assert.deepEqual(f.snapshot().turns[0].items.filter(i => i.type === 'agentMessage').map(i => i.text), ['first', 'second']);
  assert.deepEqual(f.activities.map(a => a.status), ['running', 'completed']);
  await f.end(); assert.equal(f.snapshot().turns[0].status, 'completed');
  assert.equal(f.activities.at(-1).kind, 'turn_end'); assert.equal(f.thread.running, false);
  assert.equal(f.thread.sessionId, 'native-main');
});

test('Worker echo uses exact message identity; identical Desktop text still mirrors once', async t => {
  const f = fixture(t);
  f.thread.turns.push({ id: 'worker-turn', prompt: 'same', status: 'inProgress' }); f.thread.running = true;
  f.transport.desktopObserver.sent({ id: 'worker-input' });
  f.start(); f.user('same', 'worker-input'); f.user('same', 'desktop-input'); f.stream('not double projected');
  assert.equal(f.thread.turns.length, 1); assert.deepEqual(f.thread.turns[0].inputs.map(x => x.text), ['same']);
  assert.equal(f.thread.turns[0].publicMessages, undefined);
});

test('Worker steering a native Desktop turn has one input and preserves native turn ownership', async t => {
  const f = fixture(t); f.start(); f.user('desktop start');
  const result = f.transport.sendMessage({ threadId: f.thread.id, prompt: 'steer from Feishu' });
  assert.equal(result.turnId, f.thread.turns[0].id);
  assert.deepEqual(f.thread.turns[0].inputs.map(i => i.text), ['steer from Feishu']);
  await f.end(); assert.equal(f.thread.turns.length, 1);
});

test('foreign sessions, runtime context, reasoning and raw tool output are not mirrored', async t => {
  const f = fixture(t);
  f.emit('turn/start', {}, { id: 'foreign', seq: 0 }); assert.equal(f.thread.turns.length, 0);
  f.start(); f.user('private context', 'context', { kind: 'runtime-context' });
  f.publish('agent/assistant-stream', { agent: f.agent, frame: { type: 'chunk', chunk: { type: 'reasoning-delta', text: 'private reasoning' } } });
  assert.doesNotMatch(JSON.stringify(f.snapshot()), /private/); await f.end();
});

test('baseline and event sequence prevent history replay and duplicate native inputs', async t => {
  const f = fixture(t); f.session.seq = 10; f.transport.desktopObserver.baseline(f.agent);
  f.publish('session/event', f.session, { seq: 9, type: 'turn/start' }); assert.equal(f.thread.turns.length, 0);
  f.start(); const event = f.emit('user/message', { id: 'm', source: { kind: 'user' }, content: [{ type: 'text', text: 'once' }] });
  f.publish('session/event', f.session, event); assert.equal(f.thread.turns[0].inputs, undefined); await f.end();
});

test('native error is terminal and next Desktop turn gets its own stable identity', async t => {
  const f = fixture(t); f.start(); f.user('fail'); await f.end({ kind: 'error', error: { message: 'provider failed' } });
  assert.equal(f.thread.turns[0].status, 'failed'); assert.equal(f.thread.turns[0].error, 'provider failed');
  f.start(); f.user('retry'); await f.end(); assert.equal(f.thread.turns.length, 2);
  assert.notEqual(f.thread.turns[0].id, f.thread.turns[1].id); assert.equal(f.thread.turns[1].status, 'completed');
});

test('Desktop-native clarification uses the existing question bridge and native answer projection', async t => {
  const f = fixture(t); f.start(); f.user('question test');
  const request = { agent: f.agent, wait: { callId: 'q' }, signal: new AbortController().signal,
    questions: [{ id: 'q1', question: 'Continue?', options: [{ label: 'Yes' }, { label: 'No' }] }] };
  await f.transport.questions.request(request, async () => ({ answers: [{ id: 'q1', selected: ['Yes'] }] }));
  assert.equal(f.thread.turns[0].nativeQuestions[0].delivery, 'async');
  assert.match(f.thread.turns[0].inputs[0].text, /Yes/); await f.end();
});

test('closing removes permanent listeners and does not replay on next load', async t => {
  const f = fixture(t); f.start(); f.user('one'); await f.end(); await f.transport.close();
  assert.equal(f.listeners.get('session/event').size, 0); assert.equal(f.listeners.get('agent/assistant-stream').size, 0);
});

test('continued Desktop answers settle the exact observed question through native reply projection', async t => {
  const f = fixture(t);
  f.thread.turns.push({ id: 'old', status: 'completed', nativeQuestions: [{ type: 'agentMessage', delivery: 'async',
    id: 'q', phase: 'commentary', questions: [{ title: 'Continue?', options: ['Yes', 'No'] }],
    dshQuestion: { sessionId: f.session.id, callId: 'continued-q', ids: ['q1'] } }] });
  f.start();
  f.emit('user/message', { id: 'answer', source: { kind: 'user-question-reply', callId: 'continued-q' }, content: [{ type: 'text',
    text: JSON.stringify({ kind: 'answer_to_pending_question', callId: 'continued-q', answers: [{ id: 'q1', selected: ['Yes'] }] }) }] });
  assert.match(f.thread.turns[1].prompt, /dsh_user_question_reply/);
  assert.equal(f.snapshot().turns[1].items[0].dshQuestionAnswers[0].answer, 'Yes');
  await f.end();
});

test('a Desktop-resumed agent can be observed without creating a new agent or taking ownership', async t => {
  const f = fixture(t); f.transport.live.clear(); f.start(); f.user('resumed'); await f.end();
  assert.equal(f.transport.live.get(f.thread.id).agent, f.agent);
  assert.equal(f.thread.sessionId, 'native-main'); assert.equal(f.thread.turns.length, 1);
});

test('native input images are represented without exposing binary image content', async t => {
  const f = fixture(t); f.start();
  f.emit('user/message', { id: 'image', source: { kind: 'user' }, content: [{ type: 'image', data: 'private-binary' }] });
  assert.match(f.thread.turns[0].prompt, /图片附件/); assert.doesNotMatch(JSON.stringify(f.snapshot()), /private-binary/);
  await f.end();
});

test('native Desktop events reach the real shared manager outbox once without redispatch', async t => {
  const { DesktopManager, PROTOCOL } = await import('../desktop-manager.mjs');
  const { registerNativeTransport, resetNativeTransportCache } = await import('../interface/transport/index.mjs');
  const previous = process.env.CODEXWORKER_NATIVE_TRANSPORT;
  registerNativeTransport('dsh-observer-test', { capabilities: { fork: false, desktopJournal: false, callerTurn: false } });
  process.env.CODEXWORKER_NATIVE_TRANSPORT = 'dsh-observer-test'; resetNativeTransportCache();
  t.after(() => { if (previous == null) delete process.env.CODEXWORKER_NATIVE_TRANSPORT; else process.env.CODEXWORKER_NATIVE_TRANSPORT = previous; resetNativeTransportCache(); });
  const { mkdtemp, rm } = await import('node:fs/promises');
  const { tmpdir } = await import('node:os');
  const { join } = await import('node:path');
  const f = fixture(t), root = await mkdtemp(join(tmpdir(), 'dsh-observer-manager-'));
  f.thread.cwd = root; const calls = [], sent = [];
  const manager = new DesktopManager({ stateDir: root,
    config: { token: 'test-only-not-production', projectId: 'observer-test', workspaceRoots: [root],
      pollMs: 1000000, operatingMode: 'feishu-local', nativeSenderThreadId: 'manager-test',
      feishu: { transcriptEnabled: true, streamEnabled: false, toolProgressEnabled: true } },
    listNativeTools: async () => (await f.transport.listTools()).filter(t => t.name !== 'wait_threads'),
    native: async (name, args) => { calls.push(name); return f.transport.execute(name, args); },
  });
  t.after(async () => { await manager.close(); await rm(root, { recursive: true, force: true }); });
  await manager.initialize();
  await manager.register({ protocolVersion: PROTOCOL, messageId: '968b6566-fda0-437f-8aa2-f46ad046088c', source: 'trainer', threadId: f.thread.id });
  manager.notifier = { sendMessage: async text => { sent.push(text); return { code: 0 }; } };
  f.transport.publicEvents.attach(manager);
  f.start(); f.user('desktop manager input'); f.stream('desktop manager output'); await f.end();
  await manager.scan(); await manager.scan();
  const events = manager.state.events.filter(e => e.type === 'message.observed');
  assert.equal(events.length, 1); assert.equal(events[0].payload.text, 'desktop manager input');
  assert.equal(events[0].payload.alreadyDispatched, true);
  assert.equal(calls.includes('send_message_to_thread'), false);
  assert.ok(sent.some(text => text.includes('desktop manager input')));
  assert.ok(sent.some(text => text.includes('desktop manager output')));
  assert.equal(sent.filter(text => text.includes('desktop manager output')).length, 1);
});
