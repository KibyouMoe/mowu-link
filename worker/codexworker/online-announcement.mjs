// A connection notice is a side channel, never a native task input.
export class FeishuOnlineAnnouncement {
  constructor(manager) { this.manager = manager; this.record = null; this.task = null; this.timer = null; this.clock = Date.now; }

  reset() { clearTimeout(this.timer); this.timer = null; this.record = null; }

  pause() { clearTimeout(this.timer); this.timer = null; }

  resume() {
    const socket = this.manager.ws, record = this.record;
    // Timeout may resume the worker while an old metadata/send operation is
    // still finishing. Wait for it; do not create two concurrent notice sends.
    return Promise.resolve(this.task).then(() => {
      if (record && ["stabilizing", "pending"].includes(record.status) && this.current(socket, record)) {
        return this.run(socket, record);
      }
    });
  }

  current(socket, record) {
    return !this.manager.closed && !this.manager.handoffPaused && this.manager.ws === socket && socket?._ws?.readyState === 1 &&
      socket.connectionId === record.connectionId && this.record === record;
  }

  status() {
    const record = this.record ?? this.manager.state.feishuOnlineNotice;
    if (!record) return null;
    const { connectionId, status, attempts, connectedAt, stableAfter, kind, desktopLifetimeStartedAt,
      threadId, sessionTitle, sentAt, messageId } = record;
    return { connectionId, status, attempts, connectedAt, stableAfter, kind, desktopLifetimeStartedAt,
      threadId, sessionTitle, sentAt, messageId };
  }

  connected(socket, connectionId) {
    if (this.manager.closed || this.manager.ws !== socket || socket._ws?.readyState !== 1 ||
        socket.connectionId !== connectionId || this.manager.config.feishu.onlineNotificationEnabled === false) return Promise.resolve();
    if (this.record?.connectionId === connectionId) return this.task ?? Promise.resolve();
    if (this.manager.config.hotReloadTransition) {
      this.manager.config.hotReloadTransition = false;
      this.record = { connectionId, status: "suppressed-hot-reload", attempts: 0,
        connectedAt: new Date().toISOString(), threadId: this.manager.state.selectedThreadId,
        sessionTitle: this.manager.state.feishuOnlineNotice?.sessionTitle ?? null, sentAt: null, messageId: null };
      this.task = this.manager.serial(async () => {
        this.manager.state.feishuOnlineNotice = this.status(); await this.manager.save();
      });
      return this.task;
    }
    this.reset();
    const m = this.manager, now = this.clock();
    const configuredDelay = m.config.feishu.onlineStableMs;
    const stableMs = Number.isFinite(configuredDelay) && configuredDelay >= 0
      ? Math.min(configuredDelay, 300000) : 30000;
    const previous = m.state.feishuLastOnlineNotice ??
      (["sent", "suppressed-hot-reload"].includes(m.state.feishuOnlineNotice?.status) ? m.state.feishuOnlineNotice : null);
    const lifetime = m.config.desktopLifetimeStartedAt ?? null;
    // Older generations persisted only the current hot-reload suppression,
    // without a separate last-success/lifetime field. Its actual connection
    // time can establish a prior connection in this Desktop lifetime, not a
    // claim that a human saw an earlier notification.
    const priorInLifetime = previous && (previous.desktopLifetimeStartedAt === lifetime ||
      (!previous.desktopLifetimeStartedAt && Date.parse(previous.connectedAt) >= Date.parse(lifetime)));
    const recovery = Boolean(previous && (!lifetime || priorInLifetime));
    const record = { connectionId, status: "stabilizing", attempts: 0, connectedAt: new Date(now).toISOString(),
      stableAfter: new Date(now + stableMs).toISOString(), kind: recovery ? "recovered" : "online", desktopLifetimeStartedAt: lifetime,
      threadId: null, sessionTitle: null, sentAt: null, messageId: null };
    this.record = record;
    return this.run(socket, record);
  }

  run(socket, record) {
    this.task = this.send(socket, record).catch(() => {
      // Never log native payloads, Feishu credentials or transport exceptions.
      console.error("[CodexWorker] Online notice failed; inspect authenticated integration status.");
    });
    return this.task;
  }

