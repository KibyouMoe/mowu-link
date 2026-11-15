import test from "node:test";
import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { mkdtemp, readFile, realpath, rm, lstat, unlink, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { acquireWriterLease } from "../interface/writer-lease.mjs";

async function fixture(t) {
  const root = await mkdtemp(path.join(os.tmpdir(), "writer-lease-"));
  t.after(() => rm(root, { recursive: true, force: true }));
  return { root, canonical: await realpath(root) };
}
async function release(lease) {
  try {
    if (lease.server.listening) await new Promise(resolve => lease.server.close(resolve));
    const owner = JSON.parse(await readFile(lease.ownerFile, "utf8"));
    if (owner.nonce === lease.nonce) await unlink(lease.ownerFile);
  } finally { lease.guard?.close(); }
}
async function crashedWriter(t, f) {
  const module = new URL("../interface/writer-lease.mjs", import.meta.url).href;
  const script = `import {acquireWriterLease} from ${JSON.stringify(module)};
    const lease=await acquireWriterLease(process.argv[1],process.argv[2]);
    console.log(JSON.stringify({address:lease.address,ownerFile:lease.ownerFile}));`;
  const child = spawn(process.execPath, ["--input-type=module", "-e", script, f.root, f.canonical], { stdio: ["ignore", "pipe", "pipe"], windowsHide: true });
  const exited = once(child, "exit");
  t.after(async () => { if (child.exitCode === null && child.signalCode === null) child.kill("SIGKILL"); await exited; });
  const info = await new Promise((resolve, reject) => {
    let text = "", errors = "";
    child.stderr.on("data", chunk => { errors += chunk; });
    const timer = setTimeout(() => reject(new Error("writer fixture did not start: " + errors)), 10000);
    child.once("error", error => { clearTimeout(timer); reject(error); });
    child.stdout.on("data", chunk => { text += chunk; if (text.includes("\n")) { clearTimeout(timer); resolve(JSON.parse(text.split("\n")[0])); } });
  });
  await assert.rejects(acquireWriterLease(f.root, f.canonical), { code: "EADDRINUSE" });
  child.kill("SIGKILL"); await exited;
  return info;
}

test("a real crashed writer releases the OS guard and its original state can reopen", async t => {
  const f = await fixture(t), previous = await crashedWriter(t, f);
  if (process.platform !== "win32") assert.equal((await lstat(previous.address)).isSocket(), true);
  const lease = await acquireWriterLease(f.root, f.canonical);
  try {
    assert.equal(lease.server.listening, true);
    assert.equal(JSON.parse(await readFile(lease.ownerFile, "utf8")).pid, process.pid);
    await assert.rejects(acquireWriterLease(f.root, f.canonical), { code: "EADDRINUSE" });
  } finally { await release(lease); }
});

test("a previous-boot diagnostic PID may be reused without locking out its real state", async t => {
  const f = await fixture(t), previous = await crashedWriter(t, f);
  await writeFile(previous.ownerFile, JSON.stringify({ pid: process.pid, nonce: "previous-boot",
    startedAt: new Date(Date.now() - (os.uptime() + 120) * 1000).toISOString() }));
  const lease = await acquireWriterLease(f.root, f.canonical);
  await release(lease);
});

test("recovery preserves a live same-boot owner that is still closing", { skip: process.platform === "win32" }, async t => {
  const f = await fixture(t), previous = await crashedWriter(t, f);
  const old = await readFile(previous.ownerFile);
  await writeFile(previous.ownerFile, JSON.stringify({ pid: process.pid, nonce: "live-close", startedAt: new Date().toISOString() }));
  await assert.rejects(acquireWriterLease(f.root, f.canonical), { code: "EADDRINUSE" });
  assert.equal((await lstat(previous.address)).isSocket(), true);
  await writeFile(previous.ownerFile, old);
  await release(await acquireWriterLease(f.root, f.canonical));
});

test("a regular file at the Unix lease path is preserved", { skip: process.platform === "win32" }, async t => {
  const f = await fixture(t), original = await acquireWriterLease(f.root, f.canonical);
  await release(original);
  await writeFile(original.address, "unrelated file");
  t.after(() => unlink(original.address));
  await assert.rejects(acquireWriterLease(f.root, f.canonical), { code: "EADDRINUSE" });
  assert.equal(await readFile(original.address, "utf8"), "unrelated file");
});
