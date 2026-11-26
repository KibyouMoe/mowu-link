# 旧 Router 离线迁移与回退演练

适用范围为旧 Router 的 SQLite 工单台账和内容寻址附件目录。演练输入是专用测试副本。源端已关闭写者，SQLite 已完成 checkpoint，附件上传已结束。所有操作生成在新目录中。原实例的绑定、启动参数、平台凭据以及外部 Trigger/Worker 数据需要另行交接。

## 复跑

仓库根目录运行 `python3 -m unittest -v rewrite.test_cutover`。用实际旧 RouterStore 创建一张有历史成果的归档单、一张从未派出的排队单和一份附件，关闭写者后启动演练。待派单按旧队列顺序进入新库，同一来源 UUID 与旧单号稳定映射到同一新工单号，初始持权归 Router。完整旧记录及附件保存在校验过的离线副本中。

溪流在新端调用 `router_tasks(legacy_source_id=来源UUID, legacy_task_id=旧单号)`，可取原成果、归档理由、归档时间及来源库摘要。只给来源 UUID 时可用 `limit` 与上一页的 `next_cursor` 分页。历史查询为只读，归档单没有新派发号；旧排队单可查对应的新工单号。隔离演练用同一旧号查到 `Prior result` 和 `summary_received`，Worker 身份查历史返回 403，查询前后新库摘要相同。

旧同伴私聊的参与人可在[历史入口](README.md#同伴私聊与附件)查原消息、回复关联、工单号和旧时间，并取回校验过的附件。溪流只能查自己参与的对话。隔离演练核了长中文正文逐段还原、原图字节、第三方拒绝访问、历史附件引用不能转作正式成果、原号重送不进入队列。迁移发现未确认的消息或正文中间缺段时停止。此前准备的试运行库若需带入这些旧记录，仍在无新写入时从原快照重建一份。已有新写入的库先留存对账。

另一次演练从旧 HTTP 接口实际建单。旧端给普通单自动写入目标模式、通常优先级和若干空字段。新端接收从未派发且采用这些默认字段的待派单，保留标题、处理人、要求及文本上下文，把旧 Worker 的目标约束随要求交给宿主。原生目标是否建立仍须查宿主。旧建单 UUID 在新端继续保护这笔写入。客户端以相同 UUID 提交 A02 的原始标题、处理人及要求可取同一工单号；有正文上下文时，要求包括空行和 `[router context]` 段。修改内容返回 409。已归档旧单的 UUID 只返回 409 及历史查询标识。以前已准备的新库若无此映射，须从旧快照重新演练，已有新写入需另行对账。

以下命令的路径均指向演练副本。选择一个固定的来源 UUID，并与此次版本、源库摘要和队列映射一起保存。

```sh
python3 -m rewrite.cutover backup --database ./old/router.db --attachments ./old/attachments --destination ./snapshot
python3 -m rewrite.cutover verify --snapshot ./snapshot
python3 -m rewrite.cutover prepare --snapshot ./snapshot --destination ./trial --source-id "$SOURCE_UUID"
python3 -m rewrite.cutover reconcile --snapshot ./snapshot --trial ./trial
python3 -m rewrite.cutover rollback --snapshot ./snapshot --trial ./trial --destination ./restored
```

`manifest.json` 保存数据库摘要、附件大小及内容摘要、尚待交接事项的计数。`baseline.json` 保存新旧库摘要和待派单号对照表。核验使用离线目录中的完整文件。为校验备份来源，应另外保管输出的摘要及对应版本。备份仅提供拥有该目录的用户访问。

## 停止切换和回退

- 有持单、待确认的同伴消息、待答问题、审批、订阅或处理权纠正时，备份照常保留，准备新写者会列出待处理数。指定工作区、特殊时限、紧急优先级、未知元数据或已有投递事实的排队单需要显式对照。历史旧格式任务表有内容时同样需要单独迁移。早期格式 1 快照仍按原标准校验，准备阶段再核本版支持范围。
- 附件不完整、校验失败、有未结束日志或目标已存在时，命令报错。每个结果目录完整生成后才发布。
- 新库尚未变化时可恢复到全新目录。回退后用实际旧 RouterStore 查归档理由与下一张待派单，逐字节对比附件并核源库摘要。
- 新库已写入时，直接回退会丢掉新进展，命令会拒绝。保留新旧两边及新增事实，完成逐笔核对后再制定回放顺序。这种有新业务写入的回迁仍待演练。

`reconcile` 可用准备试运行时的同一源码和 SQLite 运行环境，在临时目录从旧快照重建基线，再按实际表对账。输出包含三份库摘要、每张表两侧的差异行数、候选工单号及原建单请求号。已派过的单、未指定处理人、旧库未登记的工位及旧端会修改或拒收的正文分别列出原因。输出只列标识和记录数。

### 试运行只增加未派工单时的回退演练

只有 `only_new_queued_tasks` 为真时，才可为每一张候选单审核旧端目标模式。将回读的 `trial_sha256` 与 `execution_modes` 写入审核文件。后者必须逐一列出候选工单 UUID，值为明确审核过的 `"goal"`。

```sh
python3 -m rewrite.cutover stage-queue-rollback --snapshot ./snapshot --trial ./trial --review ./review.json --destination ./queue-recovery
```

命令在新目录生成旧库、旧附件和 `queue-replay.json`。该文件含原指令，应和离线库一起保管。已有 UUID 保留。新 Hermes 管理工具的 `hermes-…` 请求号通过来源 UUID 确定性地生成旧端可接受的请求 UUID，文件同时保存两者。仅在隔离环境打开该旧库，让旧 Router 不接 Worker，再按文件顺序将每个 `request` 发给旧端 `POST /tasks`。记录新试运行工单号、原请求号、回放请求 UUID 与旧端返回的工单号。新请求返回 202 并保持排队。同号重送须返回同一旧工单号，改正文重送须返回 409。核对旧队首、完整正文和回放后的队列顺序。复跑整个演练使用 `python3 -m unittest -v rewrite.test_cutover.Cutover.test_reviewed_new_orders_reenter_the_old_http_queue_once_and_preserve_its_head`。已派发的单、成果及其他新写入仍需逐笔处理。

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
