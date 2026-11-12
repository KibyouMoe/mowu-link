// Public clarification -> router notice -> Hermes. No approval semantics,
// model calls, new task creation, or credentials in the event text.
export function routerQuestionNotice(question, action, manager) {
  const host = ["0.0.0.0", "::", undefined].includes(manager.config.host) ? "127.0.0.1" : manager.config.host;
  const base = `http://${host.includes(":") ? "["+host+"]" : host}:${manager.config.port}`;
  const path = `/bridge/questions/${question.questionId}`;
  const request = { actor: "hermes", threadId: question.threadId, taskId: question.recipient.taskId ?? null,
    nonce: action.nonce, answers: question.questions.map(() => "<填写回答>") };
  const prefix = `溪流，${manager.config.bridgeName}需要你澄清原任务。${question.recipient.taskId ? "任务："+question.recipient.taskId : ""}\n提问：${question.questionId}`;
  const instructions = `\n\n完整题目：GET ${base+path}\n请使用 Worker API 的 Bearer 认证，回答后 POST ${base+path}/answer，正文：\n${JSON.stringify(request)}\n请填写真实回答，不要直接提交占位符；不要另建任务，回答会进入原会话。此事件是澄清，不是权限审批。`;
  let detail = question.questions.map((q,i)=>`${i+1}）${q.title}\n${(q.options??[]).map((s,j)=>`  ${j+1}. ${s}`).join("\n")}`).join("\n\n");
  if ((prefix+detail+instructions).length > 7900)
    detail = question.questions.map((q,i)=>`${i+1}）${q.title.slice(0,300)}`).join("\n")+"\n（题目或选项较长，请 GET 上方提问接口读取全文。）";
  return prefix+"\n\n"+detail+instructions;
}
