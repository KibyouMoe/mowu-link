import test from "node:test";
import assert from "node:assert/strict";
import path from "node:path";
import { projectDshMedia, resolveDshMediaPath } from "../interface/public-media.mjs";
import { markdownImagePaths } from "../public-image-relay.mjs";

const cwd = path.resolve("source-chat");
const image = path.join(cwd, "evidence", "probe image.png");
const project = (text, options = {}) => projectDshMedia(text, { cwd, complete: true, ...options });

test("DSH resolves public relative Markdown and MEDIA against the original chat directory", () => {
  assert.deepEqual(project("before\n![probe](<evidence/probe%20image.png>)\nafter"), [
    { type: "text", text: "before\n" }, { type: "image", imagePath: image }, { type: "text", text: "\nafter" },
  ]);
  assert.deepEqual(project('MEDIA: "evidence/probe image.png"\r\n'), [{ type: "image", imagePath: image }]);
  assert.equal(resolveDshMediaPath("<evidence/probe image.png>", cwd), image);
  assert.equal(resolveDshMediaPath("![invalid]", null), null);
});

test("DSH Windows paths retain their drive and accept slash or backslash separators", () => {
  for (const value of ["evidence/probe image.png", "evidence\\probe%20image.png"])
    assert.equal(resolveDshMediaPath(value, "D:\\source-chat"), "D:\\source-chat\\evidence\\probe image.png");
  for (const value of ["/evidence/probe.png", "\\evidence\\probe.png", "%2Fevidence/probe.png"])
    assert.equal(resolveDshMediaPath(value, "D:\\source-chat"), "D:\\evidence\\probe.png");
  assert.equal(resolveDshMediaPath("C:\\images\\probe.png", "D:\\source-chat"), "C:\\images\\probe.png");
  assert.equal(resolveDshMediaPath("C:/images/probe.png", "D:\\source-chat"), "C:/images/probe.png");
  assert.equal(resolveDshMediaPath("C:relative.png", "D:\\source-chat"), null);
});

test("DSH media keeps URLs, network paths and code examples as ordinary text", () => {
  const text = "```md\nMEDIA: evidence/a.png\n![code](evidence/a.png)\n```\n" +
    "`![inline](evidence/a.png)`\n\\![escaped](evidence/a.png)\n" +
    "![web](https://example.test/a.png)\nMEDIA: //server/share/a.png\n" +
    "MEDIA: data:image/png;base64,abc\n";
  assert.deepEqual(project(text), [{ type: "text", text }]);
  for (const value of ["https://example.test/a.png", "file:///a.png", "//server/a.png", "\\\\server\\a.png", "%2F%2Fserver/a.png"])
    assert.equal(resolveDshMediaPath(value, cwd), null);
});

test("DSH holds incomplete media until a full Markdown token or MEDIA line arrives", () => {
  for (const tail of ["M", "ME", "MED", "MEDIA", "MEDIA:", "MEDIA: evidence/a.png", "![probe](evidence/a.png"])
    assert.deepEqual(project("before\n" + tail, { complete: false }), [{ type: "text", text: "before\n" }]);
  assert.deepEqual(project("MEDIA: evidence/probe image.png\n", { complete: false }), [{ type: "image", imagePath: image }]);
  assert.deepEqual(project("![probe](<evidence/probe image.png>)", { complete: false }), [{ type: "image", imagePath: image }]);
  assert.deepEqual(project("MEDIA: evidence/probe image.png"), [{ type: "image", imagePath: image }]);
  assert.deepEqual(project("ordinary M"), [{ type: "text", text: "ordinary M" }]);
});

test("DSH supports escaped parentheses and preserves prose around multiple images", () => {
  const first = path.join(cwd, "evidence", "a(1).png"), second = path.join(cwd, "evidence", "b.png");
  assert.deepEqual(project("first ![a](evidence/a\\(1\\).png) second\nMEDIA: evidence/b.png\nlast"), [
    { type: "text", text: "first " }, { type: "image", imagePath: first },
    { type: "text", text: " second\n" }, { type: "image", imagePath: second }, { type: "text", text: "last" },
  ]);
});

test("the shared parser's default behavior retains Codex absolute-only Markdown handling", () => {
  assert.deepEqual(markdownImagePaths("![relative](evidence/a.png)\nMEDIA: C:/images/a.png"), []);
  assert.deepEqual(markdownImagePaths("![absolute](C:/images/a.png)"), ["C:/images/a.png"]);
});
