// NativeTransport · 选择器
//
// 业务层（mcp-business.mjs / desktop-manager.mjs）只认这两个方法：
//   listTools() -> [{ name, namespace }]
//   callTool(name, args, invocation, timeoutMs) -> { success, contentItems }
//
// 实现按环境选择，默认保持原行为（Desktop 注入管道）：
//   CODEXWORKER_NATIVE_TRANSPORT=desktop-pipe   显式指定
//   CODEXWORKER_NATIVE_TRANSPORT=app-server     CLI 后端（codex app-server JSON-RPC）
//   CODEXWORKER_NATIVE_TRANSPORT=dsh            DSH 后端（dsh --profile headless --json）
//   未设置                                      有 CODEX_APP_TOOLS_PIPE_PATH 就用它
import * as appServer from "./app-server.mjs";
import * as desktopPipe from "./desktop-pipe.mjs";
import * as dsh from "./dsh.mjs";

const IMPLEMENTATIONS = {
  [desktopPipe.name]: desktopPipe,
  [appServer.name]: appServer,
  [dsh.name]: dsh,
};

let cached = null;

export function resolveNativeTransport({ refresh = false } = {}) {
  if (cached && !refresh) return cached;
  const requested = process.env.CODEXWORKER_NATIVE_TRANSPORT?.trim();
  if (requested) {
    const implementation = IMPLEMENTATIONS[requested];
    if (!implementation) {
      throw new Error(
        `Unknown CODEXWORKER_NATIVE_TRANSPORT "${requested}"; available: ${Object.keys(IMPLEMENTATIONS).join(", ")}`,
      );
    }
    cached = implementation;
    return cached;
  }
  // 默认：沿用 Desktop 注入管道；不可用时抛错文案与改造前一致。
  cached = desktopPipe;
  return cached;
}

export function resetNativeTransportCache() {
  cached = null;
}

// 业务层只读这里的能力开关；未声明的能力一律按 Desktop 兼容语义（true）处理。
export function resolveNativeCapabilities() {
  const declared = resolveNativeTransport().capabilities ?? {};
  return {
    fork: declared.fork !== false,
    desktopJournal: declared.desktopJournal !== false,
    callerTurn: declared.callerTurn !== false,
  };
}
