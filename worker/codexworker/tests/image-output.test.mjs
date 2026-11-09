import assert from "node:assert/strict";
import test from "node:test";
import { mkdtemp, readFile, rm, writeFile, mkdir, symlink } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { randomUUID } from "node:crypto";
import { sendImage, MAX_IMAGE_BYTES, SEND_DEDUP_WINDOW_MS } from "../feishu-image-output.mjs";
import { BusinessRuntime } from "../business-runtime.mjs";

const png = Buffer.from("89504e470d0a1a0a0000000049454e44", "hex");
const response = data => ({ ok: true, json: async () => ({ code: 0, data }) });
async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "rudolf-images-"));
  const workspace = path.join(root, "workspace"); await mkdir(workspace);
  const filename = path.join(workspace, "sample.png"); await writeFile(filename, png);
  const calls = []; let invalidations = 0;
  let current = { threadId: "selected", chatId: "oc_test" };
  const context = { stateDir: root, allowedRoots: [workspace],
    retryWait: async () => {},
    notifier: { enabled: true, getToken: async () => "test-credential", invalidateToken: () => invalidations++ },
    getTarget: async id => {
      if (id && current.threadId !== id) throw Object.assign(new Error("changed"), { name: "ImageOutputError", status: 409,
        body: { error: "selected_target_changed", detail: "No fallback.", retryable: false } });
      return { ...current };
    },
    fetchImpl: async (url, options) => { calls.push({ url, options }); return response(url.includes("/images") ? { image_key: "img_test" } : { message_id: "om_image_test" }); } };
  t.after(async () => { assert.ok(root.startsWith(os.tmpdir())); await rm(root, { recursive: true, force: true }); });
  return { root, workspace, filename, calls, context, request: { imagePath: filename, threadId: "selected", messageId: randomUUID() },
    switchTarget: () => { current = { threadId: "different", chatId: "oc_other" }; }, invalidations: () => invalidations };
}

test("real multipart upload, image payload and durable exact UUID retry", async t => {
  const f = await fixture(t);
  const result = await sendImage(f.request, f.context);
  assert.equal(result.status, "sent"); assert.equal(result.feishuMessageId, "om_image_test");
  assert.equal(f.calls[0].options.body.get("image_type"), "message");
  const image = f.calls[0].options.body.get("image"); assert.equal(image.type, "image/png");
  assert.deepEqual(Buffer.from(await image.arrayBuffer()), png);
  const message = JSON.parse(f.calls[1].options.body);
  assert.equal(message.receive_id, "oc_test"); assert.equal(message.msg_type, "image");
  assert.deepEqual(JSON.parse(message.content), { image_key: "img_test" }); assert.equal(message.uuid, f.request.messageId);
  assert.equal((await sendImage(f.request, { ...f.context })).duplicate, true); assert.equal(f.calls.length, 2);
  const receipts = await readFile(path.join(f.root, "image-output-state.json"), "utf8");
  assert.equal(receipts.includes("test-credential"), false); assert.equal(receipts.includes(f.filename), false);
});

test("same UUID with changed bytes or target conflicts", async t => {
  const f = await fixture(t); await sendImage(f.request, f.context);
  await writeFile(f.filename, Buffer.concat([png, Buffer.from([1])]));
  await assert.rejects(sendImage(f.request, f.context), e => e.status === 409 && e.body.error === "message_id_conflict");
  assert.equal(f.calls.length, 2);
});

test("reject relative/URL, outside-root, non-raster, empty and oversized files before upload", async t => {
  const f = await fixture(t);
  for (const imagePath of ["relative.png", "https://example.com/img.png"]) {
    await assert.rejects(sendImage({ ...f.request, imagePath }, f.context), e => e.body.error === "invalid_request");
  }
  const outside = path.join(f.root, "outside.png"); await writeFile(outside, png);
  await assert.rejects(sendImage({ ...f.request, imagePath: outside }, f.context), e => e.status === 403);
  for (const bytes of [Buffer.from("<svg/>"), Buffer.alloc(0), Buffer.alloc(MAX_IMAGE_BYTES + 1)]) {
    await writeFile(f.filename, bytes);
    await assert.rejects(sendImage(f.request, f.context), e => e.body.error === "invalid_image");
  }
  assert.equal(f.calls.length, 0);
});

