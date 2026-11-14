import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import test from "node:test";
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
