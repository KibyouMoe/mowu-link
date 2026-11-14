import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
import { readFile } from "node:fs/promises";
import { launchDesktopMcp } from "../interface/desktop-mcp.mjs";

test("Mac replaces the launcher with the host-provided runtime before loading business code", async () => {
  let replaced = false;
  const env = { CODEX_MCP_NODE_PATH: "/Applications/Host App/Node", CODEX_APP_TOOLS_PIPE_PATH: "host-supplied" };
  await launchDesktopMcp({ platform: "darwin", env, currentExecutable: "/system/node", canonical: async value => value,
    loadServer: () => assert.fail("system Node must not load business"), spawnImpl: () => assert.fail("execve keeps the existing process ancestry"),
    replaceProcess: (executable, args, inherited) => {
      replaced = true; assert.equal(executable, env.CODEX_MCP_NODE_PATH);
      assert.equal(args[0], executable); assert.match(args[1], /[/\\]server\.mjs$/);
      assert.equal(inherited, env);
    } });
  assert.equal(replaced, true);
});

for (const platform of ["darwin", "win32"]) {
  test(platform + ": the real manifest forwards the engine identity needed to reject a foreign state directory", async () => {
    const manifest = JSON.parse(await readFile(new URL("../.mcp.json", import.meta.url), "utf8"));
    const server = manifest.mcpServers.codexworker;
    const parent = { CODEXWORKER_NATIVE_TRANSPORT: "dsh-inprocess", CODEXWORKER_STATE_DIR: "/foreign/dsh-state",
      CODEX_APP_TOOLS_PIPE_PATH: "actual-desktop-pipe" };
    const env = { ...Object.fromEntries(Object.entries(parent).filter(([key]) => server.env_vars.includes(key))), ...server.env };
    await launchDesktopMcp({ platform, env, loadServer: async () => {
      assert.equal(env.CODEXWORKER_NATIVE_TRANSPORT, undefined);
      assert.equal(env.CODEXWORKER_STATE_DIR, undefined);
      assert.equal(env.CODEX_APP_TOOLS_PIPE_PATH, parent.CODEX_APP_TOOLS_PIPE_PATH);
      assert.equal(env.CODEXWORKER_NATIVE_BOOTSTRAP, "1");
    } });
  });

  test(platform + ": a Desktop opened by DSH discards the other engine's state and child permissions", async () => {
    const env = { CODEXWORKER_NATIVE_TRANSPORT: "dsh-inprocess", CODEXWORKER_STATE_DIR: "/foreign/dsh-state",
      RUDOLF_BRIDGE_LIFECYCLE_CHILD: "1", RUDOLF_BRIDGE_LIFECYCLE_READY: "1",
      RUDOLF_BRIDGE_DESKTOP_STARTED_AT: "foreign lifetime", RUDOLF_BRIDGE_LIFETIME_LAUNCH: "foreign",
      RUDOLF_BRIDGE_RUNTIME_HOST_PID: "123", CODEXWORKER_DESKTOP_PID: "124",
      CODEXWORKER_NATIVE_IMAGE_INPUT: "1", CODEXWORKER_NATIVE_QUESTION_INPUT: "1",
      CODEXWORKER_NATIVE_INPUT_DESCRIPTOR: "/foreign/descriptor", CODEX_HOME: "/original/codex-home",
      CODEX_APP_TOOLS_PIPE_PATH: "actual-desktop-pipe" };
    await launchDesktopMcp({ platform, env, loadServer: async () => {
      assert.deepEqual(env, { CODEX_HOME: "/original/codex-home", CODEX_APP_TOOLS_PIPE_PATH: "actual-desktop-pipe" });
    } });
  });

  test(platform + ": the Desktop keeps its explicitly configured own state while resetting inherited lifecycle flags", async () => {
    const env = { CODEXWORKER_STATE_DIR: "/custom/codex-state", CODEXWORKER_NATIVE_TRANSPORT: "desktop-pipe",
      RUDOLF_BRIDGE_LIFECYCLE_CHILD: "1", CODEXWORKER_NATIVE_INPUT_DESCRIPTOR: "/own/descriptor" };
    await launchDesktopMcp({ platform, env, loadServer: async () => {
      assert.deepEqual(env, { CODEXWORKER_STATE_DIR: "/custom/codex-state", CODEXWORKER_NATIVE_TRANSPORT: "desktop-pipe",
        CODEXWORKER_NATIVE_INPUT_DESCRIPTOR: "/own/descriptor" });
    } });
  });

  test(platform + ": a matching host runtime loads MCP exactly once", async () => {
    let loaded = 0;
    const executable = platform === "win32" ? "C:\\Program Files\\Codex\\node.exe" : "/Desktop/node";
    await launchDesktopMcp({ platform, env: { CODEX_MCP_NODE_PATH: executable }, currentExecutable: executable,
      canonical: async value => value, loadServer: async () => { loaded++; }, spawnImpl: () => assert.fail("already on host runtime") });
    assert.equal(loaded, 1);
  });

  test(platform + ": invalid host runtime fails before business or a native call", async () => {
    const opts = { platform, loadServer: () => assert.fail("must not load"), spawnImpl: () => assert.fail("must not spawn") };
    await assert.rejects(launchDesktopMcp({ ...opts, env: { CODEX_MCP_NODE_PATH: "relative-node" } }), /not_absolute/);
    await assert.rejects(launchDesktopMcp({ ...opts, env: { CODEX_MCP_NODE_PATH: platform === "win32" ? "C:\\missing.exe" : "/missing" },
      canonical: async () => { throw Error("runtime unavailable"); } }), /runtime unavailable/);
  });

  test(platform + ": older Desktop hosts without a runtime hint retain their existing MCP runtime", async () => {
    let loaded = 0;
    await launchDesktopMcp({ platform, env: { CODEX_APP_TOOLS_PIPE_PATH: "older-host" },
      loadServer: async () => { loaded++; }, spawnImpl: () => assert.fail("keep the existing runtime") });
    assert.equal(loaded, 1);
  });
}

test("Windows launches the supplied runtime and preserves the MCP streams and exit code", async () => {
  const env = { CODEX_MCP_NODE_PATH: "C:\\Program Files\\Codex\\node.exe" };
  const result = await launchDesktopMcp({ env, platform: "win32", currentExecutable: "C:\\System\\node.exe", canonical: async value => value,
    replaceProcess: () => assert.fail("Windows cannot execve"), loadServer: () => assert.fail("must select host runtime"),
    spawnImpl: (executable, args, options) => {
      assert.equal(executable, env.CODEX_MCP_NODE_PATH); assert.match(args[0], /[/\\]server\.mjs$/);
      assert.deepEqual(options, { stdio: "inherit", env, windowsHide: true });
      const child = new EventEmitter(); child.kill = () => {};
      setImmediate(() => child.emit("exit", 7)); return child;
    } });
  assert.equal(result, 7);
});

test("isolated package diagnostics load without a native Desktop endpoint", async () => {
  let loaded = 0;
  await launchDesktopMcp({ env: {}, loadServer: async () => { loaded++; }, spawnImpl: () => assert.fail("no alternate runtime") });
  assert.equal(loaded, 1);
});
