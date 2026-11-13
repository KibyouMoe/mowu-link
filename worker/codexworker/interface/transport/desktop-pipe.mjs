// NativeTransport · Desktop 实现
//
// 传输：Codex Desktop 注入的 app-tools 管道（CODEX_APP_TOOLS_PIPE_PATH）。
// 帧格式：4 字节小端长度 + UTF-8 JSON-RPC。
//
// 这一层只负责「把 app tools 的 list/call 送出去」，
// 不关心业务语义；Desktop 拥有原生引擎和会话。
import { randomUUID } from "node:crypto";
import net from "node:net";
import process from "node:process";
import { setTimeout as delay } from "node:timers/promises";
import { nativeToolRejection } from "../native-dispatch.mjs";
import { hydrateNativeInputs } from "../native-history.mjs";

export const name = "desktop-pipe";

const PIPE_ENV = "CODEX_APP_TOOLS_PIPE_PATH";
const MAX_FRAME_BYTES = 8 * 1024 * 1024;
let nextPipeRequestId = 1;
const routerInputCache = new Map();

export async function shutdown() { /* The Desktop owns the engine. */ }

export function available() {
  return Boolean(process.env[PIPE_ENV]?.trim());
}

export function unavailableReason() {
  return `${PIPE_ENV} was not injected by Codex Desktop`;
}

function pipePath() {
  return process.env[PIPE_ENV]?.trim() || null;
}

/** @returns {Promise<Array<{name: string, namespace?: string}>>} */
export async function listTools() {
  const path = pipePath();
  if (!path) throw new Error(unavailableReason());
  const result = await pipeRequest(path, "tools/list", { threadStartKind: "all" });
  return Array.isArray(result?.tools) ? result.tools : [];
}

/**
 * @param {string} toolName
 * @param {object} args
 * @param {{threadId?: string, turnId?: string, callId?: string}} invocation
 * @param {number} timeoutMs
 */
export async function callTool(toolName, args, invocation, timeoutMs = 5000) {
  const path = pipePath();
  if (!path) throw new Error(unavailableReason());
  const tools = await listTools();
  const tool = tools.find((candidate) => candidate?.name === toolName);
  if (!tool?.namespace) {
    throw new Error(`Codex Desktop native tool is unavailable: ${toolName}`);
  }
  if (!invocation?.threadId) {
    throw new Error("Codex Desktop native tools require a manager task binding");
  }
  const call = (request, budget) => pipeRequest(path, "tools/call", {
    arguments: request ?? {},
    callerSource: "codex",
    callId: invocation.callId ?? `codexworker-hermes-${randomUUID()}`,
    namespace: tool.namespace,
    threadId: invocation.threadId,
    tool: tool.name,
    turnId: invocation.turnId ?? `codexworker-hermes-${randomUUID()}`,
  }, budget);
  if (toolName === "wait_threads" && !invocation.turnId && args.targets?.length === 1 &&
      args.targets[0].threadId === invocation.threadId) {
    // Desktop's native wait excludes the calling chat. The worker polls its
    // history for completion; pace that poll without submitting a self-wait.
    await delay(Math.max(0, Math.min(1500, timeoutMs, args.timeoutMs ?? 1500)));
    return { success: true, contentItems: [{ type: "text", text: '{"polls":[]}' }] };
  }
  const maximum = tool.inputSchema?.properties?.turnLimit?.maximum ?? 10;
  if (toolName === "read_thread" && Number.isInteger(args?.turnLimit) && args.turnLimit > maximum) {
    return readRouterHistory(args, maximum, call, timeoutMs);
  }
  return call(args, timeoutMs);
}

