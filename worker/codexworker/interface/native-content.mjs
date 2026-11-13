// Desktop journal uses PascalCase tags; newer content blocks use camelCase.
// Match only explicitly requested tags. Never normalize text, paths or IDs.
export const isNativeType = (value, tag) => value === tag || value === tag[0].toLowerCase() + tag.slice(1);

const invalid = code => { throw Object.assign(new Error(code), { code }); };
export function nativeContentText(content, { input = false } = {}) {
  if (!Array.isArray(content)) invalid("public_journal_public_text_incomplete");
  return content.map(part => {
    if (isNativeType(part?.type, "Text")) {
      if (typeof part.text !== "string") invalid("public_journal_public_text_incomplete");
      return part.text;
    }
    if (input && (isNativeType(part?.type, "Image") || isNativeType(part?.type, "LocalImage"))) return "[图片输入]";
    invalid("public_journal_content_unsupported");
  }).join("");
}

export function nativeMessageText(item) {
  const input = isNativeType(item?.type, "UserMessage");
  if (!input && !isNativeType(item?.type, "AgentMessage")) return "";
  if (typeof item.text === "string") return item.text;
  if (item.content == null) return "";
  // Legacy read_thread snapshots omit the Text tag; journal records do not.
  const content = Array.isArray(item.content) ? item.content.map(part =>
    part?.type == null && typeof part?.text === "string" ? { ...part, type: "Text" } : part) : item.content;
  return nativeContentText(content, { input });
}
