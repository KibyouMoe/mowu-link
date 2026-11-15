import { open } from "node:fs/promises";
import path from "node:path";
import { randomUUID } from "node:crypto";
import { imageType } from "../router-attachments.mjs";

function attachmentStore(transport, agent) {
  const store = agent?.ctx?.get?.("attachments") ?? transport.ctx?.get?.("attachments");
  return typeof store?.saveImages === "function" ? store : null;
}

export function dshImageInputAvailable(transport) {
  return Boolean(attachmentStore(transport) || [...(transport.live?.values() ?? [])].some(entry => attachmentStore(transport, entry.agent)));
}

async function imageInput(filename, limit) {
  if (typeof filename !== "string" || !path.isAbsolute(filename) || /^(?:\\\\|\/\/)/.test(filename))
    throw new Error("Image input must name an absolute local file.");
  const file = await open(filename, "r");
  try {
    const stat = await file.stat();
    if (!stat.isFile() || stat.size < 1 || stat.size > limit) throw new Error("Image exceeds native attachment limits.");
    const data = Buffer.alloc(stat.size);
    let offset = 0;
    while (offset < data.length) {
      const result = await file.read(data, offset, data.length - offset, offset);
      if (!result.bytesRead) throw new Error("Image changed while reading.");
      offset += result.bytesRead;
    }
    if ((await file.stat()).size !== stat.size) throw new Error("Image changed while reading.");
    return { data, mediaType: imageType(data), name: path.basename(filename) };
  } finally { await file.close(); }
}

export async function prepareDshImageMessage(transport, thread, { nativeImages, nativeMessageId }, text) {
  try {
    if (!Array.isArray(nativeImages) || !nativeImages.length) throw new Error("Image inputs are required.");
    if (typeof text !== "string" || !text.trim()) throw new Error("The image prompt is required.");
    const agent = await transport.ensureAgent(thread), store = attachmentStore(transport, agent);
    if (!store) throw new Error("Native attachments are unavailable.");
    const limits = store.imageLimits;
    if (nativeImages.length > limits.maxImagesPerMessage) throw new Error("Too many native image inputs.");
    const inputs = [];
    let total = 0;
    for (const filename of nativeImages) {
      const input = await imageInput(filename, Math.min(limits.maxImageBytes, limits.maxMessageImageBytes));
      total += input.data.length;
      if (total > limits.maxMessageImageBytes) throw new Error("Native image batch exceeds its byte limit.");
      inputs.push(input);
    }
    // The SDK validates the complete batch before publishing any references.
    const images = await store.saveImages(inputs);
    if (images.length !== inputs.length || transport.closing) throw new Error("Native image admission did not finish.");
    return { id: nativeMessageId ?? randomUUID(), role: "user", source: { kind: "user" },
      content: [{ type: "text", text }, ...images.map(attachment => ({ type: "image", attachment }))] };
  } catch (cause) {
    transport.log("warn", "native_image_input_rejected", { threadId: thread.id, causeCode: cause?.code ?? null });
    throw Object.assign(new Error("原生图片输入准备失败，尚未发送到会话。"), {
      code: "dsh_image_input_rejected", nativeInputOutcome: "notAttempted", cause,
    });
  }
}
