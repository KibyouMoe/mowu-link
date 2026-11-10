// One authorized incident prompt to the existing maintenance chat. No model API,
// no native listener, no task creation and no retry of an uncertain native write.
import net from "node:net";
let nextId = 1;
export function pipeRequest(pipe, method, params, timeoutMs = 10000) {
  return new Promise((resolve, reject) => {
    const id = nextId++, socket = net.createConnection(pipe);
    let bytes = Buffer.alloc(0), settled = false;
    const finish = (fn, value) => {
      if (settled) return; settled = true; clearTimeout(timer); socket.destroy(); fn(value);
    };
    const timer = setTimeout(() => finish(reject, new Error("native_outcome_unknown")), timeoutMs);
    socket.once("error", () => finish(reject, new Error("native_unavailable")));
    socket.once("connect", () => {
      const payload = Buffer.from(JSON.stringify({ jsonrpc: "2.0", id, method, params }));
      const frame = Buffer.alloc(payload.length + 4); frame.writeUInt32LE(payload.length); payload.copy(frame, 4); socket.write(frame);
    });
    socket.on("data", chunk => {
      bytes = Buffer.concat([bytes, chunk]);
      if (bytes.length < 4) return;
      const length = bytes.readUInt32LE(0);
      if (length > 8 * 1024 * 1024) return finish(reject, new Error("native_response_too_large"));
      if (bytes.length < length + 4) return;
      try {
        const response = JSON.parse(bytes.subarray(4, length + 4));
        if (response.id !== id) return finish(reject, new Error("native_response_mismatch"));
        finish(resolve, response.error ? { success: false } : response.result);
      } catch { finish(reject, new Error("native_response_invalid")); }
    });
    socket.once("end", () => finish(reject, new Error("native_outcome_unknown")));
  });
}
export async function escalateIncident({ threadId, incident, prompt, pipe = process.env.CODEX_APP_TOOLS_PIPE_PATH, request = pipeRequest }) {
  if (!pipe || !/^[a-f\d-]{36}$/i.test(threadId ?? "")) return "unavailable";
  let tools;
  try { tools = (await request(pipe, "tools/list", { threadStartKind: "all" })).tools; }
  catch { return "unavailable"; }
  const tool = tools?.find(item => item.name === "send_message_to_thread");
  if (!tool?.namespace) return "unavailable";
  try {
    const response = await request(pipe, "tools/call", { arguments: { threadId, prompt },
      callerSource: "codex", callId: incident.alert.callId, namespace: tool.namespace,
      threadId, tool: tool.name, turnId: incident.alert.callId });
    return response?.success === true ? "sent" : "rejected";
  } catch { return "unknown"; }
}
