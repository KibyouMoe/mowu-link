#!/usr/bin/env node
// CodexWorker 平台自检
//
// 在任意平台（Windows / macOS / Linux）跑一遍，报告这个实例能不能工作、
// 走的是哪条通道、缺什么。
//
//   node scripts/selfcheck.mjs            人读摘要
//   node scripts/selfcheck.mjs --json     机器可读
//
// 不写任何文件、不启动长驻进程，可以随便跑。
import { execFile } from "node:child_process";
import { access, readFile, stat } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath } from "node:url";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);
const pluginRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const isWindows = process.platform === "win32";
const home = process.env.USERPROFILE ?? process.env.HOME ?? os.homedir();
const stateDir = path.join(home, ".codex", "state", "codexworker");
const pipeEnv = "CODEX_APP_TOOLS_PIPE_PATH";

const checks = [];
const record = (area, name, status, detail) => checks.push({ area, name, status, detail });

async function exists(target) {
  try { await access(target); return true; } catch { return false; }
}

function ipcPathExample() {
  return isWindows ? "\\\\.\\pipe\\codexworker-<name>-<id>" : path.join(stateDir, "<name>.sock");
}

async function probePipe(pipePath) {
  return new Promise((resolve) => {
    const socket = net.createConnection(pipePath);
    let pending = Buffer.alloc(0);
    let settled = false;
    const done = (result) => {
      if (settled) return;
      settled = true;
      clearTimeout(timer);
      socket.destroy();
      resolve(result);
    };
    const timer = setTimeout(() => done({ ok: false, error: "timeout (5s)" }), 5000);
    socket.once("error", (error) => done({ ok: false, error: error.message }));
    socket.once("connect", () => {
      const payload = Buffer.from(JSON.stringify({ jsonrpc: "2.0", id: 1, method: "tools/list", params: { threadStartKind: "all" } }), "utf8");
      const frame = Buffer.alloc(4 + payload.length);
      frame.writeUInt32LE(payload.length, 0);
      payload.copy(frame, 4);
      socket.write(frame);
    });
    socket.on("data", (chunk) => {
      pending = Buffer.concat([pending, chunk]);
      if (pending.length < 4) return;
      const length = pending.readUInt32LE(0);
      if (pending.length < length + 4) return;
      try {
        const response = JSON.parse(pending.subarray(4, length + 4).toString("utf8"));
        if (response.error) return done({ ok: false, error: response.error.message ?? "tools/list failed" });
        const tools = Array.isArray(response.result?.tools) ? response.result.tools : [];
        done({ ok: true, tools: tools.length, names: tools.map((tool) => tool?.name).filter(Boolean) });
      } catch (error) {
        done({ ok: false, error: error.message });
      }
    });
  });
}

// 1. 运行时
record("runtime", "platform", "info", `${process.platform} / ${process.arch}`);
record("runtime", "node", "info", process.version);
const major = Number(process.version.replace(/^v/, "").split(".")[0]);
record("runtime", "node >= 20", major >= 20 ? "pass" : "fail", major >= 20 ? "ok" : "升级到 Node 20+");

// 2. 通道
record("transport", "IPC 形态", "info", isWindows ? "Windows 命名管道" : "Unix domain socket");
record("transport", "IPC 路径示例", "info", ipcPathExample());
const launchScript = path.join(pluginRoot, "scripts", isWindows ? "launch_codexworker.cmd" : "launch_codexworker.sh");
record("transport", "启动脚本", (await exists(launchScript)) ? "pass" : "fail", launchScript);

// 3. Desktop 注入
const injectedPipe = process.env[pipeEnv]?.trim();
if (injectedPipe) {
  record("desktop", `${pipeEnv}`, "info", injectedPipe);
  const probe = await probePipe(injectedPipe);
  record("desktop", "tools/list", probe.ok ? "pass" : "fail",
    probe.ok ? `${probe.tools} 个工具` : probe.error);
  if (probe.ok && probe.names.length) record("desktop", "工具", "info", probe.names.join(", "));
} else {
  record("desktop", `${pipeEnv}`, "skip", "未注入（非 Desktop 宿主，CLI 模式下正常）");
}

// 4. Desktop 守护要求
if (isWindows) {
  const powershell = path.join(process.env.SystemRoot ?? "C:/Windows", "System32", "WindowsPowerShell", "v1.0", "powershell.exe");
  record("desktop", "powershell", (await exists(powershell)) ? "pass" : "fail", powershell);
  record("desktop", "inventory job 脚本", (await exists(path.join(pluginRoot, "scripts", "launch-desktop-lifetime.ps1"))) ? "pass" : "fail", "launch-desktop-lifetime.ps1");
} else {
  record("desktop", "Desktop 守护", "skip", "非 win32，supervisor 不启用（预期）");
}

// 5. codex CLI
let codexVersion = null;
try {
  const { stdout } = await execFileAsync(isWindows ? "codex.cmd" : "codex", ["--version"], { timeout: 20000, windowsHide: true });
  codexVersion = stdout.trim();
  record("codex", "codex --version", "pass", codexVersion);
} catch (error) {
  try {
    const { stdout } = await execFileAsync("codex", ["--version"], { timeout: 20000, windowsHide: true });
    codexVersion = stdout.trim();
    record("codex", "codex --version", "pass", codexVersion);
  } catch (second) {
    record("codex", "codex --version", "fail", (second.message ?? String(second)).split("\n")[0]);
  }
}
try {
  const { stdout } = await execFileAsync("codex", ["app-server", "--help"], { timeout: 20000, windowsHide: true });
  record("codex", "app-server", stdout.includes("app server") ? "pass" : "warn", "CLI 后端可用");
} catch (error) {
  record("codex", "app-server", "fail", "这个 codex 版本没有 app-server，CLI 后端不可用");
}

// 6. 状态与配置
record("state", "状态目录", "info", stateDir);
record("state", "状态目录可写", (await (async () => {
  try { await stat(path.join(home, ".codex")); return true; } catch { return false; }
})()) ? "pass" : "warn", "首次启动会自动创建");
const configCandidates = [
  path.join(stateDir, "desktop-config.local.json"),
  path.join(home, ".hermes", "secrets", isWindows ? "codexworker.config.json" : "codexworker.config.json"),
];
for (const candidate of configCandidates) {
  record("state", path.basename(candidate), (await exists(candidate)) ? "pass" : "skip", candidate);
}

// 7. 协议参考
const schemaPath = path.join(pluginRoot, "..", "..", "references", "hermes-manager-protocol.schema.json");
record("protocol", "manager schema", (await exists(schemaPath)) ? "pass" : "skip", schemaPath);

const failed = checks.filter((check) => check.status === "fail");
const payload = { ok: failed.length === 0, platform: process.platform, node: process.version, codex: codexVersion, failed: failed.length, checks };

if (process.argv.includes("--json")) {
  process.stdout.write(`${JSON.stringify(payload, null, 2)}\n`);
} else {
  const tag = { pass: "OK  ", fail: "FAIL", warn: "WARN", skip: "SKIP", info: "INFO" };
  let area = "";
  for (const check of checks) {
    if (check.area !== area) { area = check.area; process.stdout.write(`\n[${area}]\n`); }
    process.stdout.write(`  ${tag[check.status] ?? check.status}  ${check.name.padEnd(26)} ${check.detail ?? ""}\n`);
  }
  process.stdout.write(`\n结论：${failed.length === 0 ? "可以运行" : `${failed.length} 项失败`}（${process.platform} / ${process.version}）\n`);
}
process.exitCode = failed.length === 0 ? 0 : 1;
