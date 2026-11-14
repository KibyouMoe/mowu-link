import { startRouterRuntime } from "../router-runtime.mjs";

// The Harness agent stays in-process. Peer messaging and task execution use
// the same business runtime as Codex, with DSH control/approval hooks here.
export function startDshRouterRuntime({ manager, transport, workerConfig, pluginConfig,
  identity, host, port, tokenFile, log, version }) {
  return startRouterRuntime({ manager, identity, log, version,
    config: { ...manager.config, host, port,
      subscriptions: workerConfig.subscriptions,
      subscriptionPollMs: workerConfig.subscriptionPollMs,
      router: { ...workerConfig.router, name: identity.name, tokenFile,
        enabled: pluginConfig.router !== false && workerConfig.router?.enabled !== false } },
    transport: {
      bindRouter: (client, origin) => transport.control.bindRouter(client, origin),
      taskStarted: (task, deadline) => transport.taskDeadlines.set(task.task_id,
        Number.isFinite(deadline) ? deadline : null),
      taskFinished: taskId => transport.taskDeadlines.delete(taskId),
    } });
}
