import assert from "node:assert/strict";
import { createHash, randomUUID } from "node:crypto";
import { mkdtemp, readFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";
import { DesktopManager, PROTOCOL } from "../../desktop-manager.mjs";
import { InProcessTransport } from "../../interface/inprocess.mjs";
import { createInProcessAdapter } from "../../interface/transport/inprocess.mjs";
import { registerNativeTransport, resetNativeTransportCache } from "../../interface/transport/index.mjs";
import { nativeToolRejection } from "../../interface/native-dispatch.mjs";

export const png = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+j1WkAAAAASUVORK5CYII=", "base64");
export const sha256 = bytes => createHash("sha256").update(bytes).digest("hex");
const tick = () => new Promise(resolve => setImmediate(resolve));
async function waitForNative(predicate, detail) {
  for (let attempt = 0; attempt < 500; attempt++) {
    if (predicate()) return;
    await delay(10);
  }
  assert.fail(detail);
}

export async function dshFixture(t, { wireMedia = false } = {}) {
  const root = await mkdtemp(path.join(os.tmpdir(), "dsh-business-compat-"));
  const listeners = new Map(), records = [], waiters = [], calls = [], cards = [], imageSends = [];
  const mediaMessages = [], uploads = [], questionPosts = [], downloads = [];
  const session = { id: "native-original", seq: 0, eventAt: index => records[index] };
  const questions = { continued: () => [], answer: () => false };
  const compaction = { async compactNow(subject) {
    assert.equal(subject, agent);
    emit("compaction/start", { compactionId: "manual-compaction" });
    emit("compaction/summary", { summary: "private native summary" });
    emit("compaction/end", { compactionId: "manual-compaction" });
    return { compactionId: "manual-compaction" };
  } };
  const agent = { id: session.id, session, status: "idle",
    ctx: { get: name => name === "compaction" ? compaction : name === "userQuestions" ? questions : null },
    followup(message) { this.status = "running"; emit("turn/start", {}); emit("user/message", message); },
    steer(message) { emit("user/message", message); },
    whenIdle: () => agent.status === "idle" ? Promise.resolve() : new Promise(resolve => waiters.push(resolve)),
    cancel() { finish({ kind: "user" }); },
  };
  const handle = { agent, dispose: async () => { if (agent.status !== "idle") finish({ kind: "user" }); } };
  const registry = { list: () => [agent], create: async () => handle,
    resume: async ({ resumeSessionId }) => { assert.equal(resumeSessionId, session.id); return handle; } };
  const ctx = {
    get: name => ({ agents: registry, sessions: { flush: async () => {} }, userQuestions: questions })[name],
    on(name, listener) {
      const set = listeners.get(name) ?? new Set(); set.add(listener); listeners.set(name, set);
      return () => set.delete(listener);
    },
  };
  function publish(name, ...args) { for (const listener of listeners.get(name) ?? []) listener(...args); }
  function emit(type, data) {
    const event = { seq: session.seq++, type, data }; records.push(event); publish("session/event", session, event);
  }
  function stream(text, attemptId = "attempt-1") {
    publish("agent/assistant-stream", { agent, frame: { type: "start", attemptId } });
    publish("agent/assistant-stream", { agent, frame: { type: "chunk", attemptId, chunk: { type: "text-delta", text } } });
  }
  function finish(reason = { kind: "completed" }) {
    emit("turn/end", { reason }); agent.status = "idle";
    for (const resolve of waiters.splice(0)) resolve();
  }
  const transport = new InProcessTransport({ ctx, selection: {}, cwd: root, stateDir: root, log() {} });
  const threadId = transport.createThread({ cwd: root, title: "original main" }).threadId;
  const adapter = createInProcessAdapter(transport), previous = process.env.CODEXWORKER_NATIVE_TRANSPORT;
  const transportName = "dsh-compat-" + randomUUID();
  registerNativeTransport(transportName, adapter);
  process.env.CODEXWORKER_NATIVE_TRANSPORT = transportName; resetNativeTransportCache();
  const manager = await new DesktopManager({ stateDir: root,
    config: { token: "isolated-dsh-compat-token", projectId: "compat", workspaceRoots: [root],
      nativeSenderThreadId: threadId, nativeBusyDeliveryVerified: true, pollMs: 1000000,
      operatingMode: "feishu-local", bridgeName: "玉藻十字", messageSourceLabels: false,
      feishu: { transcriptEnabled: true, streamEnabled: true, streamUpdateIntervalMs: 250,
        toolProgressEnabled: true, trainerOpenIdWhitelist: ["trainer"] } },
    listNativeTools: () => adapter.listTools(),
    native: async (name, args) => {
      const response = await adapter.callTool(name, args);
      if (!response.success) throw nativeToolRejection(name, response);
      return JSON.parse(response.contentItems[0].text);
    },
  }).initialize();
  await manager.register({ protocolVersion: PROTOCOL, messageId: randomUUID(), source: "trainer", threadId });
  manager.session(threadId).feishuChatId = "chat";
  manager.notifier = { enabled: true, chatId: "chat", addReaction: async () => null, removeReaction: async () => true,
    getToken: async () => "isolated-test-token", invalidateToken() {},
    sendMessage: async (text, options) => {
      const id = "card-" + (calls.length + 1); calls.push({ method: "POST", id, text, options });
      return { code: 0, data: { message_id: id } };
    },
    updateMessage: async (id, text, options) => {
      calls.push({ method: "PATCH", id, text, options }); return { code: 0, data: { message_id: id } };
    },
    sendQuestionCard: async card => {
      cards.push(card); questionPosts.push(card);
      return { code: 0, data: { message_id: "question-card-" + questionPosts.length } };
    },
    updateQuestionCard: async (_id, card) => { cards.push(card); return { code: 0 }; },
    downloadImage: async () => { downloads.push("image"); return png; },
    downloadFileImage: async () => { downloads.push("file"); return png; },
  };
  manager.fetch = async (url, options) => {
    const resource = new URL(url).pathname;
    if (["/open-apis/im/v1/images", "/open-apis/im/v1/files"].includes(resource)) {
      const file = resource.endsWith("/files"), bytes = Buffer.from(await options.body.get(file ? "file" : "image").arrayBuffer());
      uploads.push({ bytes: bytes.length, sha256: sha256(bytes) });
      return new Response(JSON.stringify({ code: 0, data: file ? { file_key: "file_probe" } : { image_key: "img_probe" } }));
    }
    assert.equal(resource, "/open-apis/im/v1/messages");
    const body = JSON.parse(options.body); mediaMessages.push({ ...body, content: JSON.parse(body.content) });
    return new Response(JSON.stringify({ code: 0, data: { message_id: "media-card-" + mediaMessages.length } }));
  };
  if (!wireMedia) manager.sendImage = async (request, validate) => {
    validate(); const bytes = await readFile(request.imagePath);
    imageSends.push({ ...request, sha256: sha256(bytes), bytes: bytes.length });
    return { feishuMessageId: "image-card", sha256: sha256(bytes) };
  };
  transport.control.attachManager(manager);
  async function drain() {
    await manager.scan();
    for (let pass = 0; pass < 20; pass++) {
      await manager.publicTextTask; await manager.publicImageRelay.task; await manager.tail;
      for (const entry of Object.values(manager.state.publicTextOutbox ?? {})) entry.lastAttemptMs = 0;
      await manager.drainPublicTexts(); await manager.tail; await tick();
      const pending = [...Object.values(manager.state.publicTextOutbox ?? {}), ...Object.values(manager.state.autoImages ?? {})]
        .some(entry => ["pending", "dispatching"].includes(entry.status));
      if (!pending && !manager.publicTextRunning && !manager.publicImageRelay.running) {
        clearTimeout(manager.publicTextTimer); manager.publicTextTimer = null;
        return;
      }
    }
    assert.fail("DSH compatibility fixture outbound queue did not settle.");
  }
  async function send(text, extra = {}) {
    const messageId = randomUUID();
    const firstEvent = records.length, firstTurn = transport.threads.get(threadId).turns.length;
    await manager.message({ protocolVersion: PROTOCOL, messageId, source: "trainer", targetThreadId: threadId, text, ...extra });
    await waitForNative(
      () => records.slice(firstEvent).some(event => event.type === "user/message") ||
        transport.threads.get(threadId).turns.slice(firstTurn).some(turn => turn.status !== "inProgress"),
      "DSH fixture input did not reach its native agent or a terminal rejection.",
    );
    return messageId;
  }
  async function finishTurn() {
    finish();
    await waitForNative(() => !transport.threads.get(threadId).running, "DSH fixture native turn did not settle.");
    await transport.writeChain;
  }
  t.after(async () => {
    if (agent.status !== "idle") { finish(); await tick(); }
    await manager.close(); await transport.close();
    if (previous == null) delete process.env.CODEXWORKER_NATIVE_TRANSPORT;
    else process.env.CODEXWORKER_NATIVE_TRANSPORT = previous;
    resetNativeTransportCache(); await rm(root, { recursive: true, force: true });
  });
  return { root, manager, transport, adapter, agent, threadId, session, cards, calls, imageSends,
    mediaMessages, uploads, questionPosts, downloads, records,
    emit, publish, stream, finish: finishTurn, drain, send };
}
