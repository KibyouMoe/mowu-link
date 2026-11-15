import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, rm, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { DesktopManager } from "../desktop-manager.mjs";

test("failed state loading never overwrites the original ledger during initialization cleanup", async t => {
  for (const original of [Buffer.alloc(4096), Buffer.from('{"private":"truncated ledger"'), Buffer.from('{}')]) {
    const root = await mkdtemp(path.join(os.tmpdir(), "bridge-state-integrity-"));
    t.after(() => rm(root, { recursive: true, force: true }));
    const file = path.join(root, "desktop-manager-state.json"); await writeFile(file, original);
    let calls = 0;
    const manager = new DesktopManager({ stateDir: root,
      config: { token: "isolated-state-integrity-token-123456789", workspaceRoots: [root] },
      native: async () => { calls++; }, listNativeTools: async () => { calls++; return []; } });
    await assert.rejects(manager.initialize(), error => error.body?.error === "manager_state_corrupt" && !error.message.includes("private"));
    await manager.close();
    assert.deepEqual(await readFile(file), original); assert.equal(calls, 0);
    // A subsequent retry must see the SAME failure, not a silently empty state.
    const next = new DesktopManager({ stateDir: root,
      config: { token: "isolated-state-integrity-token-123456789", workspaceRoots: [root] },
      native: async () => { calls++; }, listNativeTools: async () => [] });
    await assert.rejects(next.initialize(), error => error.body?.error === "manager_state_corrupt");
    await next.close(); assert.deepEqual(await readFile(file), original);
  }
});
