import test from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, writeFile, rm } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { createBusiness } from "../mcp-business.mjs";
import { registerNativeTransport, resetNativeTransportCache } from "../interface/transport/index.mjs";

async function fixture(t, { ownsWriter, autoStart = true, child = "0", ready = "0" }) {
  const stateDir = await mkdtemp(path.join(os.tmpdir(), "business-host-"));
  const keys = ["CODEXWORKER_STATE_DIR", "CODEXWORKER_NATIVE_TRANSPORT", "RUDOLF_BRIDGE_LIFECYCLE_CHILD", "RUDOLF_BRIDGE_LIFECYCLE_READY"];
  const previous = keys.map(key => [key, process.env[key]]);
  Object.assign(process.env, { CODEXWORKER_STATE_DIR: stateDir, CODEXWORKER_NATIVE_TRANSPORT: "test-business-host",
    RUDOLF_BRIDGE_LIFECYCLE_CHILD: child, RUDOLF_BRIDGE_LIFECYCLE_READY: ready });
  await writeFile(path.join(stateDir, "manager-binding.json"), JSON.stringify({ managerThreadId: "original", integrationMode: "desktop-native" }));
  const tokenFile = path.join(stateDir, "test.token"); await writeFile(tokenFile, "isolated-host-token-for-cold-start-regression");
  await writeFile(path.join(stateDir, "desktop-config.local.json"), JSON.stringify({ autoStart, tokenFile,
    host: "127.0.0.1", port: 0, machine: "test", bridgeName: "test", workspaceRoots: [stateDir], managerProxyWaitMs: 50 }));
  registerNativeTransport("test-business-host", { capabilities: { desktopJournal: false, callerTurn: false },
    listTools: async () => ["read_thread", "list_projects", "send_message_to_thread", "list_threads",
      "create_thread", "fork_thread", "set_thread_title"].map(name => ({ name })),
    callTool: async (name, args) => ({ success: true, contentItems: [{ type: "text", text: JSON.stringify(
      name === "read_thread" ? { thread: { id: args.threadId, cwd: stateDir }, turns: [] } : { projects: [] }) }] }) });
  const business = createBusiness({ ownsWriter, transition: false, status: () => ({ available: true }), reload: async () => ({}) });
  t.after(async () => {
    await business.close();
    for (const [key, value] of previous) value === undefined ? delete process.env[key] : process.env[key] = value;
    resetNativeTransportCache(); await rm(stateDir, { recursive: true, force: true });
  });
  return business;
}

test("an actual in-process host retains writer ownership independently of inherited client flags", async t => {
  const business = await fixture(t, { ownsWriter: true });
  assert.equal((await business.activate()).ready, true);
  assert.equal(business.manager().proxy, undefined);
  assert.equal(business.manager().server.listening, true);
});

test("host writer ownership still respects the configured autoStart switch", async t => {
  const business = await fixture(t, { ownsWriter: true, autoStart: false });
  await assert.rejects(business.activate(), /worker_autostart_disabled/);
  assert.equal(business.manager(), null);
});

test("an explicit non-writer host cannot promote itself using another client's child flag", async t => {
  const business = await fixture(t, { ownsWriter: false, child: "1", ready: "1" });
  await assert.rejects(business.activate(), /desktop_lifetime_writer_unavailable/);
  assert.equal(business.manager(), null);
});
