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
import { nativeToolRejection } from "../native-dispatch.mjs";

export const name = "desktop-pipe";

const PIPE_ENV = "CODEX_APP_TOOLS_PIPE_PATH";
const MAX_FRAME_BYTES = 8 * 1024 * 1024;
let nextPipeRequestId = 1;

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
  return pipeRequest(path, "tools/call", {
    arguments: args ?? {},
    callerSource: "codex",
    callId: invocation.callId ?? `codexworker-hermes-${randomUUID()}`,
    namespace: tool.namespace,
    threadId: invocation.threadId,
    tool: tool.name,
    turnId: invocation.turnId ?? `codexworker-hermes-${randomUUID()}`,
  }, timeoutMs);
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
