import test from "node:test";
import assert from "node:assert/strict";
import { EventEmitter } from "node:events";
import { PassThrough } from "node:stream";
import { createNativeLineRelay } from "../interface/desktop-connection/native-lines.mjs";
import { runDesktopServer } from "../interface/desktop-connection/entry.mjs";

test("native inspection preserves split UTF-8, whitespace, invalid JSON and final partial bytes", () => {
  const chunks = [], observed = [];
  const relay = createNativeLineRelay({ write: bytes => chunks.push(bytes), inspect: value => observed.push(value) });
  const bytes = Buffer.from(' {"id":"原生"} \r\ninvalid\n{"partial":');
  const split = bytes.indexOf(Buffer.from("原")) + 1;
  relay.write(bytes.subarray(0, split)); relay.write(bytes.subarray(split)); relay.end();
  assert.deepEqual(Buffer.concat(chunks), bytes);
  assert.deepEqual(observed, [{ id: "原生" }]);
});

test("oversized frames stream unchanged and inspection resumes at the next complete line", () => {
  const chunks = [], observed = [];
  const relay = createNativeLineRelay({ maxInspectBytes: 20, write: bytes => chunks.push(bytes),
    inspect: value => { observed.push(value); return value.id !== 3; } });
  const large = JSON.stringify({ data: "x".repeat(100) }) + "\n";
  relay.write(large.slice(0, 10)); relay.write(large.slice(10, 75));
  relay.write(large.slice(75) + '{"id":2}\n{"id":3}\n'); relay.end();
  assert.equal(Buffer.concat(chunks).toString(), large + '{"id":2}\n');
  assert.deepEqual(observed, [{ id: 2 }, { id: 3 }]);
});

test("native Desktop forwards image/media frames beyond 16 MiB in both directions", async () => {
  const channel = new EventEmitter(); channel.stdin = new PassThrough(); channel.stdout = new PassThrough();
  channel.kill = () => setImmediate(() => channel.emit("exit", 0));
  const input = new PassThrough(), output = new PassThrough(), errors = new PassThrough();
  const upstream = [], downstream = [];
  channel.stdin.on("data", bytes => upstream.push(bytes)); output.on("data", bytes => downstream.push(bytes));
  const done = runDesktopServer({}, { config: {}, nativePassthrough: true }, {
    stdin: input, stdout: output, stderr: errors, connect: async () => channel });
  await new Promise(resolve => setImmediate(resolve));
  const bytes = Buffer.from(JSON.stringify({ id: 1, image: "a".repeat(17 * 1024 * 1024) }) + "\n");
  input.write(bytes.subarray(0, 100)); input.write(bytes.subarray(100));
  channel.stdout.write(bytes.subarray(0, 150)); channel.stdout.write(bytes.subarray(150));
  assert.deepEqual(Buffer.concat(upstream), bytes); assert.deepEqual(Buffer.concat(downstream), bytes);
  input.end(); assert.equal(await done, 0);
});

test("repeated shutdown closes once and drains native output through close", async () => {
  const channel = new EventEmitter(); channel.stdin = new PassThrough(); channel.stdout = new PassThrough();
  const input = new PassThrough(), output = new PassThrough(), errors = new PassThrough();
  let closes = 0, downstream = "", settled = false;
  const stdoutErrors = output.listenerCount("error"), inputErrors = input.listenerCount("error");
  output.on("data", bytes => { downstream += bytes; });
  const done = runDesktopServer({}, { config: {}, nativePassthrough: true }, {
    stdin: input, stdout: output, stderr: errors, connect: async () => channel,
    terminationEvent: "close", closeChannel: async () => { closes++; } });
  done.then(() => { settled = true; });
  await new Promise(resolve => setImmediate(resolve));
  input.emit("end"); input.emit("end");
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(closes, 1);
  channel.emit("exit", 0);
  channel.stdout.write('{"final":"完整输出"}\npartial'); channel.stdout.end();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(settled, false);
  assert.equal(downstream, '{"final":"完整输出"}\npartial');
  channel.emit("close", 0); assert.equal(await done, 0);
  assert.equal(output.listenerCount("error"), stdoutErrors);
  assert.equal(input.listenerCount("error"), inputErrors);
});
