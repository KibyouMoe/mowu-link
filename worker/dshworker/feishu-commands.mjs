export const BRIDGE_HELP = `**会话**
/resume — 按最近发起/继续时间列出会话，每页10个
/resume 编号、完整标题或会话ID — 恢复指定会话
/resume next、/resume prev — 翻页（编号不变）
/resume search 关键词 — 搜索老会话
/status — 查看当前连接和任务状态
/new — 新建会话
/fork — 在当前任务空闲时分叉

**目标模式（当前 Codex 会话）**
/goal 具体目标 — 要求 Codex 设置原生目标并执行
/goal status — 查询原生目标
/goal pause — 要求暂停目标
/goal resume — 要求继续目标

直接发消息可补充正在执行的任务；/queue 内容会等当前任务结束再发送。
目标命令先送入当前会话，由 Codex 的实际执行结果确认；发送成功不代表目标已经设置。
停止当前执行请使用 Codex 的停止按钮；这里不会把无效的停止命令显示为成功。`;

export function goalPrompt(command) {
  const arg = command.replace(/^\/goal(?:\s+|$)/, "").trim();
  if (!arg) return null;
  if (/^(status|状态)$/i.test(arg)) return "训练员通过飞书请求：调用 get_goal 查询当前会话的真实目标、状态和进展，并用中文简洁汇报。不要创建或修改目标。";
  if (/^(pause|暂停)$/i.test(arg)) return "训练员通过飞书明确要求：暂停当前会话目标。调用 get_goal 核对，再用 update_goal 将现有目标设为 paused，汇报实际返回结果并停止目标工作。没有目标时如实说明。";
  if (/^(resume|继续)$/i.test(arg)) return "训练员通过飞书明确要求：继续执行当前会话原有目标。先核对 get_goal 和最新进展，继续尚未完成的工作；不要另建目标或新会话，不重做已完成事项。若原生状态需要训练员在界面恢复，明确说明，不能谎报恢复成功。";
  return `训练员通过飞书明确要求为当前会话设置并执行以下目标：\n\n${arg}\n\n先调用 get_goal 核对。没有未完成目标时用 create_goal 建立上述目标并执行；已有未完成目标时说明冲突，不擅自将其标为完成或替换。除非上面的原话明确给出 token 预算，否则不要自设 token_budget。以原生工具实际返回为准，随后简洁汇报目标设置结果并开展工作。`;
}
