export const collaborationHtml = String.raw`<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>协作记录</title>
  <style>
    :root { color-scheme: light dark; font-family: "Instrument Sans", system-ui, sans-serif; }
    body { margin: 0; color: light-dark(#242426,#eee); background: light-dark(#fafafa,#202023); }
    header { position: sticky; top: 0; padding: 18px 22px; background: inherit; border-bottom: 1px solid #8883; }
    h1 { font-size: 17px; margin: 0; font-weight: 600; }
    #status { font-size: 13px; margin: 8px 0 0; color: light-dark(#666,#aaa); }
    main { max-width: 850px; margin: auto; padding: 0 22px 20px; }
    article { padding: 22px 0; border-bottom: 1px solid #8883; }
    time { display: block; margin-bottom: 9px; color: light-dark(#666,#aaa); font-size: 12px; }
    p { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; line-height: 1.65; font-size: 15px; }
    figure { margin: 14px 0 0; }
    img { display: block; max-width: 100%; max-height: 240px; object-fit: contain; border-radius: 8px; }
    figcaption { margin-top: 5px; color: light-dark(#666,#aaa); font-size: 12px; }
  </style>
  <script src="/collaboration/view.js" defer></script>
</head>
<body>
  <header><h1>协作记录</h1><p id="status" role="status">正在读取</p></header>
  <main aria-label="协作消息" aria-live="polite"></main>
</body>
</html>
`;

export const collaborationScript = String.raw`const key = location.hash.slice(1);
const status = document.querySelector("#status");
const main = document.querySelector("main");
const images = new Map();
let revision;

async function get(route) {
  const response = await fetch("/collaboration/" + route, {
    headers: { Authorization: "Bearer " + key }, cache: "no-store", redirect: "error",
    signal: AbortSignal.timeout(10000),
  });
  if (!response.ok) throw new Error("collaboration_read_failed");
  return response;
}

async function refresh() {
  try {
    const { records } = await (await get("records")).json();
    const nextRevision = JSON.stringify(records);
    if (nextRevision !== revision) {
      const fragment = document.createDocumentFragment();
      for (const record of records) {
        const article = document.createElement("article");
        const time = document.createElement("time");
        time.dateTime = record.timestamp;
        time.textContent = new Date(record.timestamp).toLocaleString();
        const text = document.createElement("p");
        text.textContent = record.displayText;
        article.append(time, text);
        for (const attachment of record.attachments) {
          let source = images.get(attachment.id);
          if (!source) {
            source = URL.createObjectURL(await (await get("images/" + attachment.id)).blob());
            images.set(attachment.id, source);
          }
          const figure = document.createElement("figure");
          const image = document.createElement("img");
          image.src = source;
          image.alt = attachment.name || "图片附件";
          const caption = document.createElement("figcaption");
          caption.textContent = image.alt;
          figure.append(image, caption);
          article.append(figure);
        }
        fragment.append(article);
      }
      main.replaceChildren(fragment);
      revision = nextRevision;
    }
    status.textContent = records.length ? "" : "暂无协作消息";
  } catch {
    status.textContent = "连接恢复后自动更新";
  } finally {
    setTimeout(refresh, 2000);
  }
}

addEventListener("pagehide", () => { for (const source of images.values()) URL.revokeObjectURL(source); });
void refresh();
`;
