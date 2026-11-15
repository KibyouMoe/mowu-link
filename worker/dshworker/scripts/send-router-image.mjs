#!/usr/bin/env node
// Send one explicitly authorized image to Hermes as a chat attachment.
import { readFile } from "node:fs/promises";
import { parseArgs } from "node:util";
import { fileURLToPath } from "node:url";
import os from "node:os";
import path from "node:path";
import { sendRouterImage, uploadRouterImage } from "../router-attachments.mjs";
import { resolveDesktopState } from "../desktop-lifecycle.mjs";

const { values } = parseArgs({ options: {
  image: { type: "string" }, "message-id": { type: "string" },
  text: { type: "string" }, urgent: { type: "boolean" },
  config: { type: "string" },
  "upload-only": { type: "boolean" },
} });
try {
  if (!values.image || (!values["upload-only"] && !values["message-id"])) throw new Error("Required: --image <path> and either --upload-only or --message-id <UUID>; reuse UUID on retry.");
  if (values["upload-only"] && (values.urgent || values.text || values["message-id"])) throw new Error("--upload-only cannot include notification options.");
  const identity = JSON.parse(await readFile(fileURLToPath(new URL("../identity.json", import.meta.url)), "utf8"));
  const expand = value => value?.startsWith("~/") ? path.join(os.homedir(), value.slice(2)) : value;
  const stateDir = await resolveDesktopState(path.join(process.env.USERPROFILE ?? process.env.HOME ?? os.homedir(), ".codex", "state", "codexworker"));
  const config = JSON.parse(await readFile(expand(values.config ?? path.join(stateDir, "desktop-config.local.json")), "utf8"));
  const router = config.router ?? {};
  if (router.enabled === false) throw new Error("Router is disabled.");
  const tokenPath = expand(process.env.CODEXWORKER_ROUTER_TOKEN_FILE ?? router.tokenFile);
  if (!tokenPath) throw new Error("router.tokenFile is required.");
  const token = (await readFile(tokenPath, "utf8")).trim();
  const options = {
    imagePath: values.image, messageId: values["message-id"], text: values.text,
    worker: config.machine ?? identity.name, url: process.env.CODEXWORKER_ROUTER_URL ?? router.url ?? "http://127.0.0.1:8790",
    token, urgency: values.urgent ? "urgent" : "normal",
  };
  const receipt = values["upload-only"]
    ? { ok: true, imageId: (await uploadRouterImage(options)).id }
    : await sendRouterImage(options);
  console.log(JSON.stringify(receipt));
  if (!receipt.ok) process.exitCode = 1;
} catch (error) {
  console.error(JSON.stringify({ error: "router_image_failed", detail: error.message, messageId: values["message-id"] }));
  process.exitCode = 1;
}
