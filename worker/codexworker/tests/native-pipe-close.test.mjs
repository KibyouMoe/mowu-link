import assert from "node:assert/strict";
import { randomUUID } from "node:crypto";
import { once } from "node:events";
import { mkdtemp, rm } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import test from "node:test";
import { listTools } from "../interface/transport/desktop-pipe.mjs";

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
  const previous = process.env.CODEX_APP_TOOLS_PIPE_PATH;
  process.env.CODEX_APP_TOOLS_PIPE_PATH = endpoint;
  t.after(async () => {
    if (previous === undefined) delete process.env.CODEX_APP_TOOLS_PIPE_PATH;
    else process.env.CODEX_APP_TOOLS_PIPE_PATH = previous;
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
