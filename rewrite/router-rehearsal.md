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

### 导出旧飞书私聊

```sh
python3 -m rewrite.cutover export-chat --snapshot ./snapshot --worker "$WORKER_ID" --destination ./private-chat-page1
```

将 `WORKER_ID` 设为旧台账中的工位 ID，即可从已核验的离线库导出旧 `/chat/{worker}/messages` 字段。可用 `--direction in` 或 `--direction out` 筛选，按原 ID 升序分页。下一页用回执中的 `next_cursor` 作为 `--after`，并指定新目录。回执只含计数、位置和摘要，正文写入该用户独占目录里的 `messages.jsonl`，文件权限为 600。敏感的附加元数据仍留在完整原备份。导出不会生成新平台消息或新工单。重做版的日常飞书私聊继续由 Worker 持有。

## 停止切换和回退

- 有持单、待确认的同伴消息、待答问题、审批、订阅或处理权纠正时，备份照常保留，准备新写者会列出待处理数。指定工作区、特殊时限、紧急优先级、未知元数据或已有投递事实的排队单需要显式对照。历史旧格式任务表有内容时同样需要单独迁移。早期格式 1 快照仍按原标准校验，准备阶段再核本版支持范围。
- 附件不完整、校验失败、有未结束日志或目标已存在时，命令报错。每个结果目录完整生成后才发布。
- 新库尚未变化时可恢复到全新目录。回退后用实际旧 RouterStore 查归档理由与下一张待派单，逐字节对比附件并核源库摘要。
- 新库已写入时，直接回退会丢掉新进展，命令会拒绝。保留新旧两边及新增事实，按下面已演练的场景准备回退。其他新写入逐笔对账。

`reconcile` 可用准备试运行时的同一源码和 SQLite 运行环境，在临时目录从旧快照重建基线，再按实际表对账。输出包含三份库摘要、每张表两侧的差异行数、候选工单号及原建单请求号。已派过的单、未指定处理人、旧库未登记的工位及旧端会修改或拒收的正文分别列出原因。输出只列标识和记录数。

输出中的 `work_orders` 逐单列原旧单号、新单号、试运行前后的状态和派发号，以及当前正式完工和候选材料各自的编号与附件引用。试运行收到正式成果时，应保留两份库，用这些标识逐笔核对；原样回退会使旧排队单再次派出，因此命令拒绝。这条路径已用实际旧库、隔离重做 Router 和带附件的 W04 演练 8 次。原回报重投只有一条成果通知，前后两份库摘要一致，未在旧端重发工单或结果。复跑 `python3 -m unittest -v rewrite.test_cutover.Cutover.test_migrated_formal_result_and_attachment_stop_exact_rollback_with_a_crosswalk`。[对账记录](evidence/cutover-reconcile-formal-report.json)。Agent 使用夹具。

### 试运行只增加未派工单时的回退演练

只有 `only_new_queued_tasks` 为真时，才可为每一张候选单审核旧端目标模式。将回读的 `trial_sha256` 与 `execution_modes` 写入审核文件。后者必须逐一列出候选工单 UUID，值为明确审核过的 `"goal"`。

```sh
python3 -m rewrite.cutover stage-queue-rollback --snapshot ./snapshot --trial ./trial --review ./review.json --destination ./queue-recovery
```

命令在新目录生成旧库、旧附件和 `queue-replay.json`。该文件含原指令，应和离线库一起保管。已有 UUID 保留。新 Hermes 管理工具的 `hermes-…` 请求号通过来源 UUID 确定性地生成旧端可接受的请求 UUID，文件同时保存两者。仅在隔离环境打开该旧库，让旧 Router 不接 Worker，再按文件顺序将每个 `request` 发给旧端 `POST /tasks`。记录新试运行工单号、原请求号、回放请求 UUID 与旧端返回的工单号。新请求返回 202 并保持排队。同号重送须返回同一旧工单号，改正文重送须返回 409。核对旧队首、完整正文和回放后的队列顺序。复跑整个演练使用 `python3 -m unittest -v rewrite.test_cutover.Cutover.test_reviewed_new_orders_reenter_the_old_http_queue_once_and_preserve_its_head`。已派发的单、成果及其他新写入仍需逐笔处理。

