import { NAMESPACE, TRANSPORT_NAME } from "../inprocess.mjs";
import { dshImageInputAvailable } from "../image-input.mjs";

export function createInProcessAdapter(transport) {
  return {
    name: TRANSPORT_NAME,
    capabilities: Object.freeze({ fork: false, desktopJournal: false, callerTurn: false,
      get imageInput() { return dshImageInputAvailable(transport); } }),
    // Business status reads use complete in-process snapshots, including empty
    // threads and native answer items. The DSH API also exposes wait_threads.
    listTools: async () => (await transport.listTools()).filter(tool => tool.name !== "wait_threads"),
    async callTool(toolName, args, _invocation, timeoutMs) {
      try {
        const value = await transport.execute(toolName, args ?? {}, timeoutMs);
        return { success: true, tool: toolName, namespace: NAMESPACE,
          contentItems: [{ type: "text", text: JSON.stringify(value) }] };
      } catch (error) {
        return { success: false, tool: toolName, namespace: NAMESPACE,
          contentItems: [{ type: "text", text: JSON.stringify({ success: false, error: "tool_failed", tool: toolName,
            reason: error?.message ?? String(error), ...(error?.code === "dsh_image_input_rejected"
              ? { error: error.code, detail: error.message, nativeInputOutcome: error.nativeInputOutcome } : {}) }) }] };
      }
    },
    shutdown: () => transport.close("transport shutdown"),
  };
}
