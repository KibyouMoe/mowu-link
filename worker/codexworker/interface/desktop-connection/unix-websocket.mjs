// Minimal local WebSocket channel for the documented app-server Unix socket.
// It presents the same JSONL streams as the owned stdio transport. Closing this
// channel only disconnects one client; it never controls the shared daemon.
import net from "node:net";
import {lstat} from "node:fs/promises";
import {EventEmitter} from "node:events";
import {PassThrough, Writable} from "node:stream";
import {createHash, randomBytes} from "node:crypto";

const MAX_MESSAGE = 16 * 1024 * 1024;
const GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11";
function frame(opcode, payload = Buffer.alloc(0)) {
  const mask = randomBytes(4), size = payload.length;
  const head = Buffer.alloc(size < 126 ? 6 : size < 65536 ? 8 : 14);
  head[0] = 0x80 | opcode;
  if (size < 126) { head[1] = 0x80 | size; mask.copy(head, 2); }
  else if (size < 65536) { head[1] = 0x80 | 126; head.writeUInt16BE(size, 2); mask.copy(head, 4); }
  else { head[1] = 0x80 | 127; head.writeBigUInt64BE(BigInt(size), 2); mask.copy(head, 10); }
  const bytes = Buffer.alloc(size);
  for (let i = 0; i < size; i++) bytes[i] = payload[i] ^ mask[i % 4];
  return Buffer.concat([head, bytes]);
}
class UnixChannel extends EventEmitter {
  constructor(socket) {
    super(); this.socket = socket; this.stdout = new PassThrough(); this.stderr = new PassThrough();
    this.on("error", () => {}); // The app-server client attaches its handler after connect.
    this.buffer = Buffer.alloc(0); this.fragments = []; this.fragmentBytes = 0; this.fragmented = false;
    let lines = "";
    this.stdin = new Writable({write: (chunk, _, done) => {
      try {
        lines += chunk.toString("utf8");
        if (Buffer.byteLength(lines) > MAX_MESSAGE) throw Error("shared_app_server_message_too_large");
        const packets = []; let end;
        while ((end = lines.indexOf("\n")) >= 0) {
          const line = lines.slice(0, end); lines = lines.slice(end + 1);
          if (line.trim()) packets.push(frame(1, Buffer.from(line)));
        }
        if (packets.length) socket.write(Buffer.concat(packets), done); else done();
      } catch (error) { done(error); }
    }});
    socket.on("data", chunk => { try { this.feed(chunk); } catch (error) { this.emit("error", error); socket.destroy(); } });
    socket.on("error", error => this.emit("error", error));
    socket.once("close", () => { this.stdout.end(); this.stderr.end(); this.emit("exit", 0, null); });
  }
  feed(chunk) {
    this.buffer = Buffer.concat([this.buffer, chunk]);
    for (;;) {
      if (this.buffer.length < 2) return;
      const a = this.buffer[0], b = this.buffer[1], opcode = a & 15, fin = Boolean(a & 128);
      if (a & 112 || b & 128) throw Error("invalid_shared_websocket_frame");
      let length = b & 127, offset = 2;
      if (length === 126) { if (this.buffer.length < 4) return; length = this.buffer.readUInt16BE(2); offset = 4; }
      else if (length === 127) {
        if (this.buffer.length < 10) return;
        const wide = this.buffer.readBigUInt64BE(2);
        if (wide > BigInt(MAX_MESSAGE)) throw Error("shared_app_server_message_too_large");
        length = Number(wide); offset = 10;
      }
      if (length > MAX_MESSAGE || opcode < 8 && this.fragmentBytes + length > MAX_MESSAGE)
        throw Error("shared_app_server_message_too_large");
      if (opcode >= 8 && (!fin || length > 125)) throw Error("invalid_shared_websocket_control");
      if (this.buffer.length < offset + length) return;
      const payload = this.buffer.subarray(offset, offset + length);
      this.buffer = this.buffer.subarray(offset + length);
      if (opcode === 8) { if (!this.socket.writableEnded) this.socket.end(frame(8, payload)); return; }
      if (opcode === 9) { if (!this.socket.writableEnded) this.socket.write(frame(10, payload)); continue; }
      if (opcode === 10) continue;
      if (opcode === 1 && !this.fragmented) this.fragmented = !fin;
      else if (opcode !== 0 || !this.fragmented) throw Error("invalid_shared_websocket_message");
      this.fragments.push(payload); this.fragmentBytes += payload.length;
      if (!fin) continue;
      const text = new TextDecoder("utf-8", {fatal:true}).decode(Buffer.concat(this.fragments));
      this.fragments = []; this.fragmentBytes = 0; this.fragmented = false;
      this.stdout.write(text + "\n");
    }
  }
  kill() {
    if (!this.socket.destroyed) {
      this.socket.end(frame(8));
      const timer = setTimeout(() => this.socket.destroy(), 250); timer.unref();
      this.socket.once("close", () => clearTimeout(timer));
    }
  }
}

export async function connectUnixWebSocket(socketPath, {timeoutMs = 5000} = {}) {
  const metadata = await lstat(socketPath);
  if (!metadata.isSocket() || typeof process.getuid === "function" && metadata.uid !== process.getuid() || metadata.mode & 0o022)
    throw Error("shared_app_server_socket_not_private");
  return new Promise((resolve, reject) => {
    const socket = net.createConnection(socketPath), key = randomBytes(16).toString("base64");
    let buffer = Buffer.alloc(0), settled = false;
    const timer = setTimeout(() => fail(Error("shared_app_server_handshake_timeout")), timeoutMs);
    function fail(error) {
      if (settled) return; settled = true; clearTimeout(timer); socket.destroy(); reject(error);
    }
    function data(chunk) {
      buffer = Buffer.concat([buffer, chunk]);
      const end = buffer.indexOf("\r\n\r\n");
      if (end < 0) { if (buffer.length > 16384) fail(Error("invalid_shared_websocket_handshake")); return; }
      const header = buffer.subarray(0, end).toString("ascii"), lines = header.split("\r\n");
      const fields = Object.fromEntries(lines.slice(1).map(line => {
        const at = line.indexOf(":"); return [line.slice(0,at).trim().toLowerCase(),line.slice(at+1).trim()];
      }));
      const expected = createHash("sha1").update(key + GUID).digest("base64");
      if (!/^HTTP\/1\.[01] 101(?: |$)/.test(lines[0]) || fields.upgrade?.toLowerCase() !== "websocket" ||
          !fields.connection?.toLowerCase().split(/\s*,\s*/).includes("upgrade") || fields["sec-websocket-accept"] !== expected)
        return fail(Error("shared_app_server_handshake_rejected"));
      settled = true; clearTimeout(timer); socket.removeListener("data", data); socket.removeListener("error", fail); socket.removeListener("close", closed);
      const channel = new UnixChannel(socket), rest = buffer.subarray(end + 4);
      resolve(channel);
      if (rest.length) queueMicrotask(() => { try { channel.feed(rest); } catch(error) { channel.emit("error",error); socket.destroy(); } });
    }
    function closed() { fail(Error("shared_app_server_closed_during_handshake")); }
    socket.on("error",fail); socket.once("close",closed); socket.on("data",data);
    socket.once("connect", () => socket.write("GET /rpc HTTP/1.1\r\nHost: localhost\r\nUpgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Version: 13\r\nSec-WebSocket-Key: " + key + "\r\n\r\n"));
  });
}
