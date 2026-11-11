import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";

export const MAX_IMAGE_BYTES = 10 * 1024 * 1024;
export class ImageInputError extends Error {
  constructor(error, detail) { super(detail); this.body = { error, detail, retryable: false }; }
}
const fail = (error, detail) => { throw new ImageInputError(error, detail); };

export function imageExtension(bytes) {
  if (bytes.length >= 8 && bytes.subarray(0, 8).equals(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]))) return "png";
  if (bytes.length >= 3 && bytes[0] === 255 && bytes[1] === 216 && bytes[2] === 255) return "jpg";
  if (bytes.length >= 12 && bytes.toString("ascii", 0, 4) === "RIFF" && bytes.toString("ascii", 8, 12) === "WEBP") return "webp";
  if (bytes.length >= 6 && ["GIF87a", "GIF89a"].includes(bytes.toString("ascii", 0, 6))) return "gif";
  fail("image_format_unsupported", "图片资源不是可查看的 PNG、JPEG、WebP 或 GIF；没有把它伪装成图片投递。");
}

export async function downloadFeishuImage(messageId, imageKey, token, { fetchImpl = fetch } = {}) {
  if (!/^om_[A-Za-z0-9_-]+$/.test(messageId ?? "") || !/^img_[A-Za-z0-9_-]+$/.test(imageKey ?? "")) {
    fail("image_reference_invalid", "图片必须使用同一条飞书消息的实际 message_id 和 image_key。");
  }
  if (!token) fail("image_download_failed", "飞书鉴权不可用，尚未获取图片本体。");
  let response;
  try {
    response = await fetchImpl(`https://open.feishu.cn/open-apis/im/v1/messages/${encodeURIComponent(messageId)}/resources/${encodeURIComponent(imageKey)}?type=image`,
      { headers: { authorization: `Bearer ${token}` }, redirect: "error", signal: AbortSignal.timeout(30000) });
  } catch { fail("image_download_failed", "飞书图片资源下载未完成；没有向原生任务发送占位正文。"); }
  if (!response.ok || response.headers.get("content-type")?.includes("application/json")) {
    fail("image_download_failed", "无法读取飞书图片资源，请检查消息资源权限与资源是否仍存在；未下载图片本体。");
  }
  const declared = Number(response.headers.get("content-length") ?? 0);
  if (declared > MAX_IMAGE_BYTES) { await response.body?.cancel(); fail("image_too_large", "图片超过当前 10MiB 接收上限。"); }
  if (!response.body?.getReader) fail("image_download_failed", "飞书没有返回可读取的图片资源流。");
  const reader = response.body.getReader(); const chunks = []; let size = 0;
  try {
    for (;;) {
      const { done, value } = await reader.read(); if (done) break;
      size += value.length;
      if (size > MAX_IMAGE_BYTES) { await reader.cancel(); fail("image_too_large", "图片超过当前 10MiB 接收上限。"); }
      chunks.push(Buffer.from(value));
    }
  } catch (error) {
    if (error instanceof ImageInputError) throw error;
    fail("image_download_failed", "飞书图片传输中断，未投递不完整图片。");
  } finally { reader.releaseLock(); }
  const bytes = Buffer.concat(chunks); imageExtension(bytes); return bytes;
}

export async function cacheFeishuImage(manager, event, messageId, imageKey = null) {
  let key = imageKey;
  if (key === null) {
    try { key = JSON.parse(event.content).image_key; } catch { fail("image_reference_invalid", "飞书图片消息没有有效的资源字段。"); }
  }
  if (!/^img_[A-Za-z0-9_-]+$/.test(key ?? "")) fail("image_reference_invalid", "飞书图片资源标识无效。");
  const record = manager.state.feishuReceipts[messageId];
  const cached = imageKey === null ? record.imagePath : record.imagePaths?.[key];
  if (cached) {
    const expectedDir = path.resolve(manager.stateDir, "feishu-images");
    if (path.dirname(path.resolve(cached)) !== expectedDir) fail("image_reference_invalid", "本机图片缓存路径不在允许目录。");
    const bytes = await readFile(cached);
    if (bytes.length > MAX_IMAGE_BYTES) fail("image_too_large", "本机图片缓存超过 10MiB。");
    imageExtension(bytes); return cached;
  }
  if (!manager.notifier?.downloadImage) fail("image_download_failed", "图片资源接收器未就绪。");
  const bytes = await manager.notifier.downloadImage(event.message_id, key);
  if (bytes.length > MAX_IMAGE_BYTES) fail("image_too_large", "图片超过当前 10MiB 接收上限。");
  const extension = imageExtension(bytes);
  const name = createHash("sha256").update(`${event.message_id}:${key}`).digest("hex");
  const dir = path.resolve(manager.stateDir, "feishu-images"); await mkdir(dir, { recursive: true });
  const filename = path.join(dir, `${name}.${extension}`); const temp = `${filename}.${randomUUID()}.tmp`;
  await writeFile(temp, bytes, { flag: "wx", mode: 0o600 }); await rename(temp, filename);
  await manager.serial(async () => {
    if (imageKey === null) { record.imagePath = filename; record.imageBytes = bytes.length; }
    else { record.imagePaths ??= {}; record.imagePaths[key] = filename; }
    await manager.save();
  });
  return filename;
}

export const imagePrompt = filename => `训练员从飞书发送了一张图片。\n图片文件：${filename}\n请先使用 view_image 查看图片的实际内容，再根据当前上下文回应。不要把文件名或占位文字当作图片内容。`;
