// Inspect normal approval frames while forwarding larger native image/media
// messages unchanged. The inspection budget never limits native payload size.
export function createNativeLineRelay({ write, inspect, maxInspectBytes = 16 * 1024 * 1024 }) {
  let pending = [], pendingBytes = 0, passthrough = false;

  function segment(bytes, complete) {
    if (!passthrough && pendingBytes + bytes.length > maxInspectBytes) {
      for (const chunk of pending) write(chunk);
      pending = []; pendingBytes = 0; passthrough = true;
    }
    if (passthrough) write(bytes);
    else { pending.push(bytes); pendingBytes += bytes.length; }
    if (!complete) return;
    if (!passthrough) {
      const line = Buffer.concat(pending, pendingBytes);
      let message;
      try { message = JSON.parse(line.toString("utf8")); } catch { /* Native owns protocol errors. */ }
      if (!message || inspect(message) !== false) write(line);
    }
    pending = []; pendingBytes = 0; passthrough = false;
  }

  return {
    write(chunk) {
      const bytes = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
      let start = 0, end;
      while ((end = bytes.indexOf(10, start)) >= 0) {
        segment(bytes.subarray(start, end + 1), true);
        start = end + 1;
      }
      if (start < bytes.length) segment(bytes.subarray(start), false);
    },
    end() {
      for (const chunk of pending) write(chunk);
      pending = []; pendingBytes = 0; passthrough = false;
    },
  };
}
