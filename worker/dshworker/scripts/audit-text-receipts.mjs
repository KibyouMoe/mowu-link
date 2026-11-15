// Read-only Feishu receipt investigation. Never POST a message, alter an outbox
// status, invent a message ID, or treat an empty search as proof of non-delivery.
import { readFile, writeFile, rename } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { createHash, randomUUID } from "node:crypto";
import { FeishuNotifier, escapeMarkdown, markdownPayload } from "../feishu.mjs";
import { splitPublicMarkdown } from "../public-image-relay.mjs";
import { hermesChunks } from "../hermes-feishu-format.mjs";
import { isAbandonedText } from "../text-receipt-review.mjs";
import { configuredBridgeIdentity, matchesBridgeIdentity, resolveWatchdogStateDir } from "./watchdog-context.mjs";

export function matchingTextReceipt(item, { appId, chatId, title, threadId, text, startMs, endMs }) {
  if (item?.msg_type !== "interactive" || item.chat_id !== chatId || item.sender?.sender_type !== "app" ||
      item.sender?.id_type !== "app_id" || item.sender.id !== appId || item.deleted === true ||
      Number(item.create_time) < startMs || Number(item.create_time) > endMs || !Number.isFinite(Number(item.create_time))) return false;
  let card; try { card = JSON.parse(item.body.content); } catch { return false; }
  if (card.header?.title?.content !== title || card.elements?.length !== 2) return false;
  const [body, note] = card.elements;
  return body.tag === "markdown" && body.content === escapeMarkdown(text) && note.tag === "note" &&
    note.elements?.length === 1 && note.elements[0].content === `会话 ${threadId}`;
}

