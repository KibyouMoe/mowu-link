// Audit probe for a shared delivery race. Uses isolated in-memory sinks only.
// It intentionally reports the existing Codex behavior for a separate fix.
import { QuestionRelay as DshRelay } from "../question-relay.mjs";
import { QuestionRelay as CodexRelay } from "../../codexworker/question-relay.mjs";

for (const [engine, Relay] of [["codex", CodexRelay], ["dsh", DshRelay]]) {
  let entered = 0, release;
  const checked = new Promise(resolve => { release = resolve; });
  const notices = [];
  const manager = { stateDir: ".", config: { bridgeName: engine, port: 0 }, routerClient: {
    async getTask() {
      entered++;
      if (entered === 2) release();
      await checked;
      return { state: "running" };
    },
    async notice(body) { notices.push(body); return { ok: true, event_id: "audit-" + notices.length }; },
  } };
  const relay = new Relay(manager);
  relay.save = async () => {};
  const question = { questionId: "00000000-0000-4000-8000-000000000000", threadId: "audit-thread",
    recipient: { kind: "hermes", taskId: "t-audit" }, questions: [{ title: "Continue?" }] };
  const action = { status: "open", nonce: "audit-only", revision: 1 };
  await Promise.all([relay.forwardToInitiator(question, action), relay.forwardToInitiator(question, action)]);
  console.log(JSON.stringify({ engine, simultaneousForwardCalls: 2, routerNotices: notices.length,
    finalStatus: action.routerDelivery.status }));
}