test("internal exact public-file grant permits an out-of-workspace preview, not siblings or HTTP fields", async t => {
  const f = await fixture(t), outside = path.join(f.root, "archived.png"), sibling = path.join(f.root, "not-displayed.png");
  await writeFile(outside, png); await writeFile(sibling, png);
  const displayed = { ...f.context, displayedFiles: [outside] };
  assert.equal((await sendImage({ ...f.request, imagePath: outside }, displayed)).status, "sent");
  await assert.rejects(sendImage({ ...f.request, imagePath: sibling, messageId: randomUUID() }, displayed), e => e.body.error === "image_path_forbidden");
  await assert.rejects(sendImage({ ...f.request, imagePath: sibling, displayedFiles: [sibling], messageId: randomUUID() }, f.context), e => e.body.error === "image_path_forbidden");
  for (const bytes of [Buffer.from("<svg/>"), Buffer.alloc(0), Buffer.alloc(MAX_IMAGE_BYTES + 1)]) {
    await writeFile(outside, bytes);
    await assert.rejects(sendImage({ ...f.request, imagePath: outside, messageId: randomUUID() }, displayed), e => e.body.error === "invalid_image");
  }
  assert.equal(f.calls.length, 2);
});

test("public-file grants do not authorize network paths or aliases of the Feishu input cache", async t => {
  const f = await fixture(t), cache = path.join(f.root, "feishu-images"), alias = path.join(f.root, "alias");
  await mkdir(cache); await writeFile(path.join(cache, "input.png"), png);
  await symlink(cache, alias, process.platform === "win32" ? "junction" : "dir");
  const filename = path.join(alias, "input.png");
  await assert.rejects(sendImage({ ...f.request, imagePath: filename }, { ...f.context, displayedFiles: [filename], excludedRoots: [cache] }),
    e => e.body.error === "image_input_excluded");
  for (const imagePath of ["//server/share/a.png", "\\\\server\\share\\a.png"]) {
    await assert.rejects(sendImage({ ...f.request, imagePath }, { ...f.context, displayedFiles: [imagePath] }), e => e.body.error === "invalid_request");
  }
  assert.equal(f.calls.length, 0);
});

test("switch during upload refuses send, never silently targets another chat", async t => {
  const f = await fixture(t); f.context.fetchImpl = async () => { f.calls.push("upload"); f.switchTarget(); return response({ image_key: "img_test" }); };
  await assert.rejects(sendImage(f.request, f.context), e => e.body.error === "selected_target_changed");
  assert.equal(f.calls.length, 1);
});

test("known token rejection retries only once with identical send UUID", async t => {
  const f = await fixture(t); let sends = 0;
  f.context.fetchImpl = async (url, options) => {
    f.calls.push({ url, options });
    if (url.includes("/images")) return response({ image_key: "img_test" });
    if (sends++ === 0) return { ok: false, json: async () => ({ code: 99991663 }) };
    return response({ message_id: "om_refreshed" });
  };
  assert.equal((await sendImage(f.request, f.context)).status, "sent"); assert.equal(f.invalidations(), 1);
  assert.equal(f.calls[1].options.body, f.calls[2].options.body);
});

