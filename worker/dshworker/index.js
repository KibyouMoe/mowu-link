// dshworker — DSH 侧 worker（cordis 插件版）
//
// 与 codexworker 同构：只连 router，不直连飞书；汇报走 router /notice。
// 设计见 docs/WORKERS.md「重做原则」。
//
// M1 已验证：插件加载 → 注册 router → SSE 常驻。
// M2：任务进来直接调 DSH 的 agents/sessions 跑一轮，不起子进程。
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawn } from "node:child_process";

import { RouterClient } from "./router-client.mjs";

export const name = "dshworker";
export const inject = ["appReady"];

const DEFAULT_ROUTER = "http://127.0.0.1:8790";
const DEFAULT_WORKER = "TamamoCross";
const VERSION = "0.2.0";
const SUMMARY_LIMIT = 4000;

function expandHome(p) {
  const s = String(p || "");
  return s.startsWith("~/") ? path.join(os.homedir(), s.slice(2)) : s;
}

/** 读 KEY=VALUE 形式的 .env（不解析复杂语法，够用即可）。 */
function readDotenv(file) {
  const out = {};
  try {
    const text = fs.readFileSync(expandHome(file), "utf8");
    for (const line of text.split("\n")) {
      const m = line.match(/^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$/);
      if (!m) continue;
      let v = m[2].trim();
      if (
        (v.startsWith('"') && v.endsWith('"')) ||
        (v.startsWith("'") && v.endsWith("'"))
      ) {
        v = v.slice(1, -1);
      }
      out[m[1]] = v;
    }
  } catch {
    /* 没有 .env 就返回空对象 */
  }
  return out;
}

function readToken(file) {
  try {
    return fs
      .readFileSync(expandHome(file), "utf8")
      .split("\n")[0]
      .trim()
      .replace(/^Bearer\s+/i, "");
  } catch {
    return "";
  }
}

function makeLogger(file) {
  const target = expandHome(file);
  try {
    fs.mkdirSync(path.dirname(target), { recursive: true });
  } catch {
    /* ignore */
  }
  return (level, event, fields = {}) => {
    const extra =
      fields && Object.keys(fields).length ? ` ${JSON.stringify(fields)}` : "";
    const line = `${new Date().toISOString()} [${level}] ${event}${extra}`;
    try {
      fs.appendFileSync(target, line + "\n");
    } catch {
      /* ignore */
    }
    // DSH 启动器默认没有 info/warn exporter，stderr 是唯一稳妥的出口
    try {
      process.stderr.write(line + "\n");
    } catch {
      /* ignore */
    }
  };
}

