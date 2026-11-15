import { readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { resolveDesktopState } from "../desktop-lifecycle.mjs";

// Only the existing loopback adapter receives native approval metadata.
export async function requestPermissionReview(event, { env = process.env, signal } = {}) {
  const home = env.CODEX_HOME ?? path.join(env.USERPROFILE ?? os.homedir(), ".codex");
  const state = await resolveDesktopState(path.join(home, "state", "codexworker"), env);
  let config;
  try { config = JSON.parse(await readFile(path.join(state, "desktop-config.local.json"), "utf8")); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
  if (!config || config.autoStart === false) return {};
  const port = config.port ?? 8903;
  if ((config.host ?? "127.0.0.1") !== "127.0.0.1" || !Number.isInteger(port) || port < 1 || port > 65535)
    throw new Error("permission_endpoint_invalid");
  const token = (await readFile(config.tokenFile, "utf8")).trim();
  if (token.length < 24) throw new Error("permission_credential_invalid");
  let response;
  try {
    response = await fetch(`http://127.0.0.1:${port}/bridge/permissions/review`, {
      method: "POST", headers: { authorization: "Bearer " + token, "content-type": "application/json" },
      body: JSON.stringify(event), signal: signal
        ? AbortSignal.any([signal, AbortSignal.timeout(560000)]) : AbortSignal.timeout(560000) });
  } catch (error) {
    if (signal?.aborted) throw error;
    return {}; // A stopped adapter leaves the native prompt available.
  }
  if ([404, 503].includes(response.status)) return {};
  if (!response.ok) throw new Error("permission_review_unavailable");
  return response.json();
}
