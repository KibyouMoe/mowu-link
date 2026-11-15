import os from "node:os";
import path from "node:path";

// Codex owns the session logs; Worker stateDir may be nested anywhere.
export function defaultPublicJournalRoot(env = process.env, home = os.homedir()) {
  const root = env.CODEX_HOME?.trim() || path.join(home, ".codex");
  return path.resolve(root.startsWith("~/") ? path.join(home, root.slice(2)) : root, "sessions");
}
