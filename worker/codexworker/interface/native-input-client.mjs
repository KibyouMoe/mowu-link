import { lstat, readFile } from "node:fs/promises";
import net from "node:net";

export async function sendNativeInput(args, { env = process.env, timeoutMs = 30000 } = {}) {
  const flag = args.operation === "questionReply" ? "CODEXWORKER_NATIVE_QUESTION_INPUT" : "CODEXWORKER_NATIVE_IMAGE_INPUT";
  if (env[flag] !== "1" || !env.CODEXWORKER_NATIVE_INPUT_DESCRIPTOR)
    throw new Error("native_input_unavailable");
  const file = env.CODEXWORKER_NATIVE_INPUT_DESCRIPTOR, stat = await lstat(file);
  if (!stat.isFile() || stat.isSymbolicLink() || typeof process.getuid === "function" &&
      (stat.uid !== process.getuid() || stat.mode & 0o077)) throw new Error("native_image_descriptor_not_private");
  const descriptor = JSON.parse(await readFile(file, "utf8"));
  if (descriptor.version !== 1 || typeof descriptor.endpoint !== "string" ||
      !/^[0-9a-f]{64}$/.test(descriptor.token ?? "") ||
      descriptor.desktopOwner?.pid !== Number(env.CODEXWORKER_DESKTOP_PID) ||
      descriptor.desktopOwner?.startedAt !== env.RUDOLF_BRIDGE_DESKTOP_STARTED_AT)
    throw new Error("native_image_desktop_owner_mismatch");
  const request = JSON.stringify({ ...args, token: descriptor.token }) + "\n";
  if (Buffer.byteLength(request) > 128 * 1024) throw new Error("native_image_request_too_large");
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(descriptor.endpoint); let bytes = Buffer.alloc(0), settled = false;
    const finish = (error, result) => {
      if (settled) return; settled = true; clearTimeout(timer); socket.destroy();
      if (error) reject(error); else resolve(result);
    };
    const timer = setTimeout(() => finish(new Error("native_image_outcome_unknown")), timeoutMs);
    socket.once("connect", () => socket.write(request));
    socket.once("error", error => finish(error));
    socket.once("close", () => finish(new Error("native_image_outcome_unknown")));
    socket.on("data", chunk => {
      bytes = Buffer.concat([bytes, chunk]);
      if (bytes.length > 65536) { finish(new Error("native_image_response_invalid")); return; }
      const end = bytes.indexOf(10); if (end < 0) return;
      try {
        const response = JSON.parse(bytes.subarray(0, end).toString("utf8"));
        if (response.ok !== true) {
          const code = /^native_[a-z_]+$/.test(response.error ?? "") ? response.error : "native_image_dispatch_failed";
          finish(Object.assign(new Error(code), { nativeErrorCode: code, nativeReachable: true,
            nativeImageOutcome: ["notAttempted", "rejected", "unknown"].includes(response.outcome) ? response.outcome : "unknown" }));
          return;
        }
        finish(null, { success: true, contentItems: [{ type: "text", text: JSON.stringify(response.result) }] });
      } catch (error) { finish(error); }
    });
  });
}
