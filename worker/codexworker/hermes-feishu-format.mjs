// Markdown post renderer ported from NousResearch/hermes-agent (MIT),
// plugins/platforms/feishu/adapter.py at 74885f997bece88c1393bd174b25b32338336cb6.
// Copyright (c) 2025 Nous Research. Full license in THIRD_PARTY_NOTICES.md.
const markdownHint = /(^\|.*\|\s*\n\|[-:|\s]+\|)|(^#{1,6}\s)|(^\s*[-*]\s)|(^\s*\d+\.\s)|(^\s*---+\s*$)|(```)|(`[^`\n]+`)|(\*\*[^*\n].+?\*\*)|(~~[^~\n].+?~~)|(<u>.+?<\/u>)|(\*[^*\n]+\*)|(\[[^\]]+\]\([^)]+\))|(^>\s)/m;

export function hermesPostRows(content) {
  if (!content || !content.includes("```")) return [[{ tag: "md", text: content }]];
  const rows = []; let current = [], inCode = false;
  const flush = () => { const text = current.join("\n"); if (text.trim()) rows.push([{ tag: "md", text }]); current = []; };
  for (const line of content.split(/\r?\n/)) {
    const fence = (inCode ? /^```\s*$/ : /^```([^\n`]*)\s*$/).test(line.trim());
    if (fence && !inCode) flush();
    current.push(line);
    if (fence) { inCode = !inCode; if (!inCode) flush(); }
  }
  flush(); return rows.length ? rows : [[{ tag: "md", text: content }]];
}

export function hermesOutbound(content, preferPost = false) {
  return preferPost || markdownHint.test(content)
    ? { msg_type: "post", content: JSON.stringify({ zh_cn: { content: hermesPostRows(content) } }) }
    : { msg_type: "text", content: JSON.stringify({ text: content }) };
}
export const FEISHU_MAX_MESSAGE_CHARS = 4096;

// Final (in_code, lang) after walking `text` line by line, mirroring Hermes'
// `fence_state_after`: a line starting with ``` toggles the fence and carries
// the language tag so a split chunk can reopen it unchanged.
export function fenceStateAfter(text, inCode = false, lang = "") {
  let inside = Boolean(inCode);
  let tag = String(lang ?? "");
  for (const line of String(text ?? "").split("\n")) {
    const stripped = line.trim();
    if (!stripped.startsWith("```")) continue;
    if (inside) { inside = false; tag = ""; }
    else {
      const parts = stripped.slice(3).trim().split(/\s+/).filter(Boolean);
      inside = true; tag = parts[0] ?? "";
    }
  }
  return [inside, tag];
}

function countBackticks(text) {
  return (text.match(/`/g) ?? []).length - (text.match(/\\`/g) ?? []).length;
}

// Split one reply into Feishu messages, mirroring Hermes' truncate_message:
// prefer a paragraph break, then a newline, then a space; never cut an inline
// code span; close an open ``` at the chunk end and reopen it (same language)
// in the next chunk so every message is valid Markdown on its own.
export function splitForFeishu(content, maxChars = FEISHU_MAX_MESSAGE_CHARS) {
  const text = String(content ?? "");
  const cap = Number.isFinite(maxChars) && maxChars > 64 ? Math.floor(maxChars) : FEISHU_MAX_MESSAGE_CHARS;
  if (text.length <= cap) return [text];
  const FENCE_CLOSE = "\n```";
  const chunks = [];
  let remaining = text;
  let carry = null; // language tag of a fence left open at the previous boundary
  while (remaining.length) {
    const prefix = carry !== null ? `\`\`\`${carry}\n` : "";
    // Leave room for the closing fence this chunk may need.
    const headroom = Math.max(1, cap - prefix.length - FENCE_CLOSE.length);
    if (prefix.length + remaining.length <= cap) {
      let last = prefix + remaining;
      if (carry !== null && fenceStateAfter(remaining, true, carry)[0]) last += FENCE_CLOSE;
      chunks.push(last);
      break;
    }
    const region = remaining.slice(0, headroom);
    const floor = headroom >> 2;
    let splitAt = region.lastIndexOf("\n\n");
    // Keep the blank line with the previous chunk so a paragraph boundary is
    // never turned into a leading blank line.
    if (splitAt >= floor) splitAt += 1;
    if (splitAt < floor) splitAt = region.lastIndexOf("\n");
    if (splitAt < floor) splitAt = region.lastIndexOf(" ");
    if (splitAt < 1) splitAt = headroom;
    // Don't split inside an inline `code span`.
    const candidate = remaining.slice(0, splitAt);
    if (countBackticks(candidate) % 2 === 1) {
      let lastBt = candidate.lastIndexOf("`");
      while (lastBt > 0 && candidate[lastBt - 1] === "\\") lastBt = candidate.lastIndexOf("`", lastBt - 1);
      if (lastBt > 0) {
        const safe = Math.max(candidate.lastIndexOf(" ", lastBt), candidate.lastIndexOf("\n", lastBt));
        if (safe > floor) splitAt = safe;
      }
    }
    const body = remaining.slice(0, splitAt);
    remaining = remaining.slice(splitAt).replace(/^\s+/, "");
    const [inCode, lang] = fenceStateAfter(body, carry !== null, carry ?? "");
    carry = inCode ? lang : null;
    chunks.push(prefix + body + (inCode ? FENCE_CLOSE : ""));
  }
  return chunks;
}

// Keep fences balanced across long chunks and preserve complete Unicode code
// points. Like Hermes, choose post-vs-text for the WHOLE message before splitting.
export function hermesChunks(content, cap = 8000) {
  const chunks = []; let rest = content, reopen = "";
  while (rest.length) {
    const budget = cap - reopen.length - 8;
    let cut = rest.length <= budget ? rest.length : rest.lastIndexOf("\n", budget);
    if (cut <= 0) cut = Math.min(budget, rest.length);
    else if (cut < rest.length && rest[cut] === "\n") cut++;
    if (cut < rest.length && /[\uD800-\uDBFF]/.test(rest[cut - 1])) cut--;
    const raw = reopen + rest.slice(0, cut); rest = rest.slice(cut);
    let fence = "";
    for (const line of raw.split(/\r?\n/)) {
      if (!fence && /^```[^`]*$/.test(line.trim())) fence = line.trim();
      else if (fence && /^```\s*$/.test(line.trim())) fence = "";
    }
    chunks.push(raw + (fence ? "\n```" : "")); reopen = fence ? fence + "\n" : "";
  }
  return chunks.length ? chunks : [""];
}
