// Reloadable business leaf. Keep imports builtin-only: the runtime snapshots this
// module by content hash, without replacing the manager, native host or WS reader.
import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, readFile, realpath, rename, writeFile } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { setTimeout as delay } from "node:timers/promises";

export const API_VERSION = 1;
export const FEATURE_VERSION = "0.2.17";
export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
// Feishu documents UUID deduplication for one hour. Keep a ten-minute margin,
// measured from the FIRST possible message POST, never from the latest retry.
export const SEND_DEDUP_WINDOW_MS = 50 * 60 * 1000;
const MAX_ATTEMPTS = 3;
const API = "https://open.feishu.cn/open-apis";
const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const sha = bytes => createHash("sha256").update(bytes).digest("hex");
export class ImageOutputError extends Error {
  constructor(status, error, detail, retryable = false, extra = {}) {
    super(detail); this.name = "ImageOutputError"; this.status = status;
    this.body = { error, detail, retryable, ...extra };
  }
}
const fail = (status, error, detail, extra) => { throw new ImageOutputError(status, error, detail, false, extra); };
const inside = (root, file) => { const relative = path.relative(root, file); return relative === "" || (!relative.startsWith(".." + path.sep) && relative !== ".." && !path.isAbsolute(relative)); };

export function imageType(bytes) {
  if (bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) return { ext: "png", mime: "image/png" };
  if (bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) return { ext: "jpg", mime: "image/jpeg" };
  if (["GIF87a", "GIF89a"].includes(bytes.subarray(0, 6).toString("ascii"))) return { ext: "gif", mime: "image/gif" };
  if (bytes.subarray(0, 4).toString("ascii") === "RIFF" && bytes.subarray(8, 12).toString("ascii") === "WEBP") return { ext: "webp", mime: "image/webp" };
  fail(422, "invalid_image", "Only PNG, JPEG, GIF or WebP raster files are accepted.");
}

async function loadImage(filename, roots, displayedFiles = [], excludedRoots = []) {
  const network = value => /^(?:\\\\|\/\/)/.test(value);
  if (typeof filename !== "string" || !path.isAbsolute(filename) || network(filename)) fail(400, "invalid_request", "imagePath must be an absolute local path, not a URL or network path.");
  let resolved;
  try { resolved = await realpath(filename); }
  catch { fail(404, "image_not_found", "The local image does not exist."); }
  if (network(resolved)) fail(403, "image_path_forbidden", "Remote filesystem images must be handled by their own Bridge.");
  for (const root of excludedRoots) {
    let excluded; try { excluded = await realpath(root); } catch { continue; }
    if (inside(excluded, resolved)) fail(403, "image_input_excluded", "Input image cache is not an automatic output.");
  }
  const allowed = [];
  for (const root of roots) { try { allowed.push(await realpath(root)); } catch { /* nonexistent roots grant nothing */ } }
  // An internal public-display grant authorizes exactly this file, never its
  // parent directory. HTTP/MCP request fields cannot manufacture these grants.
  let displayed = false;
  for (const filename of displayedFiles) {
    try { if (await realpath(filename) === resolved) displayed = true; } catch { /* disappeared public file */ }
  }
  if (!displayed && !allowed.some(root => inside(root, resolved))) fail(403, "image_path_forbidden", "Image must be in an allowed workspace/cache or an authorized public display.");
  const file = await open(resolved, "r");
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size === 0 || stat.size > MAX_IMAGE_BYTES) fail(422, "invalid_image", "Image must be a regular file of at most 10MiB.");
    const bytes = await file.readFile();
    if (bytes.length !== stat.size || bytes.length > MAX_IMAGE_BYTES) fail(422, "invalid_image", "Image changed while reading; select a stable file.");
    return { bytes, ...imageType(bytes) };
  } finally { await file.close(); }
}