### 回退已验收的迁入工单

此入口覆盖从旧队列迁入、原文与处理人未改、经过一次 W03 和 W04、已由溪流验收结单的原工单。所有试用投递须有原接收方确认。可以同时带回试用中新建且从未派过的工单。Worker 仍忙、有未收清的状态查询、出现无关消息、转派、候选材料、待验收成果或损坏附件时，命令会停止并保留输入。

停止试运行写者后运行 `reconcile`，把 `trial_sha256` 和逐单核对过的 `closed_assignments` 写入审核文件。`closed_assignments` 的键是新工单号，值是正式 W04 对应的派发号。审核文件必须覆盖这批已结单的全部变更。

如果同时有 `queue_candidates`，把它们全部加入同一审核文件的 `execution_modes`，逐张确认值为 `"goal"`。`blocked_new_tasks` 必须为空。审核有遗漏时不生成任何回退目录。

```sh
python3 -m rewrite.cutover stage-reviewed-rollback --snapshot ./snapshot --trial ./trial --review ./review.json --destination ./reviewed-recovery
```

目标目录中的 `legacy.db` 用旧 Router 正常打开，旧附件在 `legacy.db.attachments/`。已验收的原单在恢复库标记为 `reviewed_on_rewrite`，留有指向私有档案的 `recovery_reference`。原单号、原建单幂等键以及尚未执行的旧队列顺序保留。旧 Router 再查原单能看到归档状态，重试原建单只回原单号。

`private-review/manifest.json` 保存正文、原 W04 和验收请求键、通知凭据、旧新编号及附件摘要，`private-review/files/` 保存逐块校验过的新成果附件。`private-review/trial-router.db` 是带校验摘要的完整试运行库副本。旧 Router 的通用凭据只能查看归档标记和原已授权附件。试用的新成果从私有回退包查阅，原通知无需重发。

带回新工单时，同一次操作还会生成私有的 `queue-replay.json`。首次打开旧 Router 前，核对文件 SHA-256 与私有清单的 `queue_replay_sha256`，核对预回放旧库摘要与 `restored_legacy_sha256`。保留这份初始目录并从副本启动隔离旧 Router，在整个回放窗口保持 Worker 断开。按文件原序提交给旧端 `POST /tasks` 并保存新旧编号对应。部分请求成功后中断时，用**同一已写入的旧库**重开，从第一条按原请求重送。已落库项应返回 200 和原单号，后续项返回 202 并留在队列。出现 409、任何实际派发或编号不一致时停止，保存当前库和回执对账。放开 Worker 前，检查已验收原单仍归档，旧队首及相对顺序保留，新工单各出现一次，正文逐字相同。[中断后从头回放](evidence/cutover-mixed-rollback.json)

复跑 `python3 -B -m unittest -v rewrite.test_cutover`。实际旧 Router 和重做 Router 都经隔离 HTTP 运行。Worker 模型使用夹具。待验收或通知未确认时未生成回退库，另验损坏附件及新入站消息使准备阶段停止。[旧端归档和附件读回](evidence/cutover-reviewed-rollback.json)

## 旧 Trigger 历史和订阅账本

`rewrite.trigger_archive` 接收明确准备好的旧版离线 SQLite 副本，可另带已有的 blocked 与 drops 日志。保存原事件、读收据、游标、订阅和规则表，逐文件核摘要。捕获时记录活跃订阅中有未确认可见事件的数量，切换前需和实际消费者对账。归档本身既没有订阅，也没有发送队列。

