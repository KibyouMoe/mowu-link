import os from "node:os";
import path from "node:path";

// Both clients must use the same Unix WebSocket daemon; a failed shared connection must never
// fall back to an independent writer that would lock the thread again.
export function appServerConnection(env = process.env) {
  const mode = env.CODEXWORKER_APP_SERVER_MODE?.trim() || "standalone";
  if (!["standalone", "shared"].includes(mode)) throw new Error("invalid_app_server_mode");
  const configuredSocket = env.CODEXWORKER_APP_SERVER_SOCKET?.trim();
  if (mode === "standalone") {
    if (configuredSocket) throw new Error("shared_socket_requires_shared_mode");
    return { mode, args: ["app-server"], socket: null, ownsRuntime: true };
  }
  const home = env.CODEX_HOME?.trim() || path.join(os.homedir(), ".codex");
  if (!path.isAbsolute(home)) throw new Error("app_server_home_must_be_absolute");
  const socket = configuredSocket || path.join(home, "app-server-control", "app-server-control.sock");
  if (!path.isAbsolute(socket)) throw new Error("app_server_socket_must_be_absolute");
  return { mode, args: null, socket, ownsRuntime: false };
}