test("unknown outcome retries the exact payload three times then stays frozen across restart", async t => {
  const f = await fixture(t);
  f.context.fetchImpl = async url => { f.calls.push(url); if (url.includes("/images")) return response({ image_key: "img_test" }); throw new Error("lost response"); };
  const check = e => e.body.error === "image_send_outcome_unknown" && e.body.retryable === false;
  await assert.rejects(sendImage(f.request, f.context), check);
  await assert.rejects(sendImage(f.request, { ...f.context }), e => check(e) && e.body.duplicate === true);
  assert.equal(f.calls.length, 4);
  const ledger = JSON.parse(await readFile(path.join(f.root, "image-output-state.json"), "utf8")).receipts[f.request.messageId];
  assert.equal(ledger.stage, "message"); assert.equal(ledger.imageKey, "img_test"); assert.equal(ledger.sendAttempts, 3);
});

test("transient upload failure recovers before sending and records its original sanitized cause", async t => {
  const f = await fixture(t); let uploads = 0;
  f.context.fetchImpl = async (url, options) => {
    f.calls.push({ url, options });
    if (url.includes("/images") && uploads++ === 0) throw Object.assign(new TypeError("secret /private/path"), { cause: { code: "ECONNRESET" } });
    return response(url.includes("/images") ? { image_key: "img_recovered" } : { message_id: "om_recovered" });
  };
  const result = await sendImage(f.request, f.context); assert.equal(result.uploadAttempts, 2); assert.equal(result.sendAttempts, 1);
  const raw = await readFile(path.join(f.root, "image-output-state.json"), "utf8"), receipt = JSON.parse(raw).receipts[f.request.messageId];
  assert.equal(receipt.lastFailure.causeCode, "ECONNRESET"); assert.equal(receipt.lastFailure.stage, "upload");
  assert.doesNotMatch(raw, /secret|private\/path|test-credential/);
});

test("server committed but response was lost: same UUID and identical payload yield one visible image", async t => {
  const f = await fixture(t), visible = new Map(); let attempts = 0;
  f.context.fetchImpl = async (url, options) => {
    f.calls.push({ url, options });
    if (url.includes("/images")) return response({ image_key: "img_test" });
    const body = JSON.parse(options.body); visible.set(body.uuid, "om_single");
    if (attempts++ === 0) throw new Error("receipt lost");
    return response({ message_id: visible.get(body.uuid) });
  };
  const result = await sendImage(f.request, f.context);
  assert.equal(result.feishuMessageId, "om_single"); assert.equal(result.sendAttempts, 2); assert.equal(visible.size, 1);
  assert.equal(f.calls[1].options.body, f.calls[2].options.body);
  assert.equal((await sendImage(f.request, f.context)).duplicate, true); assert.equal(f.calls.length, 3);
});

test("interrupted uploaded/sending phase resumes with its saved image key and original first-send deadline", async t => {
  const f = await fixture(t); await sendImage(f.request, f.context);
  const filename = path.join(f.root, "image-output-state.json"), ledger = JSON.parse(await readFile(filename, "utf8"));
  const receipt = ledger.receipts[f.request.messageId]; receipt.status = "sending"; receipt.stage = "message"; delete receipt.result;
  const firstSendAt = receipt.firstSendAt; await writeFile(filename, JSON.stringify(ledger)); f.calls.length = 0;
  const result = await sendImage(f.request, f.context); assert.equal(result.status, "sent"); assert.equal(f.calls.length, 1);
  assert.ok(f.calls[0].url.includes("/messages")); assert.equal(JSON.parse(JSON.parse(f.calls[0].options.body).content).image_key, "img_test");
  assert.equal(JSON.parse(await readFile(filename, "utf8")).receipts[f.request.messageId].firstSendAt, firstSendAt);
});

test("expired or backward-clock send window never issues another upload/message POST", async t => {
  for (const offset of [SEND_DEDUP_WINDOW_MS, -1]) {
    const f = await fixture(t); await sendImage(f.request, f.context);
    const filename = path.join(f.root, "image-output-state.json"), ledger = JSON.parse(await readFile(filename, "utf8"));
    const receipt = ledger.receipts[f.request.messageId]; receipt.status = "unknown"; receipt.stage = "message"; delete receipt.result;
    await writeFile(filename, JSON.stringify(ledger)); f.calls.length = 0;
    f.context.now = () => Date.parse(receipt.firstSendAt) + offset;
    await assert.rejects(sendImage(f.request, { ...f.context, recoveryRequested: true }), e => e.body.error === "image_recovery_window_expired");
    assert.equal(f.calls.length, 0);
  }
});