```sh
python3 -m rewrite.trigger_archive capture --database ./old-offline/trigger.db --destination ./trigger-snapshot
python3 -m rewrite.trigger_archive verify --snapshot ./trigger-snapshot --manifest-sha256 "$TRIGGER_SHA256"
python3 -m rewrite.trigger_archive event --snapshot ./trigger-snapshot --manifest-sha256 "$TRIGGER_SHA256" --event-id "$OLD_EVENT_ID"
python3 -m rewrite.trigger_archive restore --snapshot ./trigger-snapshot --manifest-sha256 "$TRIGGER_SHA256" --destination ./trigger-restored
```

有额外日志时，`capture` 可加 `--blocked` 或 `--drops` 指向相应副本。包含原文的查询留在隔离目录内执行。只把恢复文件交给经授权的旧服务窗口。复跑 `python3 -m unittest -v rewrite.test_trigger_archive`。实测使用真实旧 TriggerStore，5 条历史、原订阅和回执都读回。旧程序重开时只查到两条原本未读的可投递消息；日志档案与已丢弃消息均原样保留。活动日志或篡改的归档成员会阻止生成恢复目录。

Mac mini 的 [旧 Trigger 只读核对](evidence/legacy-trigger-status.json) 确认 0.7.0 仍在运行，首读有 1,492 条历史，订阅状态 active 1、expired 6、closed 9。复核时 active 订阅刚有活动，游标 1,496，当时未读为零。实际切换时仍需重新核对并取生产快照。未看到此工位有默认规则文件或显式规则文件参数；其他工位单独核对。

## 旧采集来源交接

在演练窗口准备好来源游标副本和专用配置。交接检查旧采集端已处理到的最后一条、待上行队列和新实例标识。游标导入只保存位置与摘要。仍在旧采集端等待发送的事件需要在接管前另外对账。

```sh
python3 -m rewrite.feeds mail --config ./mail-trial.json --import-legacy-cursor ./old-offline/mail-cursor.json --confirm-legacy-mailbox
python3 -m rewrite.feeds game --config ./game-trial.json --import-legacy-cursor ./old-offline/game-cursor.json
```

旧邮件游标只记文件夹、UIDVALIDITY 和末尾 UID。使用 `--confirm-legacy-mailbox` 前，核对旧配置的服务器、端口、账号和文件夹确实相同。首次采集读回原 UIDVALIDITY，且服务端最新可读位置不小于旧 UID 时，才验证成功。游戏游标必须和配置的桥地址相同，首次观测到序号比旧游标小时会中止。旧桥缺少启动标识，若切换前桥已重启并重新超过旧游标，仅凭序号无法判定同一实例。核对连续运行状态后再用该游标。

相同旧游标重导入只返回原摘要与验证状态。目标库已开始工作或保存过不同游标时拒绝覆盖。隔离复跑 `python3 -m unittest -v rewrite.test_feeds`。邮件用协议夹具，游戏走临时 HTTP 流。生产来源与订阅继续待验。

## 重做版五组件的离线恢复

有了验证过的五组件快照后，可先逐项核对持单。

```sh
python3 -m rewrite.state_bundle audit-handoff --snapshot "$SNAPSHOT" --manifest-sha256 "$MANIFEST_SHA256"
```

`worker:<ID>` 和 `agent:<ID>` 的 ID 使用 Router 上的准确拼写。命令只读取离线快照，比较 Router 持权、Worker 持单及会话、原生适配器收件凭据、待核的 W04 和原派发清理记录。输出 `needs_review: true` 时用隔离副本逐项对账。检查结果只覆盖上述账本，不能据此判断生产可切换。

`native_session_pending` 另列换绑前需要核对的离线账本。包括持单、Router 已保留的派发、未处理收件、私聊与队列、同伴收据、问题、压缩、清理以及没有原生关闭回执的命令审批。缺少的旧库表显示未知。只有这些记录均为零且前述持单检查无误，`native_session_ledger_clear` 才为真。原会话重开可继续未确认的工作，换到别的会话前要把这些记录留在原会话对账。实际忙闲、原生目标和权限须在独立宿主核验。

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

### Codex 自动登记工位的恢复与回退