async function lease(stateDir) {
  const key = sha(path.resolve(stateDir)).slice(0, 24);
  const socketPath = process.platform === "win32" ? `\\\\.\\pipe\\codexworker-feishu-image-output-${key}` : path.join(stateDir, "image-output.sock");
  const server = net.createServer(socket => socket.destroy());
  try {
    await new Promise((resolve, reject) => { server.once("error", reject); server.listen(socketPath, resolve); });
  } catch {
    throw new ImageOutputError(409, "image_output_busy", "An image send is already in progress; wait and retry the same messageId.", true);
  }
  return () => new Promise(resolve => server.close(resolve));
}

const clock = context => context.now?.() ?? Date.now();
const diagnostic = (error, stage) => ({ stage,
  errorName: ["Error", "TypeError", "SyntaxError", "TimeoutError", "AbortError", "ImageOutputError"].includes(error?.name) ? error.name : "Error",
  causeCode: /^(?:ECONNRESET|ECONNREFUSED|ETIMEDOUT|ENOTFOUND|EAI_AGAIN|EPIPE|UND_ERR_[A-Z_]+)$/.test(error?.cause?.code ?? error?.code ?? "")
    ? (error.cause?.code ?? error.code) : null });
const withinWindow = (receipt, context) => {
  const elapsed = clock(context) - Date.parse(receipt.firstSendAt);
  return Number.isFinite(elapsed) && elapsed >= 0 && elapsed < SEND_DEDUP_WINDOW_MS;
};

async function apiRequest(url, body, context, target, stage, beforePost) {
  for (let attempt = 0; attempt < 2; attempt++) {
    let token;
    try { token = await context.notifier.getToken(); }
    catch (error) { throw new ImageOutputError(503, "feishu_authorization_failed", "Cannot obtain Feishu authorization.", true, diagnostic(error, "authorization")); }
    if (!token) fail(503, "feishu_unavailable", "Feishu authorization is unavailable.");
    await beforePost?.();
    if (target) context.validateTarget?.(target);
    let response, result;
    try {
      response = await context.fetchImpl(API + url, { method: "POST", redirect: "error",
        headers: { authorization: `Bearer ${token}`, ...(typeof body === "string" ? { "content-type": "application/json" } : {}) },
        body, signal: AbortSignal.timeout(30000) });
      result = await response.json();
    } catch (error) {
      throw new ImageOutputError(502, "feishu_transport_failed", "Feishu returned no usable response.", true,
        { ...diagnostic(error, stage), upstreamHttpStatus: response?.status ?? null });
    }
    if ([99991663, 99991664, 401].includes(result?.code) && attempt === 0) {
      context.notifier.invalidateToken(); continue;
    }
    if (!response.ok || result?.code !== 0) throw new ImageOutputError(502, "feishu_api_rejected", "Feishu explicitly rejected the image operation.",
      response.status === 429 || (response.status >= 500 && (result?.code === 0 || result?.code == null)),
      { stage, upstreamHttpStatus: response.status ?? null, feishuCode: typeof result?.code === "number" ? result.code : null });
    return result.data;
  }
}

