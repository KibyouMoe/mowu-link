import { createHash, randomUUID } from "node:crypto";
import { lstat, open, readFile, unlink } from "node:fs/promises";
import net from "node:net";
import os from "node:os";
import path from "node:path";

const busy = () => Object.assign(new Error("manager_instance_active"), { code: "EADDRINUSE" });
const listen = (server, address) => new Promise((resolve, reject) => {
  const failed = error => { server.removeListener("listening", ready); reject(error); };
  const ready = () => { server.removeListener("error", failed); resolve(); };
  server.once("error", failed); server.once("listening", ready); server.listen(address);
});

async function staleSocket(address, ownerFile) {
  const stat = await lstat(address).catch(error => { if (error.code !== "ENOENT") throw error; });
  if (!stat) return;
  if (!stat.isSocket() || stat.uid !== process.getuid()) throw busy();
  let owner;
  try { owner = JSON.parse(await readFile(ownerFile, "utf8")); }
  catch (error) { if (error.code !== "ENOENT" && !(error instanceof SyntaxError)) throw error; }
  // A live owner can be closing its socket. Let its own cleanup finish.
  // Previous-boot PIDs can have been reused by unrelated applications.
  if (owner && Date.parse(owner.startedAt) >= Date.now() - os.uptime() * 1000) {
    try { process.kill(owner.pid, 0); throw busy(); }
    catch (error) { if (error.code !== "ESRCH") throw error; }
  }
  const outcome = await new Promise(resolve => {
    const socket = net.createConnection(address);
    const finish = value => { clearTimeout(timer); socket.destroy(); resolve(value); };
    const timer = setTimeout(() => finish("unknown"), 500);
    socket.once("connect", () => finish("live")); socket.once("error", error => finish(error.code));
  });
  if (!["ECONNREFUSED", "ENOENT"].includes(outcome)) throw busy();
  await unlink(address).catch(error => { if (error.code !== "ENOENT") throw error; });
}

export async function acquireWriterLease(stateDir, canonicalState) {
  const id = createHash("sha256").update(JSON.stringify(process.platform === "win32"
    ? canonicalState.toLowerCase() : canonicalState)).digest("hex").slice(0, 24);
  const address = process.platform === "win32" ? `\\\\.\\pipe\\codexworker-manager-${id}` : path.join(os.tmpdir(), `cw-${id}.sock`);
  const ownerFile = path.join(stateDir, "desktop-manager.lock");
  const server = net.createServer(socket => socket.end());
  let guard;
  try {
    if (process.platform !== "win32") {
      // Serialize recovery and shutdown using an OS-backed lock that releases
      // after a crash. The existing Unix socket still interlocks older writers.
      const file = path.join(stateDir, "desktop-manager-lease.sqlite");
      const handle = await open(file, "a", 0o600); await handle.close();
      const { DatabaseSync } = await import("node:sqlite");
      guard = new DatabaseSync(file);
      try { guard.exec("BEGIN EXCLUSIVE"); }
      catch (error) { if ([5, 6].includes(error.errcode)) throw busy(); throw error; }
    }
    try { await listen(server, address); }
    catch (error) {
      if (error.code !== "EADDRINUSE" || process.platform === "win32") throw error;
      await staleSocket(address, ownerFile); await listen(server, address);
    }
    // Both current and legacy writers must own the OS lease first. The old
    // diagnostic file can therefore be replaced even if its PID was reused.
    await unlink(ownerFile).catch(error => { if (error.code !== "ENOENT") throw error; });
    const nonce = randomUUID(), handle = await open(ownerFile, "wx", 0o600);
    try { await handle.writeFile(JSON.stringify({ pid: process.pid, nonce, startedAt: new Date().toISOString() })); }
    finally { await handle.close(); }
    return { server, guard, ownerFile, nonce, address };
  } catch (error) {
    if (server.listening) await new Promise(resolve => server.close(resolve));
    guard?.close(); throw error;
  }
}