test("legacy unknown is frozen normally; explicit bounded recovery retains original UUID and fingerprint", async t => {
  const f = await fixture(t); await sendImage(f.request, f.context);
  const filename = path.join(f.root, "image-output-state.json"), ledger = JSON.parse(await readFile(filename, "utf8"));
  const saved = ledger.receipts[f.request.messageId]; ledger.receipts[f.request.messageId] = { fingerprint: saved.fingerprint,
    status: "unknown", createdAt: saved.createdAt, threadId: "selected", error: { error: "image_send_outcome_unknown", detail: "Legacy unknown", retryable: false } };
  await writeFile(filename, JSON.stringify(ledger)); f.calls.length = 0;
  await assert.rejects(sendImage(f.request, f.context), e => e.body.error === "image_send_outcome_unknown"); assert.equal(f.calls.length, 0);
  const result = await sendImage(f.request, { ...f.context, recoveryRequested: true });
  assert.equal(result.messageId, f.request.messageId); assert.equal(result.recovered, true); assert.equal(f.calls.length, 2);
  assert.equal(JSON.parse(f.calls[1].options.body).uuid, f.request.messageId);
  assert.equal(JSON.parse(await readFile(filename, "utf8")).receipts[f.request.messageId].firstSendAt, saved.createdAt);
});

test("retry backoff and slow token fetch recheck selection and deadline before any POST", async t => {
  const f = await fixture(t); f.context.retryWait = async () => f.switchTarget();
  f.context.fetchImpl = async (url, options) => { f.calls.push({ url, options }); if (url.includes("/images")) return response({ image_key: "img_test" }); throw new TypeError("reset"); };
  await assert.rejects(sendImage(f.request, f.context), e => e.body.error === "selected_target_changed"); assert.equal(f.calls.length, 2);
  const g = await fixture(t); let now = Date.now(); g.context.now = () => now;
  g.context.notifier.getToken = async () => { if (g.calls.length) now += SEND_DEDUP_WINDOW_MS; return "test"; };
  await assert.rejects(sendImage(g.request, g.context), e => e.body.error === "image_recovery_window_expired"); assert.equal(g.calls.length, 1);
});

test("all upload transport failures report upload phase, not unknown message outcome", async t => {
  const f = await fixture(t); f.context.fetchImpl = async url => { f.calls.push(url); throw new SyntaxError("bad response with secret"); };
  await assert.rejects(sendImage(f.request, f.context), e => e.body.error === "image_upload_failed" && e.body.stage === "upload" && e.body.sendAttempts === 0);
  assert.equal(f.calls.length, 3);
});

test("explicit API rejection is honest, no permission/token detail leakage", async t => {
  const f = await fixture(t); f.context.fetchImpl = async () => ({ ok: false, json: async () => ({ code: 230006, msg: "sensitive vendor detail" }) });
  await assert.rejects(sendImage(f.request, f.context), e => e.body.error === "feishu_api_rejected" && e.body.feishuCode === 230006 && !e.body.detail.includes("sensitive"));
});

test("shared output lease rejects concurrent writes without overwriting receipts", async t => {
  const f = await fixture(t); let release; let entered;
  const ready = new Promise(resolve => { entered = resolve; });
  const hold = new Promise(resolve => { release = resolve; });
  f.context.fetchImpl = async url => { if (url.includes("/images")) { entered(); await hold; return response({ image_key: "img_test" }); } return response({ message_id: "om_test" }); };
  const first = sendImage(f.request, f.context); await ready;
  await assert.rejects(sendImage(f.request, f.context), e => e.body.error === "image_output_busy" && e.body.retryable === true);
  release(); assert.equal((await first).status, "sent");
  assert.equal((await sendImage(f.request, f.context)).duplicate, true);
});

