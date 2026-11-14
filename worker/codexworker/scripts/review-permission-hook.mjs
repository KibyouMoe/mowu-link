import { readFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { resolveDesktopState } from "../desktop-lifecycle.mjs";

const denied = () => ({ hookSpecificOutput: { hookEventName: "PermissionRequest",
  decision: { behavior: "deny", message: "The router permission review could not finish." } } });
let result;
try {
  let input = "";
  for await (const chunk of process.stdin) {
    input += chunk;
    if (Buffer.byteLength(input) > 1024 * 1024) throw new Error("permission_input_too_large");
  }
  const event = JSON.parse(input);
  const codexHome = process.env.CODEX_HOME ?? path.join(process.env.USERPROFILE ?? os.homedir(), ".codex");
  const stateDir = await resolveDesktopState(path.join(codexHome, "state", "codexworker"));
  let config;
  try { config = JSON.parse(await readFile(path.join(stateDir, "desktop-config.local.json"), "utf8")); }
  catch (error) { if (error.code !== "ENOENT") throw error; }
  if (!config || config.autoStart === false) result = {};
  else {
    if ((config.host ?? "127.0.0.1") !== "127.0.0.1" || !Number.isInteger(config.port) ||
        config.port < 1 || config.port > 65535) throw new Error("permission_endpoint_invalid");
    const token = (await readFile(config.tokenFile, "utf8")).trim();
    if (token.length < 24) throw new Error("permission_credential_invalid");
    const response = await fetch(`http://127.0.0.1:${config.port}/bridge/permissions/review`, {
      method: "POST", headers: { authorization: "Bearer " + token, "content-type": "application/json" },
      body: JSON.stringify(event), signal: AbortSignal.timeout(560000) });
    if (!response.ok) throw new Error("permission_review_unavailable");
    result = await response.json();
  }
} catch { result = denied(); }
process.stdout.write(JSON.stringify(result) + "\n");
