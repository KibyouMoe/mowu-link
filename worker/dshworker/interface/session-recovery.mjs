import * as zlib from "node:zlib";

const MESSAGE_ROLES = new Map([
  ["user/message", "user"], ["system/message", "system"],
  ["developer/message", "developer"], ["assistant/message", "assistant"],
  ["tool/result", "tool"],
]);
const RECOVERY = Symbol.for("mowulink.dsh.session-role-recovery");

// Keep sequence coordinates so later tool and compaction references stay stable.
// The original record remains in the canonical log and its recovery backup.
export function skipInvalidMessageRoles(sessionId, events, log) {
  if (!Array.isArray(events)) return events;
  let result = events;
  for (const [index, event] of events.entries()) {
    const expectedRole = MESSAGE_ROLES.get(event.type);
    if (!expectedRole) continue;
    const message = event.type === "user/message" ? event.data : event.data?.message;
    if (!message || message.role === expectedRole) continue;
    if (result === events) result = events.slice();
    const actualRole = typeof message.role === "string" ? message.role : null;
    result[index] = { seq: event.seq, time: event.time,
      type: "dshworker/recovery-skipped", ignorable: true,
      data: { originalType: event.type, expectedRole, actualRole } };
    log("warn", "session_role_skipped", { sessionId, seq: event.seq,
      eventType: event.type, expectedRole, actualRole });
  }
  return result;
}

function recoverDecodedRoles(buffer, log) {
  // Native V4 checks system/developer/tool roles inside the physical decoder.
  // Retry complete frames in memory for those early failures, preserving the
  // original header frame and letting the native validator check every field.
  let remaining = buffer;
  const frames = [];
  while (remaining.length) {
    const decoded = zlib.zstdDecompressSync(remaining, { info: true });
    frames.push({ bytes: remaining.subarray(0, decoded.engine.bytesWritten), text: decoded.buffer });
    remaining = remaining.subarray(decoded.engine.bytesWritten);
  }
  const header = JSON.parse(frames[0].text.toString("utf8"));
  if (header.version !== 4) return null;
  const body = Buffer.concat(frames.slice(1).map(frame => frame.text)).toString("utf8");
  if (!body.endsWith("\n")) return null;
  const events = body.slice(0, -1).split("\n").map(line => JSON.parse(line));
  const recovered = skipInvalidMessageRoles(header.id, events, log);
  if (recovered === events) return null;
  return Buffer.concat([frames[0].bytes,
    zlib.zstdCompressSync(recovered.map(event => JSON.stringify(event) + "\n").join(""))]);
}

export function installSessionRoleRecovery(ctx, log) {
  const restorers = [];
  const sessions = ctx.get?.("sessions");
  const persistence = ctx.get?.("sessionPersistence");
  function wrap(service, name, implementation) {
    const original = service?.[name];
    if (typeof original !== "function") return;
    let state = original[RECOVERY];
    if (!state) {
      state = { original, loggers: new Map() };
      state.replacement = implementation(original, (...args) => [...state.loggers.values()].at(-1)?.(...args));
      state.replacement[RECOVERY] = state;
      service[name] = state.replacement;
    }
    const owner = Symbol(name);
    state.loggers.set(owner, log);
    restorers.push(() => {
      state.loggers.delete(owner);
      if (!state.loggers.size && service[name] === state.replacement) service[name] = state.original;
    });
  }
  wrap(sessions, "prepare", (original, warn) => function (id, options) {
    const seed = skipInvalidMessageRoles(id, options?.seed, warn);
    return original.call(this, id, seed === options?.seed ? options : { ...options, seed });
  });
  // This adapter targets the installed native V4 Zstd backend. Decode first,
  // before validateStoredEvents adopts message roles. Preserve physical offsets,
  // torn-tail recovery and the original file bytes owned by the native backend.
  wrap(persistence, "readZstdPrefix", (original, warn) => async function (...args) {
    let parsed;
    try { parsed = await original.apply(this, args); }
    catch (error) {
      args[1]?.throwIfAborted();
      let recovered;
      try { recovered = recoverDecodedRoles(args[0], warn); }
      catch { throw error; }
      if (!recovered) throw error;
      parsed = await original.call(this, recovered, args[1]);
    }
    const events = skipInvalidMessageRoles(parsed.meta.id, parsed.events, warn);
    const recoveredTail = skipInvalidMessageRoles(parsed.meta.id, parsed.recoveredTail, warn);
    return events === parsed.events && recoveredTail === parsed.recoveredTail
      ? parsed : { ...parsed, events, recoveredTail };
  });
  if (persistence && typeof persistence.readZstdPrefix !== "function") {
    log("warn", "session_role_recovery_unavailable", {
      backend: persistence.constructor?.name, compression: persistence.config?.compression,
      message: "当前持久化后端缺少已核验的 V4 Zstd 恢复入口", });
  }
  return () => { for (const restore of restorers.reverse()) restore(); };
}
