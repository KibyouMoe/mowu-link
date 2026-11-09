import assert from "node:assert/strict";
import { once } from "node:events";
import { cp, mkdtemp, readFile, rm } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";
import { fileURLToPath } from "node:url";

const MAX_FRAME_BYTES = 8 * 1024 * 1024;
const sourcePluginRoot = fileURLToPath(new URL("..", import.meta.url));
const testRoot = await mkdtemp(path.join(os.tmpdir(), "codexworker-test-"));
const pipePath = `\\\\.\\pipe\\codexworker-test-${process.pid}-${Date.now()}`;
const nativePipe = net.createServer((socket) => handleNativeConnection(socket));
nativePipe.listen(pipePath);
await once(nativePipe, "listening");

try {
  const result = await runBridgeBindingTest();
  assert.equal(result.structuredContent.desktopNative, true);
  assert.equal(result.structuredContent.pipeInjected, true);
  assert.equal(result.structuredContent.nativeTaskReady, false);
  assert.equal(result.structuredContent.nativeReadProbeSucceeded, true);
  assert.equal(result.structuredContent.binding.managerThreadId, "thread-native-test");
  assert.equal(result.structuredContent.binding.managerTurnId, "turn-native-test");

  const bindingPath = path.join(testRoot, ".codex", "state", "codexworker", "manager-binding.json");
  const binding = JSON.parse(await readFile(bindingPath, "utf8"));
  assert.equal(binding.managerThreadId, "thread-native-test");
  assert.equal(binding.managerTurnId, "turn-native-test");
  const toolList = await runBridgeBindingTest({ jsonrpc: "2.0", id: 2, method: "tools/list", params: {} });
  const questionTool = toolList.tools.find(t => t.name === "bridge_publish_question");
  assert.ok(questionTool); assert.deepEqual(questionTool.inputSchema.required, ["questions"]);
  assert.equal(questionTool.inputSchema.properties.questions.maxItems, 3);
  const imageTool = toolList.tools.find(t => t.name === "bridge_send_image");
  assert.deepEqual(imageTool.inputSchema.required, ["imagePath"]);
  assert.equal(imageTool.inputSchema.additionalProperties, false);
  assert.equal("chatId" in imageTool.inputSchema.properties, false);
  assert.ok(toolList.tools.some(t => t.name === "bridge_reload_business_modules"));
  // An inventory client may close stdin immediately after initialize. The
  // initialization response must still drain without a dangling startup task.
  const init = await runBridgeBindingTest({ jsonrpc: "2.0", id: 3, method: "initialize", params: {} });
  assert.equal(init.serverInfo.version, "0.2.38");

  // Exercise the distributable package through its real launcher, not an
  // absolute server path that would conceal a hard-coded development cwd.
  const relocatedRoot = path.join(testRoot, "installed plugin with spaces");
  await cp(sourcePluginRoot, relocatedRoot, { recursive: true });
  const mcp = JSON.parse(await readFile(path.join(relocatedRoot, ".mcp.json"), "utf8"))
    .mcpServers.rudolf_hermes_bridge;
  assert.equal(path.resolve(relocatedRoot, mcp.cwd), relocatedRoot,
    "the package must start from its installed root, not an old development directory");
  const manifest = JSON.parse(await readFile(path.join(relocatedRoot, ".codex-plugin", "plugin.json"), "utf8"));
  assert.equal(manifest.version.split("+")[0], init.serverInfo.version);
  if (process.platform === "win32") {
    const relocatedInit = await runBridgeBindingTest(
      { jsonrpc: "2.0", id: 4, method: "initialize", params: {} }, { pluginRoot: relocatedRoot, mcp });
    assert.equal(relocatedInit.serverInfo.version, init.serverInfo.version);
    const relocatedBinding = await runBridgeBindingTest(undefined, { pluginRoot: relocatedRoot, mcp });
    assert.equal(relocatedBinding.structuredContent.nativeReadProbeSucceeded, true);
    assert.equal(relocatedBinding.structuredContent.binding.managerThreadId, "thread-native-test");
  }
  process.stdout.write("server.test.mjs: passed\n");
} finally {
  nativePipe.close();
  await rm(testRoot, { recursive: true, force: true });
}

function handleNativeConnection(socket) {
  let pending = Buffer.alloc(0);
  socket.on("data", (chunk) => {
    pending = Buffer.concat([pending, chunk]);
    if (pending.length < 4) return;
    const length = pending.readUInt32LE(0);
    assert.ok(length <= MAX_FRAME_BYTES, "bridge request must respect the native frame limit");
    if (pending.length < length + 4) return;
    const request = JSON.parse(pending.subarray(4, length + 4).toString("utf8"));
    let result;
    if (request.method === "tools/list") {
      result = { tools: [{ name: "list_threads", namespace: "codex_app" }] };
    } else if (request.method === "tools/call") {
      assert.equal(request.params.tool, "list_threads");
      assert.equal(request.params.callerSource, "codex", "Desktop app tools require an explicit callerSource");
      assert.equal(request.params.namespace, "codex_app");
      assert.equal(request.params.threadId, "thread-native-test");
      assert.equal(request.params.turnId, "turn-native-test");
      result = {
        success: true,
        contentItems: [{ type: "inputText", text: "[]" }],
      };
    } else {
      throw new Error(`unexpected native pipe method: ${request.method}`);
    }
    socket.end(encodeFrame(JSON.stringify({ jsonrpc: "2.0", id: request.id, result })));
  });
}

async function runBridgeBindingTest(input, { pluginRoot = sourcePluginRoot, mcp } = {}) {
  const child = spawn(mcp?.command ?? process.execPath, mcp?.args ?? [path.join(pluginRoot, "server.mjs")], {
    cwd: mcp ? path.resolve(pluginRoot, mcp.cwd) : path.dirname(sourcePluginRoot),
    env: {
      ...process.env,
      CODEX_APP_TOOLS_PIPE_PATH: pipePath,
      CODEX_MCP_NODE_PATH: process.execPath,
      CODEXWORKER_LIFECYCLE_CHILD: "1",
      USERPROFILE: testRoot,
      HOME: testRoot,
    },
    stdio: ["pipe", "pipe", "pipe"],
  });
  const stdout = [];
  const stderr = [];
  child.stdout.setEncoding("utf8");
  child.stderr.setEncoding("utf8");
  child.stdout.on("data", (chunk) => stdout.push(chunk));
  child.stderr.on("data", (chunk) => stderr.push(chunk));

  child.stdin.end(`${JSON.stringify(input ?? {
    jsonrpc: "2.0",
    id: 1,
    method: "tools/call",
    params: {
      name: "bridge_bind_manager",
      arguments: {},
      _meta: {
        "openai/threadId": "thread-native-test",
        "openai/turnId": "turn-native-test",
      },
    },
  })}\n`);

  const [exitCode] = await once(child, "exit");
  assert.equal(exitCode, 0, `bridge process failed: ${stderr.join("")}`);
  const response = JSON.parse(stdout.join("").trim());
  assert.ok(!response.error, response.error?.message);
  return response.result;
}

function encodeFrame(message) {
  const payload = Buffer.from(message, "utf8");
  const frame = Buffer.alloc(4 + payload.length);
  frame.writeUInt32LE(payload.length, 0);
  payload.copy(frame, 4);
  return frame;
}

