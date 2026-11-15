// Loaded by Harness's supported client-module registry, inside the workbench.
window.__ModuleLoader__.load({
  id: "dshworker",
  factory(require) {
    const React = require("react");
    const { GuideArtworkBrowser } = require("@deepseek-ai/dsh-client-ui-primitives");
    const tabId = "dshworker/collaboration";
    const kind = "worker-collaboration";
    return {
      inject: ["slots", "sidebarRight", "sidebarRightTabs"],
      apply(ctx, config = {}) {
        const open = () => ctx.sidebarRight.openTab(kind);
        const title = () => "协作记录";
        ctx.effect(() => ctx.sidebarRightTabs.register({
          id: tabId, kind, title,
          guide: [{ id: "collaboration", order: 40, title,
            description: () => "查看 worker 之间的消息和附件", icon: GuideArtworkBrowser }],
        }));
        ctx.effect(() => ctx.slots.inject("conversation.session.header.corner", () =>
          ctx.slots.register({ name: "conversation.session.header.corner", key: tabId },
            () => React.createElement("button", { type: "button", onClick: open, title: "协作记录" }, "协作记录"))));
        ctx.effect(() => ctx.slots.inject("sidebar.right.pane.tab.title", () =>
          ctx.slots.register({ name: "sidebar.right.pane.tab.title", key: tabId }, title)));
        ctx.effect(() => ctx.slots.inject("sidebar.right.pane.tab", () =>
          ctx.slots.register({ name: "sidebar.right.pane.tab", key: tabId }, () => {
            if (!config.collaborationUrl) return React.createElement("p", null, "协作记录入口待配置");
            return React.createElement("iframe", { src: config.collaborationUrl, title: "协作记录",
              referrerPolicy: "no-referrer", sandbox: "allow-scripts allow-same-origin",
              style: { width: "100%", height: "100%", border: 0 } });
          })));
      },
    };
  },
});
