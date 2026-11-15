// DSH owns its in-process engine and needs no Codex Desktop startup repair.
export async function ensureDesktopStartup() { return { restarting: false }; }
