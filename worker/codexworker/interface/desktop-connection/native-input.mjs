// Typed image input on the existing Desktop-owned connection. Only a saved
// Feishu receipt may authorize a write; this endpoint exposes no generic RPC.
import { createHash, randomBytes, randomUUID, timingSafeEqual } from "node:crypto";
import { chmod, lstat, mkdir, mkdtemp, open, readFile, realpath, rename, rm, unlink, writeFile } from "node:fs/promises";
import net from "node:net";
import path from "node:path";

const MAX_REQUEST_BYTES = 128 * 1024;
const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const digest = value => createHash("sha256").update(value).digest("hex");
const invalid = (code, nativeImageOutcome) => { throw Object.assign(new Error(code), { code, nativeImageOutcome }); };
const equalToken = (left, right) => typeof left === "string" && /^[0-9a-f]{64}$/.test(left) &&
  timingSafeEqual(Buffer.from(left), Buffer.from(right));

export async function createNativeInput({ stateDir, owner, send, timeoutMs = 30000 }) {
  const token = randomBytes(32).toString("hex");
  const descriptorFile = path.join(stateDir, "desktop-native-input.local.json");
  const pending = new Map(), activeMessages = new Map(), threads = new Map(), sockets = new Set();
  let initialized = false, initializeId, closed = false;
  const directory = process.platform === "win32" ? null : await mkdtemp(path.join("/tmp", "cw-input-"));
  if (directory) await chmod(directory, 0o700);
  const endpoint = directory ? path.join(directory, "input.sock") : "\\\\.\\pipe\\cw-input-" + randomUUID();
  const descriptor = { version: 1, endpoint, token, controllerPid: process.pid, desktopOwner: owner };

  function rpc(method, params) {
    if (closed || !initialized) invalid("native_input_not_ready");
    return new Promise((resolve, reject) => {
      const id = "codexworker-input-" + randomUUID();
      const timer = setTimeout(() => {
        pending.delete(id); reject(Object.assign(new Error("native_input_rpc_timeout"), { code: "native_input_rpc_timeout" }));
      }, timeoutMs);
      pending.set(id, { resolve, reject, timer });
      try { send({ id, method, params }); }
      catch (error) { clearTimeout(timer); pending.delete(id); reject(error); }
    });
  }

  async function validate(request) {
    if (!uuid.test(request.messageId ?? "") || typeof request.threadId !== "string" ||
        typeof request.prompt !== "string" || !request.prompt.trim() ||
        !["steer", "queue"].includes(request.delivery) || !Array.isArray(request.images) ||
        request.images.length < 1 || request.images.length > 10) invalid("native_image_request_invalid");
    const config = JSON.parse(await readFile(path.join(stateDir, "desktop-config.local.json"), "utf8"));
    if (config.autoStart === false) invalid("native_image_autostart_disabled");
    const state = JSON.parse(await readFile(path.join(stateDir, "desktop-manager-state.json"), "utf8"));
    const receipt = state.receipts?.[request.messageId], feishu = state.feishuReceipts?.[request.messageId];
    if (!state.sessions?.[request.threadId] || ![undefined, null, "local"].includes(state.sessions[request.threadId].hostId) ||
        state.selectedThreadId !== request.threadId ||
        receipt?.request?.source !== "trainer" || receipt.nativeTargetThreadId !== request.threadId ||
        receipt.nativeInputHash !== digest(JSON.stringify(request.prompt)) || receipt.nativeDispatch?.status !== "dispatching" ||
        !/^om_[A-Za-z0-9_-]+$/.test(feishu?.incomingMessageId ?? "") || feishu.imageThreadId !== request.threadId ||
        JSON.stringify(feishu.nativeImagePaths) !== JSON.stringify(request.images)) invalid("native_image_receipt_mismatch");
    const cache = await realpath(path.join(stateDir, "feishu-images"));
    for (const image of request.images) {
      if (typeof image !== "string" || !path.isAbsolute(image) || path.dirname(await realpath(image)) !== cache)
        invalid("native_image_path_invalid");
      const stat = await lstat(image);
      if (!stat.isFile() || stat.isSymbolicLink() || stat.size < 3 || stat.size > 100_000_000)
        invalid("native_image_file_invalid");
      const file = await open(image, "r");
      try {
        const bytes = Buffer.alloc(12), { bytesRead } = await file.read(bytes, 0, 12, 0);
        const png = bytesRead >= 8 && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
        const jpg = bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255;
        const gif = ["GIF87a", "GIF89a"].includes(bytes.toString("ascii", 0, 6));
        const webp = bytesRead === 12 && bytes.toString("ascii", 0, 4) === "RIFF" && bytes.toString("ascii", 8, 12) === "WEBP";
        if (!png && !jpg && !gif && !webp) invalid("native_image_format_invalid");
      } finally { await file.close(); }
    }
  }

  async function deliver(request, fingerprint) {
    const ledger = path.join(stateDir, "native-image-receipts");
    await mkdir(ledger, { recursive: true, mode: 0o700 });
    const file = path.join(ledger, request.messageId + ".json");
    let previous;
    try { previous = JSON.parse(await readFile(file, "utf8")); }
    catch (error) { if (error.code !== "ENOENT") { error.nativeImageOutcome = "unknown"; throw error; } }
    if (previous) {
      if (previous.fingerprint !== fingerprint) invalid("native_image_message_conflict");
      if (previous.status === "accepted") return previous.result;
      invalid(previous.status === "rejected" ? "native_image_rejected" : "native_image_outcome_unknown",
        previous.status === "rejected" ? "rejected" : "unknown");
    }
    await validate(request);
    if (closed || !initialized) invalid("native_input_not_ready");
    const page = await rpc("thread/turns/list", { threadId: request.threadId, limit: 1,
      sortDirection: "desc", itemsView: "notLoaded" });
    if (!Array.isArray(page?.data)) invalid("native_image_turn_state_invalid");
    const active = page.data.find(turn => turn.status === "inProgress");
    if (active && typeof active.id !== "string") invalid("native_image_turn_state_invalid");
    // Recheck the durable binding after read-only preflight, immediately before
    // recording a possibly accepted write. Uncertain writes are never replayed.
    await validate(request);
    const record = { fingerprint, status: "dispatching" };
    await writeFile(file, JSON.stringify(record) + "\n", { flag: "wx", mode: 0o600 });
    try {
      const params = { threadId: request.threadId, clientUserMessageId: request.messageId,
        input: [{ type: "text", text: request.prompt }, ...request.images.map(image => ({ type: "localImage", path: image }))] };
      const method = active ? request.delivery === "queue" ? "thread/queue/add" : "turn/steer" : "turn/start";
      if (method === "turn/steer") params.expectedTurnId = active.id;
      const response = await rpc(method, params);
      record.result = { threadId: request.threadId, turnId: response?.turnId ?? response?.turn?.id ?? null,
        queued: method === "thread/queue/add" };
      record.status = "accepted";
      return record.result;
    } catch (error) {
      record.status = error.nativeRejected ? "rejected" : "unknown";
      error.nativeImageOutcome = record.status;
      throw error;
    } finally {
      const temporary = file + "." + randomUUID() + ".tmp";
      try {
        await writeFile(temporary, JSON.stringify(record) + "\n", { flag: "wx", mode: 0o600 });
        await rename(temporary, file);
      } catch (error) { error.nativeImageOutcome = "unknown"; throw error; }
    }
  }

  function dispatch(request) {
    if (closed || !initialized) invalid("native_input_not_ready");
    if (!equalToken(request.token, token)) invalid("native_input_unauthorized");
    if (!uuid.test(request.messageId ?? "")) invalid("native_image_request_invalid");
    const fingerprint = digest(JSON.stringify([request.threadId, request.messageId, request.prompt, request.images, request.delivery]));
    const current = activeMessages.get(request.messageId);
    if (current) {
      if (current.fingerprint !== fingerprint) invalid("native_image_message_conflict");
      return current.promise;
    }
    const before = threads.get(request.threadId) ?? Promise.resolve();
    const promise = before.catch(() => {}).then(() => deliver(request, fingerprint));
    activeMessages.set(request.messageId, { fingerprint, promise }); threads.set(request.threadId, promise);
    void promise.finally(() => {
      activeMessages.delete(request.messageId);
      if (threads.get(request.threadId) === promise) threads.delete(request.threadId);
    }).catch(() => {});
    return promise;
  }

  const server = net.createServer(socket => {
    sockets.add(socket); socket.once("close", () => sockets.delete(socket)); socket.on("error", () => {});
    socket.setTimeout(timeoutMs + 1000, () => socket.destroy());
    let bytes = Buffer.alloc(0), received = false;
    socket.on("data", chunk => {
      if (received) return;
      bytes = Buffer.concat([bytes, chunk]);
      if (bytes.length > MAX_REQUEST_BYTES) { received = true; socket.destroy(); return; }
      const end = bytes.indexOf(10); if (end < 0) return;
      received = true;
      void Promise.resolve().then(() => dispatch(JSON.parse(bytes.subarray(0, end).toString("utf8"))))
        .then(result => socket.end(JSON.stringify({ ok: true, result }) + "\n"),
          error => socket.end(JSON.stringify({ ok: false, error: /^native_[a-z_]+$/.test(error.code ?? "")
            ? error.code : "native_image_dispatch_failed", outcome: error.nativeImageOutcome ?? "notAttempted" }) + "\n"));
    });
  });
  try {
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(endpoint, resolve); });
    if (directory) await chmod(endpoint, 0o600);
    await mkdir(stateDir, { recursive: true, mode: 0o700 });
    const temporary = descriptorFile + "." + randomUUID() + ".tmp";
    await writeFile(temporary, JSON.stringify(descriptor) + "\n", { flag: "wx", mode: 0o600 });
    await rename(temporary, descriptorFile);
  } catch (error) {
    server.close(); if (directory) await rm(directory, { recursive: true, force: true }); throw error;
  }

  return {
    env: { CODEXWORKER_NATIVE_IMAGE_INPUT: "1", CODEXWORKER_NATIVE_INPUT_DESCRIPTOR: descriptorFile },
    client(message) {
      if (message?.method === "initialize") initializeId = message.id;
      if (message?.method === "initialized") initialized = true;
    },
    server(message) {
      if (initializeId !== undefined && message?.id === initializeId && message.result) initialized = true;
      const call = pending.get(message?.id); if (!call) return true;
      pending.delete(message.id); clearTimeout(call.timer);
      if (message.error) call.reject(Object.assign(new Error("native_image_rejected"),
        { code: "native_image_rejected", nativeRejected: true }));
      else call.resolve(message.result);
      return false;
    },
    async close() {
      if (closed) return; closed = true;
      for (const call of pending.values()) { clearTimeout(call.timer); call.reject(new Error("native_input_closed")); }
      pending.clear(); for (const socket of sockets) socket.destroy();
      await new Promise(resolve => server.close(resolve));
      await Promise.allSettled([...activeMessages.values()].map(value => value.promise));
      try { const value = JSON.parse(await readFile(descriptorFile, "utf8")); if (value.token === token) await unlink(descriptorFile); }
      catch (error) { if (error.code !== "ENOENT") throw error; }
      if (directory) await rm(directory, { recursive: true, force: true });
    },
  };
}
