import assert from "node:assert/strict";
import test from "node:test";
import { markdownImagePaths, publicImages, splitPublicMarkdown, PublicImageRelay } from "../public-image-relay.mjs";

test("an active ordered image send cannot be joined by a second automatic drainer", async () => {
  let joined = 0;
  const manager = { state: {}, publicTextRunning: true, publicTextTask: Promise.resolve("active"),
    drainPublicTexts() { joined++; return this.publicTextTask; } };
  assert.equal(await new PublicImageRelay(manager).kick(), "active"); assert.equal(joined, 1);
});

test("Markdown image splitting retains exact prose and ignores code, escaped syntax and remote URLs", () => {
  const prefix = '😀前段\n```md\n![代码](C:/code.png)\n```\n`![内联](C:/inline.png)`\n';
  const tail = '\n后段\n\\![转义](C:/escaped.png)\n![remote](https://example.test/image.png)';
  const result = splitPublicMarkdown(prefix + '![本机](<C:/images/a (b).png> "标题")' + tail);
  assert.deepEqual(result, [{ type: "text", text: prefix }, { type: "image", imagePath: "C:/images/a (b).png" }, { type: "text", text: tail }]);
});

test("Markdown images support absolute local paths, spaces, parentheses and titles, not links or URLs", () => {
  assert.deepEqual(markdownImagePaths('![a](C:/design/a.png) ![b](<C:/design/b (2).png>) ![c](/tmp/c.png "caption") ![d](C:\\design\\d.png)'),
    ["C:/design/a.png", "C:/design/b (2).png", "/tmp/c.png", "C:\\design\\d.png"]);
  assert.deepEqual(markdownImagePaths('![a](C:/design/b(1).png) ![b](C:/design/a%20b.png)'), ["C:/design/b(1).png", "C:/design/a b.png"]);
  assert.deepEqual(markdownImagePaths('[link](C:/a.png) ![web](https://example.com/a.png) ![base64](data:image/png;base64,a) ![file](file:///C:/a.png) ![relative](a.png) ![network](//server/a.png)'), []);
});

test("Code examples, escaped syntax and incomplete streaming markup cannot upload files", () => {
  assert.deepEqual(markdownImagePaths('`![inline](C:/private.png)`\n```md\n![fenced](C:/private.png)\n```\n\\![escaped](C:/private.png)\n![incomplete](C:/private.png'), []);
  assert.deepEqual(markdownImagePaths('~~~md\n![unclosed](C:/private.png)'), []);
  assert.deepEqual(markdownImagePaths('`unclosed ![private](C:/private.png)'), []);
});

test("Public native image shapes are whitelisted; same asset in a turn is sent once", () => {
  const snapshot = { turns: [{ id: "t1", items: [
    { type: "imageGeneration", id: "generated", status: "completed", savedPath: "C:/design/a.png", revisedPrompt: "DO NOT EXPORT" },
    { type: "imageView", id: "viewed", path: "C:/design/a.png" },
    { type: "agentMessage", id: "reply", text: "![preview](C:/design/a.png) ![other](C:/design/b.png)" },
    { type: "imageGeneration", status: "inProgress", savedPath: "C:/private.png" },
    { type: "reasoning", content: "![private](C:/private.png)" },
    { type: "mcpToolCall", result: { text: "![private](C:/private.png)" } },
    { type: "commandExecution", output: "C:/private.png" },
    { type: "functionCallOutput", output: "![private](C:/private.png)" }
  ] }] };
  const images = publicImages(snapshot);
  assert.deepEqual(images.map(i => i.imagePath), ["C:/design/a.png", "C:/design/b.png"]);
  assert.ok(!JSON.stringify(images).includes("DO NOT EXPORT"));
});

test("User input images are not echoed even when subsequently viewed or embedded", () => {
  const snapshot = { turns: [{ id: "input", items: [{ type: "userMessage", content: [{ type: "localImage", path: "C:/input.png" }] }] },
    { id: "reply", items: [{ type: "imageView", path: "C:\\input.png" }, { type: "agentMessage", text: "![input](C:/input.png) ![result](C:/output.png)" }] }] };
  assert.deepEqual(publicImages(snapshot).map(i => i.imagePath), ["C:/output.png"]);
});

test("Reverse chronological native pages are restored to display order", () => {
  const turns = ["new", "old"].map(id => ({ id, items: [{ type: "imageView", path: `C:/${id}.png` }] }));
  assert.deepEqual(publicImages({ page: { order: "newest_first" }, turns }).map(i => i.turnId), ["old", "new"]);
});
