import assert from "node:assert/strict";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import process from "node:process";
import { pathToFileURL } from "node:url";
import { isNativeQuestionReply, projectPublicQuestion } from "../interface/public-questions.mjs";
import { IMAGE_UPLOAD_LIMIT_BYTES } from "../feishu-image-output.mjs";
import { dshFixture, png, sha256 } from "./fixtures/dsh-harness.mjs";

async function withFixture(work) {
  const cleanup = [];
  try { return await work(await dshFixture({ after: action => cleanup.push(action) }, { wireMedia: true })); }
  finally { for (const action of cleanup.reverse()) await action(); }
}

async function outbound(format) {
  return withFixture(async f => {
    await mkdir(path.join(f.root, "evidence"));
    const filename = path.join(f.root, "evidence", "probe image.png");
    const bytes = format === "large-media" ? Buffer.concat([png, Buffer.alloc(IMAGE_UPLOAD_LIMIT_BYTES + 1 - png.length)]) : png;
    await writeFile(filename, bytes); await f.send("media probe");
    const text = format === "absolute-markdown" ? `![probe](<${filename}>)`
      : format === "relative-markdown" ? "![probe](<evidence/probe image.png>)"
      : format === "absolute-media" ? `MEDIA: ${filename}`
      : format === "large-media" ? "MEDIA: evidence/probe image.png"
      : "before\nMEDIA: evidence/probe image.png\nafter";
    f.stream(text);
    f.emit("assistant/message", { message: { content: [{ type: "text", text }] } });
    await f.finish(); await f.drain();
    const leaked = f.calls.filter(call => /MEDIA:|!\[probe\]/.test(call.text));
    return { direction: "dsh-to-feishu", format, mediaMessages: f.mediaMessages.length,
      mediaTypes: f.mediaMessages.map(message => message.msg_type), leakedMediaText: leaked.length,
      pureMedia: f.mediaMessages.every(message => ["image", "file"].includes(message.msg_type)
        && Object.keys(message.content).length === 1),
      uploadedSha256: f.uploads[0]?.sha256 ?? null, expectedSha256: sha256(bytes) };
  });
}

async function inbound(type) {
  return withFixture(async f => {
    const event = { message_id: "om_receive_probe", message_type: type, open_id: "trainer", chat_type: "p2p",
      sender_type: "user", chat_id: "chat", content: JSON.stringify(type === "image"
        ? { image_key: "img_receive_probe" } : { file_key: "file_receive_probe", file_name: "original.png" }) };
    const result = await f.manager.receiveFeishu(event);
    for (let index = 1; index < 20; index++) await f.manager.receiveFeishu(event);
    const receipt = f.manager.state.feishuReceipts[result.messageId];
    const bytes = await readFile(receipt.imagePath);
    const thread = f.transport.threads.get(f.threadId);
    const output = { direction: "feishu-to-dsh", type, repeats: 20, downloads: f.downloads.length,
      nativeInputs: f.records.filter(record => record.type === "user/message").length,
      receipts: Object.keys(f.manager.state.feishuReceipts).length,
      cachedSha256: sha256(bytes), expectedSha256: sha256(png),
      promptHasCachedPath: thread.turns[0].prompt.includes(receipt.imagePath) };
    await f.finish(); return output;
  });
}

