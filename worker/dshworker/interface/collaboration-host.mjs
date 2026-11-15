import { createRequire } from "node:module";
import { pathToFileURL } from "node:url";

// Use Harness's authenticated Connection/Remote transport in both desktop and
// web clients. Client modules do not inherit the host Cordis config object.
export async function registerCollaborationHost(ctx, resolveView, sdk) {
  sdk ??= await import(pathToFileURL(createRequire(process.argv[1])
    .resolve("@deepseek-ai/dsh-typert-protocol")).href);
  const { Remote, TypertRemoteService } = sdk;
  class WorkerCollaboration extends TypertRemoteService {
    constructor() { super(ctx, "workerCollaboration"); }
    view() { return resolveView(); }
  }
  const initializers = [];
  Remote(WorkerCollaboration.prototype.view, { kind: "method", name: "view", private: false, static: false,
    addInitializer: initializer => initializers.push(initializer) });
  const service = new WorkerCollaboration();
  for (const initialize of initializers) initialize.call(service);
  return service;
}
