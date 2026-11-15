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
export const prefersMarkdown = content => markdownHint.test(content);

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
