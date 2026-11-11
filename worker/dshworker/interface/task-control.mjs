import { timingSafeEqual } from "node:crypto";

export const taskIdOf = (turn) => /^\[router (t-[^\]\s]+)\]/m.exec(turn?.prompt ?? "")?.[1] ?? null;

export function installTaskControl(transport) {
  const cancelledTasks = new Set();
  function find(taskId) {
    for (const thread of transport.threads.values()) {
      const turn = [...(thread.turns ?? [])].reverse().find(t => taskIdOf(t) === taskId);
      if (turn) return { thread, turn };
    }
    return null;
  }
  async function cancelTask(taskId) {
    cancelledTasks.add(taskId);
    const found = find(taskId);
    if (!found) return { ok: true, task_id: taskId, cancelled: false, reason: "task_not_started" };
    const { thread, turn } = found;
    if (["completed", "failed", "interrupted", "cancelled", "canceled"].includes(turn.status))
      return { ok: true, task_id: taskId, cancelled: false, reason: "already_terminal" };
    turn.cancelRequested = true;
    if (thread.queue.includes(turn.id)) {
      thread.queue = thread.queue.filter(id => id !== turn.id);
      turn.status = "interrupted";
      turn.error = "cancelled by router"; turn.reasonKind = "cancelled";
      turn.completedAt = new Date().toISOString();
    } else {
      transport.live.get(thread.id)?.agent?.cancel({ kind: "user" });
    }
    await transport.save();
    transport.log("info", "router_task_cancelled", { taskId, threadId: thread.id, turnId: turn.id });
    return { ok: true, task_id: taskId, cancelled: true, thread_id: thread.id, turn_id: turn.id };
  }
  return {
    cancelTask,
    isCancelled: (taskId) => cancelledTasks.has(taskId),
    bindRouter(router, controlUrl) {
      transport.approvals.setRouter(router);
      const original = router._request.bind(router);
      router._request = (method, path, body, options) =>
        original(method, path, method === "POST" && path === "/register"
          ? { ...body, control_url: controlUrl } : body, options);
    },
    attachManager(manager) {
      if (!manager || manager.dshTaskControl) return;
      manager.dshTaskControl = true;
      const original = manager.handle.bind(manager);
      manager.handle = async (req, res, context = {}) => {
        const pathname = new URL(req.url, "http://worker.invalid").pathname;
        const match = /^\/manager\/tasks\/(t-[A-Za-z0-9-]+)(\/cancel)?$/.exec(pathname);
        if (!match) return original(req, res, context);
        const send = (status, body) => {
          res.writeHead(status, { "Content-Type": "application/json", "Cache-Control": "no-store" });
          res.end(JSON.stringify(body));
        };
        const actual = Buffer.from(req.headers.authorization ?? "");
        const expected = Buffer.from("Bearer " + (context.token ?? manager.config.token));
        if (actual.length !== expected.length || !timingSafeEqual(actual, expected))
          return send(401, { ok: false, error: "unauthorized" });
        try {
          if (req.method === "POST" && match[2]) {
            let size = 0, chunks = [];
            for await (const chunk of req) {
              size += chunk.length;
              if (size > 16384) return send(413, { ok: false, error: "body_too_large" });
              chunks.push(chunk);
            }
            const body = chunks.length ? JSON.parse(Buffer.concat(chunks).toString("utf8")) : {};
            if (body.task_id && body.task_id !== match[1])
              return send(400, { ok: false, error: "task_id_mismatch" });
            return send(200, await cancelTask(match[1]));
          }
          if (req.method === "GET" && !match[2]) {
            const found = find(match[1]);
            if (!found) return send(404, { ok: false, error: "unknown_task" });
            const { thread, turn } = found;
            return send(200, { ok: true, task_id: match[1], thread_id: thread.id,
              turn_id: turn.id, status: turn.status, approval_id: turn.approval_id ?? null,
              waiting_for_approval: turn.waiting_for_approval ?? false,
              approval: turn.approval ?? null, progress: turn.progress ?? [] });
          }
          return send(405, { ok: false, error: "method_not_allowed" });
        } catch (error) {
          transport.log("error", "router_task_cancel_failed", { taskId: match[1], message: error.message });
          return send(500, { ok: false, error: "worker_control_failed", message: error.message });
        }
      };
    },
  };
}
