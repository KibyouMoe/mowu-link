// Only explicitly displayed public images are eligible. Never search raw tool
// arguments/results, commands, reasoning or arbitrary workspace files for paths.
import { createHash, randomUUID } from "node:crypto";
import path from "node:path";

export function localImagePath(value) {
  if (typeof value !== "string") return null;
  let filename = value.trim().replace(/\\([()])/g, "$1");
  try { filename = decodeURIComponent(filename); } catch { /* literal percent */ }
  if (/^(?:[a-z][a-z\d+.-]*:\/\/|data:|file:|\\\\|\/\/)/i.test(filename)) return null;
  if (!path.isAbsolute(filename) && !/^[a-z]:[\\/]/i.test(filename)) return null;
  return filename;
}
const canonical = filename => /^[a-z]:[\\/]/i.test(filename)
  ? path.win32.normalize(filename).toLowerCase() : path.normalize(filename);
const hash = value => createHash("sha256").update(value).digest("hex");

export function markdownImageSpans(text) {
  // Code examples must not become outbound messages. Mask inline code and
  // fenced blocks, including a fence which is still streaming/incomplete.
  let fence = null;
  text = text.split("\n").map(line => {
    const match = line.match(/^\s{0,3}(`{3,}|~{3,})/);
    if (fence) { if (match && match[1][0] === fence[0] && match[1].length >= fence.length) fence = null; return " ".repeat(line.length); }
    if (match) { fence = match[1]; return " ".repeat(line.length); } return line;
  }).join("\n");
  let visible = "";
  for (let index = 0; index < text.length;) {
    if (text[index] !== "`") { visible += text[index++]; continue; }
    let end = index; while (text[end] === "`") end++;
    const delimiter = text.slice(index, end);
    let closing = end;
    while ((closing = text.indexOf(delimiter, closing)) >= 0) {
      if (text[closing - 1] !== "`" && text[closing + delimiter.length] !== "`") break;
      closing += delimiter.length;
    }
    if (closing < 0) { visible += " ".repeat(text.length - index); break; }
    visible += " ".repeat(closing + delimiter.length - index);
    index = closing + delimiter.length;
  }
  text = visible;
  const result = [];
  const start = /!\[[^\]\n]*\]\(/g;
  for (let match; (match = start.exec(text));) {
    if (match.index > 0 && text[match.index - 1] === "\\") continue;
    let index = start.lastIndex, depth = 1, angle = false, destination = "";
    if (text[index] === "<") { angle = true; index++; }
    for (; index < text.length; index++) {
      const char = text[index];
      if (char === "\n") break;
      if (char === "\\" && /[()]/.test(text[index + 1] ?? "")) { destination += text.slice(index, index + 2); index++; continue; }
      if (angle) { if (char === ">") { index++; break; } destination += char; continue; }
      if (char === "(") depth++;
      else if (char === ")" && --depth === 0) break;
      destination += char;
    }
    const suffix = angle ? text.slice(index).match(/^\s*(?:["'][^\n]*?["']\s*)?\)/) : null;
    if (angle ? !suffix : depth !== 0) continue;
    destination = destination.replace(/\s+["'][^\n]*?["']\s*$/, "");
    const filename = localImagePath(destination);
    if (filename) result.push({ imagePath: filename, start: match.index, end: index + (suffix?.[0].length ?? 1) });
    start.lastIndex = index + (suffix?.[0].length ?? 1);
  }
  return result;
}

export const markdownImagePaths = text => markdownImageSpans(text).map(span => span.imagePath);

export function splitPublicMarkdown(text) {
  const parts = []; let offset = 0;
  for (const span of markdownImageSpans(text)) {
    if (span.start > offset && text.slice(offset, span.start).trim()) parts.push({ type: "text", text: text.slice(offset, span.start) });
    parts.push({ type: "image", imagePath: span.imagePath }); offset = span.end;
  }
  if (text.slice(offset).trim()) parts.push({ type: "text", text: text.slice(offset) });
  return parts;
}

export function publicImages(snapshot) {
  const turns = snapshot.page?.order === "newest_first" ? [...(snapshot.turns ?? [])].reverse() : snapshot.turns ?? [];
  const inputPaths = new Set();
  for (const turn of turns) for (const item of turn.items ?? []) {
    if (item.type !== "userMessage") continue;
    for (const content of item.content ?? []) {
      if (!["localImage", "image"].includes(content.type)) continue;
      const filename = localImagePath(content.path ?? content.url);
      if (filename) inputPaths.add(canonical(filename));
    }
  }
  const result = [], seen = new Set();
  for (const turn of turns) for (const [index, item] of (turn.items ?? []).entries()) {
    const candidates = item.type === "agentMessage" && typeof item.text === "string" ? markdownImagePaths(item.text)
      : item.type === "imageGeneration" && item.status === "completed" ? [item.savedPath]
      : item.type === "imageView" ? [item.path] : [];
    for (const candidate of candidates) {
      const filename = localImagePath(candidate);
      if (!filename || inputPaths.has(canonical(filename))) continue;
      const key = hash(`${turn.id}\n${canonical(filename)}`);
      if (seen.has(key)) continue; seen.add(key);
      result.push({ key, turnId: turn.id, itemKey: `${turn.id}/${item.id ?? index}`, imagePath: filename });
    }
  }
  return result;
}

export class PublicImageRelay {
  constructor(manager) { this.manager = manager; this.running = false; this.task = null; }
  baseline(session, snapshot) { session.seenImages = Object.fromEntries(publicImages(snapshot).map(image => [image.key, true])); }
  observe(session, snapshot, bindingEpoch, { inline = false, publicTextKey } = {}) {
    const m = this.manager, images = publicImages(snapshot);
    // First activation on a pre-image-relay installation seeds existing history,
    // just as adoption does. No surprise backlog of historical uploads.
    if (!session.seenImages) { this.baseline(session, snapshot); return; }
    m.state.autoImages ??= {};
    for (const image of images) {
      const id = hash(`${session.threadId}\n${image.key}`), previous = m.state.autoImages[id];
      // 0.2.15 rejected these before any upload/ledger write. Recover only once,
      // from a fresh public read in the very same selected binding, same UUID.
      const recover = previous?.status === "failed" && previous.error === "image_path_forbidden" &&
        !previous.publicDisplayPolicy && previous.bindingEpoch === bindingEpoch;
      if (session.seenImages[image.key] && !recover) {
        // A still-unsent preview can take its place inside the final Markdown.
        // Never move an in-flight image or resend a completed/unknown receipt.
        if (inline && previous?.status === "pending" && previous.bindingEpoch === bindingEpoch && this.activeEntry !== previous && m.nextPublicOutputOrder) {
          previous.order = m.nextPublicOutputOrder(); previous.publicTextKey = publicTextKey;
        }
        continue;
      }
      session.seenImages[image.key] = true;
      if ((m.relaySession ? !m.relaySession(session.threadId, bindingEpoch) : session.threadId !== m.state.selectedThreadId || bindingEpoch !== m.state.selectionEpoch) ||
          m.config.feishu.autoImageOutput === false || !m.notifier?.enabled) continue;
      // Ingress originals are private input, even if the agent views that cache.
      const cache = path.resolve(m.stateDir, "feishu-images");
      const relative = path.relative(cache, path.resolve(image.imagePath));
      if (relative === "" || (relative !== ".." && !relative.startsWith(".." + path.sep) && !path.isAbsolute(relative))) continue;
      if (recover) {
        previous.previousFailure = { error: previous.error, completedAt: previous.completedAt };
        Object.assign(previous, { imagePath: image.imagePath, chatId: session.feishuChatId ?? m.notifier.chatId,
          status: "pending", publicDisplayPolicy: 1 });
        delete previous.error; delete previous.completedAt;
        continue;
      }
      if (previous) continue; // Never replace a sent/unknown/canceled receipt.
      m.state.autoImages[id] = { ...image, ...(inline ? { publicTextKey } : {}), mirror: m.transcriptEnabled === true, threadId: session.threadId, bindingEpoch, order: m.nextPublicOutputOrder?.(),
        chatId: session.feishuChatId ?? m.notifier.chatId, messageId: randomUUID(), status: "pending", publicDisplayPolicy: 1, observedAt: Date.now() };
    }
  }
  status() {
    const entries = Object.values(this.manager.state.autoImages ?? {});
    return { enabled: this.manager.config.feishu.autoImageOutput !== false,
      pending: entries.filter(e => e.status === "pending").length,
      sent: entries.filter(e => e.status === "sent").length,
      failed: entries.filter(e => e.status === "failed").length,
      canceled: entries.filter(e => e.status === "canceled").length,
      lastError: entries.filter(e => e.error).at(-1)?.error ?? null };
  }
  kick() {
    if (this.manager.drainPublicTexts && (this.manager.publicTextRunning || Object.values(this.manager.state.publicTextOutbox ?? {}).some(e => ["pending", "dispatching", "unknown"].includes(e.status)))) return this.manager.drainPublicTexts();
    if (this.running || this.manager.closed || this.manager.handoffPaused) return this.task;
    this.running = true;
    this.task = this.manager.track(this.drain().catch(() => {
      // Persistence failures leave the same durable UUID pending for recovery.
      console.error("[bridge] Public image relay persistence failed; check its receipts.");
    }).finally(() => { this.running = false; }));
    return this.task;
  }
  async drain(onlyEntry = null) {
    const m = this.manager;
    while (!m.closed && !m.handoffPaused) {
      if (!onlyEntry && m.drainPublicTexts && Object.values(m.state.publicTextOutbox ?? {}).some(e => ["pending", "dispatching", "unknown"].includes(e.status))) return;
      let entry;
      await m.serial(async () => {
        entry = onlyEntry ?? Object.values(m.state.autoImages ?? {}).find(e => e.status === "pending");
        if (entry) {
          if (m.transcriptEnabled && !entry.transcriptId) {
            const event = await m.emit("image.observed", { targetThreadId: entry.threadId, source: "assistant" },
              { itemKey: entry.itemKey }, { turnId: entry.turnId });
            const record = await m.messageJournal.record({ seq: event.seq, direction: "output", source: "assistant",
              logicalId: "image:" + entry.threadId + ":" + entry.key, text: "[图片输出]", phase: "final_answer",
              kind: "image", threadId: entry.threadId, turnId: entry.turnId, messageId: entry.messageId });
            entry.transcriptId = record.id;
            await m.messageJournal.plan(record.id, [entry.messageId]);
          }
          await m.save(); // Intent is durable before any upload.
        }
      });
      if (!entry) return;
      this.activeEntry = entry;
      const validateBinding = () => {
        if (m.closed || (m.relayCurrent ? !m.relayCurrent(entry) : entry.threadId !== m.state.selectedThreadId || entry.bindingEpoch !== m.state.selectionEpoch) ||
            m.config.feishu.autoImageOutput === false || entry.chatId !== (m.session(entry.threadId).feishuChatId ?? m.notifier?.chatId)) {
          const error = new Error("Feishu-selected binding changed; old image was not sent.");
          error.body = { error: "selected_target_changed", retryable: false }; throw error;
        }
      };
      let result, failure;
      try {
        validateBinding();
        result = await m.sendImage({ imagePath: entry.imagePath, threadId: entry.threadId, messageId: entry.messageId }, validateBinding, entry);
      } catch (error) { failure = error; }
      // A competing explicit send holds the same image lease. No upload happened:
      // retain the UUID and let the next normal scan retry instead of spinning.
      if (failure?.body?.error === "image_output_busy") { this.activeEntry = null; return; }
      await m.serial(async () => {
        entry.status = result ? "sent" : failure?.body?.error === "selected_target_changed" ? "canceled" : "failed";
        entry.completedAt = Date.now();
        if (result) entry.feishuMessageId = result.feishuMessageId;
        else entry.error = failure?.body?.error ?? "image_send_outcome_unknown";
        if (entry.transcriptId) await m.messageJournal.delivered(entry.transcriptId, entry.messageId,
          result ? "sent" : "unknown", result?.feishuMessageId);
        delete entry.imagePath; delete entry.chatId; // no permanent path history
        await m.save();
      });
      if (entry.status === "failed" && entry.threadId === m.state.selectedThreadId && entry.bindingEpoch === m.state.selectionEpoch) {
        await m.notifier?.sendMessage?.(`本会话有一张公开图片未确认送达飞书（${entry.error}）。已执行有限恢复；未另建 UUID 重复发送。请在 Codex 查看原图和分阶段回执。`,
          { title: `${m.config.bridgeName}-图片回传`, threadId: entry.threadId }).catch(() => {});
      }
      this.activeEntry = null;
      if (onlyEntry) return;
    }
  }
}
