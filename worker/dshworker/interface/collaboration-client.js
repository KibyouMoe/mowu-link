// Loaded by Harness's supported client-module registry, inside the workbench.
window.__ModuleLoader__.load({
  id: "dshworker",
  factory(require) {
    const React = require("react");
    const { GuideArtworkBrowser } = require("@deepseek-ai/dsh-client-ui-primitives");
    const tabId = "dshworker/collaboration";
    const kind = "worker-collaboration";
    return {
      inject: ["slots", "layout", "connection", "sidebarRight", "sidebarRightTabs"],
      apply(ctx) {
        const title = () => "协作记录";
        const view = () => {
          const [state, update] = React.useState({ url: null, error: false });
          React.useEffect(() => {
            const controller = new AbortController();
            ctx.connection.rpc.call("/api", "workerCollaboration/view", { args: {} }, controller.signal)
              .then(result => {
                if (!controller.signal.aborted) update(result.ok ? { url: result.value.url, error: false } : { url: null, error: true });
              }).catch(() => { if (!controller.signal.aborted) update({ url: null, error: true }); });
            return () => controller.abort();
          }, []);
          if (!state.url) return React.createElement("p", null, state.error ? "协作记录暂未连接" : "正在读取协作记录");
          return React.createElement("iframe", { src: state.url, title: "协作记录",
            referrerPolicy: "no-referrer", sandbox: "allow-scripts allow-same-origin",
            style: { display: "block", width: "100%", height: "100%", minHeight: "400px", border: 0 } });
        };
        ctx.effect(() => ctx.slots.inject("sidebar.panellist", () =>
          ctx.slots.register({ name: "sidebar.panellist", id: tabId, label: title, order: 20 },
            () => React.createElement(GuideArtworkBrowser))));
        ctx.effect(() => ctx.slots.inject("main", () =>
          ctx.slots.register({ name: "main", key: tabId }, view)));
        ctx.effect(() => ctx.sidebarRightTabs.register({
          id: tabId, kind, title,
          guide: [{ id: "collaboration", order: 40, title,
            description: () => "查看 worker 之间的消息和附件", icon: GuideArtworkBrowser }],
        }));
        ctx.effect(() => ctx.slots.inject("sidebar.right.pane.tab.title", () =>
          ctx.slots.register({ name: "sidebar.right.pane.tab.title", key: tabId }, title)));
        ctx.effect(() => ctx.slots.inject("sidebar.right.pane.tab", () =>
          ctx.slots.register({ name: "sidebar.right.pane.tab", key: tabId }, view)));
      },
    };
  },
});
