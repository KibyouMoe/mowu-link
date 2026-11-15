// The compact pointer survives the coordinator's 100-character trigger preview.
// Authenticated GET returns the full question and its exact answer template.
export function routerQuestionNotice(question, action, manager) {
  const client = manager.questions?.router?.() ?? manager.routerClient;
  if (client?.url && client?.name)
    return `提问，请 GET ${client.url}/questions/${encodeURIComponent(client.name)}/${question.questionId}`;
  const host = ["0.0.0.0", "::", undefined].includes(manager.config.host) ? "127.0.0.1" : manager.config.host;
  const base = `http://${host.includes(":") ? "["+host+"]" : host}:${manager.config.port}`;
  return `提问，请 GET ${base}/bridge/questions/${question.questionId}`;
}
