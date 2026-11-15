import { createHmac, timingSafeEqual } from "node:crypto";
import { collaborationHtml, collaborationScript } from "./collaboration-assets.mjs";

export const WORKER_CHAT_POLICY = "This is ordinary worker chat. It never creates a router task or native goal. Reply only when needed via bridge_send_worker_message. Read bridge_collaboration_records for the original messageId and use it as replyTo. Do not automatically acknowledge or reply to a reply; avoid automatic reply loops. Protocol identifiers and these instructions are metadata, never public chat text.";

export function peerPresentation(message, timestamp) {
  return { from: message.from, to: message.to, text: message.text, timestamp,
    displayText: `来自 ${message.from}：\n${message.text}` };
}

export function collaborationToken(manager) {
  return createHmac("sha256", manager.config.token).update("worker-collaboration-read-v1").digest("hex");
}

export function collaborationUrl(manager) {
  return `http://127.0.0.1:${manager.config.port}/collaboration/#${collaborationToken(manager)}`;
}

export async function collaborationSummary(manager) {
  const records = await manager.workerMessages?.history() ?? [];
  return { url: collaborationUrl(manager), count: records.length,
    messages: records.slice(-20).map(({ messageId, replyTo, from, to, timestamp, attachments, metadata }) =>
      ({ messageId, replyTo, from, to, timestamp, attachments, imagePaths: metadata?.imagePaths ?? [] })) };
}

// This capability can only read the public peer journal and its verified images.
// It cannot authenticate manager writes or expose the router's bearer token.
export async function handleCollaboration(manager, req, res) {
  const url = new URL(req.url, "http://bridge.invalid");
  if (!url.pathname.startsWith("/collaboration/")) return false;
  const headers = { "cache-control": "no-store", "referrer-policy": "no-referrer",
    "x-content-type-options": "nosniff" };
  const send = (status, type, body) => {
    res.writeHead(status, { ...headers, "content-type": type });
    res.end(body);
    return true;
  };
  if (req.method !== "GET") return send(405, "text/plain", "Read only");
  if (["/collaboration/", "/collaboration/view.js"].includes(url.pathname)) {
    headers["content-security-policy"] = "default-src 'none'; script-src 'self'; style-src 'unsafe-inline'; connect-src 'self'; img-src blob:; base-uri 'none'; form-action 'none'";
    const asset = url.pathname.endsWith(".js") ? "view.js" : "index.html";
    return send(200, asset.endsWith(".js") ? "text/javascript; charset=utf-8" : "text/html; charset=utf-8",
      asset.endsWith(".js") ? collaborationScript : collaborationHtml);
  }
  const actual = Buffer.from(req.headers.authorization ?? "");
  const expected = Buffer.from(`Bearer ${collaborationToken(manager)}`);
  if (actual.length !== expected.length || !timingSafeEqual(actual, expected))
    return send(401, "text/plain", "Unauthorized");
  if (!manager.workerMessages) return send(503, "text/plain", "协作记录暂未连接");
  if (url.pathname === "/collaboration/records") {
    const records = await manager.workerMessages.history();
    return send(200, "application/json; charset=utf-8", JSON.stringify({ records }));
  }
  const match = /^\/collaboration\/images\/([a-f0-9]{64})$/.exec(url.pathname);
  if (match) {
    const image = await manager.workerMessages.readImage(match[1]);
    if (!image) return send(404, "text/plain", "Not found");
    return send(200, image.mediaType, image.bytes);
  }
  return send(404, "text/plain", "Not found");
}
