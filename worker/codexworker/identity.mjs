// Instance identity.
//
// A bridge is code; an instance is one running deployment of it. The shell name
// ("CodexWorker") may only appear as the `bridge` field and in the repository —
// never as a user-visible title, a health name or a state directory. Every
// instance therefore carries its own identity file:
//
//   { "bridge": "CodexWorker", "name": "OguriCap", "display": "小栗帽",
//     "port": 8905, "configFile": "~/.hermes/secrets/codexworker.config.json" }
//
//   name      internal identifier: state directory, log file, health.name
//   display   what a human sees: Feishu card title, log prefix, health.display
//   bridge    the product this instance runs (the only place the shell name lives)
//
// Resolution order is identity.json -> config.bridgeName -> refuse to start.
// A file that exists but lacks name/display is an error, never a silent
// fall-through: a half-configured instance must not boot under a shell name.
import { readFile } from "node:fs/promises";

export const IDENTITY_REQUIRED_MESSAGE = "缺少实例身份：identity.json 需包含 name 与 display";

// Names that belong to the bridge itself, not to an instance.
const SHELL_NAMES = new Set(["codexworker", "codex-worker", "codex"]);

export class IdentityError extends Error {
  constructor(message, { file = null, field = null } = {}) {
    super(message);
    this.name = "IdentityError";
    this.code = "identity_invalid";
    this.file = file;
    this.field = field;
  }
}

const clean = value => (typeof value === "string" ? value.trim() : "");

function requireText(value, field, { file, source }) {
  const text = clean(value);
  if (!text) throw new IdentityError(`${IDENTITY_REQUIRED_MESSAGE}（${source} 缺少 ${field}）`, { file, field });
  if (SHELL_NAMES.has(text.toLowerCase())) {
    throw new IdentityError(`实例身份不能使用桥的壳名 "${text}"：${field} 必须是这个实例自己的名字`, { file, field });
  }
  return text;
}

function requirePort(value, { file }) {
  if (value == null || value === "") return null;
  const port = Number(value);
  if (!Number.isInteger(port) || port < 0 || port > 65535) {
    throw new IdentityError(`${IDENTITY_REQUIRED_MESSAGE}（identity.json 的 port 不合法：${value}）`, { file, field: "port" });
  }
  return port;
}

// Reads one identity file. A missing file returns null so the caller can move on
// to the next candidate; a present-but-broken file throws (never skipped).
export async function readIdentityFile(file) {
  let raw;
  try { raw = await readFile(file, "utf8"); }
  catch (error) {
    if (error.code === "ENOENT") return null;
    throw new IdentityError(`无法读取 identity 文件 ${file}: ${error.code ?? error.message}`, { file });
  }
  let value;
  try { value = JSON.parse(raw); }
  catch (error) { throw new IdentityError(`identity 文件不是合法 JSON (${file}): ${error.message}`, { file }); }
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new IdentityError(`identity 文件必须是 JSON 对象: ${file}`, { file });
  }
  return { file, value };
}

export function identityFromFile({ file, value }) {
  const source = "identity.json";
  const name = requireText(value.name, "name", { file, source });
  if (/[\\/]/.test(name) || name === "." || name === "..") {
    throw new IdentityError(`identity.json 的 name 不能包含路径分隔符: ${name}`, { file, field: "name" });
  }
  const display = requireText(value.display, "display", { file, source });
  return {
    name,
    display,
    bridge: clean(value.bridge) || "CodexWorker",
    port: requirePort(value.port, { file }),
    configFile: clean(value.configFile) || null,
    source,
    file,
  };
}

// The legacy carrier: bridgeName in the bridge config. It still works so an
// existing deployment keeps booting, but only when it is a real instance name.
export function identityFromConfig(config = {}) {
  const value = clean(config?.bridgeName);
  if (!value || SHELL_NAMES.has(value.toLowerCase())) return null;
  return { name: value, display: value, bridge: clean(config?.bridge) || "CodexWorker",
    port: null, configFile: null, source: "config.bridgeName", file: null };
}

export function resolveIdentity({ identity = null, config = {} } = {}) {
  if (identity) return identityFromFile(identity);
  const fromConfig = identityFromConfig(config);
  if (fromConfig) return fromConfig;
  throw new IdentityError(IDENTITY_REQUIRED_MESSAGE);
}
