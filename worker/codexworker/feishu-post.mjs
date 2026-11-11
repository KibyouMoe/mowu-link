import { cacheFeishuImage, ImageInputError } from "./feishu-images.mjs";

const invalid = detail => { throw new ImageInputError("post_content_invalid", detail); };
export function parseFeishuPost(content) {
  let value;
  try { value = JSON.parse(content); } catch { invalid("飞书图文消息不是有效 JSON。"); }
  if (!value || typeof value !== "object" || Array.isArray(value)) invalid("飞书图文消息格式无效。");
  if (!Array.isArray(value.content) && !Array.isArray(value.content_v2)) {
    value = [value.zh_cn, value.en_us, ...Object.values(value)].find(v =>
      v && typeof v === "object" && (Array.isArray(v.content) || Array.isArray(v.content_v2)));
  }
  if (!value) invalid("飞书图文消息缺少正文。");
  const rows = Array.isArray(value.content_v2) ? value.content_v2 : value.content;
  if (!Array.isArray(rows)) invalid("飞书图文消息缺少正文。");
  const images = [];
  const lines = rows.map(row => {
    if (!Array.isArray(row)) invalid("飞书图文消息段落格式无效。");
    return row.map(node => {
      if (!node || typeof node !== "object") invalid("飞书图文消息元素格式无效。");
      if (node.tag === "img") {
        if (!/^img_[A-Za-z0-9_-]+$/.test(node.image_key ?? "")) invalid("飞书图文图片资源标识无效。");
        if (!images.includes(node.image_key)) images.push(node.image_key);
        return { imageKey: node.image_key };
      }
      if (["text", "a", "at", "emotion"].includes(node.tag)) {
        const text = node.text ?? node.user_name ?? node.emoji_type ?? "";
        if (typeof text !== "string") invalid("飞书图文消息文字格式无效。");
        return { text: node.tag === "a" && node.href ? text + " (" + node.href + ")" : text };
      }
      invalid("飞书图文消息含暂不支持的元素：" + String(node.tag ?? "unknown"));
    });
  });
  if (images.length > 10) invalid("同一条图文消息最多接收 10 张图片；本条未部分投递。");
  const title = value.title ?? "";
  if (typeof title !== "string") invalid("飞书图文标题格式无效。");
  if (!title.trim() && !images.length && !lines.some(row => row.some(n => n.text?.trim()))) invalid("飞书图文消息为空。");
  return { title, lines, images };
}

export async function feishuPostPrompt(manager, event, messageId, post = parseFeishuPost(event.content)) {
  const paths = new Map();
  for (const key of post.images) paths.set(key, await cacheFeishuImage(manager, event, messageId, key));
  const text = [post.title, ...post.lines.map(row =>
    row.map(n => n.imageKey ? "\n图片文件：" + paths.get(n.imageKey) + "\n" : n.text).join(""))].filter(Boolean).join("\n");
  if (!post.images.length) return text;
  return "训练员从飞书发送了图文消息：\n" + text +
    "\n请先使用 view_image 查看以上每张图片的实际内容，再回答同一条消息里的问题；不要把文件名或占位文字当作图片内容。";
}