async function question(repeats, recipient) {
  return withFixture(async f => {
    const notices = [];
    f.manager.routerClient = {
      getTask: async () => ({ state: "running" }),
      notice: async body => { notices.push(body); return { ok: true, event_id: "probe-notice" }; },
    };
    await f.send("question probe", recipient === "hermes" ? { source: "hermes", origin: "router", taskId: "t-probe" } : {});
    const controller = new AbortController();
    const request = { agent: f.agent, signal: controller.signal, wait: { callId: "native-question-probe", timed: true },
      questions: [{ id: "choice", question: "Continue?", options: [{ label: "Yes" }, { label: "No" }] }] };
    let nativeProviders = 0, answered = 0;
    const pending = [];
    for (let index = 0; index < repeats; index++) {
      const result = f.transport.questions.request(request, () => {
        nativeProviders++; return Promise.reject(Object.assign(new Error("no native provider"), { code: "NO_PROVIDER" }));
      });
      result.then(() => { answered++; }, () => {}); pending.push(result);
    }
    try {
      const turn = f.transport.threads.get(f.threadId).turns[0], item = turn.nativeQuestions[0];
      const projections = Array.from({ length: repeats }, (_, index) => projectPublicQuestion(item,
        { threadId: f.threadId, turnId: index === 0 ? turn.id : "reconnected-wrapper-" + index }));
      await Promise.all(projections.map(projected => f.manager.questions.publish(projected)));
      const sourceTurn = f.transport.readThread({ threadId: f.threadId }).turns[0];
      for (let index = 0; index < repeats; index++) await f.manager.captureTaskQuestions(f.threadId, sourceTurn);
      await f.drain();
      const first = projections[0], action = f.manager.questions.state.entries[first.questionId];
      const callback = { header: { event_type: "card.action.trigger", event_id: "probe-answer" },
        event: { operator: { open_id: "trainer" }, context: { open_message_id: action.messageId, open_chat_id: "chat" },
          action: { value: { bridgeQuestion: first.questionId, nonce: action.nonce, action: "form_select",
            questionIndex: 0, optionIndex: 0 }, form_value: {} } } };
      const answer = { actor: "hermes", threadId: f.threadId, taskId: "t-probe", nonce: action.nonce, answers: ["Yes"] };
      for (let index = 0; index < repeats; index++) {
        if (recipient === "hermes") await f.manager.questions.acceptAgentAnswer(first.questionId, answer);
        else await f.manager.questions.acceptCard(callback);
      }
      await f.manager.questions.process(); await new Promise(resolve => setImmediate(resolve)); await f.drain();
      return { direction: "question", recipient, repeats, taskCaptureCalls: repeats,
        nativeQuestionItems: turn.nativeQuestions.length, nativeProviders,
        publicQuestionIds: new Set(projections.map(projected => projected.questionId)).size,
        cards: f.questionPosts.length, receipts: Object.keys(f.manager.state.publicQuestions).length,
        routerNotices: notices.length,
        answerReceipts: Object.values(f.manager.state.receipts).filter(receipt =>
          receipt.nativeDispatch?.status === "accepted" && isNativeQuestionReply(receipt.request?.text)).length,
        answeredWaiters: answered, nonces: new Set(Object.values(f.manager.questions.state.entries).map(entry => entry.nonce)).size };
    } finally {
      controller.abort(new Error("probe finished")); await Promise.allSettled(pending); await f.finish();
    }
  });
}

export async function runProbes(report = console.log) {
  const output = [];
  for (const format of ["absolute-markdown", "relative-markdown", "absolute-media", "mixed-media", "large-media"]) {
    const result = await outbound(format); output.push(result); report(JSON.stringify(result));
  }
  for (const type of ["image", "file"]) {
    const result = await inbound(type); output.push(result); report(JSON.stringify(result));
  }
  for (const recipient of ["trainer", "hermes"]) {
    const result = await question(20, recipient); output.push(result); report(JSON.stringify(result));
  }
  return output;
}

export function assertFixed(results) {
  for (const result of results) {
    if (result.direction === "dsh-to-feishu") {
      assert.equal(result.mediaMessages, 1, result.format);
      assert.deepEqual(result.mediaTypes, [result.format === "large-media" ? "file" : "image"]);
      assert.equal(result.leakedMediaText, 0, result.format);
      assert.equal(result.pureMedia, true); assert.equal(result.uploadedSha256, result.expectedSha256);
    } else if (result.direction === "feishu-to-dsh") {
      assert.equal(result.downloads, 1); assert.equal(result.nativeInputs, 1); assert.equal(result.receipts, 1);
      assert.equal(result.cachedSha256, result.expectedSha256); assert.equal(result.promptHasCachedPath, true);
    } else {
      assert.equal(result.nativeQuestionItems, 1); assert.equal(result.nativeProviders, 1);
      assert.equal(result.publicQuestionIds, 1); assert.equal(result.cards, 1); assert.equal(result.receipts, 1);
      assert.equal(result.routerNotices, result.recipient === "hermes" ? 1 : 0); assert.equal(result.answerReceipts, 1);
      assert.equal(result.answeredWaiters, result.repeats); assert.equal(result.nonces, 1);
    }
  }
}

if (process.argv[1] && pathToFileURL(path.resolve(process.argv[1])).href === import.meta.url) {
  const results = await runProbes();
  if (process.argv.includes("--expect-fixed")) assertFixed(results);
}
