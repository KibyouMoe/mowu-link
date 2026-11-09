// Explicit live acceptance only; never invoked by the offline node --test suite.
// Prepare a synthetic PNG, display its absolute path in an assistant message,
// then check the real automatic relay receipt and Feishu's stored image message.
import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { deflateSync } from "node:zlib";
import { FeishuNotifier } from "../feishu.mjs";

const [mode, threadId, stateDir, outputPath] = process.argv.slice(2);
if (!["prepare", "check"].includes(mode) || !/^[a-f\d-]{36}$/i.test(threadId ?? "") || !path.isAbsolute(stateDir ?? ""))
  throw new Error("Usage: live-public-image-acceptance.mjs prepare|check <authorized selected threadId> <absolute machine stateDir>");
const config = JSON.parse(await readFile(path.join(stateDir, "desktop-config.local.json"), "utf8"));
const token = (await readFile(config.tokenFile, "utf8")).trim();
const healthResponse = await fetch(`http://127.0.0.1:${config.port ?? 8903}/health`, { headers: { authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(8000) });
if (!healthResponse.ok) throw new Error("Authenticated Bridge health unavailable");
const health = await healthResponse.json();
if (health.integrations.selectedThreadId !== threadId || health.capabilities.feishuPublicImageSync !== "selected-thread-public-images" || !health.integrations.feishu.connected)
  throw new Error("Selected local image relay is not ready; no file prepared or test dispatched");
const manifestPath = path.join(stateDir, "public-image-acceptance.local.json");
const sha = bytes => createHash("sha256").update(bytes).digest("hex");

function png(seed) {
  const width = 320, height = 160, rows = Buffer.alloc((1 + width * 3) * height);
  const colors = [[65, 125, 225], [36, 175, 128], [245, 176, 66], [148, 99, 211]];
  for (let y = 0; y < height; y++) for (let x = 0; x < width; x++) {
    const color = x < 12 || y < 12 || x >= width - 12 || y >= height - 12 ? [245, 246, 250]
      : y >= 124 ? [seed[x % seed.length], 60, 100] : colors[Math.min(3, Math.floor(x / 80))];
    const offset = y * (1 + width * 3) + 1 + x * 3; rows.set(color, offset);
  }
  const crc = bytes => { let value = 0xffffffff; for (const byte of bytes) { value ^= byte;
    for (let bit = 0; bit < 8; bit++) value = value & 1 ? (value >>> 1) ^ 0xedb88320 : value >>> 1; } return (value ^ 0xffffffff) >>> 0; };
  const chunk = (type, bytes) => { const name = Buffer.from(type), prefix = Buffer.alloc(4), suffix = Buffer.alloc(4);
    prefix.writeUInt32BE(bytes.length); suffix.writeUInt32BE(crc(Buffer.concat([name, bytes]))); return Buffer.concat([prefix, name, bytes, suffix]); };
  const header = Buffer.alloc(13); header.writeUInt32BE(width); header.writeUInt32BE(height, 4); header[8] = 8; header[9] = 2;
  return Buffer.concat([Buffer.from("89504e470d0a1a0a", "hex"), chunk("IHDR", header), chunk("IDAT", deflateSync(rows)), chunk("IEND", Buffer.alloc(0))]);
}

if (mode === "prepare") {
  if (outputPath && !path.isAbsolute(outputPath)) throw new Error("Optional acceptance output must be an authorized absolute local path");
  const filename = outputPath ?? path.join(process.env.CODEX_HOME || path.join(os.homedir(), ".codex"), "generated_images", threadId, `bridge-relay-acceptance-${randomUUID()}.png`);
  const bytes = png(Buffer.from(randomUUID().replaceAll("-", ""), "hex"));
  await mkdir(path.dirname(filename), { recursive: true }); await writeFile(filename, bytes, { flag: "wx", mode: 0o600 });
  await writeFile(manifestPath, JSON.stringify({ threadId, filename, imageSha256: sha(bytes), preparedAt: Date.now() }), { mode: 0o600 });
  console.log(JSON.stringify({ status: "prepared-not-sent", filename, imageBytes: bytes.length, threadId }));
} else {
  const manifest = JSON.parse(await readFile(manifestPath, "utf8"));
  if (manifest.threadId !== threadId || sha(await readFile(manifest.filename)) !== manifest.imageSha256) throw new Error("Acceptance fixture changed; not checked");
  const ledger = JSON.parse(await readFile(path.join(stateDir, "image-output-state.json"), "utf8"));
  const receipt = Object.entries(ledger.receipts).find(([, r]) => r.threadId === threadId && r.result?.imageSha256 === manifest.imageSha256 && Date.parse(r.createdAt) >= manifest.preparedAt);
  if (!receipt) { console.log(JSON.stringify({ status: "pending", threadId, publicImages: health.integrations.feishu.publicImages })); }
  else {
    const [messageId, image] = receipt;
    const managerState = JSON.parse(await readFile(path.join(stateDir, "desktop-manager-state.json"), "utf8"));
    const automatic = Object.values(managerState.autoImages ?? {}).find(r => r.messageId === messageId);
    if (!automatic || automatic.status !== "sent" || image.status !== "sent") throw new Error("Automatic relay not confirmed sent; no retry performed");
    const f = config.feishu ?? {}, secret = f.configFile ? JSON.parse(await readFile(f.configFile, "utf8")) : {};
    const notifier = new FeishuNotifier({ appId: secret.app_id, appSecret: secret.app_secret, ...f });
    const accessToken = await notifier.getToken();
    if (!accessToken) throw new Error("Feishu readback authorization unavailable");
    const response = await fetch(`https://open.feishu.cn/open-apis/im/v1/messages/${encodeURIComponent(image.result.feishuMessageId)}`,
      { headers: { authorization: `Bearer ${accessToken}` }, redirect: "error", signal: AbortSignal.timeout(10000) });
    const result = await response.json(), stored = result.data?.items?.[0];
    const expectedChat = managerState.sessions[threadId].feishuChatId ?? f.chatId ?? secret.chat_id ?? secret.trainer_chat_id;
    if (!response.ok || result.code !== 0 || stored?.msg_type !== "image" || !JSON.parse(stored.body?.content ?? "{}").image_key || stored.chat_id !== expectedChat)
      throw new Error("Feishu stored image message could not be confirmed; no resend performed");
    console.log(JSON.stringify({ status: "sent-and-read-back", automatic: true, threadId, messageId,
      feishuMessageId: image.result.feishuMessageId, msgType: stored.msg_type, imageBytes: image.result.imageBytes, destinationMatched: true,
      humanReceipt: "not-yet-confirmed" }));
  }
}
