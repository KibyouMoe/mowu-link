import { readFile } from "node:fs/promises";
import path from "node:path";

export async function modelSettings({ rpc, stateDir, request }) {
  if (!["read", "update"].includes(request.action) || typeof request.threadId !== "string")
    throw new Error("native_model_request_invalid");
  const state = JSON.parse(await readFile(path.join(stateDir, "desktop-manager-state.json"), "utf8"));
  const session = state.sessions?.[request.threadId];
  if (!session || ![undefined, null, "local"].includes(session.hostId)) throw new Error("native_model_thread_unavailable");
  const models = [], seen = new Set();
  let cursor;
  do {
    const page = await rpc("model/list", { includeHidden: false, limit: 100, ...(cursor ? { cursor } : {}) });
    if (!Array.isArray(page.data)) throw new Error("native_model_catalog_invalid");
    models.push(...page.data.filter(model => !model.hidden).map(model => ({ id: model.model, name: model.displayName,
      defaultEffort: model.defaultReasoningEffort, isDefault: model.isDefault,
      efforts: model.supportedReasoningEfforts.map(effort => ({ id: effort.reasoningEffort, name: effort.reasoningEffort })) })));
    cursor = page.nextCursor;
    if (cursor != null && (typeof cursor !== "string" || !cursor || seen.has(cursor))) throw new Error("native_model_cursor_invalid");
    seen.add(cursor);
  } while (cursor != null);
  if (request.action === "update") {
    const model = models.find(model => model.id === request.model);
    if (!model || request.effort != null && !model.efforts.some(effort => effort.id === request.effort))
      throw new Error("native_model_selection_invalid");
    await rpc("thread/settings/update", { threadId: request.threadId, model: model.id, effort: request.effort });
  }
  const { thread } = await rpc("thread/read", { threadId: request.threadId, includeTurns: false });
  const primary = models.find(model => model.isDefault);
  return { models, current: { model: thread.model, effort: thread.reasoningEffort ?? null },
    defaultSelection: primary ? { model: primary.id, effort: primary.defaultEffort ?? null } : null };
}
