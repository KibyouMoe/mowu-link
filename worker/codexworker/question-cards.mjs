// Explicit public clarification UI only. Never render raw native tool arguments.
import { escapeMarkdown } from "./feishu.mjs";
export const QUESTION_CARD_LAYOUT_VERSION = 3;

export function questionCard(question, state, bridgeName) {
  const terminal = !["open", "answer_pending", "dispatching"].includes(state.status);
  const labels = { open: "待回答", answer_pending: "答案已保存，正在投递", dispatching: "正在投递答案",
    answered: "已回答", expired: "已过期", canceled: "已取消", answer_unknown: "投递结果待核实", rejected: "答案未投递" };
  const elements = [];
  const actionable = question.recipient?.kind !== "hermes" && state.status === "open" && state.sourceTurnId && question.questions.every(q => q.options);
  question.questions.forEach((q, qi) => {
    const compactChoices = q.options?.every(option => option.length <= 16 && !option.includes("\n"));
    let text = `${question.questions.length > 1 ? `${qi + 1}. ` : ""}${q.title}`;
    // Short full labels already appear on the buttons. Long labels remain in
    // the body with small numbered buttons; never truncate the actual options.
    if (!actionable || !compactChoices) q.options?.forEach((option, oi) => { text += `\n${oi + 1}）${option}`; });
    const answer = state.answers?.[qi] ?? (state.selected?.[qi] != null ? q.options?.[state.selected[qi]] : null);
    if (answer) text += `\n\n${question.recipient?.kind === "hermes" ? "溪流的回答" : terminal ? "所选答案" : "当前选择"}：${answer}`;
    elements.push({ tag: "markdown", content: escapeMarkdown(text) });
    if (!actionable) return;
    for (let offset = 0; offset < q.options.length; offset += 3) {
      elements.push({ tag: "column_set", flex_mode: "flow", columns: q.options.slice(offset, offset + 3).map((option, index) => {
        const oi = offset + index;
        return { tag: "column", width: "weighted", weight: 1, elements: [{ tag: "button",
          text: { tag: "plain_text", content: compactChoices ? `${oi + 1}）${option}` : `选择 ${oi + 1}` },
          type: state.selected?.[qi] === oi ? "primary" : "default",
          behaviors: [{ type: "callback", value: { bridgeQuestion: question.questionId, nonce: state.nonce,
            action: "select", questionIndex: qi, optionIndex: oi } }] }] };
      }) });
    }
  });
  if (question.recipient?.kind !== "hermes" && state.status === "open" && state.sourceTurnId && question.questions.length > 1 && question.questions.every(q => q.options)) {
    elements.push({ tag: "button", text: { tag: "plain_text", content: "提交所选答案" }, type: "primary",
      behaviors: [{ type: "callback", value: { bridgeQuestion: question.questionId, nonce: state.nonce, action: "submit" } }] });
  }
  const reason = { source_task_ended: "原派发任务已经结束，旧问题不再接收回答。", source_turn_ended: "原提问任务已经结束，选项不再接收答案。",
    selected_target_changed: "接入会话已切换，旧卡片不再接收答案。", source_unavailable: "无法核实原提问任务，未投递答案。",
    dispatch_outcome_unknown: "原生投递结果不确定，请在 Codex 核对；不会自动重发。", writer_conflict: "此设备当前不支持忙态投递，答案未发送。",
    canceled_by_owner: "提问已由原会话取消。" }[state.reason];
  elements.push({ tag: "markdown", content: (reason ? reason + "\n\n" : "") +
    (state.status === "open" ? question.recipient?.kind === "hermes"
      ? (state.routerDelivery?.status === "sent" ? "已向溪流提问，等待溪流回复。" : state.routerDelivery?.status === "unknown" ? "向溪流发送的结果待核实。" : "正在连接溪流的回问通道。") + "\n\n"
      : "点选或直接回复即可。\n\n" : "") + `提问 ${question.questionId.slice(0, 8)}` });
  return { schema: "2.0", config: { update_multi: true }, header: { template: state.status === "expired" ? "grey" : "indigo",
    title: { tag: "plain_text", content: `${bridgeName}-提问 · ${labels[state.status] ?? "状态待核实"}` } }, body: { elements } };
}

export function parseCardAction(payload) {
  try {
    const data = typeof payload === "object" && !(payload instanceof Uint8Array) ? payload : JSON.parse(new TextDecoder().decode(payload));
    if (data?.header?.event_type !== "card.action.trigger") return null;
    const e = data.event;
    let value = e?.action?.value;
    if (typeof value === "string") value = JSON.parse(value);
    if (!value || typeof value !== "object" || Array.isArray(value)) return null;
    return { eventId: data.header.event_id, openId: e.operator?.open_id,
      chatId: e.context?.open_chat_id, messageId: e.context?.open_message_id, value };
  } catch { return null; }
}
