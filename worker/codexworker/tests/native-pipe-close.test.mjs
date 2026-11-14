import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { listTools, callTool } from "../interface/transport/desktop-pipe.mjs";

async function fixture(t, connected) {
  const directory = process.platform === "win32" ? null : await mkdtemp(path.join(os.tmpdir(), "cw-"));
  const endpoint = process.platform === "win32" ? "\\\\.\\pipe\\cw-native-close-" + randomUUID() : path.join(directory, "rpc.sock");
  const sockets = new Set();
  const server = net.createServer(socket => {
    sockets.add(socket);
    socket.once("close", () => sockets.delete(socket));
    connected(socket);
  });
  server.listen(endpoint); await once(server, "listening");
  const keys = ['CODEX_APP_TOOLS_PIPE_PATH', 'CODEXWORKER_NATIVE_INPUT_DESCRIPTOR',
    'CODEXWORKER_NATIVE_IMAGE_INPUT', 'CODEXWORKER_NATIVE_QUESTION_INPUT'];
  const previous = keys.map(key => [key, process.env[key]]);
  for (const key of keys) delete process.env[key];
  process.env.CODEX_APP_TOOLS_PIPE_PATH = endpoint;
  t.after(async () => {
    for (const [key, value] of previous) {
      if (value === undefined) delete process.env[key]; else process.env[key] = value;
    }
    for (const socket of sockets) socket.destroy();
    await new Promise(resolve => server.close(resolve));
    if (directory) await rm(directory, { recursive: true, force: true });
  });
}

test("native peer closing before a response reports closure without waiting for timeout", { timeout: 2500 }, async t => {
  await fixture(t, socket => socket.end());
  await assert.rejects(listTools(), error => {
    assert.equal(error.code, "native_pipe_closed");
    assert.match(error.message, /closed.*before responding/);
    return true;
  });
});

test("a complete response remains successful when the native peer closes afterward", async t => {
  await fixture(t, socket => socket.once("data", chunk => {
    const request = JSON.parse(chunk.subarray(4).toString("utf8"));
    const body = Buffer.from(JSON.stringify({ id: request.id, jsonrpc: "2.0", result: { tools: [{ name: "list_threads", namespace: "test" }] } }));
    const header = Buffer.alloc(4); header.writeUInt32LE(body.length);
    socket.end(Buffer.concat([header, body]));
  }));
  assert.deepEqual(await listTools(), [{ name: "list_threads", namespace: "test" }]);
});

test("Desktop's renderer transport receives no private router routing fields", async t => {
  const calls = [];
  await fixture(t, socket => socket.once("data", chunk => {
    const request = JSON.parse(chunk.subarray(4).toString("utf8"));
    calls.push(request);
    const result = request.method === "tools/list"
      ? { tools: [{ name: "send_message_to_thread", namespace: "test" }] } : { success: true };
    const body = Buffer.from(JSON.stringify({ id: request.id, jsonrpc: "2.0", result }));
    const header = Buffer.alloc(4); header.writeUInt32LE(body.length);
    socket.end(Buffer.concat([header, body]));
  }));
  await callTool("send_message_to_thread", { threadId: "thread", prompt: "[router t-test] task",
    nativeTask: { taskId: "t-test", messageId: randomUUID() } }, { threadId: "thread" });
  assert.deepEqual(calls.at(-1).params.arguments, { threadId: "thread", prompt: "[router t-test] task" });
});
