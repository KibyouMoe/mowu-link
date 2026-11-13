import { setTimeout as delay } from "node:timers/promises";

// A known clarification wait is not a stalled model turn. Keep the task open
// without a new wall-clock limit; still honor explicit deadlines and cancel.
export async function waitForTaskClarification({ manager, router, taskId, threadId, consumed = new Set(),
  deadline = Infinity, sleep = delay, now = Date.now, log = () => {} }) {
  let lastCheck = -Infinity, announced = false;
  for (;;) {
    const questions = Object.values(manager.state.publicQuestions ?? {}).filter(q =>
      q.threadId === threadId && q.recipient?.kind === "hermes" && q.recipient.taskId === taskId && !consumed.has(q.questionId));
    if (!questions.length) return null;
    const question = questions.sort((a,b)=>String(a.createdAt).localeCompare(String(b.createdAt)))[0];
    const action = manager.questions.state.entries[question.questionId];
    if (!action) throw Error("澄清状态不可用；任务未标记完成。");
    if (Number.isFinite(deadline) && now() >= deadline) throw Error("任务的显式 deadline 已到，澄清尚未完成。");
    if (["answered", "skipped"].includes(action.status)) {
      const observed = action.nativeAnswerObservation;
      if (observed?.messageId === action.nativeMessageId && observed.threadId === threadId &&
          typeof observed.turnId === "string" && observed.turnId)
        return { questionId: question.questionId, messageId: observed.messageId, turnId: observed.turnId };
      const receipt = action.nativeMessageId && (manager.state.receipts?.[action.nativeMessageId] ?? await manager.receiptArchive?.get(action.nativeMessageId));
      if (receipt?.nativeDispatch?.status !== "accepted") throw Error("澄清没有可确认的原生接收回执；任务未标记完成。");
      return { questionId: question.questionId, messageId: action.nativeMessageId, turnId: receipt.nativeTurnId ?? null };
    }
    if (["expired","canceled","rejected","answer_unknown"].includes(action.status))
      throw Error(`澄清未完成：${question.questionId} (${action.reason ?? action.status})`);
    if (action.routerDelivery?.status === "unavailable") throw Error("回问溪流的通道不可用；未把问题转交给训练员。");
    if (action.routerDelivery?.status === "unknown")
      throw Error("向溪流提问的投递结果待核实；未自动重复提问。");
    if (router?.getTask && now()-lastCheck >= 5000) {
      lastCheck = now();
      let task;
      try { task = await router.getTask(taskId); }
      catch { /* A temporary read failure cannot authorize progress or new input. */ }
      if (["cancelled","canceled","failed","done"].includes(task?.state))
        throw Error(`原任务已经 ${task.state}；停止等待澄清。`);
    }
    if (!announced) { log("info","router_question_waiting",{taskId,questionId:question.questionId}); announced=true; }
    await sleep(500);
  }
}
