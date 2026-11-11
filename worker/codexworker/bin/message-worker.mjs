#!/usr/bin/env node
// One ingress for a local terminal: reuse the worker's primary conversation.
import { readFile } from "node:fs/promises";
import path from "node:path";
import os from "node:os";
import { randomUUID } from "node:crypto";
import { fileURLToPath } from "node:url";
import { PROTOCOL } from "../desktop-manager.mjs";
const expand = value => value.startsWith("~/") ? path.join(os.homedir(), value.slice(2)) : value;

export async function sendWorkerMessage({ configFile, text, threadId = null, messageId = randomUUID(),
  delivery = "steer", fetchImpl = fetch, onIntent = () => {} }) {
  if (typeof text !== "string" || !text.trim()) throw Error("Message text is required.");
  if (!/^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/i.test(messageId))
    throw Error("A UUID v4 message ID is required.");
  if (!["queue", "steer"].includes(delivery)) throw Error("Delivery must be queue or steer.");
  const config = JSON.parse(await readFile(expand(configFile), "utf8"));
  const host = config.host || "127.0.0.1";
  if (!["127.0.0.1", "localhost", "::1", "[::1]", "0.0.0.0"].includes(host))
    throw Error("Local worker ingress requires a loopback host.");
  const token = (await readFile(expand(config.tokenFile || "~/.hermes/secrets/router.token"), "utf8")).trim();
  if (!token) throw Error("Worker token is empty.");
  const target = host === "0.0.0.0" ? "127.0.0.1" : host === "::1" ? "[::1]" : host;
  const identity = JSON.parse(await readFile(new URL("../identity.json", import.meta.url), "utf8"));
  const port = config.port ?? identity.port;
  if (!Number.isInteger(port) || port < 1 || port > 65535) throw Error("Invalid worker port.");
  const body = { protocolVersion: PROTOCOL, messageId, source: "trainer", origin: "cli",
    text, targetThreadId: threadId, delivery };
  onIntent({ messageId });
  let response;
  try {
    response = await fetchImpl("http://" + target + ":" + port + "/manager/messages",
      { method: "POST", headers: { authorization: "Bearer " + token, "content-type": "application/json" },
        body: JSON.stringify(body), signal: AbortSignal.timeout(15000) });
    const result = await response.json();
    if (!response.ok) throw Error("HTTP " + response.status + ": " + (result.error || "worker rejected message"));
    return { messageId, ...result };
  } catch (error) {
    // No automatic retry with a fresh UUID after a lost acknowledgement.
    throw Error("Message " + messageId + " was not confirmed (" + (response ? "HTTP " + response.status : "connection") +
      "). Check the worker receipt before retrying with this same --message-id.", { cause: error });
  }
}
async function main() {
  const identity = JSON.parse(await readFile(new URL("../identity.json", import.meta.url), "utf8"));
  const opts = { configFile: identity.configFile }, args = process.argv.slice(2);
  for (let i = 0; i < args.length; i++) {
    if (args[i] === "--help") {
      console.log("message-worker.mjs --text TEXT [--thread ID] [--delivery steer|queue] [--message-id UUID] [--config FILE]");
      return;
    }
    const key = { "--text": "text", "--thread": "threadId", "--delivery": "delivery",
      "--message-id": "messageId", "--config": "configFile" }[args[i]];
    if (!key || args[i + 1] == null) throw Error("Unknown or incomplete option: " + args[i]);
    opts[key] = args[++i];
  }
  const result = await sendWorkerMessage({ ...opts,
    onIntent: ({ messageId }) => console.error("message_id=" + messageId) });
  console.log(JSON.stringify(result));
}
if (process.argv[1] && path.resolve(process.argv[1]) === fileURLToPath(import.meta.url))
  main().catch(error => { console.error(error.message); process.exitCode = 1; });
