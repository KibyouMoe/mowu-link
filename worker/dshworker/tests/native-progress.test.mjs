import assert from "node:assert/strict";
import test from "node:test";
import { publicProgress } from "../interface/native-progress.mjs";
const poll = (extra = {}) => ({ thread: { id: "target", hostId: "local" }, latestTurn: { id: "turn", status: "inProgress" },
  latestAssistantMessageId: "message", latestAssistantMessage: { id: "message", turnId: "turn", phase: "commentary", text: "完整公开段落\n\n第二段" },
  latestToolMarker: { private: "never exported" }, cursor: "native-cursor", ...extra });
test("only explicit public text and native IDs enter the limited progress result", () => {
  const result = publicProgress({ polls: [poll()] }, "target");
  assert.equal(result.message.text, "完整公开段落\n\n第二段"); assert.equal(result.message.id, "message");
  assert.equal(result.cursor, "native-cursor"); assert.doesNotMatch(JSON.stringify(result), /never exported/);
});
test("wrong task, wrong host, wrong message identity and explicit truncation are rejected", () => {
  for (const value of [poll({ thread: { id: "other" } }), poll({ thread: { id: "target", hostId: "remote" } }),
    poll({ latestAssistantMessageId: "other" }), poll({ latestAssistantMessage: { ...poll().latestAssistantMessage, truncated: true } })]) {
    assert.throws(() => publicProgress({ polls: [value] }, "target"));
  }
});
test("private phases and omitted unchanged text never become public output", () => {
  for (const message of [{ ...poll().latestAssistantMessage, phase: "analysis" }, undefined]) {
    assert.equal(publicProgress({ polls: [poll({ latestAssistantMessage: message })] }, "target").message, null);
  }
});
test("the native latest message keeps its own previous turn ID when the new turn has no output yet", () => {
  const result = publicProgress({ polls: [poll({ latestAssistantMessage: { ...poll().latestAssistantMessage, turnId: "previous", phase: "final" } })] }, "target");
  assert.equal(result.turnId, "turn"); assert.equal(result.message.turnId, "previous"); assert.equal(result.message.final, true);
});
