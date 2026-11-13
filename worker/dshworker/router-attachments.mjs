import { open } from "node:fs/promises";
import path from "node:path";
import { createHash } from "node:crypto";

export const MAX_ROUTER_IMAGE_BYTES = 100_000_000;

export function imageType(bytes) {
  if (bytes.subarray(0, 8).equals(Buffer.from([137,80,78,71,13,10,26,10]))) return "image/png";
  if (bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) return "image/jpeg";
  if (["GIF87a", "GIF89a"].includes(bytes.subarray(0, 6).toString())) return "image/gif";
  if (bytes.subarray(0, 4).toString() === "RIFF" && bytes.subarray(8, 12).toString() === "WEBP") return "image/webp";
  throw new Error("Unsupported image attachment.");
}

// Explicitly authorized file only. A retry MUST reuse messageId; neither source
// paths nor credentials are placed in the recipient's message.
export async function uploadRouterImage({ imagePath, url, token, fetchImpl = fetch }) {
  if (!token) throw new Error("Invalid router attachment destination.");
  const endpoint = new URL(url);
  if (!["http:", "https:"].includes(endpoint.protocol) || endpoint.username || endpoint.password || endpoint.search || endpoint.hash)
    throw new Error("Use the configured router origin without embedded credentials.");
  const file = await open(imagePath, "r");
  let bytes;
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size < 1 || stat.size > MAX_ROUTER_IMAGE_BYTES) throw new Error("Image must be 1..100000000 bytes.");
    // Bound reads even if another process grows the authorized file.
    bytes = Buffer.alloc(stat.size);
    let offset = 0;
    while (offset < bytes.length) {
      const r = await file.read(bytes, offset, bytes.length-offset, offset);
      if (!r.bytesRead) throw new Error("Image changed while reading.");
      offset += r.bytesRead;
    }
    if ((await file.stat()).size !== stat.size) throw new Error("Image changed while reading.");
  } finally { await file.close(); }
  const mediaType = imageType(bytes);
  const sha = createHash("sha256").update(bytes).digest("hex");
  const base = url.replace(/\/+$/, "");
  const upload = await fetchImpl(base + "/attachments", { method: "POST",
    redirect: "error", signal: AbortSignal.timeout(120000),
    headers: { "X-Router-Token": token, "Content-Type": mediaType, "X-Attachment-SHA256": sha },
    body: bytes });
  if (!upload.ok) throw new Error("Router image upload rejected: HTTP " + upload.status);
  const { attachment } = await upload.json();
  if (attachment?.id !== sha || attachment?.sha256 !== sha ||
      attachment?.size !== bytes.length || attachment?.media_type !== mediaType)
    throw new Error("Router attachment receipt mismatch.");
  // Return only verified metadata, never arbitrary server fields or image bytes.
  return { id: sha, sha256: sha, size: bytes.length, media_type: mediaType, name: path.basename(imagePath) };
}

export async function sendRouterImage({ imagePath, messageId, text = "图片附件",
  worker, url, token, urgency = "normal", fetchImpl = fetch }) {
  if (!/^[0-9a-f]{8}-[0-9a-f-]{27}$/i.test(messageId ?? "")) throw new Error("Reuse an explicit UUID messageId.");
  if (!worker || !token || !["normal", "urgent"].includes(urgency)) throw new Error("Invalid router attachment destination.");
  const attachment = await uploadRouterImage({ imagePath, url, token, fetchImpl });
  const base = url.replace(/\/+$/, "");
  const response = await fetchImpl(base + "/notice", { method: "POST", redirect: "error",
    signal: AbortSignal.timeout(15000), headers: { "X-Router-Token": token, "Content-Type": "application/json" },
    body: JSON.stringify({ worker, text, kind: "worker.image", urgency, message_id: messageId,
      attachments: [{ id: attachment.id, name: attachment.name }] }) });
  if (!response.ok) throw new Error("Router image notice rejected: HTTP " + response.status + "; retry with the same messageId.");
  const receipt = await response.json();
  return { ...receipt, messageId, attachment: { ...attachment, name: path.basename(imagePath) } };
}
