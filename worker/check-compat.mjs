#!/usr/bin/env node
import { createHash } from "node:crypto";
import { spawn, spawnSync } from "node:child_process";
import { mkdtemp, open, readFile, readdir, writeFile } from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import process from "node:process";
import { fileURLToPath, pathToFileURL } from "node:url";

const repository = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");

export async function compareSharedModules(root) {
  const roots = ["codexworker", "dshworker"].map(worker => path.join(root, "worker", worker));
  const names = await Promise.all(roots.map(async directory =>
    (await readdir(directory)).filter(name => name.endsWith(".mjs"))));
  const modules = [...new Set(names.flat())].sort();
  if (!modules.length) throw new Error("No public root modules found.");
  return Promise.all(modules.map(async name => {
    const hashes = await Promise.all(roots.map(async (directory, index) => {
      if (!names[index].includes(name)) return null;
      return createHash("sha256").update(await readFile(path.join(directory, name))).digest("hex");
    }));
    return { file: name, codexSha256: hashes[0], dshSha256: hashes[1],
      match: hashes[0] !== null && hashes[0] === hashes[1] };
  }));
}

export function testEnvironment(environment) {
  return Object.fromEntries(Object.entries(environment).filter(([key]) =>
    /^CODEXWORKER_TEST_/i.test(key) ||
    !/^(CODEXWORKER_|DSHWORKER_|RUDOLF_BRIDGE_)/i.test(key) && key.toUpperCase() !== "CODEX_APP_TOOLS_PIPE_PATH"));
}

export function pythonCommands(platform, configured) {
  if (configured) return [[configured]];
  return platform === "win32" ? [["py", "-3"], ["python"], ["python3"]] : [["python3"], ["python"]];
}

export function testFailureDetails(text) {
  const lines = text.split(/\r?\n/), failures = [];
  for (let index = 0; index < lines.length; index++) {
    if (!/^\s*not ok \d+/.test(lines[index])) continue;
    let end = index + 1;
    while (end < lines.length && end < index + 120 && !/^\s*\.\.\.$/.test(lines[end])) end++;
    failures.push(lines.slice(Math.max(0, index - 1), end + 1).join("\n"));
  }
  return failures.length ? failures.join("\n") : lines.slice(-60).join("\n");
}

async function runCheck(command, args, { root, environment, logPath }) {
  const output = await open(logPath, "w");
  let result;
  try {
    result = await new Promise((resolve, reject) => {
      const child = spawn(command, args, { cwd: root, env: environment, windowsHide: true,
        stdio: ["ignore", output.fd, output.fd] });
      child.once("error", reject);
      child.once("close", (code, signal) => resolve({ code, signal }));
    });
  } finally {
    await output.close();
  }
  return { ...result, text: await readFile(logPath, "utf8") };
}

export async function runCompatibility({ root = repository, hashOnly = false, workersOnly = false,
  environment = process.env, report = console.log } = {}) {
  const logDir = await mkdtemp(path.join(os.tmpdir(), "worker-compat-"));
  report(`platform=${process.platform} node=${process.version} logs=${logDir}`);
  const modules = await compareSharedModules(root);
  await writeFile(path.join(logDir, "module-hashes.json"), JSON.stringify(modules, null, 2) + "\n");
  const mismatches = modules.filter(module => !module.match);
  report(`shared_root_modules=${modules.length} matched=${modules.length - mismatches.length} mismatched=${mismatches.length}`);
  if (mismatches.length) {
    for (const module of mismatches) report(`HASH_MISMATCH ${JSON.stringify(module)}`);
    return false;
  }
  if (hashOnly) return true;
  const cleanEnvironment = testEnvironment(environment);
  let passed = true;
  for (const worker of ["codexworker", "dshworker"]) {
    const directory = path.join(root, "worker", worker, "tests");
    const files = (await readdir(directory)).filter(name => name.endsWith(".test.mjs")).sort()
      .map(name => path.join(directory, name));
    if (!files.length) throw new Error(`No tests found for ${worker}.`);
    const result = await runCheck(process.execPath, ["--test", "--test-concurrency=2", "--test-reporter=tap", ...files],
      { root, environment: cleanEnvironment, logPath: path.join(logDir, worker + ".tap") });
    const count = name => Number(result.text.match(new RegExp(`^# ${name} (\\d+)$`, "m"))?.[1] ?? NaN);
    const total = count("tests"), success = count("pass"), failed = count("fail"), skipped = count("skipped");
    const ok = result.code === 0 && total > 0 && failed === 0 && success + skipped === total;
    report(`${worker} passed=${success} failed=${failed} skipped=${skipped} total=${total} exit=${result.code} signal=${result.signal ?? "none"}`);
    if (!ok) { passed = false; report(testFailureDetails(result.text)); }
  }
  if (workersOnly) {
    report("router=not_run scope=workers-only");
    report(passed ? "WORKER_SUITES_PASS" : "WORKER_COMPAT_FAIL");
    return passed;
  }
  const python = pythonCommands(process.platform, cleanEnvironment.WORKER_COMPAT_PYTHON).find(([command, ...args]) => {
    const probe = spawnSync(command, [...args, "--version"], { env: cleanEnvironment, encoding: "utf8",
      windowsHide: true, timeout: 10000 });
    return probe.status === 0 && /^Python 3\./.test((probe.stdout || probe.stderr || "").trim());
  });
  if (!python) throw new Error("Python 3 is unavailable. Set WORKER_COMPAT_PYTHON to its executable path.");
  const [command, ...pythonArgs] = python;
  const router = await runCheck(command, [...pythonArgs, path.join(root, "server", "router", "selftest_router.py")],
    { root, environment: { ...cleanEnvironment, ROUTER_TEST_PORT: cleanEnvironment.ROUTER_TEST_PORT ?? "18790" },
      logPath: path.join(logDir, "router.log") });
  const counts = router.text.match(/^TOTAL (\d+)\/(\d+) PASS$/m);
  const routerOk = router.code === 0 && counts && Number(counts[1]) > 0 && counts[1] === counts[2];
  report(`router ${counts?.[0] ?? "missing test summary"} exit=${router.code} signal=${router.signal ?? "none"}`);
  if (!routerOk) { passed = false; report(router.text.split("\n").slice(-60).join("\n")); }
  report(passed ? "WORKER_COMPAT_PASS" : "WORKER_COMPAT_FAIL");
  return passed;
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  try {
    if (process.argv.slice(2).some(argument => !["--hash-only", "--workers-only"].includes(argument)))
      throw new Error("Usage: node worker/check-compat.mjs [--hash-only | --workers-only]");
    process.exitCode = await runCompatibility({ hashOnly: process.argv.includes("--hash-only"),
      workersOnly: process.argv.includes("--workers-only") }) ? 0 : 1;
  } catch (error) {
    console.error(error.message); process.exitCode = 1;
  }
}
