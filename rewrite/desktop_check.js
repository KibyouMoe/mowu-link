// Codex Desktop scoped tool-host driver. The Python side cannot choose a chat or tool name.
async function desktopCheck(tools, workdir) {
  function value(reply) {
    if (reply.isError) throw new Error(reply.content?.[0]?.text || "Desktop tool failed");
    return reply.structuredContent ?? JSON.parse(reply.content[0].text);
  }
  const tag = Date.now().toString(36);
  const bootstrap = "独立 Desktop 验收 " + tag + "。只回复「测试会话就绪」，不用工具。";
  const result = value(await tools.mcp__codex_app__create_thread({
    title: "MowuLink 独立宿主验收 " + tag, prompt: bootstrap,
    target: {type: "projectless", directoryName: "mowulink-desktop-acceptance-" + tag}
  }));
  const target = {hostId: result.hostId, threadId: result.threadId};
  if (target.hostId !== "local" || !target.threadId || !result.projectlessOutputDirectory)
    throw new Error("missing local projectless test chat");
  const production = value(await tools.mcp__codexworker__bridge_binding_status({}));
  if (!production.binding?.managerThreadId || target.threadId === production.binding.managerThreadId)
    throw new Error("test chat isolation unconfirmed");
  async function snapshot() {
    const snap = value(await tools.mcp__codex_app__read_thread({...target, includeOutputs: true,
      turnLimit: 10, itemLimit: 64, maxOutputCharsPerItem: 20000}));
    if (snap.thread?.hostId !== target.hostId || snap.thread.id !== target.threadId ||
        result.projectlessOutputDirectory !== snap.thread.cwd + "/outputs" ||
        !snap.thread.title.startsWith("MowuLink 独立宿主验收 "))
      throw new Error("unexpected test chat identity");
    return snap;
  }
  value(await tools.mcp__codex_app__wait_threads({targets: [target], timeoutMs: 45000}));
  const first = await snapshot();
  if (first.turns?.length !== 1 || first.thread.status.type !== "idle")
    throw new Error("test chat is not fresh and idle");
  const opts = {max_output_tokens: 5000, yield_time_ms: 250};
  let proc = await tools.exec_command({...opts, workdir, tty: true,
    cmd: "stty -echo -icanon && exec python3 -u -m rewrite.desktop_check"});
  const id = proc.session_id;
  if (!id) throw new Error("test adapter did not start: " + proc.output);
  const io = chars => tools.write_stdin({...opts, session_id: id, chars});
  let buffer = proc.output, outcome;
  try {
    proc = await io(JSON.stringify({session: target.threadId, bootstrap}) + "\n");
    for (;;) {
      buffer += proc.output;
      const newline = buffer.indexOf("\n");
      if (newline < 0) {
        if (proc.exit_code != null) break;
        proc = await io("");
        continue;
      }
      const line = buffer.slice(0, newline).trim();
      buffer = buffer.slice(newline + 1);
      if (!line.startsWith("{")) throw new Error("adapter failed: " + line + "\n" + buffer);
      const req = JSON.parse(line);
      if (req.op === "result") {
        outcome = req.result;
        if (proc.exit_code == null) proc = await io("");
        if (proc.exit_code !== 0) throw new Error("adapter did not exit normally");
        break;
      }
      let response;
      try {
        if (req.op === "read") response = {result: await snapshot()};
        else if (req.op === "send") {
          if (typeof req.prompt !== "string" || !req.prompt.startsWith("魔物链接独立工单\n"))
            throw new Error("unexpected delivery");
          const envelope = JSON.parse(req.prompt.split("\n").slice(1).join("\n"));
          if (envelope.session !== target.threadId || envelope.payload?.kind !== "task")
            throw new Error("delivery target mismatch");
          value(await tools.mcp__codex_app__send_message_to_thread({...target, prompt: req.prompt}));
          response = {result: true};
        } else throw new Error("unsupported Desktop operation");
      } catch (error) {
        response = {error: String(error)};
      }
      proc = await io(JSON.stringify(response) + "\n");
    }
    if (!outcome) throw new Error("no result from adapter: " + buffer);
    return {threadId: target.threadId, ...outcome};
  } catch (error) {
    if (proc.exit_code == null) await io("\u0003");
    throw error;
  }
}