先停止隔离 Router、Worker、适配器和该工位的原生 App Server，保留原生 profile 和已有连接配置。将工位 ID 与原登记目录的绝对路径写入 `registries.json`。将工位 ID 与原私有回调密钥文件的绝对路径写入本机 `managed-keys.json`。两份清单只填路径。

```sh
python3 -B -m rewrite.state_bundle capture --sources sources.json --managed-registries registries.json \
  --revision "$SOURCE_SHA" --destination ./snapshot
python3 -B -m rewrite.state_bundle verify --snapshot ./snapshot --manifest-sha256 "$MANIFEST_SHA256"
python3 -B -m rewrite.state_bundle audit-handoff --snapshot ./snapshot --manifest-sha256 "$MANIFEST_SHA256"
python3 -B -m rewrite.state_bundle restore --snapshot ./snapshot --manifest-sha256 "$MANIFEST_SHA256" \
  --managed-keys managed-keys.json --destination ./trial
```

恢复后的适配器使用恢复出来的根账本，`--managed-sessions-dir` 指向试用目录下的 `native--<工位ID>`，`--session` 和原生 profile 继续用原配置。Worker 使用恢复后的账本和原 `--session-callback-key` 文件。快照带子会话 SQLite 和回调摘要，恢复时从原密钥重建私有登记文件。原生对话历史仍由宿主保存。开启了自动登记的快照每个工位对应一个登记目录。仍绑定其他外部适配器或有交接指向外部适配器时，先结束或核清交接。

重开后先核同一个宿主会话、原派发和原生收件。待核登记应沿用原操作号，已持单应沿用原派发号。切换会话要通过 Worker 的 `/resume` 交接。返用前一版本时，停止试用写者，提供恢复输出的数据库清单作为 `trial.json`，以及试用登记目录清单 `trial-registries.json`。

```sh
python3 -B -m rewrite.state_bundle rollback --snapshot ./snapshot --manifest-sha256 "$MANIFEST_SHA256" \
  --trial-sources trial.json --managed-registries trial-registries.json --managed-keys managed-keys.json \
  --destination ./previous-state
```

回退核对试用库和所有子会话账本。若试用已写入成果、投递或其他状态，先逐笔核对并保留试用数据；旧快照回退会拒绝覆盖这段进展。独立 App Server 已实测从快照恢复丢失登记回执、让前一版适配器接回原持单并交件、拒绝丢弃试用成果。原生 profile 在同一台机器独立保存，模型为脚本驱动。[恢复与回退证据](evidence/codex-managed-bundle-rollback.json)

DSH 自动登记工位使用相同命令。停止隔离宿主插件后再采集。启动恢复目录前，同时把插件的 `managed_sessions_dir` 与适配器的 `--managed-sessions-dir` 指向同一个恢复路径。原生历史目录另行保留。隔离演练已核新开、分支与清空的三个 SQLite 子库在恢复和回退后逐字节一致。[DSH 换绑与快照证据](evidence/dsh-managed-native-sessions.json)

### Worker 和适配器逐个升级

```sh
python3 -m rewrite.release_check --repository . --component worker-pair \
  --previous 1bdfaca0f6145a45e2dbae84c4e8ee4fe7cb2760 \
  --new db43808f3eaf518edb240298e35e8a18b5a94805
```

两版各按提交制作和校验发布包。在 macOS 临时目录先让旧 Worker 和旧适配器收到一条私聊，然后停止两端并保存 Worker、适配器、宿主夹具三份库。新适配器先用检验副本启动，旧 Worker 重送原消息。再用新版 Worker 核原回执已写入新表。最后将原备份复制到回退目录，用旧版两端重跑。四次回执完全相同，三个阶段的原生账本均只有一条输入，原始快照摘要未变。

Router、Worker、适配器的独立进程和回环 TLS 实际运行，平台输入与宿主是隔离夹具。本例只用已确认私聊验证兼容顺序与零新增业务的回退。实际试用期收到新的生产消息、打开新的原生回合或提交工单成果时，需要先对账。

