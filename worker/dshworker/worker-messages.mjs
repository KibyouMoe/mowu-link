// Ordinary peer chat only. The router/client owner supplies SSE and polling;
// this service never submits tasks, creates goals, or automatically replies.
import { createHash, randomUUID } from "node:crypto";
import { mkdir, open, readFile, rename } from "node:fs/promises";
import path from "node:path";
import { PROTOCOL } from "./desktop-manager.mjs";
import { imageType, MAX_ROUTER_IMAGE_BYTES } from "./router-attachments.mjs";
import { localHost } from "./interface/native-dispatch.mjs";

const owners = new WeakMap();
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const UUID4 = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
const IMAGE_ID = /^[0-9a-f]{64}$/;
const extensions = { "image/png": ".png", "image/jpeg": ".jpg", "image/gif": ".gif", "image/webp": ".webp" };
const digest = value => createHash("sha256").update(value).digest("hex");
const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);
const copy = value => structuredClone(value);

function fail(code, detail, messageId, outcome) {
  throw Object.assign(new Error(detail), { code, ...(messageId ? { messageId, retrySameMessageId: true } : {}),
    ...(outcome ? { outcome } : {}) });
}
function uuid(value) {
  if (typeof value !== "string" || !UUID.test(value))
    fail("invalid_message_id", "An explicit UUID messageId is required; reuse it for retries.");
  return value.toLowerCase();
}
function nativeUUID(id) {
  if (UUID4.test(id)) return id;
  // The router accepts all UUID versions; DesktopManager accepts only v4.
  // Keep the original peer UUID in metadata, prompts, disk keys and router ACKs.
  // Only the manager receipt key is adapted, deterministically across restarts.
  const h = digest("worker-chat-native-message:" + id);
  return h.slice(0, 8) + "-" + h.slice(8, 12) + "-4" + h.slice(13, 16) + "-" +
    ((parseInt(h[16], 16) & 3) | 8).toString(16) + h.slice(17, 20) + "-" + h.slice(20, 32);
}
function workerName(value) {
  if (typeof value !== "string" || !value.isWellFormed() || !/^[^\s\[\]\x00-\x1f\x7f]{1,128}$/u.test(value))
    fail("invalid_worker", "Use a worker name of 1..128 characters without whitespace, controls or brackets.");
  return value;
}
function textBody(value) {
  if (typeof value !== "string" || !value.trim() || !value.isWellFormed() || [...value].length > 20_000)
    fail("invalid_text", "text must be nonempty, valid Unicode and at most 20000 characters.");
  return value;
}
function displayName(value) {
  if (value == null) return undefined;
  if (typeof value !== "string" || !value.isWellFormed() || [...value].length > 200)
    fail("invalid_attachment", "Attachment names must be valid Unicode of at most 200 characters.");
  return value.replace(/\\/g, "/").split("/").at(-1).replace(/[\x00-\x1f\x7f]/g, " ");
}
function attachments(values = [], incoming = false) {
  if (!Array.isArray(values) || values.length > 10) fail("invalid_attachment", "Use at most 10 existing uploaded image IDs.");
  return values.map(value => {
    if (typeof value === "string") value = { id: value };
    if (!value || typeof value !== "object" || !IMAGE_ID.test(value.id ?? "") ||
        (!incoming && Object.keys(value).some(key => !["id", "name"].includes(key))))
      fail("invalid_attachment", "Attachments accept existing image IDs and display names only.");
    const name = displayName(value.name), ref = { id: value.id, ...(name !== undefined ? { name } : {}) };
    if (!incoming) return ref;
    if (value.sha256 !== value.id || !Number.isSafeInteger(value.size) || value.size < 1 ||
        value.size > MAX_ROUTER_IMAGE_BYTES || !Object.hasOwn(extensions, value.media_type))
      fail("invalid_attachment", "Incoming image metadata must contain matching SHA256, size and image type.");
    // Never preserve supplied URLs or paths (including another machine's paths).
    return { ...ref, sha256: value.sha256, size: value.size, media_type: value.media_type };
  });
}
function incoming(value, worker) {
  value = value?.message ?? value;
  if (value?.to !== worker) fail("wrong_recipient", "The peer message is not addressed to this worker.");
  const from = workerName(value.from), id = uuid(value.message_id);
  if (from === worker) fail("invalid_worker", "Peer messages must come from another worker.", id);
  return { message_id: id, from, to: worker, text: textBody(value.text),
    reply_to: value.reply_to == null ? null : uuid(value.reply_to), attachments: attachments(value.attachments, true) };
}
async function json(file) {
  try { return JSON.parse(await readFile(file, "utf8")); }
  catch (error) { if (error.code === "ENOENT") return null; throw error; }
}
async function atomic(file, value) {
  await mkdir(path.dirname(file), { recursive: true, mode: 0o700 });
  const temp = `${file}.${randomUUID()}.tmp`, handle = await open(temp, "wx", 0o600);
  try { await handle.writeFile(JSON.stringify(value) + "\n"); await handle.sync(); }
  finally { await handle.close(); }
  // Windows readers/antivirus may briefly hold the previous receipt open.
  for (let attempt = 0; ; attempt++) {
    try { await rename(temp, file); break; }
    catch (error) {
      if (!["EPERM", "EBUSY", "EACCES"].includes(error.code) || attempt >= 7) throw error;
      await new Promise(resolve => setTimeout(resolve, 20 * (attempt + 1)));
    }
  }
}
function marker(message, outgoing = false) {
  return `[worker ${message.from}${outgoing ? " -> " + message.to : ""} message ${message.message_id}` +
    (message.reply_to ? " reply_to " + message.reply_to : "") + "]";
}
function routerReceipt(result, message) {
  if (result?.ok !== true || result.message_id !== message.message_id || !["queued", "accepted"].includes(result.status) ||
      !result.message || result.message.from !== message.from || result.message.to !== message.to || result.message.text !== message.text ||
      (result.message.reply_to ?? null) !== (message.reply_to ?? null))
    fail("invalid_router_receipt", "Router acceptance is unconfirmed; retry only with the same messageId.", message.message_id, "unknown");
  return result;
}

