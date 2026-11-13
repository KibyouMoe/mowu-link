# 截图样式对齐：布局 6

根据用户提供的 Codex 原生提问截图，改用浅色标题、无边框纵排编号选项、
「或自行撰写回复」输入提示，底部「跳过」「发送」紧凑排列并靠右。
待答卡片移除重复状态、长操作说明与技术编号；终态和异常原因仍可见。
本次只调整展示，表单回调、原生问题绑定和幂等逻辑保持不变。

两侧 question-cards / question-routing / native-public-questions 定向回归：
`node --test --test-reporter=tap worker/{codexworker,dshworker}/tests/{question-cards,question-routing,native-public-questions}.test.mjs`
（Bash/Zsh 展开；Windows 使用实际文件列表。）

```text
# tests 166
# pass 166
# fail 0
# cancelled 0
# skipped 0
```

飞书结构验证返回 HTTP 200 / code=0 / card_id=7707660496032549881。
日志：`/tmp/oguri-question-layout6.tap`。真实飞书客户端视觉效果仍需用户验收，
不将接口接受等同于截图一致。以下是上一版功能验收记录。

# 飞书提问表单验收（2026-11-13）

布局版本从 4 升为 5。问题、编号选项、多行回答框、提交和跳过放在同一张卡片。
每题最多 1000 字（飞书服务端实际校验上限）。选项只填入草稿，单题也显式提交；
多题选择携带整个表单，保留其他题已填内容。跳过仅跳过空白题。

`question-cards.mjs` 生成 form/input 和表单按钮，提取 `action.form_value`；
`question-relay.mjs` 验证字段、持久化草稿/答案后 ACK，再经原回答通道写入。
身份、原消息/聊天、nonce、会话绑定、幂等和未知发送处理沿用原约束。
旧 select/submit/skip 回调继续兼容；router 发起的提问仍由溪流回答。
Codex/DSH 两份实现同步，三个公共业务文件未修改且逐字节相同。

## 本机定向回归

从仓库根目录运行，Node v26.12.1；清除外层 Worker 环境变量以隔离测试。
以下计数分别来自两个真实命令，没有将重复测试文件另加进总数。

```sh
env -u CODEXWORKER_NATIVE_TRANSPORT -u CODEXWORKER_STATE_DIR -u CODEXWORKER_IDENTITY node --test --test-reporter=tap worker/codexworker/tests/question-cards.test.mjs worker/codexworker/tests/question-routing.test.mjs worker/codexworker/tests/native-public-questions.test.mjs worker/codexworker/tests/feishu-post.test.mjs worker/codexworker/tests/feishu-stream.test.mjs worker/codexworker/tests/desktop-protocol-compat.test.mjs
# tests 119
# pass 119
# fail 0
# cancelled 0
# skipped 0

env -u CODEXWORKER_NATIVE_TRANSPORT -u CODEXWORKER_STATE_DIR -u CODEXWORKER_IDENTITY node --test --test-reporter=tap worker/dshworker/tests/question-cards.test.mjs worker/dshworker/tests/question-routing.test.mjs worker/dshworker/tests/native-public-questions.test.mjs
# tests 83
# pass 83
# fail 0
# cancelled 0
# skipped 0
```

新增表单测试覆盖自由输入、混合题型、选项不自动提交、草稿恢复、空白与非法值、
单题长度、重复回调、身份校验、原生问题 ID、WebSocket ACK 和部分跳过。
原生集成用例同时核对原卡片更新、同轮回填、客户端已答观察与归档恢复。
日志：`/tmp/oguri-question-form-regression.log`、`/tmp/oguri-dsh-question-form-regression.log`。
没有重跑全量历史失败套件，不能据此声称全部测试全绿。

## 飞书接口与运行装载

飞书 `POST /open-apis/cardkit/v1/cards` 接受生成的双题表单：

```json
{"http":200,"code":0,"data":{"card_id":"7707654014948051961"},"msg":"success"}
```

这个未发送的结构验证实体不能代替真人提交验收。
本机 CLI Worker 正常重载后 PID 36131，health 中 ready=true、feishu.connected=true、
cardLayoutVersion=5。主会话仍为 `01a1c413-6bd5-7e10-a388-665d44d2b6ec`。
没有重启 Codex 共享引擎、DSH、router 或 gateway。

实际异步提问已通过正式通道发到飞书：
questionId=`c9625afd-8b5c-42c5-860d-14eaa16ffcbe`，
messageId=`om_x100b604538fa9ca8b088b097b19f7f8`。
记录时尚未收到用户答案；实际选项填入、修改、提交和最终视觉效果仍待用户验收。

Computer Use 无法读取当前 Codex 界面：
`Computer Use is not allowed to use the app 'com.openai.codex' for safety reasons.`
飞书窗口读取返回：`Computer Use server error -10005: cgWindowNotFound`。
未换工具绕过；不声称与当前 Codex 客户端像素级一致。

DSH 只同步源码，没有重载生产服务；Windows 安装端也需要更新并实际验收。