export async function sendImage(request, context) {
  const messageId = request.messageId ?? randomUUID();
  if (!UUID4.test(messageId)) fail(400, "invalid_request", "messageId must be UUIDv4.");
  const target = await context.getTarget(request.threadId);
  const image = await loadImage(request.imagePath, context.allowedRoots, context.displayedFiles, context.excludedRoots);
  const fingerprint = sha(JSON.stringify({ imageSha256: sha(image.bytes), threadId: target.threadId, chatId: target.chatId }));
  await mkdir(context.stateDir, { recursive: true });
  const release = await lease(context.stateDir);
  const filename = path.join(context.stateDir, "image-output-state.json");
  let ledger = { version: 1, receipts: {} };
  const save = async () => {
    const temporary = `${filename}.${randomUUID()}.tmp`;
    await writeFile(temporary, JSON.stringify(ledger), { mode: 0o600 });
    await rename(temporary, filename);
  };
  try {
    try { ledger = JSON.parse(await readFile(filename, "utf8")); }
    catch (error) { if (error.code !== "ENOENT") fail(503, "image_state_invalid", "Cannot read image receipts; no image was sent."); }
    if (ledger.version !== 1 || !ledger.receipts || typeof ledger.receipts !== "object") fail(503, "image_state_invalid", "Unsupported image receipt format; no image was sent.");
    const previous = ledger.receipts[messageId];
    const duplicate = Boolean(previous);
    if (previous) {
      if (previous.fingerprint !== fingerprint) fail(409, "message_id_conflict", "messageId already belongs to different image bytes or target.", { messageId });
      if (previous.status === "sent") return { ...previous.result, duplicate: true };
      // Legacy receipts have neither a durable phase nor the uploaded image key.
      // Ordinary/automatic retries remain frozen. Only the authenticated explicit
      // recovery route may re-upload identical bytes with the ORIGINAL UUID, and
      // only inside the conservative dedup window counted from legacy createdAt.
      if (!previous.recoveryVersion && context.recoveryRequested && previous.status === "unknown") {
        const legacy = { ...previous, firstSendAt: previous.createdAt };
        if (!withinWindow(legacy, context)) fail(409, "image_recovery_window_expired", "Original UUID deduplication window expired; no message was repeated.", { messageId });
        Object.assign(previous, { recoveryVersion: 1, stage: "upload", firstSendAt: previous.createdAt,
          legacyRecovery: true, previousFailure: previous.error, uploadAttempts: 0, sendAttempts: 0 });
        delete previous.error;
      } else if (!previous.recoveryVersion || previous.status === "failed") {
        if (previous.error) throw new ImageOutputError(previous.httpStatus ?? 502, previous.error.error, previous.error.detail, false, { ...previous.error, retryable: false, messageId, duplicate: true });
        fail(409, "image_send_outcome_unknown", "Legacy operation has no safe recovery phase; it was not repeated.", { messageId, duplicate: true });
      }
      if (previous.firstSendAt && !withinWindow(previous, context)) fail(409, "image_recovery_window_expired", "Original UUID deduplication window expired; no message was repeated.", { messageId, duplicate: true });
      if (!context.recoveryRequested && (previous.stage === "upload" ? previous.uploadAttempts : previous.sendAttempts) >= MAX_ATTEMPTS) {
        throw new ImageOutputError(previous.httpStatus ?? 502, previous.error?.error ?? "image_send_outcome_unknown",
          previous.error?.detail ?? "Bounded retries exhausted; no further message was repeated.", false, { ...previous.error, retryable: false, messageId, duplicate: true });
      }
    }
    if (!context.notifier?.enabled) fail(503, "feishu_unavailable", "Feishu image output is not configured.");
    const receipt = previous ?? { fingerprint, status: "uploading", stage: "upload", recoveryVersion: 1,
      createdAt: new Date(clock(context)).toISOString(), threadId: target.threadId, uploadAttempts: 0, sendAttempts: 0 };
    ledger.receipts[messageId] = receipt; await save();
    try {
      const checkTarget = async () => {
        const current = await context.getTarget(target.threadId);
        if (current.threadId !== target.threadId || current.chatId !== target.chatId) fail(409, "selected_target_changed", "Feishu-selected chat changed; image was not sent to another target.");
        context.validateTarget?.(target);
        if (receipt.firstSendAt && !withinWindow(receipt, context)) fail(409, "image_recovery_window_expired", "Original UUID deduplication window expired; no message was repeated.");
      };
      const bounded = async (stage, operation) => {
        const counter = stage === "upload" ? "uploadAttempts" : "sendAttempts";
        const limit = context.recoveryRequested ? receipt[counter] + MAX_ATTEMPTS : MAX_ATTEMPTS;
        while (receipt[counter] < limit) {
          await checkTarget();
          receipt.stage = stage; receipt.status = stage === "upload" ? "uploading" : "sending";
          if (stage === "message") receipt.firstSendAt ??= new Date(clock(context)).toISOString();
          receipt[counter]++; await save(); // durable BEFORE any possible POST
          try { return await operation(); }
          catch (error) {
            receipt.lastFailure = { ...diagnostic(error, stage), ...error.body, retryable: Boolean(error.body?.retryable) };
            await save();
            if (!error.body?.retryable || receipt[counter] >= limit) throw error;
            await (context.retryWait ?? delay)(receipt[counter] === 1 ? 250 : 1000);
            // Target/ABA/deadline are checked again after backoff and token fetch.
          }
        }
        fail(409, "image_send_outcome_unknown", "Bounded retries exhausted; no further message was repeated.");
      };
      if (!receipt.imageKey) {
        const uploaded = await bounded("upload", async () => {
          const form = new FormData(); form.set("image_type", "message");
          form.set("image", new Blob([image.bytes], { type: image.mime }), `bridge-image.${image.ext}`);
          const result = await apiRequest("/im/v1/images", form, context, target, "upload", checkTarget);
          if (!result?.image_key) throw new ImageOutputError(502, "feishu_invalid_response", "Feishu upload returned no image key; no message was sent.", true, { stage: "upload" });
          return result;
        });
        receipt.imageKey = uploaded.image_key; receipt.stage = "message"; receipt.status = "uploaded"; await save();
      }
      const body = JSON.stringify({ receive_id: target.chatId, msg_type: "image", content: JSON.stringify({ image_key: receipt.imageKey }), uuid: messageId });
      const sent = await bounded("message", async () => {
        const result = await apiRequest("/im/v1/messages?receive_id_type=chat_id", body, context, target, "message", checkTarget);
        if (!result?.message_id) throw new ImageOutputError(502, "feishu_invalid_response", "Feishu message response has no receipt.", true, { stage: "message" });
        return result;
      });
      receipt.status = "sent"; receipt.stage = "complete"; receipt.sentAt = new Date(clock(context)).toISOString();
      receipt.result = { messageId, threadId: target.threadId, status: "sent", feishuMessageId: sent.message_id,
        imageBytes: image.bytes.length, imageSha256: sha(image.bytes), duplicate,
        uploadAttempts: receipt.uploadAttempts, sendAttempts: receipt.sendAttempts, recovered: duplicate || receipt.uploadAttempts > 1 || receipt.sendAttempts > 1 };
      delete receipt.error;
      await save(); return receipt.result;
    } catch (error) {
      if (receipt.result?.feishuMessageId) {
        // A received server receipt is NOT a failed network send. Retry only the
        // local commit; never POST again in this invocation after save failure.
        receipt.status = "sent";
        try { await save(); return receipt.result; }
        catch { throw new ImageOutputError(503, "image_state_write_failed", "Feishu accepted the image but its local receipt could not be saved. Keep the original UUID.", false,
          { messageId, stage: "receipt", feishuMessageId: receipt.result.feishuMessageId }); }
      }
      const uncertain = receipt.stage === "message" && error.body?.retryable;
      const mapped = error?.body && !error.body.retryable ? error : new ImageOutputError(502,
        receipt.firstSendAt ? "image_send_outcome_unknown" : "image_upload_failed",
        receipt.firstSendAt ? "Same-UUID recovery attempts exhausted; delivery is unconfirmed. No new UUID was sent." : "Image upload failed after bounded retries; no message was sent.",
        false, { ...diagnostic(error, receipt.stage), upstreamHttpStatus: error.body?.upstreamHttpStatus ?? null,
          feishuCode: error.body?.feishuCode ?? null });
      receipt.status = uncertain || (!error.body && receipt.firstSendAt) ? "unknown" : "failed";
      receipt.error = { ...mapped.body, error: uncertain ? "image_send_outcome_unknown" : mapped.body.error,
        retryable: false, messageId, threadId: target.threadId, stage: receipt.stage,
        uploadAttempts: receipt.uploadAttempts, sendAttempts: receipt.sendAttempts };
      receipt.httpStatus = mapped.status ?? 409;
      try { await save(); } catch { throw new ImageOutputError(503, "image_state_write_failed", "Cannot persist image receipt. Keep the original UUID; do not create a replacement send.", false,
        { messageId, stage: receipt.stage }); }
      throw new ImageOutputError(receipt.httpStatus, receipt.error.error, receipt.error.detail, false, receipt.error);
    }
  } finally { await release(); }
}