// send({messageId, to, text, replyTo?/reply_to?, attachments?: [{id,name?}]})
// receive takes the worker_message SSE payload (or a REST {message} receipt).
// inbox is read-only; optional {after,limit} pages only this worker's inbox.
export function startWorkerMessaging({ manager, client, worker, log = () => {} } = {}) {
  worker = workerName(worker);
  if (!manager?.stateDir || !manager.state || typeof manager.message !== "function" ||
      typeof manager.mirrorInput !== "function" || typeof manager.publicReply !== "function" ||
      typeof client?._request !== "function" || (client.name != null && client.name !== worker))
    fail("invalid_messaging_owner", "A manager, its authenticated router client, and matching worker identity are required.");
  const prior = owners.get(manager);
  if (prior) {
    if (prior.client !== client || prior.worker !== worker) fail("messaging_owner_conflict", "Close the previous peer messaging owner first.");
    return prior.service;
  }
  const root = path.resolve(manager.stateDir, "worker-messages", digest(worker));
  const abort = new AbortController(), stopped = Object.assign(new Error("worker_messaging_closed"), { code: "worker_messaging_closed" });
  let closed = false, pending = 0, tail = Promise.resolve(), closing;
  const check = () => { if (closed || manager.closed || manager.handoffPaused || manager.proxy) throw stopped; };
  const note = (level, event, fields) => { try { log(level, event, fields); } catch {} };
  // Stop observing native writes on close, never cancel or repeat their effects.
  function observe(operation) {
    check();
    return new Promise((resolve, reject) => {
      const cancel = () => reject(stopped);
      abort.signal.addEventListener("abort", cancel, { once: true });
      Promise.resolve().then(() => { check(); return operation(); }).then(resolve, reject)
        .finally(() => abort.signal.removeEventListener("abort", cancel));
    });
  }
  function enqueue(operation) {
    if (closed) return Promise.reject(stopped);
    pending++;
    const running = tail.then(() => { check(); return operation(); }).finally(() => { pending--; });
    tail = running.catch(() => {});
    return running;
  }
  const file = (direction, id) => path.join(root, direction, id + ".json");
  async function save(direction, record) { check(); await atomic(file(direction, record.payload.message_id), record); }
  async function load(direction, payload) {
    const old = await json(file(direction, payload.message_id));
    if (old && (old.version !== 1 || !same(old.payload, payload)))
      fail("message_id_conflict", "This messageId already belongs to different peer message content.", payload.message_id);
    return old;
  }
  async function knownReceipt(id) {
    return manager.state.receipts?.[id] ?? await observe(() => manager.receiptArchive?.get(id));
  }
  async function durableReceipt(id) {
    // An in-memory accepted flag can survive a failed save; it is not an ACK.
    const state = await json(manager.statePath ?? path.join(manager.stateDir, "desktop-manager-state.json"));
    return { receipt: state?.receipts?.[id] ?? await observe(() => manager.receiptArchive?.get(id)), state };
  }
  function target(previous) {
    const id = previous?.request?.targetThreadId ?? previous?.nativeTargetThreadId ??
      previous?.nativeDispatch?.threadId ?? manager.state.selectedThreadId;
    if (!id || !manager.state.sessions?.[id]) fail("no_selected_session", "Peer chat requires an existing registered native session.");
    return id;
  }

  // router-attachments currently exports upload/signature helpers, but no byte
  // downloader. Keep its format/size rules and fetch only the configured router's
  // content-addressed resource, ignoring all supplied paths. No new upload API.
  async function image(ref) {
    const filename = path.join(root, "images", ref.id + extensions[ref.media_type]);
    const verify = bytes => {
      if (bytes.length !== ref.size || digest(bytes) !== ref.id || imageType(bytes) !== ref.media_type)
        fail("attachment_mismatch", "Router image bytes do not match the attachment receipt.");
      return bytes;
    };
    try { verify(await readFile(filename)); return filename; }
    catch (error) { if (error.code !== "ENOENT") throw error; }
    const base = new URL(client.url);
    if (!["http:", "https:"].includes(base.protocol) || base.username || base.password || base.search || base.hash ||
        typeof client.headers !== "function") fail("invalid_attachment_origin", "Use the configured authenticated router origin.");
    const response = await observe(() => fetch(client.url.replace(/\/+$/, "") + "/attachments/" + ref.id,
      { headers: client.headers(), redirect: "error", signal: AbortSignal.any([abort.signal, AbortSignal.timeout(120000)]) }));
    if (!response.ok || response.headers.get("x-attachment-sha256") !== ref.id ||
        response.headers.get("content-type")?.split(";")[0].trim() !== ref.media_type ||
        Number(response.headers.get("content-length")) !== ref.size || !response.body?.getReader) {
      await response.body?.cancel().catch(() => {});
      fail("attachment_mismatch", "Router image download has no matching size, SHA256 or image type.");
    }
    const reader = response.body.getReader(), chunks = []; let size = 0;
    try {
      for (;;) {
        const { done, value } = await observe(() => reader.read());
        if (done) break;
        size += value.length;
        if (size > ref.size || size > MAX_ROUTER_IMAGE_BYTES) fail("attachment_mismatch", "Router image exceeds its declared size.");
        chunks.push(Buffer.from(value));
      }
    } catch (error) { await reader.cancel().catch(() => {}); throw error; }
    finally { reader.releaseLock(); }
    const bytes = verify(Buffer.concat(chunks, size));
    check(); await mkdir(path.dirname(filename), { recursive: true, mode: 0o700 });
    const temp = filename + "." + randomUUID() + ".tmp", handle = await open(temp, "wx", 0o600);
    try { await handle.writeFile(bytes); await handle.sync(); } finally { await handle.close(); }
    await rename(temp, filename);
    return filename;
  }

  async function mirrorSent(record) {
    const key = `output:${record.targetThreadId}:worker-message-sent:${record.payload.message_id}`;
    if (["dispatching", "unknown"].includes(record.mirror)) {
      record.mirror = manager.state.latestTranscript?.[key] ? "accepted" : "unknown";
      await save("sent", record);
      return; // An uncertain public write must not be blindly replayed.
    }
    if (record.mirror === "accepted") return;
    if (!record.targetThreadId || !manager.state.sessions?.[record.targetThreadId]) return;
    record.mirror = "dispatching"; await save("sent", record);
    try {
      const accepted = await observe(() => manager.publicReply({ messageId: record.payload.message_id,
        targetThreadId: record.targetThreadId, source: "bridge", origin: "worker-chat", workerName: worker,
        peerMessageId: record.payload.message_id, reply_to: record.payload.reply_to ?? null },
      marker(record.payload, true) + "\n" + record.payload.text +
        (record.payload.attachments?.length ? "\nImages: " + record.payload.attachments.map(a => a.id).join(", ") : ""), "worker-message-sent"));
      record.mirror = accepted === true ? "accepted" : "pending";
    } catch (error) {
      check(); record.mirror = manager.state.latestTranscript?.[key] ? "accepted" : "unknown";
      note("warn", "worker_message_mirror_unconfirmed", { messageId: record.payload.message_id, mirror: record.mirror });
    }
    await save("sent", record);
  }

  async function receiveMessage(envelope) {
    const payload = incoming(envelope, worker), id = payload.message_id;
    const nativeId = nativeUUID(id);
    let record = await load("received", payload);
    const previous = await knownReceipt(nativeId);
    if (!record) {
      record = { version: 1, payload, targetThreadId: target(previous), inputMirror: previous ? "accepted" : "pending" };
      await save("received", record);
    }
    if (!record.request) {
      if (payload.attachments.length && !localHost(manager.state.sessions[record.targetThreadId]?.hostId))
        fail("remote_image_target", "Peer image paths require a native session on this machine.", id);
      const paths = [];
      for (const ref of payload.attachments) paths.push(await image(ref));
      record.request = { protocolVersion: PROTOCOL, messageId: nativeId, source: "hermes", origin: "worker-chat",
        workerName: payload.from, peerMessageId: id, reply_to: payload.reply_to,
        targetThreadId: record.targetThreadId, delivery: "queue",
        text: marker(payload) + "\nThis is ordinary worker chat. It never creates a router task or native goal. " +
          "Reply only when needed via bridge_send_worker_message, setting reply_to to this message UUID. " +
          "Do not automatically acknowledge or reply to a reply; avoid automatic reply loops.\n\n" + payload.text +
          (paths.length ? "\n\nImage files on this machine (use view_image to inspect the actual bytes):\n" + paths.join("\n") : ""),
        ...(paths.length ? { attachments: payload.attachments.map((ref, i) => ({ ...ref, localPath: paths[i] })) } : {}) };
      if (previous?.request && !same(previous.request, record.request))
        fail("message_id_conflict", "The original UUID belongs to a different manager request.", id);
      await save("received", record);
    }
    if (record.inputMirror === "pending") {
      record.inputMirror = "dispatching"; await save("received", record);
      await observe(() => manager.mirrorInput(copy(record.request)));
      record.inputMirror = "accepted"; await save("received", record);
    }
    // Always use the exact saved request, including its original target and
    // local paths. Manager receipts own queue/deferred/unknown semantics.
    const accepted = await observe(() => manager.message(copy(record.request)));
    const { receipt, state } = await durableReceipt(nativeId);
    const queued = state?.sessions?.[record.targetThreadId];
    const inQueue = [...queued?.queue ?? [], ...queued?.steerQueue ?? []].some(item => item.messageId === nativeId);
    if (accepted?.accepted !== true || receipt?.response?.accepted !== true || receipt.pending || receipt.error ||
        !same(receipt.request, record.request) ||
        (!(["accepted"].includes(receipt.nativeDispatch?.status)) &&
          !(inQueue && [undefined, "deferred", "canceled"].includes(receipt.nativeDispatch?.status))))
      fail("manager_acceptance_unconfirmed", "No durable manager acceptance; leave pending and retry this same UUID only.", id, "unknown");
    record.managerAccepted = true; await save("received", record);
    const ack = routerReceipt(await observe(() => client._request("POST", `/messages/${id}/ack`, { worker })), payload);
    if (ack.status !== "accepted") fail("worker_message_ack_unconfirmed", "Router ACK is unconfirmed; retry the same UUID only.", id, "unknown");
    record.acked = true; await save("received", record);
    return { ok: true, messageId: id, nativeMessageId: nativeId, accepted: true, acked: true,
      duplicate: accepted.duplicate === true, action: accepted.action };
  }

  async function readInbox({ after = 0, limit = 100 } = {}) {
    if (!Number.isSafeInteger(after) || after < 0 || !Number.isSafeInteger(limit) || limit < 1 || limit > 100)
      fail("invalid_inbox_page", "Use a nonnegative after sequence and a limit of 1..100.");
    const result = await observe(() => client._request("GET", `/messages?worker=${encodeURIComponent(worker)}&after=${after}&limit=${limit}`));
    if (result?.ok !== true || result.worker !== worker || !Array.isArray(result.messages))
      fail("invalid_worker_inbox", "Router returned an invalid inbox for this worker.");
    const messages = result.messages.filter(value => value?.to === worker).map(value => ({
      ...incoming(value, worker), seq: value.seq, status: value.status, created_at: value.created_at,
    }));
    return { ok: true, worker, count: messages.length, messages, has_more: result.has_more === true, next_after: result.next_after ?? null };
  }

  const service = {
    send(request) {
      return enqueue(async () => {
        const id = uuid(request?.messageId), to = workerName(request.to);
        if (to === worker) fail("invalid_worker", "Send peer chat to another worker.", id);
        if (request.replyTo != null && request.reply_to != null && uuid(request.replyTo) !== uuid(request.reply_to))
          fail("invalid_reply_to", "replyTo and reply_to must identify the same message.", id);
        const reply = request.replyTo ?? request.reply_to, refs = attachments(request.attachments);
        // Ignore caller-supplied from/source/worker: the local owner is the sender.
        const payload = { message_id: id, from: worker, to, text: textBody(request.text),
          ...(reply != null ? { reply_to: uuid(reply) } : {}), ...(refs.length ? { attachments: refs } : {}) };
        let record = await load("sent", payload);
        const duplicate = Boolean(record?.router);
        record ??= { version: 1, payload, targetThreadId: manager.state.selectedThreadId ?? null, mirror: "pending" };
        if (!record.router) {
          record.status = "posting"; await save("sent", record);
          try {
            const result = routerReceipt(await observe(() => client._request("POST", "/messages", copy(payload))), payload);
            record.router = { status: result.status, duplicate: result.duplicate === true };
            record.status = "accepted"; await save("sent", record);
          } catch (error) {
            check(); record.status = "unknown"; await save("sent", record);
            fail("worker_message_send_unconfirmed", "Message acceptance is unconfirmed; retry only with the same messageId and content.", id, "unknown");
          }
        }
        await mirrorSent(record);
        return { ok: true, messageId: id, message_id: id, status: record.router.status,
          duplicate: duplicate || record.router.duplicate, mirror: record.mirror };
      });
    },
    receive: envelope => enqueue(() => receiveMessage(envelope)),
    inbox: request => enqueue(() => readInbox(request)),
    poll() {
      return enqueue(async () => {
        let after = 0, accepted = 0;
        const failures = [];
        do {
          const page = await readInbox({ after });
          for (const message of page.messages) {
            try { await receiveMessage(message); accepted++; }
            catch (error) {
              check();
              failures.push({ messageId: message.message_id, code: error.code ?? "worker_message_receive_failed", retrySameMessageId: true });
              note("warn", "worker_message_receive_failed", failures.at(-1));
            }
          }
          if (!page.has_more) break;
          if (!Number.isSafeInteger(page.next_after) || page.next_after <= after)
            fail("invalid_inbox_page", "Router inbox pagination did not advance.");
          after = page.next_after;
        } while (true);
        // Every later poll starts at zero: failures are never skipped by a
        // persisted cursor, and later messages can pass a failed earlier one.
        return { ok: failures.length === 0, worker, accepted, failures };
      });
    },
    busy: () => pending > 0,
    close() {
      if (closing) return closing;
      closed = true; abort.abort(stopped);
      closing = tail.finally(() => { if (owners.get(manager)?.service === service) owners.delete(manager); });
      return closing;
    },
  };
  owners.set(manager, { service, client, worker });
  return service;
}