export function apply(ctx, config = {}) {
  const log = makeLogger(config.logFile || "~/.hermes/logs/dshworker.log");
  const token = config.token || readToken(config.tokenFile || "~/.hermes/secrets/router.token");
  if (!token) {
    log("error", "router_token_missing");
    return;
  }

  const dshBin = config.dshBin || "dsh";
  const dshProfile = config.dshProfile || "headless";
  const workspace = expandHome(config.workspace || "~");
  const taskTimeoutMs = Number(config.taskTimeoutMs) > 0 ? Number(config.taskTimeoutMs) : 30 * 60 * 1000;

  /**
   * 跑一次任务：起一个 `dsh --profile headless <goal>` 子进程。
   *
   * 为什么不用进程内的 agents/sessions：web profile 的传输层过不去
   * （同一个环境里 headless CLI 能跑通，插件内 agents 调用报
   *  `DeepSeek Messages transport failed / TRANSPORT`），
   * 而 headless 子进程是本机验证过的路径。插件负责常驻与 router，执行交给它。
   */
  function runAgent(goal, taskId) {
    const started = Date.now();
    return new Promise((resolve, reject) => {
      // 显式构造子进程环境：web 进程内的代理变量不可信（DSH 会改），
      // 这里按已验证的组合写死，只保留必要的继承。
      const env = {
        ...readDotenv(config.envFile || "~/.hermes/.env"),
        ...process.env,
        HTTPS_PROXY: config.httpsProxy || "http://127.0.0.1:10808",
        HTTP_PROXY: config.httpProxy || "http://127.0.0.1:10808",
        https_proxy: config.httpsProxy || "http://127.0.0.1:10808",
        http_proxy: config.httpProxy || "http://127.0.0.1:10808",
        DEEPSEEK_BASE_URL:
          config.deepseekBaseUrl || "https://api.deepseek.com/anthropic",
        NODE_USE_ENV_PROXY: "1",
      };
      delete env.ALL_PROXY;
      delete env.all_proxy;
      const child = spawn(dshBin, ["--profile", dshProfile, goal], {
        cwd: workspace,
        env,
        stdio: ["ignore", "pipe", "pipe"],
      });
      let out = "";
      let err = "";
      let settled = false;
      const finish = (fn, value) => {
        if (settled) return;
        settled = true;
        clearTimeout(timer);
        fn(value);
      };
      const timer = setTimeout(() => {
        try {
          child.kill("SIGKILL");
        } catch {
          /* ignore */
        }
        finish(reject, new Error(`dsh 超时（${Math.round(taskTimeoutMs / 1000)}s）`));
      }, taskTimeoutMs);

      child.stdout.on("data", (buf) => {
        out += buf.toString("utf8");
      });
      child.stderr.on("data", (buf) => {
        err += buf.toString("utf8");
      });
      child.on("error", (error) => finish(reject, error));
      child.on("close", (code) => {
        const summary = out.trim();
        log("info", "agent_turn_done", {
          taskId,
          ms: Date.now() - started,
          code,
          chars: summary.length,
          stderr: err.trim().slice(-300) || null,
        });
        if (code === 0 && summary) {
          finish(resolve, summary);
        } else {
          finish(
            reject,
            new Error(
              `dsh 退出码 ${code}` + (err.trim() ? `：${err.trim().slice(-400)}` : "")
            )
          );
        }
      });
    });
  }

  const client = new RouterClient({
    url: config.routerUrl || DEFAULT_ROUTER,
    token,
    name: config.workerName || DEFAULT_WORKER,
    version: VERSION,
    subscriptions: Array.isArray(config.subscriptions) ? config.subscriptions : [],
    logger: log,
  });

  let busy = false;

  async function handleTask(task) {
    const taskId = String(task?.task_id || "");
    const goal = String(task?.goal || "").trim();
    if (!taskId) {
      log("warn", "task_without_id");
      return;
    }
    if (!goal) {
      await client.result({ taskId, status: "failed", summary: "任务缺少 goal" });
      return;
    }
    busy = true;
    client.setActivity({ state: "running", taskId });
    log("info", "task_received", { taskId, chars: goal.length });
    try {
      // 开始看板：走 /notice，由溪流转达
      await client.notice({
        text: `来自超级溪流的指令（任务 ${taskId}）\n${goal}`,
        urgency: "normal",
      });

      const summary = await runAgent(goal, taskId);
      await client.result({ taskId, status: "done", summary });
      log("info", "task_done", { taskId });
    } catch (err) {
      const message = String((err && err.message) || err);
      log("error", "task_failed", { taskId, error: message });
      try {
        await client.result({ taskId, status: "failed", summary: message });
      } catch {
        /* ignore */
      }
    } finally {
      busy = false;
      client.setActivity({ state: "idle", taskId: null });
    }
  }

  client.start({
    onTask: handleTask,
    onConnect: () => log("info", "router_connected", { worker: client.name }),
    onDisconnect: (reason) => log("warn", "router_disconnected", { reason: String(reason || "") }),
    onError: (err) => log("warn", "router_error", { error: String((err && err.message) || err) }),
    onSubscriptionEvent: (events) => {
      for (const ev of events || []) {
        log("info", "subscription_event", { source: ev?.source, id: ev?.id });
      }
    },
  });

  ctx.effect(() => {
    log("info", "plugin_active", { worker: client.name, url: client.url, version: VERSION });
    return () => {
      log("info", "plugin_disposing");
      Promise.resolve(client.stop()).catch(() => {});
    };
  });

  ctx.provide("dshWorker", {
    status: () => client.status(),
    busy: () => busy,
    worker: client.name,
  });
}
