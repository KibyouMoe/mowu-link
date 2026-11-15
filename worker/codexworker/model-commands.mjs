import { createHash, randomUUID } from "node:crypto";
import { mkdir, readFile, rename, writeFile } from "node:fs/promises";
import path from "node:path";
import { UUID_V4_PATTERN } from "./identifiers.mjs";

const PAGE_SIZE = 10;
const sameSelection = (left, right) => left?.model === right?.model && (left?.effort ?? null) === (right?.effort ?? null);
const reject = (status, error, detail) => { throw Object.assign(new Error(detail), { status, body: { error, detail } }); };

// Worker defaults are separate from Desktop's global defaults and existing chats.
export class ModelCommands {
  constructor(manager) {
    this.manager = manager;
    this.file = path.join(manager.stateDir, "model-settings.local.json");
    this.tail = Promise.resolve();
    this.pages = new Map();
    this.pending = 0;
  }

  async load() {
    try {
      const state = JSON.parse(await readFile(this.file, "utf8"));
      if (state.version !== 1 || !state.defaults || typeof state.defaults.model !== "string" ||
          !["string", "undefined"].includes(typeof state.defaults.effort) && state.defaults.effort !== null)
        throw new Error("invalid model settings");
      return state;
    } catch (error) {
      if (error.code === "ENOENT") return null;
      reject(503, "worker_model_config_invalid", "Worker 模型配置无法读取，请检查 model-settings.local.json。");
    }
  }

  async save(state) {
    await mkdir(this.manager.stateDir, { recursive: true });
    const temporary = this.file + "." + randomUUID() + ".tmp";
    await writeFile(temporary, JSON.stringify(state, null, 2) + "\n", { flag: "wx", mode: 0o600 });
    await rename(temporary, this.file);
  }

  native(threadId, action, selection) {
    if (Array.isArray(this.manager.tools) && !this.manager.tools.some(tool => tool.name === "model_settings"))
      reject(503, "native_model_control_unavailable", "当前客户端原生模型控制尚未加载，请更新对应连接适配器并正常重开客户端。");
    return this.manager.call("model_settings", { threadId, action, ...selection });
  }

  async settle(state) {
    if (!state.pending) return state;
    const { threadId, selection, previous } = state.pending;
    const abandon = async () => { const reverted = { ...state }; delete reverted.pending; await this.save(reverted); };
    let observed = await this.native(threadId, "read");
    if (!sameSelection(observed.current, selection) && !sameSelection(observed.current, previous)) {
      await abandon();
      reject(409, "concurrent_model_change", "客户端模型设置已被其他操作修改，请重新发送命令。");
    }
    try {
      if (!sameSelection(observed.current, selection)) observed = await this.native(threadId, "update", selection);
      if (!sameSelection(observed.current, selection))
        reject(503, "model_update_unconfirmed", "原生会话模型读回未匹配，配置事务保留等待核对。");
    } catch (error) {
      // A read proving the original value lets us abandon a rejected update.
      // Uncertain outcomes retain their exact thread and desired selection.
      const latest = await this.native(threadId, "read").catch(() => null);
      if (latest && !sameSelection(latest.current, selection)) {
        await abandon();
        if (!sameSelection(latest.current, previous))
          reject(409, "concurrent_model_change", "客户端模型设置已被其他操作修改，请重新发送命令。");
      }
      throw error;
    }
    const completed = { ...state, defaults: state.pending.defaults }; delete completed.pending;
    await this.save(completed);
    return completed;
  }

  run(input) {
    this.pending++;
    const task = this.tail.then(async () => {
      if (!UUID_V4_PATTERN.test(input.messageId ?? "")) reject(400, "invalid_command_id", "模型命令需要稳定的请求标识。");
      const fingerprint = createHash("sha256").update(JSON.stringify([
        input.kind, input.action, input.threadId, input.value, input.page, input.direction])).digest("hex");
      const previous = (await this.load())?.receipts?.[input.messageId];
      if (previous) {
        if (previous.fingerprint !== fingerprint) reject(409, "model_command_conflict", "同一条命令的请求标识与内容不一致。");
        return previous.result;
      }
      const result = await this.execute(input);
      const state = await this.load();
      const receipts = { ...state.receipts, [input.messageId]: { fingerprint, result } };
      state.receipts = Object.fromEntries(Object.entries(receipts).slice(-100));
      await this.save(state);
      return result;
    }).finally(() => { this.pending--; });
    this.tail = task.catch(() => {});
    return task;
  }

  defaults(threadId) {
    this.pending++;
    const task = this.tail.then(() => this.readDefaults(threadId)).finally(() => { this.pending--; });
    this.tail = task.catch(() => {});
    return task;
  }

