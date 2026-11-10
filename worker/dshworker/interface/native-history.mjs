// Read only public history. Native delegated user inputs live in a special tool
// output, so hydrate those exact items without retaining other tool output.
const outputBudgets = [20000, 4000, 512, 64, 1];
const key = (threadId, turnId, item) => `${threadId}/${turnId}/${item.id}`;
const delegation = item => item.type === "functionCallOutput" && ["create_thread", "send_message_to_thread"].includes(item.name);
const complete = item => item.output != null && item.output?.truncated !== true;
const windowRejection = error => ["native_snapshot_too_large", "native_read_rejected"].includes(error.body?.error);
const blockedTurn = (requested, retryAt) => {
  const error = new Error("Desktop cannot read even the latest single turn; wait for a readable turn. No history was reconstructed.");
  error.body = { error: "native_single_turn_too_large", detail: error.message, retryable: true };
  error.nativeHistoryWindow = { requestedTurns: requested, readLimit: 1, returnedTurns: 0, limitedBySize: true,
    degraded: true, blocked: true, fallbackReason: "native_single_turn_too_large", retryAt: new Date(retryAt).toISOString() };
  return error;
};

export async function readNativeWindow(readPage, args, windows = new Map(), clock = Date.now) {
  const requested = args.turnLimit, id = JSON.stringify([args.threadId, args.hostId ?? null, requested]);
  const now = clock(), previous = windows.get(id);
  if (previous?.blockedUntil > now) throw blockedTurn(requested, previous.blockedUntil);
  const cached = previous && previous.limit < requested && (now < previous.retryAt || previous.blockedUntil);
  const read = async initial => {
    const limits = [...new Set([initial, 5, 2, 1])].filter(limit => limit <= initial);
    let reason = initial < requested ? previous?.reason ?? null : null;
    for (const limit of limits) {
      try { return { snapshot: await readPage({ ...args, turnLimit: limit }), limit, reason }; }
      catch (error) {
        if (limit === 1 && error.body?.error === "native_snapshot_too_large") {
          windows.set(id, { limit: 1, head: previous?.head ?? null, reason: "native_snapshot_too_large", blockedUntil: now + 30000, retryAt: now + 30000 });
          while (windows.size > 512) windows.delete(windows.keys().next().value);
          throw blockedTurn(requested, now + 30000);
        }
        if (!windowRejection(error) || limit === 1) throw error; reason = error.body.error;
      }
    }
  };
  let result = await read(cached ? previous.limit : requested);
  const head = value => (value.page?.order === "newest_first" ? value.turns?.[0] : value.turns?.at(-1))?.id;
  let widened = !cached;
  if (cached && head(result.snapshot) !== previous.head) { result = await read(requested); widened = true; }
  windows.set(id, { limit: result.limit, reason: result.reason, head: head(result.snapshot),
    retryAt: widened ? now + 30000 : previous.retryAt });
  while (windows.size > 512) windows.delete(windows.keys().next().value);
  result.snapshot.nativeHistoryWindow = { requestedTurns: requested, readLimit: result.limit,
    returnedTurns: result.snapshot.turns?.length ?? 0,
    limitedBySize: result.limit < requested && result.reason === "native_snapshot_too_large",
    degraded: result.limit < requested, fallbackReason: result.limit < requested ? result.reason : null };
  return result.snapshot;
}

export async function hydrateNativeInputs(snapshot, readPage, cache = new Map()) {
  const threadId = snapshot.thread?.id, pending = new Map();
  for (const turn of snapshot.turns ?? []) for (const item of turn.items ?? []) {
    if (!delegation(item) || complete(item)) continue;
    if (!item.id) throw incomplete();
    const id = key(threadId, turn.id, item), saved = cache.get(id);
    if (cache.has(id)) { if (saved !== null) item.output = structuredClone(saved); }
    else pending.set(id, item);
  }
  if (!pending.size) return snapshot;
  let cursor;
  const cursors = new Set();
  // Follow only Desktop-returned cursors. Allow two pages of head movement
  // while an active turn changes; never construct a cursor or read another chat.
  const pageLimit = Math.min(12, (snapshot.turns?.length ?? 0) + 2);
  for (let pageIndex = 0; pending.size && pageIndex < pageLimit; pageIndex++) {
    let page;
    for (const budget of outputBudgets) {
      try {
        page = await readPage({ threadId, turnLimit: 1, includeOutputs: true,
          maxOutputCharsPerItem: budget, ...(cursor ? { cursor } : {}) });
        break;
      } catch (error) {
        if (!windowRejection(error) || budget === outputBudgets.at(-1)) throw error;
      }
    }
    if (page?.thread?.id !== threadId) throw incomplete();
    for (const turn of page.turns ?? []) for (const item of turn.items ?? []) {
      const id = key(threadId, turn.id, item), target = pending.get(id);
      if (!target || !delegation(item) || !complete(item)) continue;
      // Copy only the exact native delegated-input output; never other command
      // results, reasoning or image paths discovered inside tool logs.
      const raw = typeof item.output === "string" ? item.output : item.output?.text;
      const publicInput = typeof raw === "string" && raw.includes("<input>") && raw.includes("</input>");
      if (publicInput) target.output = structuredClone(item.output);
      pending.delete(id); cache.set(id, publicInput ? structuredClone(item.output) : null);
      while (cache.size > 512) cache.delete(cache.keys().next().value);
    }
    const next = page.page?.nextCursor;
    if (!next || cursors.has(next)) break;
    cursors.add(next); cursor = next;
  }
  // A truncated input must not become a guessed correlation or a silent loss.
  if (pending.size) throw incomplete();
  return snapshot;
}

function incomplete() {
  const error = new Error("Native delegated input could not be read completely; no input was replayed.");
  error.body = { error: "native_snapshot_incomplete", detail: error.message, retryable: true };
  return error;
}
