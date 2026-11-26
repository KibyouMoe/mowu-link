# 旧 Router 离线迁移与回退演练

适用范围为旧 Router 的 SQLite 工单台账和内容寻址附件目录。演练输入是专用测试副本。源端已关闭写者，SQLite 已完成 checkpoint，附件上传已结束。所有操作生成在新目录中。原实例的绑定、启动参数、平台凭据以及外部 Trigger/Worker 数据需要另行交接。

## 复跑

仓库根目录运行 `python3 -m unittest -v rewrite.test_cutover`。用实际旧 RouterStore 创建一张有历史成果的归档单、一张从未派出的排队单和一份附件，关闭写者后启动演练。待派单按旧队列顺序进入新库，同一来源 UUID 与旧单号稳定映射到同一新工单号，初始持权归 Router。旧附件及归档事实保存在经过核验的旧格式副本中。新端对历史数据的界面另验。

以下命令的路径均指向演练副本。选择一个固定的来源 UUID，并与此次版本、源库摘要和队列映射一起保存。

```sh
python3 -m rewrite.cutover backup --database ./old/router.db --attachments ./old/attachments --destination ./snapshot
python3 -m rewrite.cutover verify --snapshot ./snapshot
python3 -m rewrite.cutover prepare --snapshot ./snapshot --destination ./trial --source-id "$SOURCE_UUID"
python3 -m rewrite.cutover rollback --snapshot ./snapshot --trial ./trial --destination ./restored
```

`manifest.json` 保存数据库摘要、附件大小及内容摘要、尚待交接事项的计数。`baseline.json` 保存新旧库摘要和待派单号对照表。核验使用离线目录中的完整文件。为校验备份来源，应另外保管输出的摘要及对应版本。备份仅提供拥有该目录的用户访问。

## 停止切换和回退

- 有持单、待确认的同伴消息、待答问题、审批、订阅或处理权纠正时，备份照常保留，准备新写者会列出待处理数。带额外上下文或已有投递事实的排队单需要显式对照。历史旧格式任务表有内容时同样需要单独迁移。
- 附件不完整、校验失败、有未结束日志或目标已存在时，命令报错。每个结果目录完整生成后才发布。
- 新库尚未变化时可恢复到全新目录。回退后用实际旧 RouterStore 查归档理由与下一张待派单，逐字节对比附件并核源库摘要。
- 新库已写入时，直接回退会丢掉新进展，命令会拒绝。保留新旧两边及新增事实，完成逐笔核对后再制定回放顺序。这种有新业务写入的回迁仍待演练。

## 重做版五组件的离线恢复

`python3 -m unittest -v rewrite.test_state_bundle` 在临时端口跑一张已持有的单、一个还没送到宿主的通知、一个待交原附件和一个等待投递的飞书私聊。关闭这套服务，将七个数据库作为一批备份，再从新目录重开。Worker 保留原会话和派发，宿主按原投递返回原凭据；平台沿用原卡游标，私聊只进入一次；Trigger 保留实例号，恢复后上传的 332800 字节与原件全部相同。两端原生记录和飞书云端由独立夹具保留，HTTP、SQLite 和恢复程序实跑。

1. 在隔离工位停写并关闭服务，确认宿主保留了原生会话和输入记录。为这次演练列出要备份的每个数据库。跨机时先在各自的停写窗口取完整离线副本，所有工位停写区间须重叠。要从本机集中快照，源数据库使用 SQLite 的 DELETE 日志模式，待处理的 WAL 和事务需先由原程序正常关闭并检查。程序遇到占锁或活动日志会退出。
2. 准备 `sources.json`，用 `角色:实例名` 指向每个数据库的绝对路径，例如 `"worker:oguri":"/staging/oguri/worker.db"`。角色为 router、worker、agent、hermes、trigger；使用独立宿主投递账本或飞书时，分别加入 hermes-host、feishu。每个 Worker 与对应 agent 的实例名相同；一个以上来源或工位各占一行。核对清单与各机器实际配置。凭据、连接配置和宿主登录态由各自所有者保管。缓存可由已备份的原附件重建。
3. 执行下列命令，单独保管采集输出的源码版本、快照号和清单摘要。恢复目标必须是新目录。用返回的角色与文件映射重新连接隔离组件并验证原身份、持单、原生会话和未回执队列。

```sh
python3 -m rewrite.state_bundle capture --sources sources.json --revision "$SOURCE_SHA" --destination ./snapshot
python3 -m rewrite.state_bundle verify --snapshot ./snapshot --manifest-sha256 "$MANIFEST_SHA256"
python3 -m rewrite.state_bundle restore --snapshot ./snapshot --manifest-sha256 "$MANIFEST_SHA256" --destination ./recovered
```

快照只覆盖清单中的重做版状态，原生宿主和飞书已发生的外部动作不会随数据库回退。原生会话不可核、平台已过幂等时限或快照后存在新的业务写入时，应先隔离恢复目录，对照两端记录后再启用写者。旧系统的待派单转换仍用上节的旧 Router 演练流程。

需要退回原版本时，保留新系统试运行的全部库，将其映射写成 `trial.json`；可直接使用 restore 输出的映射。使用下列命令，将试运行库全部锁定并与原快照核对。任一库有写入、角色清单变化或写者仍占锁，都会拒绝生成回退目录。

```sh
python3 -m rewrite.state_bundle rollback --snapshot ./snapshot --manifest-sha256 "$MANIFEST_SHA256" --trial-sources ./trial.json --destination ./previous-state
```

版本级复跑为 `python3 -m rewrite.release_check --repository . --previous b31cee5da7c8dc74a76accdd78d8a6c5cb49484c --new 5de36ad66df26dc34fe3832ed14e77b1d9f19551`。两个已提交版本在各自新目录启动真实 Router，以专用临时 TLS 和身份验同一张待派单。旧包→新包→旧包后只查到原单一张，新试用库字节未变。该演练在 macOS 跑过；Windows 与跨机宿主尚待验收。
