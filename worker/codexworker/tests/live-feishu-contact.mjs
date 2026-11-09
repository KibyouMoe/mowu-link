// Bounded, explicitly authorized staged acceptance. Not a production client
// fallback and not a second Codex conversation/runtime host.
import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { readFile } from "node:fs/promises";
import { FeishuNotifier, FeishuWebsocketClient } from "../feishu.mjs";

const [threadId, secretFile, tokenFile, hermesUrl, mode] = process.argv.slice(2);
if (!threadId || !secretFile || !tokenFile || !hermesUrl || mode !== "--staged-acceptance") {
  throw new Error("Requires an authorized threadId, external secret/token files, Hermes URL and --staged-acceptance.");
}
const endpoint = "http://127.0.0.1:8903";
const token = (await readFile(tokenFile, "utf8")).trim();
const external = JSON.parse(await readFile(secretFile, "utf8"));
assert.ok(external.app_id && external.app_secret && external.trainer_open_id);
const chatId = external.chat_id ?? external.trainer_chat_id;
assert.ok(chatId, "A trainer chat is required.");
const marker = `RUDOLF_LOOP_${randomUUID().slice(0, 8)}`;
const messageId = randomUUID();
const expectedReply = `RUDOLF_FEISHU_ROUNDTRIP_OK ${marker}`;
const notifier = new FeishuNotifier({ appId: external.app_id, appSecret: external.app_secret,
  chatId, bridgeName: "鲁道夫 · Codex" });
const http = async (route, body) => {
  const response = await fetch(endpoint + route, { method: body ? "POST" : "GET",
    headers: { authorization: `Bearer ${token}`, "content-type": "application/json" },
    ...(body ? { body: JSON.stringify(body) } : {}), signal: AbortSignal.timeout(45000) });
  const result = await response.json();
  if (!response.ok) throw new Error(`Bridge rejected acceptance: HTTP ${response.status}, ${result.error}`);
  return result;
};
const health = await http("/health");
assert.equal(health.integrationMode, "desktop-native");
assert.equal(health.ready, true);
assert.equal(health.managerReady, false, "This harness is only the explicitly selected staged acceptance.");
assert.equal((await http(`/manager/sessions/${threadId}`)).session.threadId, threadId);
let resolveInbound;
const inbound = new Promise(resolve => { resolveInbound = resolve; });
let received = false;
let inboundTimer;
const client = new FeishuWebsocketClient(notifier, { onMessage: event => {
  if (received || event.open_id !== external.trainer_open_id || event.chat_id !== chatId ||
      event.chat_type !== "p2p" || event.sender_type !== "user" || event.message_type !== "text" ||
      event.content.trim() !== marker) return;
  received = true; resolveInbound(event);
} });
const sleep = ms => new Promise(resolve => setTimeout(resolve, ms));
try {
  client.start();
  const connectionDeadline = Date.now() + 25000;
  while (client._ws?.readyState !== 1 && Date.now() < connectionDeadline) await sleep(250);
  assert.equal(client._ws?.readyState, 1, "Feishu native WebSocket must open before asking for a reply.");
  const prompt = await notifier.sendMessage(`训练员，飞书入站回环测试已准备好。请在这个飞书单聊中只回复下面一行：\n\n${marker}\n\n仅这条匹配消息会进入当前 Codex 聊天。接收窗口为 3 分钟；若 Codex 正在工作，会排队，随后把原生回复回传飞书与超级溪流。本次是有时限的联调，不代表完整 manager 已交付。`,
    { title: "鲁道夫 · 飞书回环验收", threadId });
  assert.equal(prompt?.code, 0, "The actual Feishu prompt must be accepted.");
  process.stdout.write(`${JSON.stringify({ stage: "awaiting_trainer_feishu_reply", marker, threadId,
    feishuMessageId: prompt.data?.message_id, windowSeconds: 180 })}\n`);
  const event = await Promise.race([inbound, new Promise(resolve => {
    inboundTimer = setTimeout(() => resolve(null), 180000);
  })]);
  clearTimeout(inboundTimer);
  assert.ok(event, "No matching authorized Feishu reply arrived in the acceptance window.");
  client.stop(); // No second consumer is left running after the one test input.
  const request = { protocolVersion: "1.1.1-draft", messageId, source: "trainer", targetThreadId: threadId,
    delivery: "queue", text: `【训练员飞书回环验收 ${marker}】\n飞书原文：${event.content}\n这是训练员已授权的单条联调消息。请只回复 ${expectedReply}，不要调用工具、不要修改文件。这条回复将由验收驱动发回飞书与超级溪流。` };
  const accepted = await http("/manager/messages", request);
  assert.equal((await http("/manager/messages", request)).duplicate, true);
  process.stdout.write(`${JSON.stringify({ stage: "feishu_input_accepted_by_native_bridge", marker, messageId,
    threadId, action: accepted.action, feishuInboundMessageId: event.message_id })}\n`);
  let cursor = accepted.eventSeq - 1;
  let completed = false; let reply = ""; let turnId;
  const turnDeadline = Date.now() + 600000;
  while (Date.now() < turnDeadline) {
    const page = await http(`/manager/events?after=${cursor}&stream=0&limit=500`);
    for (const item of page.events) {
      cursor = Math.max(cursor, item.seq);
      if (item.messageId !== messageId) continue;
      if (item.type === "message.rejected") throw new Error(`Native acceptance rejected: ${item.payload.error}`);
      if (item.type === "item.completed") { reply = item.payload.snapshot; turnId = item.turnId; }
      if (item.type === "turn.completed") {
        assert.equal(item.payload.status, "completed", "Native turn must finish successfully.");
        completed = true;
      }
    }
    if (completed && reply.includes(expectedReply)) break;
    await sleep(page.hasMore ? 0 : 1000);
  }
  assert.ok(completed && reply.includes(expectedReply), "Native matching turn/reply must complete; no automatic new-message retry.");
  const delivered = await notifier.sendMessage(reply, { title: "鲁道夫 · Codex 原生回环回复", threadId });
  assert.equal(delivered?.code, 0);
  const report = await fetch(`${hermesUrl.replace(/\/$/, "")}/report`, { method: "POST",
    headers: { "content-type": "application/json" }, body: JSON.stringify({ machine: "CodexWorker",
      thread_id: threadId, type: "reply", content: reply }), signal: AbortSignal.timeout(8000) });
  assert.ok(report.ok, "The unchanged Hermes report endpoint must accept the native reply.");
  process.stdout.write(`${JSON.stringify({ stage: "passed", passed: true, marker, messageId, threadId, turnId,
    feishuInbound: true, nativeReply: true, feishuReplyMessageId: delivered.data?.message_id,
    hermesReportStatus: report.status, managerReady: false })}\n`);
} finally {
  clearTimeout(inboundTimer);
  client.stop();
  // The reused DSH module may have a reconnect sleep in flight. This bounded
  // acceptance owns only itself; it never stops a native task or Desktop app.
}