  async prepare(socket, record) {
    const m = this.manager;
    for (let attempt = 0; attempt < 3; attempt++) {
      const threadId = m.state.selectedThreadId;
      const session = m.state.sessions[threadId];
      let title = null; let verified = false;
      if (session) {
        try {
          // Only metadata is needed; do not fetch or forward private history.
          const snapshot = await m.call("read_thread", { threadId, ...(session.hostId ? { hostId: session.hostId } : {}),
            turnLimit: 1, includeOutputs: false, maxOutputCharsPerItem: 1 });
          if (snapshot.thread?.id === threadId) { m.checkWorkspace(snapshot); title = snapshot.thread.title; verified = true; }
        } catch { /* Preserve the binding and describe unavailability honestly. */ }
      }
      if (!this.current(socket, record)) return false;
      if (threadId !== m.state.selectedThreadId) continue;
      record.threadId = threadId ?? null;
      record.sessionTitle = typeof title === "string" && title.trim() ? title.trim() : verified ? "未命名会话" : null;
      record.chatId = session?.feishuChatId ?? m.config.feishu.chatId;
      // A socket-open event does not establish native task readiness. Retry
      // metadata availability instead of sending a misleading success card.
      if (threadId && !verified) return false;
      const time = new Date(this.clock()).toLocaleString("zh-CN", { timeZone: "Asia/Shanghai", hour12: false });
      const headline = !threadId ? `${m.config.bridgeName}的飞书连接已建立，当前尚未接入会话。`
        : record.kind === "recovered" ? `${m.config.bridgeName}的飞书连接已恢复，当前接入会话可读取。`
        : `${m.config.bridgeName}已上线，飞书连接和当前接入会话已就绪。`;
      const lines = [headline, `就绪时间：${time}`];
      if (record.sessionTitle) {
        const literalTitle = record.sessionTitle.replace(/[\r\n]/g, " ").replace(/`/g, "｀").slice(0, 500);
        lines.push(`当前接入会话：\`${literalTitle}\``);
      } else if (threadId) {
        lines.push("原绑定会话的标题暂不可读取，尚未确认会话可用；绑定未更改，没有转移到其他会话。");
      } else lines.push("当前尚未接入会话，发送 /resume 查看最近会话并选择。");
      lines.push("可用 /resume 查看会话，输入 /resume 0～4 切换；/new 或 /clear 开启新会话。");
      record.text = lines.join("\n\n");
      return true;
    }
    return false; // Selection kept changing; retry later, never guess a target.
  }

  async send(socket, record) {
    if (!this.current(socket, record)) return;
    const m = this.manager;
    const remaining = Date.parse(record.stableAfter) - this.clock();
    if (remaining > 0) {
      await m.serial(async () => { if (this.current(socket, record)) { m.state.feishuOnlineNotice = this.status(); await m.save(); } });
      if (this.current(socket, record)) {
        clearTimeout(this.timer);
        this.timer = setTimeout(() => { void this.run(socket, record); }, remaining); this.timer.unref();
      }
      return;
    }
    record.attempts++;
    let sent = null;
    try {
      if (m.server?.listening && (record.text || await this.prepare(socket, record)) && this.current(socket, record)) {
        if (!record.chatId) record.status = "unavailable";
        else sent = await m.notifier.sendMessage(record.text, { chatId: record.chatId,
          title: `${m.config.bridgeName}-${record.kind === "recovered" ? "连接恢复" : "上线通知"}`,
          threadId: record.threadId ?? undefined, uuid: record.connectionId });
      }
    } catch { /* Bounded retry reuses the same UUID and exact notice body. */ }
    if (!this.current(socket, record)) return;
    if (sent?.code === 0) {
      record.status = "sent"; record.sentAt = new Date().toISOString(); record.messageId = sent.data?.message_id ?? null;
    } else if (record.status !== "unavailable") record.status = record.attempts < 3 ? "pending" : "failed";
    await m.serial(async () => {
      if (!this.current(socket, record)) return;
      m.state.feishuOnlineNotice = this.status();
      if (record.status === "sent") m.state.feishuLastOnlineNotice = this.status();
      await m.save();
    });
    if (record.status === "pending" && this.current(socket, record)) {
      this.timer = setTimeout(() => { void this.run(socket, record); }, 5000); this.timer.unref();
    }
  }
}
