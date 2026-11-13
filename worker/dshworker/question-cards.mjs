// Explicit public clarification UI only. Never render raw native tool arguments.
import { escapeMarkdown } from "./feishu.mjs";
import { SKIPPED_QUESTION_ANSWER } from "./interface/public-questions.mjs";
export const QUESTION_CARD_LAYOUT_VERSION = 9;
export const QUESTION_ANSWER_LIMIT = 1000;
export const questionInputName = index => `answer_${index}`;

export function questionCard(question, state, bridgeName) {
  const labels = { open: "待回答", answer_pending: "答案已保存，正在投递", dispatching: "正在投递答案",
    answered: "已回答", skipped: (state.skippedQuestionIndexes?.length ?? question.questions.length) < question.questions.length ? "已完成 · 部分跳过" : "已跳过", expired: "已过期", canceled: "已取消", answer_unknown: "投递结果待核实", rejected: "答案未投递" };
  const elements = [];
  const canRespond = question.recipient?.kind !== "hermes" && state.status === "open" && state.sourceTurnId;
  const form = [];
  let footerInput;
  const button = (label, action, name, extra = {}, primary = false) => ({ tag: "button", name,
    text: { tag: "plain_text", content: label }, type: primary ? "primary" : "default", size: "small", action_type: "form_submit",
    value: { bridgeQuestion: question.questionId, nonce: state.nonce, action, ...extra } });
  question.questions.forEach((q, qi) => {
    const title = `${question.questions.length > 1 ? `${qi + 1}. ` : ""}${q.title}`;
    if (canRespond) {
      form.push({ tag: "markdown", content: escapeMarkdown(title) });
      const draft = state.draftAnswers?.[qi] ?? (state.selected?.[qi] != null ? q.options?.[state.selected[qi]] : null) ?? state.answers?.[qi] ?? "";
      q.options?.forEach((option, oi) => {
        const short = option.length <= 40 && !option.includes("\n");
        if (!short) form.push({ tag: "markdown", content: escapeMarkdown(`${oi + 1}）${option}`) });
        form.push(button(`${draft === option ? "☑" : "☐"} ${short ? `${oi + 1}  ${option}` : `选择 ${oi + 1}`}`, "form_select", `select_${qi}_${oi}`,
          { questionIndex: qi, optionIndex: oi }, draft === option));
      });
      const input = { tag: "input", name: questionInputName(qi), input_type: draft.includes("\n") ? "multiline_text" : "text", width: "fill",
        placeholder: { tag: "plain_text", content: "或自行撰写回复" },
        default_value: draft, max_length: QUESTION_ANSWER_LIMIT, required: false };
      if (qi === question.questions.length - 1) footerInput = input;
      else form.push(input);
      if (qi < question.questions.length - 1) form.push({ tag: "hr" });
      return;
    }
    let text = title;
    q.options?.forEach((option, oi) => { text += `\n${oi + 1}）${option}`; });
    const answer = state.answers?.[qi] ?? (state.selected?.[qi] != null ? q.options?.[state.selected[qi]] : null);
    if (answer === SKIPPED_QUESTION_ANSWER) text += "\n\n已跳过，未提供答案";
    else if (answer) text += `\n\n${state.answerActor === "native" ? "客户端回答" : question.recipient?.kind === "hermes" ? "溪流的回答" : "回答"}：${answer}`;
    elements.push({ tag: "markdown", content: escapeMarkdown(text) });
  });
  if (canRespond) {
    form.push({ tag: "column_set", flex_mode: "none", horizontal_align: "right", horizontal_spacing: "8px", columns: [
      { tag: "column", width: "weighted", weight: 1, vertical_align: "center", elements: [footerInput] },
      { tag: "column", width: "auto", vertical_align: "center", elements: [{ ...button("跳过", "form_skip", "skip_answers"), type: "default" }] },
      { tag: "column", width: "auto", vertical_align: "center", elements: [{ ...button("发送", "form_submit", "submit_answers"), type: "primary_filled" }] },
    ] });
    elements.push({ tag: "form", name: "question_answers", vertical_spacing: "8px", padding: "0px", elements: form });
  }
  const reason = { source_task_ended: "原派发任务已经结束，旧问题不再接收回答。", source_turn_ended: "原提问任务已经结束，选项不再接收答案。",
    selected_target_changed: "接入会话已切换，旧卡片不再接收答案。", source_unavailable: "无法核实原提问任务，未投递答案。",
    dispatch_outcome_unknown: "原生投递结果不确定，请在 Codex 核对；不会自动重发。", writer_conflict: "此设备当前不支持忙态投递，答案未发送。",
    canceled_by_owner: "提问已由原会话取消。" }[state.reason];
  if (reason) elements.push({ tag: "markdown", content: reason });
  if (state.status === "open" && question.recipient?.kind === "hermes") {
    elements.push({ tag: "markdown", content: state.routerDelivery?.status === "sent" ? "已向溪流提问，等待溪流回复。"
      : state.routerDelivery?.status === "unknown" ? "向溪流发送的结果待核实。" : "正在连接溪流的回问通道。" });
  }
  return { schema: "2.0", config: { update_multi: true }, header: { template: state.status === "expired" ? "grey" : "indigo",
    title: { tag: "plain_text", content: `${bridgeName}-提问${state.status === "open" ? "" : ` · ${labels[state.status] ?? "状态待核实"}`}` } },
    body: { padding: "12px", vertical_spacing: "8px", elements } };
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
      chatId: e.context?.open_chat_id, messageId: e.context?.open_message_id, value, formValues: e.action.form_value };
  } catch { return null; }
}

// Form values are explicit trainer input; only the bound question fields count.
// Routing identities always come from the authenticated callback, never a form field.
export function questionFormAnswers(question, values) {
  if (!values || typeof values !== "object" || Array.isArray(values)) throw new Error("invalid_question_form");
  return question.questions.map((_, index) => {
    const name = questionInputName(index), value = Object.hasOwn(values, name) ? values[name] : "";
    if (value == null) return "";
    if (typeof value !== "string" || value.length > QUESTION_ANSWER_LIMIT) throw new Error("invalid_question_form");
    return value.trim();
  });
}
