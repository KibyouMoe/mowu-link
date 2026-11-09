// Explicit staged-module acceptance only; does not start a Bridge, WS receiver,
// model, native task or supervisor. The persistent plugin still needs normal load.
import { readFile } from "node:fs/promises";
import { readFileSync } from "node:fs";
import path from "node:path";
import { FeishuNotifier, FEISHU_API_BASE } from "../feishu.mjs";
import { sendImage } from "../feishu-image-output.mjs";

const [imagePath, threadId, messageId, permission, recovery] = process.argv.slice(2);
if (!imagePath || !threadId || !messageId || permission !== "--staged-acceptance" || (recovery && recovery !== "--recover-unknown")) {
  process.stderr.write("Usage: send-feishu-image.mjs <absolute imagePath> <selected threadId> <same UUIDv4> --staged-acceptance [--recover-unknown]\n"); process.exit(2);
}
const stateDir = path.join(process.env.USERPROFILE ?? process.env.HOME, ".codex/state/codexworker");
try {
  const config = JSON.parse(await readFile(path.join(stateDir, "desktop-config.local.json"), "utf8"));
  const token = (await readFile(config.tokenFile, "utf8")).trim();
  const secret = JSON.parse(await readFile(config.feishu.configFile, "utf8"));
  const endpoint = `http://127.0.0.1:${config.port ?? 8903}`;
  const get = async route => {
    const response = await fetch(endpoint + route, { headers: { authorization: `Bearer ${token}` }, signal: AbortSignal.timeout(10000) });
    if (!response.ok) throw new Error("Cannot verify the existing Bridge target.");
    return response.json();
  };
  const health = await get("/health");
  if (health.integrationMode !== "desktop-native" || !health.ready) throw new Error("Existing desktop-native Bridge is not ready.");
  const statePath = path.join(stateDir, "desktop-manager-state.json");
  const selectedState = JSON.parse(await readFile(statePath, "utf8"));
  const bindingEpoch = selectedState.selectionEpoch;
  const bindingChanged = () => { throw Object.assign(new Error("Selected binding changed; no fallback."), {
    status: 409, body: { error: "selected_target_changed", detail: "Selected binding changed; no fallback.", retryable: false } }); };
  const validateBinding = () => {
    const state = JSON.parse(readFileSync(statePath, "utf8"));
    if (state.selectedThreadId !== threadId || state.selectionEpoch !== bindingEpoch) bindingChanged();
    if (recovery) {
      const entry = Object.values(state.autoImages ?? {}).find(e => e.messageId === messageId && e.threadId === threadId);
      if (!entry || !["failed", "sent"].includes(entry.status) || (entry.status === "failed" && entry.error !== "image_send_outcome_unknown") || entry.bindingEpoch !== bindingEpoch)
        bindingChanged();
    }
  };
  validateBinding();
  const notifier = new FeishuNotifier({ appId: secret.app_id, appSecret: secret.app_secret,
    chatId: secret.chat_id ?? secret.trainer_chat_id, bridgeName: config.bridgeName });
  const getTarget = async expected => {
    validateBinding();
    const state = await get("/manager/sessions");
    if (state.selectedThreadId !== threadId || (expected && expected !== threadId)) throw new Error("Selected target changed; no fallback.");
    const session = state.sessions.find(s => s.threadId === threadId);
    if (!session || (session.hostId && session.hostId !== "local")) throw new Error("Expected local selected task is unavailable.");
    const chatId = session.feishuChatId ?? notifier.chatId;
    if (!chatId) throw new Error("Feishu destination is unavailable.");
    validateBinding(); return { threadId, chatId };
  };
  const expected = await getTarget(threadId);
  const result = await sendImage({ imagePath, threadId, messageId }, { stateDir,
    allowedRoots: [...config.workspaceRoots, path.join(stateDir, "feishu-images")], displayedFiles: [imagePath],
    excludedRoots: recovery ? [path.join(stateDir, "feishu-images")] : [], recoveryRequested: Boolean(recovery),
    notifier, fetchImpl: fetch, getTarget, validateTarget: validateBinding });
  let readback = "unverified";
  const accessToken = await notifier.getToken();
  const response = await fetch(`${FEISHU_API_BASE}/im/v1/messages/${encodeURIComponent(result.feishuMessageId)}`,
    { headers: { authorization: `Bearer ${accessToken}` }, signal: AbortSignal.timeout(15000) });
  const payload = await response.json();
  const item = payload.data?.items?.find(item => item.message_id === result.feishuMessageId);
  if (response.ok && payload.code === 0 && item?.msg_type === "image" && item.chat_id === expected.chatId) readback = "image-message-confirmed";
  process.stdout.write(JSON.stringify({ ...result, readback, moduleAcceptanceOnly: true, persistentPluginVersion: health.version,
    recoveryRequested: Boolean(recovery), autoQueueUpdated: false }) + "\n");
} catch (error) {
  process.stderr.write(JSON.stringify(error.body ?? { error: "image_acceptance_failed", detail: "Image acceptance failed; inspect local configuration/target without printing credentials.", retryable: false }) + "\n");
  process.exitCode = 1;
}
