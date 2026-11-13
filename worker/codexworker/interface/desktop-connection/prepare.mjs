// Prepare an immutable user-owned Desktop connector. No process, launchd job,
// environment, managed plugin cache, or session is changed by this command.
import { createHash } from "node:crypto";
import { lstat, mkdir, readFile, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { loadDesktopConnection } from "./entry.mjs";

const sources = ["desktop-lifecycle.mjs", "interface/desktop-connection/entry.mjs",
  "interface/desktop-connection/unix-websocket.mjs"];
const shellQuote = value => "'" + value.replaceAll("'", "'\\''") + "'";

export async function prepareDesktopConnection({ platform = process.platform, env = process.env,
  pluginRoot = fileURLToPath(new URL("../..", import.meta.url)), nodePath = process.execPath } = {}) {
  if (platform !== "darwin") throw new Error("desktop_connection_platform_unsupported");
  await loadDesktopConnection(env); // Validate the already configured private endpoint.
  if (!path.isAbsolute(nodePath)) throw new Error("desktop_connection_node_not_absolute");
  const files = await Promise.all(sources.map(async name => [name, await readFile(path.join(pluginRoot, name))]));
  const hash = createHash("sha256");
  for (const [name, bytes] of files) hash.update(name + "\0").update(bytes);
  hash.update(nodePath);
  const revision = hash.digest("hex");
  const home = env.CODEX_HOME || path.join(os.homedir(), ".codex");
  const parent = path.join(home, "state/codexworker/desktop-connection");
  await mkdir(parent, { recursive: true, mode: 0o700 });
  const metadata = await lstat(parent);
  if (!metadata.isDirectory() || metadata.isSymbolicLink() || metadata.mode & 0o077 ||
      typeof process.getuid === "function" && metadata.uid !== process.getuid()) throw new Error("desktop_connection_install_not_private");
  const root = path.join(parent, revision);
  await mkdir(root, { mode: 0o700 }).catch(error => { if (error.code !== "EEXIST") throw error; });
  const rootStat = await lstat(root);
  if (!rootStat.isDirectory() || rootStat.isSymbolicLink() || rootStat.mode & 0o077 ||
      typeof process.getuid === "function" && rootStat.uid !== process.getuid()) throw new Error("desktop_connection_install_not_private");
  const launcher = path.join(root, "codex");
  files.push(["codex", Buffer.from("#!/bin/sh\nexec " + shellQuote(nodePath) + " " +
    shellQuote(path.join(root, "interface/desktop-connection/entry.mjs")) + ' "$@"\n')]);
  for (const [name, bytes] of files) {
    const target = path.join(root, name);
    await mkdir(path.dirname(target), { recursive: true, mode: 0o700 });
    try { await writeFile(target, bytes, { flag: "wx", mode: name === "codex" ? 0o700 : 0o600 }); }
    catch (error) { if (error.code !== "EEXIST") throw error; }
    const actual = await lstat(target);
    if (!actual.isFile() || actual.isSymbolicLink() || !(await readFile(target)).equals(bytes))
      throw new Error("desktop_connection_install_mismatch");
  }
  return { launcher, revision, fileCount: files.length, activationRequired: true };
}

if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url)) {
  prepareDesktopConnection().then(result => console.log(JSON.stringify(result)), error => {
    console.error(error.message); process.exitCode = 1;
  });
}
