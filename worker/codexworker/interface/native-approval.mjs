export function isToolApproval(request) {
  const p = request?.params;
  return request?.method === "mcpServer/elicitation/request" &&
    ["string", "number"].includes(typeof request.id) && typeof p?.threadId === "string" &&
    typeof p.serverName === "string" && ["form", "openai/form", "openaiForm"].includes(p.mode) &&
    p._meta?.codex_approval_kind === "mcp_tool_call" && p.requestedSchema?.type === "object" &&
    p.requestedSchema.properties !== null && typeof p.requestedSchema.properties === "object" &&
    !Array.isArray(p.requestedSchema.properties) && Object.keys(p.requestedSchema.properties).length === 0 &&
    !(p.requestedSchema.required?.length);
}
