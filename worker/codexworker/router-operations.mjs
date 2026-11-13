// User-authorized work orders and peer chat are distinct router operations.
const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i;
export class RouterOperationError extends Error {
  constructor(status, error, detail, extra = {}) { super(detail); this.name = "RouterOperationError";
    this.status = status; this.body = { error, detail, ...extra }; }
}
export function routerOperations(manager) {
  const client = () => {
    if (!manager?.routerClient) throw new RouterOperationError(503, "router_unavailable", "Enable this plugin's router integration first.");
    return manager.routerClient;
  };
  const call = async (method, route, body) => {
    try { return await client()._request(method, route, body); }
    catch (error) {
      if (error instanceof RouterOperationError) throw error;
      throw new RouterOperationError(error.status ?? 503, error.code ?? "router_request_unconfirmed",
        error.status ? "Router rejected the request." : "Router acceptance is unconfirmed; retry with the same messageId and content.",
        body?.message_id ? { messageId: body.message_id } : {});
    }
  };
  return {
    workers: () => call("GET", "/workers"),
    createWorkOrder(request) {
      const { messageId, worker, goal, context, deadlineS, queueIfOffline = true } = request ?? {};
      if (!UUID.test(messageId ?? "") || typeof worker !== "string" || !worker.trim() || worker !== worker.trim() ||
          typeof goal !== "string" || !goal.trim() || goal.length > 20000 || typeof queueIfOffline !== "boolean" ||
          deadlineS != null && (!Number.isFinite(deadlineS) || deadlineS <= 0))
        throw new RouterOperationError(400, "invalid_work_order", "Provide a stable UUIDv4 messageId, exact worker name and project goal; ordinary chat uses worker messages.");
      return call("POST", "/tasks", { message_id: messageId, worker, goal, execution_mode: "goal", queue_if_offline: queueIfOffline,
        ...(context != null ? { context } : {}), ...(deadlineS != null ? { deadline_s: deadlineS } : {}) });
    },
    workOrder(taskId) {
      if (!/^t-[a-zA-Z0-9_-]+$/.test(taskId ?? "")) throw new RouterOperationError(400, "invalid_task_id", "An exact task_id is required.");
      return call("GET", "/tasks/" + encodeURIComponent(taskId));
    },
    async message(method, request = {}) {
      if (!manager?.workerMessages) throw new RouterOperationError(503, "worker_chat_unavailable", "Peer chat requires this plugin's active router connection.");
      try { return await manager.workerMessages[method](request); }
      catch (error) { throw new RouterOperationError(409, error.code ?? "worker_message_unconfirmed", error.message,
        { ...(error.messageId ? { messageId: error.messageId } : {}), ...(error.outcome ? { outcome: error.outcome } : {}) }); }
    },
  };
}