async function readRouterHistory(args, maximum, call, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  const budget = () => {
    const remaining = deadline - Date.now();
    if (remaining <= 0) throw new Error("Timed out reading the Desktop's router history.");
    return remaining;
  };
  const turns = [], ids = new Set(), cursors = new Set();
  let cursor = args.cursor, first, page, reply;
  if (cursor) cursors.add(cursor);
  do {
    const response = await call({ ...args, turnLimit: Math.min(maximum, args.turnLimit - turns.length), ...(cursor ? { cursor } : {}) }, budget());
    if (response.success !== true) return response;
    page = parseHistory(response, args.threadId);
    reply ??= response;
    first ??= page;
    for (const turn of page.turns) {
      if (ids.has(turn.id)) continue;
      ids.add(turn.id);
      turns.push(turn);
    }
    cursor = page.page?.nextCursor;
    if (!cursor || !page.turns.length) break;
    if (cursors.has(cursor)) throw new Error("Desktop returned a repeated history cursor.");
    cursors.add(cursor);
  } while (turns.length < args.turnLimit);

  const snapshot = { ...first, turns, page: { ...first.page, nextCursor: cursor ?? null, returnedTurns: turns.length } };
  // A Desktop send receipt may omit turnId. Its public input is a delegated
  // item, so hydrate only those exact outputs for marker-based correlation.
  try {
    await hydrateNativeInputs(snapshot, async query => {
      const response = await call(query, budget());
      if (response.success !== true) throw nativeToolRejection("read_thread", response);
      return parseHistory(response, args.threadId);
    }, routerInputCache);
  } catch (error) {
    if (error.body?.error !== "native_snapshot_incomplete" && !error.nativeReachable) throw error;
  }
  for (const turn of turns) turn.items = (turn.items ?? []).map(item => routerInput(item, args.threadId));
  const textIndex = reply.contentItems.findIndex(item => typeof item.text === "string");
  return { ...reply, contentItems: reply.contentItems.map((item, index) =>
    index === textIndex ? { ...item, text: JSON.stringify(snapshot) } : item) };
}

function parseHistory(response, threadId) {
  const text = response.contentItems?.find(item => typeof item.text === "string")?.text;
  const page = JSON.parse(text);
  if (page.thread?.id !== threadId || !Array.isArray(page.turns)) throw new Error("Unexpected Desktop chat history.");
  return page;
}

function routerInput(item, threadId) {
  if (item.type !== "functionCallOutput" || item.name !== "send_message_to_thread" || item.output?.truncated) return item;
  const raw = typeof item.output === "string" ? item.output : item.output?.text;
  if (typeof raw !== "string") return item;
  const start = raw.indexOf("<input>"), end = raw.lastIndexOf("</input>");
  const source = raw.slice(0, start).match(/<source_thread_id>([^<]+)<\/source_thread_id>/)?.[1];
  if (source !== threadId || start < 0 || end <= start) return item;
  const text = raw.slice(start + 7, end);
  return /^\[router t-[^\]\s]+\]/.test(text) ? { type: "userMessage", id: item.id, text } : item;
}

function pipeRequest(path, method, params, timeoutMs = 5000) {
  return new Promise((resolve, reject) => {
    const id = nextPipeRequestId++;
    const socket = net.createConnection(path);
    let pending = Buffer.alloc(0);
    let settled = false;

    const finish = (callback, value) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      callback(value);
    };

    const timer = setTimeout(() => {
      finish(reject, new Error("Timed out waiting for the Codex Desktop native tools pipe"));
    }, timeoutMs);

    socket.once("error", (error) => finish(reject, error));
    socket.once("connect", () => {
      socket.write(encodeFrame(JSON.stringify({ jsonrpc: "2.0", id, method, params })));
    });
    socket.on("data", (chunk) => {
      pending = Buffer.concat([pending, chunk]);
      if (pending.length < 4) return;
      const length = pending.readUInt32LE(0);
      if (length > MAX_FRAME_BYTES) {
        finish(reject, new Error("Codex Desktop native tools response exceeded the size limit"));
        return;
      }
      if (pending.length < length + 4) return;
      try {
        const response = JSON.parse(pending.subarray(4, length + 4).toString("utf8"));
        if (response.error) {
          finish(reject, nativeToolRejection(params.tool ?? method, { success: false,
            contentItems: [{ text: response.error.message ?? "Native tools request failed" }] }));
        } else {
          finish(resolve, response.result);
        }
      } catch (error) {
        finish(reject, error);
      }
    });
  });
}

function encodeFrame(message) {
  const payload = Buffer.from(message, "utf8");
  if (payload.length > MAX_FRAME_BYTES) {
    throw new Error("Native tools request exceeded the size limit");
  }
  const frame = Buffer.alloc(4 + payload.length);
  frame.writeUInt32LE(payload.length, 0);
  payload.copy(frame, 4);
  return frame;
}
