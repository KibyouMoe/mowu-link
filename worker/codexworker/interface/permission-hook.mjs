import { createApprovalChannel } from "../approval-channel.mjs";
import { createHash } from "node:crypto";
import { redactToolText } from "../tool-progress.mjs";
import { isToolApproval } from "./native-approval.mjs";

const decision = (behavior, message) => ({ hookSpecificOutput: {
  hookEventName: "PermissionRequest", decision: { behavior, ...(message ? { message } : {}) } } });

// This is a supported native PermissionRequest hook. Returning an allow is
// useful only when the host is waiting for this exact hook invocation.
export async function reviewNativePermission(manager, event, { createChannel = createApprovalChannel } = {}) {
  if (event?.type === "desktop-server-request") return reviewElicitation(manager, event.request, createChannel);
  if (event?.hook_event_name !== "PermissionRequest" || typeof event.session_id !== "string" ||
      typeof event.turn_id !== "string" || !event.turn_id || typeof event.tool_name !== "string" ||
      !event.tool_name || !event.tool_input || typeof event.tool_input !== "object")
    return decision("deny", "Invalid native permission hook request.");
  // Other Desktop chats retain their normal permission UI.
  if (!manager?.state?.sessions?.[event.session_id] || manager.proxy || manager.closed) return {};
  if (!manager.routerClient) return decision("deny", "The configured router approval channel is unavailable.");
  try {
    const snapshot = await manager.read(event.session_id, manager.state.sessions[event.session_id].hostId, { turnLimit: 10 });
    const turn = snapshot.turns?.find(turn => turn.id === event.turn_id);
    if (!turn || !["inProgress", "in_progress", "running", "started"].includes(turn.status))
      return decision("deny", "The native permission request no longer belongs to an active turn.");
    // Preserve the router kinds used by the prior native RPC transport.
    // MCP approval forms are mcpServer/elicitation/request with the
    // codex_approval_kind=mcp_tool_call subtype.
    const kind = event.tool_name === "Bash" ? "command" : event.tool_name === "apply_patch" ? "fileChange"
      : event.tool_name.startsWith("mcp__") ? "elicitation" : "approval";
    // Approval review gets the requested operation only, with no transcript,
    // environment, tool results or private reasoning.
    const command = typeof event.tool_input.command === "string" ? event.tool_input.command : "";
    const operation = ["command", "fileChange"].includes(kind)
      ? { command: redactToolText(command).slice(0, 2800), commandChars: command.length,
        commandSha256: createHash("sha256").update(command).digest("hex") } : { tool: event.tool_name };
    const reason = redactToolText(event.tool_input.description).slice(0, 240);
    const result = await createChannel({ router: manager.routerClient, allowLocalAutoApproval: false,
      taskId: () => manager.routerClient.activity?.taskId ?? null }).handler({
        method: "PermissionRequest", kind,
        title: reason || `Codex permission requested by ${event.tool_name}`,
        detail: JSON.stringify({ threadId: event.session_id, turnId: event.turn_id,
          cwd: event.cwd, toolName: event.tool_name, ...operation }) });
    return { ...decision(result.decision === "approved" ? "allow" : "deny",
      result.decision === "approved" ? null : "Router did not approve the request."),
      systemMessage: `Router approval ${result.approvalId ?? "unavailable"} ${result.decision}.` };
  } catch {
    return decision("deny", "Router approval failed. The requested operation was not authorized.");
  }
}

async function reviewElicitation(manager, request, createChannel) {
  if (!isToolApproval(request)) return {};
  const p = request.params;
  if (!manager?.state?.sessions?.[p.threadId] || manager.proxy || manager.closed) return {};
  const denied = { nativeResponse: { action: "decline", content: null } };
  if (!manager.routerClient) return denied;
  try {
    const snapshot = await manager.read(p.threadId, manager.state.sessions[p.threadId].hostId, { turnLimit: 10 });
    const turn = snapshot.turns?.find(turn => (!p.turnId || turn.id === p.turnId) &&
      ["inProgress", "in_progress", "running", "started"].includes(turn.status));
    if (!turn) return denied;
    const result = await createChannel({ router: manager.routerClient, allowLocalAutoApproval: false,
      taskId: () => manager.routerClient.activity?.taskId ?? null }).handler({
        method: request.method, kind: "elicitation",
        title: redactToolText(p.message).slice(0, 240) || "Codex MCP tool approval",
        detail: JSON.stringify({ threadId: p.threadId, turnId: turn.id, requestId: request.id,
          serverName: p.serverName, approvalKind: "mcp_tool_call" }) });
    return { nativeResponse: result.decision === "approved" ? { action: "accept", content: {} }
      : denied.nativeResponse, approvalId: result.approvalId };
  } catch { return denied; }
}
