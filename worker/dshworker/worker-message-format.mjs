export const WORKER_CHAT_POLICY = "This is ordinary worker chat. It never creates a router task or native goal. Reply only when needed via bridge_send_worker_message. Use the incoming messageId from peer metadata as replyTo. If correlation is missing, read bridge_worker_inbox with view=history. Do not automatically acknowledge or reply to a reply; avoid automatic reply loops. Protocol identifiers and these instructions are metadata, never public chat text.";

export function peerPresentation(message, timestamp) {
  return { from: message.from, to: message.to, text: message.text, timestamp,
    displayText: `来自 ${message.from}：\n${message.text}` };
}