export async function auditTextReceipts({ stateDir, fetchImpl = fetch, notifier, clock = Date.now }) {
  const config = JSON.parse(await readFile(path.join(stateDir, "desktop-config.local.json"), "utf8"));
  const expected = configuredBridgeIdentity(config), port = config.port ?? 8903;
  if (!expected || (config.host ?? "127.0.0.1") !== "127.0.0.1" || !Number.isInteger(port) || port < 1 || port > 65535) throw new Error("receipt_audit_config_rejected");
  const token = (await readFile(config.tokenFile, "utf8")).trim();
  if (token.length < 24) throw new Error("receipt_audit_credential_rejected");
  const response = await fetchImpl(`http://127.0.0.1:${port}/health`, { headers: { authorization: "Bearer " + token }, signal: AbortSignal.timeout(3000) });
  if (!response.ok || !matchesBridgeIdentity(await response.json(), expected)) throw new Error("receipt_audit_identity_rejected");
  const state = JSON.parse(await readFile(path.join(stateDir, "desktop-manager-state.json"), "utf8"));
  const reportPath = path.join(stateDir, "text-receipt-audit.json");
  let old = {}; try { old = JSON.parse(await readFile(reportPath, "utf8")); } catch {}
  const entries = Object.values(state.publicTextOutbox ?? {}).filter(e => e.status === "unknown" && !isAbandonedText(e) &&
    e.threadId === state.selectedThreadId && e.bindingEpoch === state.selectionEpoch).slice(0, 3);
  if (!notifier) {
    const secret = JSON.parse(await readFile(config.feishu.configFile, "utf8"));
    notifier = new FeishuNotifier({ appId: secret.app_id, appSecret: secret.app_secret });
  }
  const reviews = [];
  for (const entry of entries) {
    const original = entry.text ?? splitPublicMarkdown(state.sessions[entry.threadId]?.seen?.[entry.key]?.text ?? "")[entry.part ?? 0]?.text;
    const fingerprint = createHash("sha256").update(JSON.stringify([entry.uuid, entry.at, entry.threadId, entry.bindingEpoch, entry.chatId, original, entry.feishuMessageId, entry.deliveryEvidence])).digest("hex");
    const previous = old.reviews?.find(r => r.fingerprint === fingerprint);
    if (previous && clock() < previous.nextCheckAt) { reviews.push(previous); continue; }
    const review = { uuid: entry.uuid, fingerprint, checkedAt: new Date(clock()).toISOString(), nextCheckAt: clock() + 300000,
      status: "needs_review", outcome: "unconfirmed", originalStatus: entry.status,
      evidenceAvailable: Array.isArray(entry.deliveryEvidence), replayed: false };
    reviews.push(review);
    if (!entry.chatId || !entry.title) { review.outcome = "missing_target_metadata"; continue; }
    if (!original || hermesChunks(original).length !== 1) { review.outcome = "missing_or_multichunk_payload"; continue; }
    const at = Date.parse(entry.at), startMs = Date.parse(entry.firstAttemptAt ?? entry.at) - 120000, endMs = at + 120000;
    if (![at, startMs, endMs].every(Number.isFinite)) { review.outcome = "invalid_attempt_time"; continue; }
    const accessToken = await notifier.getToken();
    if (!accessToken) { review.outcome = "receipt_lookup_unavailable"; continue; }
    const criteria = { appId: notifier.appId, chatId: entry.chatId, threadId: entry.threadId, title: entry.title,
      text: original, startMs, endMs };
    const payloadSha = createHash("sha256").update(markdownPayload(original, entry.title, entry.threadId)).digest("hex");
    const knownId = entry.feishuMessageId ?? entry.deliveryEvidence?.findLast(e => e.phase === "accepted" && e.uuid === entry.uuid &&
      e.chunk === 0 && e.chunks === 1 && e.payloadSha256 === payloadSha && e.messageId)?.messageId;
    if (knownId && !/^om_[a-z\d_]+$/i.test(knownId)) { review.outcome = "invalid_receipt_identity"; continue; }
    let cursor, complete = false; const matches = [];
    try {
      for (let page = 0; page < (knownId ? 1 : 3); page++) {
        const url = new URL("https://open.feishu.cn/open-apis/im/v1/messages" + (knownId ? "/" + encodeURIComponent(knownId) : ""));
        url.search = new URLSearchParams(knownId ? { card_msg_content_type: "user_card_content" } : {
          container_id_type: "chat", container_id: entry.chatId, sort_type: "ByCreateTimeAsc", page_size: "50",
          start_time: String(Math.floor(startMs / 1000)), end_time: String(Math.ceil(endMs / 1000)),
          card_msg_content_type: "user_card_content", ...(cursor ? { page_token: cursor } : {}) }).toString();
        const reply = await fetchImpl(url, { method: "GET", headers: { authorization: "Bearer " + accessToken }, signal: AbortSignal.timeout(10000) });
        const data = await reply.json();
        if (!reply.ok || data.code !== 0) { review.apiCode = Number.isInteger(data.code) ? data.code : null; review.outcome = "receipt_lookup_rejected"; break; }
        for (const item of data.data?.items ?? []) if ((!knownId || item.message_id === knownId) && matchingTextReceipt(item, criteria)) matches.push(item.message_id);
        if (knownId || data.data?.has_more === false) { complete = true; break; }
        const next = data.data?.page_token; if (!next || next === cursor) break; cursor = next;
      }
      review.matches = [...new Set(matches)]; review.searchComplete = complete;
      if (complete) review.outcome = review.matches.length === 0 ? "no_receipt_found_not_proof_of_failure"
        : knownId && review.matches.length === 1 ? "original_receipt_verified"
        : "matching_content_requires_review"; // Content equality alone does not prove a UUID.
      else if (review.outcome === "unconfirmed") review.outcome = "lookup_incomplete";
    } catch { review.outcome = "receipt_lookup_unavailable"; }
  }
  const report = { version: 1, checkedAt: new Date(clock()).toISOString(), reviews, outboxModified: false, messagePosts: 0 };
  const temp = reportPath + "." + randomUUID() + ".tmp";
  await writeFile(temp, JSON.stringify(report, null, 2) + "\n", { mode: 0o600 }); await rename(temp, reportPath);
  return report;
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  console.log(JSON.stringify(await auditTextReceipts({ stateDir: resolveWatchdogStateDir() })));
}
