# Worker 0.2.40：客户端作答回到原飞书卡片

训练员在Codex客户端点选第二次测试的选项，飞书原提问却显示“已过期”，
并额外出现一张包含questionItemId的原始JSON输入卡。
此前将该测试称作飞书作答是不准确的，实际方向是客户端 → Worker → 飞书。

## 原因与实现

- 客户端答案是下一回合的原生UserMessage，含完整问题ID、原题和实际答案；
  原异步sourceState仅判断当前turn是否仍为原turn，未先识别下一回合中的答案。
- 原生输入观察器未关联问题ID，误将该答案作为普通输入转发。
- interface/public-questions.mjs严格解析原生公开答复封装（允许首尾换行、数组或单对象），
  根据原问题ID及题目匹配答案；不从普通文字、裸JSON或工具结果猜测回答。
- QuestionRelay.observeNativeReply保存真实原生观察，按绑定会话和选择epoch核对，
  支持多题分次回答；只纠正source_turn_ended的误过期，不复活取消等关闭状态。
- DesktopManager在原生输入留档前关联提问，正常记录人类可读原题与答案，
  投递计划指向原提问卡片，避免单独发JSON输入卡。
- 恢复既有公开留档时可修正原卡片，不向模型重新发送历史答案。
- 卡片与状态接口明确标注客户端回答；router澄清可引用真实原生输入观察，
  不伪造manager派发回执。
- 共享业务三文件两侧逐字节一致，生产只重载Codex Worker。

## 验证与上线

生产Node v26.12.1：
node --test worker/codexworker/tests/{native-public-questions,question-routing,question-cards,router-tasks,router-client,primary-session-cli,message-transcript}.test.mjs
111 tests / 111 pass / 0 fail / 0 skipped。
7项语法检查通过，共享业务一致性3/3，git diff --check通过。

已获工具审批，优雅重载ai.hermes.codexworker至0.2.40；
原主会话01a1c413-6bd5-7e10-a388-665d44d2b6ec未变，飞书与router连接恢复。
没有重启共享Codex app-server、DSH、router或gateway，没有新建主会话。

真实案例0feba9db-758e-47d2-8d41-4f98538b8e19：
上线前status=expired、reason=source_turn_ended；
上线后status=answered、answeredBy=native、answers=[正常，可以点选]、
cardUpdated=true（原卡片更新收到API成功回执）。
关联原生消息仍为72e27fb1-4a65-471a-89ea-1f562675cb8f，未重发输入或创建测试答案。

实时接收已在生产确认：溪流t-a2e16ef851于修复进行中进入模型上下文；
训练员随后说明这就是打断测试。该任务的电视ADB自测失败，不代表消息接收失败。

旧的已发JSON输入卡未撤回；修复阻止后续同类重复卡片。
没有Git提交或推送，原工作树其他改动保留。
完整测试及部署回执：/Users/kibyou/Workspace/OguriCap/artifacts/worker-0.2.40。
