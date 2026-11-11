// Sidebar schema v4 can expose a chat only as a section itemKey. Resolve those
// native references before filtering/sorting; never invent names or workspaces.
const localCodex = task => task?.kind === "codex" && (task.hostId ?? "local") === "local" && typeof task.id === "string";
export const resumeTimestamp = value => {
  if (typeof value === "number" && Number.isFinite(value)) return value < 1e12 ? value * 1000 : value;
  return Date.parse(value) || 0;
};

export function resumeActivityTime(task, activity = {}) {
  // Desktop recency tracks user activation separately from background updates.
  // An explicit Bridge resume/input is also user activity; listing is not.
  const native = resumeTimestamp(task.recencyAt ?? task.lastUserMessageAt ?? task.lastActiveAt ?? task.updatedAt);
  return Math.max(native, resumeTimestamp(activity[task.id]?.at));
}

export async function resumeCandidates(listing, { readThread, allowed, maxResolve = 12, localTasks = [], activity = {}, requiredThreadIds = [] }) {
  const metadata = new Map(), eligible = new Map(), references = new Set();
  for (const task of localTasks) if (localCodex(task) && allowed(task)) {
    metadata.set(task.id, task); eligible.set(task.id, task);
  }
  for (const task of [...(listing.pinnedThreads ?? []), ...(listing.threads ?? [])]) {
    if (typeof task?.id !== "string") continue;
    const merged = { ...metadata.get(task.id), ...Object.fromEntries(Object.entries(task).filter(([, v]) => v != null)) };
    metadata.set(task.id, merged);
    if (!localCodex(task)) { eligible.delete(task.id); continue; }
    if (typeof merged.cwd !== "string") references.add(task.id);
    else if (allowed(merged)) eligible.set(task.id, merged);
    else eligible.delete(task.id);
  }
  for (const section of listing.sections ?? []) for (const item of section.itemKeys ?? []) {
    if (typeof item !== "string") continue;
    const match = /^codex:thread:local:([a-z\d_-]{1,128})$/i.exec(item);
    if (match && !metadata.has(match[1])) references.add(match[1]);
  }
  // A bound native thread may be older than the first listing page or absent
  // from an unavailable catalog. Verify its identity rather than inventing a
  // candidate from the saved alias; prioritize it within the same read budget.
  const required = new Set(requiredThreadIds.filter(id => typeof id === "string" && id.length > 0 && id.length <= 128));
  for (const id of required) if (!metadata.has(id)) references.add(id);
  const missing = [...references].sort((a, b) => Number(required.has(b)) - Number(required.has(a)));
  const pending = missing.slice(0, maxResolve);
  let next = 0, resolved = 0, unavailable = 0;
  await Promise.all(Array.from({ length: Math.min(4, pending.length) }, async () => {
    while (next < pending.length) {
      const id = pending[next++];
      try {
        const snapshot = await readThread(id);
        const thread = snapshot?.thread;
        if (thread?.id !== id || (thread.kind ?? "codex") !== "codex" || (thread.hostId ?? "local") !== "local") {
          unavailable++; continue;
        }
        if (allowed(thread)) {
          // The title is the native title verbatim; stored aliases are not a
          // fallback for an unverified/missing thread or an unreadable scope.
          eligible.set(id, { ...metadata.get(id), ...Object.fromEntries(Object.entries(thread).filter(([, v]) => v != null)), kind: "codex", hostId: "local" });
          resolved++;
        }
      } catch { unavailable++; }
    }
  }));
  const tasks = [...eligible.values()].map(task => ({ ...task, lastActivityAt: resumeActivityTime(task, activity) }))
    .sort((a, b) => b.lastActivityAt - a.lastActivityAt || a.id.localeCompare(b.id));
  return { tasks, discovery: { sectionReferences: missing.length, resolved,
    unavailable, deferred: Math.max(0, missing.length - pending.length),
    partial: unavailable > 0 || missing.length > pending.length } };
}
