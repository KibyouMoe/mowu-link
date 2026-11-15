import path from "node:path";
import { localImagePath, markdownImageSpans } from "../public-image-relay.mjs";

export function resolveDshMediaPath(value, cwd) {
  if (typeof value !== "string" || !value.trim()) return null;
  let reference = value.trim();
  if (reference.startsWith("<") && reference.endsWith(">") || reference.startsWith('"') && reference.endsWith('"'))
    reference = reference.slice(1, -1);
  const absolute = localImagePath(reference);
  const windowsCwd = /^[a-z]:[\\/]/i.test(cwd ?? "");
  // Rooted Windows paths inherit the source chat's drive, not the server's drive.
  if (windowsCwd && /^[\\/](?![\\/])/.test(absolute ?? reference))
    return path.win32.resolve(cwd, absolute ?? reference);
  if (absolute) return absolute;
  let decoded = reference;
  try { decoded = decodeURIComponent(reference); } catch { /* Preserve literal percent signs. */ }
  if (!cwd || /^(?:[a-z][a-z\d+.-]*:|\\\\|\/\/)/i.test(decoded)) return null;
  const filesystem = windowsCwd ? path.win32 : path;
  return localImagePath(filesystem.resolve(cwd, reference));
}

function mediaDirectives(text, cwd, complete) {
  const spans = [];
  let offset = 0, fence = null;
  for (const line of text.match(/[^\n]*(?:\n|$)/g).filter(Boolean)) {
    const body = line.replace(/\r?\n$/, "");
    const marker = body.match(/^ {0,3}(`{3,}|~{3,})/);
    if (marker) {
      if (!fence) fence = marker[1];
      else if (marker[1][0] === fence[0] && marker[1].length >= fence.length) fence = null;
    } else if (!fence) {
      const directive = body.match(/^ {0,3}MEDIA:[ \t]*(.*?)[ \t]*$/);
      const tail = body.replace(/^ {0,3}/, "");
      if (!complete && !line.endsWith("\n") && (directive || tail && "MEDIA:".startsWith(tail))) {
        spans.push({ start: offset, end: offset + line.length, pending: true });
      } else if (directive) {
        const imagePath = resolveDshMediaPath(directive[1], cwd);
        if (imagePath) spans.push({ start: offset, end: offset + line.length, imagePath });
      }
    }
    offset += line.length;
  }
  return spans;
}

export function projectDshMedia(text, { cwd, complete = false } = {}) {
  const spans = [...markdownImageSpans(text, { resolvePath: value => resolveDshMediaPath(value, cwd) }),
    ...mediaDirectives(text, cwd, complete)];
  const pendingImage = text.lastIndexOf("![");
  if (!complete && pendingImage >= 0 && !text.slice(pendingImage).includes("\n") &&
      !spans.some(span => span.start <= pendingImage && span.end > pendingImage))
    spans.push({ start: pendingImage, end: text.length, pending: true });
  spans.sort((left, right) => left.start - right.start || right.end - left.end);
  const parts = [];
  let offset = 0;
  for (const span of spans) {
    if (span.start < offset) continue;
    const before = text.slice(offset, span.start);
    if (before.trim()) parts.push({ type: "text", text: before });
    if (!span.pending) parts.push({ type: "image", imagePath: span.imagePath });
    offset = span.end;
  }
  if (text.slice(offset).trim()) parts.push({ type: "text", text: text.slice(offset) });
  return parts;
}