  async readDefaults(threadId) {
    let state = await this.load();
    if (!state) {
      const native = await this.native(threadId, "read");
      const defaults = native.defaultSelection ?? native.current;
      if (typeof defaults?.model !== "string" || !defaults.model)
        reject(503, "worker_model_default_unavailable", "原生默认模型尚未就绪。");
      state = { version: 1, defaults };
      await this.save(state);
    }
    return (await this.settle(state)).defaults;
  }

  async execute(input) {
    const { kind, action, value, threadId } = input;
    if (!["model", "effort"].includes(kind) || !["list", "set"].includes(action) ||
        !this.manager.state.sessions[threadId]) reject(400, "invalid_model_command", "使用 /model 或 /effort。");
    if (threadId !== this.manager.state.selectedThreadId)
      reject(409, "selected_target_changed", "当前会话已切换，请在新会话重新发送模型命令。");
    const defaults = await this.readDefaults(threadId);
    const snapshot = await this.native(threadId, "read");
    const currentModel = snapshot.models.find(model => model.id === snapshot.current.model);
    if (kind === "effort" && !currentModel) reject(503, "current_model_unavailable", "当前会话模型不在原生目录中，请先选择可用模型。");
    if (action === "set") {
      const choices = kind === "model" ? snapshot.models : currentModel.efforts;
      const matches = choices.filter(item => [item.id, item.name].some(name => name?.toLocaleLowerCase() === value?.toLocaleLowerCase()));
      if (matches.length !== 1) reject(422, "model_value_unavailable", `该${kind === "model" ? "模型" : "思考力度"}不可用，请先发送 /${kind} 查看原生目录。`);
      const selected = matches[0];
      const model = kind === "model" ? selected : currentModel;
      const effort = kind === "effort" ? selected.id
        : model.efforts.some(item => item.id === snapshot.current.effort) ? snapshot.current.effort : model.defaultEffort ?? null;
      const selection = { model: model.id, effort };
      const defaultModel = kind === "model" ? model : snapshot.models.find(model => model.id === defaults.model);
      if (!defaultModel || kind === "effort" && !defaultModel.efforts.some(item => item.id === selected.id))
        reject(422, "default_model_effort_unavailable", "Worker 默认模型不支持该力度，请先用 /model 选择兼容模型。");
      const workerDefaults = { model: defaultModel.id, effort: kind === "effort" ? selected.id
        : defaultModel.efforts.some(item => item.id === defaults.effort) ? defaults.effort : defaultModel.defaultEffort ?? null };
      const state = { ...await this.load(), pending: { threadId, selection, defaults: workerDefaults, previous: snapshot.current } };
      await this.save(state);
      await this.settle(state);
      return { action: "model_settings_updated", current: selection, defaults: workerDefaults, configFile: this.file,
        text: `当前会话与 Worker 默认配置均已生效\n当前会话模型 ${selection.model}\n当前会话思考力度 ${selection.effort ?? "原生默认"}\nWorker 默认模型 ${workerDefaults.model}\nWorker 默认思考力度 ${workerDefaults.effort ?? "原生默认"}` };
    }
    const key = JSON.stringify([threadId, kind]);
    const previousPage = this.pages.get(key) ?? 0;
    const page = input.page ?? (input.direction ? previousPage + input.direction : 0);
    const choices = kind === "model" ? snapshot.models : currentModel.efforts;
    const pages = Math.max(1, Math.ceil(choices.length / PAGE_SIZE));
    if (!Number.isSafeInteger(page) || page < 0 || page >= pages)
      reject(400, "model_page_out_of_range", `没有这一页，发送 /${kind} 刷新列表。`);
    this.pages.set(key, page);
    const items = choices.slice(page * PAGE_SIZE, (page + 1) * PAGE_SIZE).map((item, index) => ({
      ...item, number: page * PAGE_SIZE + index + 1,
      sources: [snapshot.current[kind] === item.id && "current", defaults[kind] === item.id && "config"].filter(Boolean),
    }));
    const lines = items.map(item => `${item.number}. ${item.id}${item.name && item.name !== item.id ? " — " + item.name : ""}${item.sources.length ? " (" + item.sources.join(",") + ")" : ""}`);
    return { action: "model_settings_listed", kind, page, pages, total: choices.length, items,
      current: snapshot.current, defaults, configFile: this.file,
      text: [`${kind === "model" ? "模型" : "思考力度"}，第 ${page + 1}/${pages} 页`, ...lines,
        ...(choices.length ? [] : ["此模型未提供可选思考力度。"]),
        ...(page > 0 ? [`上一页 /${kind} prev`] : []),
        ...(page + 1 < pages ? [`下一页 /${kind} next`] : [])].join("\n") };
  }
}