### 保留实际 Codex 测试工位的升级与回退

```sh
python3 -m rewrite.codex_release_check --repository . --codex-binary "$CODEX_BINARY" \
  --previous f7104c3250986467df0c871eb7461505338fcc8f \
  --new 2a8f081841310259a48ea9f077297d1440857c56
```

命令从两个指定提交分别构建和校验发布包，使用同一个临时 Codex profile、独立会话、测试模型与回环 TLS。旧包先完成私聊和一次正式派发，Agent 经原生工具提交 W04，Router 持成果待验，Worker 释放派发。试运行副本模拟 Worker 丢失私聊回执。先用新适配器配旧 Worker，再用新版两端，最后回退到旧包及原状态副本。每次按原号重投并用独立 App Server 读回同一会话。

连续三轮的初始、混合、新版和回退都保留初始化、私聊、派发三条输入。首次 W04 产生两个原生工具调用和一条 Router 通知，后续没有新增原生回合、模型请求或成果通知。新 Worker 可以补记同一原收件，旧快照摘要保留，两个包重新校验通过。[升级与回退记录](evidence/codex-native-release-cycle.json)。实际使用 App Server 与完整发布包，模型为本地脚本，试运行有新增业务时仍走前述对账。

### 隔离工位冷换原生会话

确认原宿主无待处理原生回合，目标会话属于已授权的独立工位。按上一节在隔离环境停写并关闭进程、保存原生历史，采集完整快照。工位有飞书接入时，把同名的 `feishu:<Worker ID>` 库放入快照。原 Worker 必须已用 `--adapter-session` 收到原宿主的有效忙闲回包，原适配器库须登记同一会话。

```sh
python3 -B -m rewrite.state_bundle stage-native-handoff \
  --snapshot "$SNAPSHOT" --manifest-sha256 "$MANIFEST_SHA256" \
  --worker-id "$WORKER_ID" --source-session "$ORIGINAL_SESSION" \
  --target-session "$TARGET_SESSION" --destination "$TRIAL_DIRECTORY"
```

有持单、预约派发、待收业务帧、待答题、待确认的原生收件或审批时，工具返回对应待办类型，交接目录不会生成。活动宿主为空闲须从宿主侧核实。快照、原工位历史与关联附件按原备份流程保留。

通过后从新目录启动隔离 Router 和 Worker。Worker 使用 `worker_db` 和 `expected_session`，目标适配器使用 `target_adapter_db` 和同一个会话。先回读 `worker-status`，确认工位和目标会话后再放开测试输入。`previous_adapter_db` 是原会话账本，可供切回使用。首次绑定的新目标有独立账本，复制到分叉会话的旧输入仍算父会话历史。

已有试用消息或成果时，先停止试用端并用**试用后的当前库**重新采集快照，再以上次目标为来源、原会话为目标重复换绑。源组件清单里的 `agent:<Worker ID>` 使用试用适配器库；切回启动适配器使用之前保留的原会话账本。这样保留试用期间的新工单、成果和两端收据。旧版恢复到试验前数据仍受快照摘要检查。

复跑 `python3 -B -m unittest -v rewrite.test_state_bundle rewrite.test_codex_app_server.CodexAppServer.test_cold_switch_and_return_preserve_receipts_and_use_the_target_native_session`。原生用例运行独立 Codex App Server，实际工作单经 W04 待验，模型与平台采用夹具。已经运行的独立适配器另有[飞书切换会话](README.md#飞书切换会话)入口，日用会话待验。

跨引擎复跑用 `python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_one_worker_cold_switches_between_native_engines_and_keeps_both_reports`。在同一台 Mac 上运行实际 Codex App Server 和 DSH SDK，使用同一个 Worker 身份、两个适配器账本和一套连续的 Worker 与平台记录。两个会话各接一条相同的测试图文和一张正式单。切回后仍保留两张原卡和两份待验成果。[跨引擎记录](evidence/cross-native-handoff.json)
