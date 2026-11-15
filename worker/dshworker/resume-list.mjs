export const RESUME_PAGE_SIZE = 10;

export function resumePage(candidates, page = 0) {
  const pages = Math.max(1, Math.ceil(candidates.length / RESUME_PAGE_SIZE));
  if (!Number.isSafeInteger(page) || page < 0 || page >= pages) throw new Error("resume_page_out_of_range");
  return { sessions: candidates.slice(page * RESUME_PAGE_SIZE, (page + 1) * RESUME_PAGE_SIZE),
    page: { number: page + 1, pages, total: candidates.length, size: RESUME_PAGE_SIZE } };
}

export function resumeListText(result) {
  const current = result.currentSession;
  const title = current?.title?.replace(/[\r\n]/g, " ").replace(/`/g, "｀");
  const lines = [current ? `当前接入会话：${title ? `\`${title}\`` : "原绑定会话标题暂不可读取，绑定未更改"}` +
    (current.index == null ? "（不在本次列表中）" : `（编号 ${current.index}）`) : "当前尚未接入会话。"];
  if (result.resumeQuery) lines.push(`匹配：${result.resumeQuery}`);
  lines.push(`按最近发起或继续对话排序｜第 ${result.resumePage.number}/${result.resumePage.pages} 页｜共 ${result.resumePage.total} 个`);
  lines.push(result.sessions.length ? result.sessions.map(s => `编号 ${s.index}｜${s.alias}${s.threadId === current?.threadId ? " 【当前】" : ""}`).join("\n\n") : "当前没有匹配的可恢复会话。");
  lines.push("使用 /resume 编号 选择；也可输入完整标题或会话 ID。\n/resume next 下一页 · /resume prev 上一页\n/resume search 关键词 搜索 · /resume 刷新列表\n编号对应这次列表，翻页不会重排；查看列表不会切换会话。");
  if (result.resumeDiscovery?.partial) lines.push(`另有 ${result.resumeDiscovery.unavailable + result.resumeDiscovery.deferred} 个侧栏引用暂不可读取；已核验的本机会话仍可选择。`);
  return lines.join("\n\n");
}