const businessCode = version => `export const API_VERSION=1; export const FEATURE_VERSION='${version}'; export async function sendImage(r,c) { return c.block ? await c.block() : {version:FEATURE_VERSION}; }`;
async function runtimeFixture(t, maxLoads) {
  const f = await fixture(t); const source = path.join(f.root, "plugin-source"); await mkdir(source);
  const file = path.join(source, "feishu-image-output.mjs");
  await writeFile(path.join(f.root, "runtime-source.local.json"), JSON.stringify({ sourceRoot: source }));
  const runtime = new BusinessRuntime({ stateDir: f.root, maxLoads });
  return { ...f, runtime, source, file };
}

test("reload by content hash updates behavior, identical hash is a no-op", async t => {
  const f = await runtimeFixture(t); await f.runtime.initialize();
  await writeFile(f.file, businessCode("business-test-1")); const first = await f.runtime.reload();
  assert.equal(first.featureVersion, "business-test-1"); assert.equal((await f.runtime.sendImage({}, {})).version, "business-test-1");
  assert.equal((await f.runtime.reload()).duplicate, true); assert.equal(f.runtime.status().loadCount, 2);
  await writeFile(f.file, businessCode("business-test-2")); const second = await f.runtime.reload();
  assert.notEqual(first.sha256, second.sha256); assert.equal((await f.runtime.sendImage({}, {})).version, "business-test-2");
});

test("editing development source applies on next use, invalid update retains last good behavior", async t => {
  const f = await runtimeFixture(t); await f.runtime.initialize();
  await writeFile(f.file, businessCode("auto-first"));
  assert.equal((await f.runtime.sendImage({}, {})).version, "auto-first");
  const firstHash = f.runtime.status().sha256;
  await writeFile(f.file, businessCode("auto-second"));
  assert.equal((await f.runtime.sendImage({}, {})).version, "auto-second");
  assert.notEqual(f.runtime.status().sha256, firstHash);
  await writeFile(f.file, "syntax error !");
  assert.equal((await f.runtime.sendImage({}, {})).version, "auto-second");
  assert.equal(f.runtime.status().lastReloadError, "runtime_reload_failed");
  await writeFile(f.file, businessCode("fixed"));
  assert.equal((await f.runtime.sendImage({}, {})).version, "fixed");
  assert.equal(f.runtime.status().lastReloadError, null);
});

test("bad syntax/API retains good module; reload memory budget is bounded", async t => {
  const f = await runtimeFixture(t, 4); await writeFile(f.file, businessCode("good")); await f.runtime.reload();
  const good = f.runtime.status().sha256;
  for (const code of ["this is not valid JS !", "export const API_VERSION=99;"]) {
    await writeFile(f.file, code); await assert.rejects(f.runtime.reload(), e => e.body.error === "runtime_reload_failed");
    assert.equal(f.runtime.status().sha256, good);
  }
  await writeFile(f.file, businessCode("last")); await f.runtime.reload();
  await writeFile(f.file, businessCode("over-limit")); await assert.rejects(f.runtime.reload(), e => e.body.error === "runtime_reload_limit");
});

test("active send blocks module swap, completion releases reload without restarting anything", async t => {
  const f = await runtimeFixture(t); await writeFile(f.file, businessCode("running")); await f.runtime.reload();
  let release; const hold = new Promise(resolve => { release = resolve; });
  const pending = f.runtime.sendImage({}, { block: () => hold });
  await writeFile(f.file, businessCode("next")); await assert.rejects(f.runtime.reload(), e => e.body.error === "runtime_busy");
  release({ complete: true }); await pending; await f.runtime.reload(); assert.equal(f.runtime.status().featureVersion, "next");
});
