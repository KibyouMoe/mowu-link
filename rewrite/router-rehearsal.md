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

实际旧 Router 的 `/chat/log` 已核重复消息复用原行号，同一平台消息号的双向记录均保留。以旧共享令牌读两工位的历史时均返回 200。离线导出所得页与所选工位的 HTTP 字段逐条一致，文件不含另一工位记录。旧在线摘要保留在旧端。重做版未开放跨工位聊天旁听，现役消费者及所需的查阅范围须在停用旧入口前确认。复跑 `python3 -B -m unittest -v rewrite.test_cutover -k export_legacy_chat`。

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

命令在新目录生成旧库、旧附件和 `queue-replay.json`。旧库保存待回放的请求号与指纹。首次启动旧 Router 前，核对数据库摘要等于 `restored_legacy_sha256`。回放文件含原指令，应与离线库一起保管。已有 UUID 保留。新 Hermes 管理工具的 `hermes-…` 请求号通过来源 UUID 确定性地生成旧端可接受的请求 UUID，文件同时保存两者。仅在隔离环境打开该旧库，让旧 Router 不接 Worker，再按文件顺序将每个 `request` 发给旧端 `POST /tasks`。记录新试运行工单号、原请求号、回放请求 UUID 与旧端返回的工单号。新请求返回 202 并保持排队。同号重送须返回同一旧工单号，改正文重送须返回 409。核对旧队首、完整正文和回放后的队列顺序。以后再次迁入重做版时，旧库须有每条回放的原任务记录和匹配指纹。原 Hermes 请求号与旧 UUID 重试都会返回同一张迁入待派单，正文有变返回 409。未完成旧端回放时保留旧库、试用库及审核过的回放文件，完成核对后才能再迁入。复跑整个演练使用 `python3 -m unittest -v rewrite.test_cutover.Cutover.test_reviewed_new_orders_reenter_the_old_http_queue_once_and_preserve_its_head`。已派发的单、成果及其他新写入仍需逐笔处理。

试用单先建后指定时，`queue-replay.json` 保留原 A02 请求、A06 指定和派发前修订回执。只有能按原修订重建设定且该工位在旧库有登记，才生成目标模式的回放请求。另用固定的旧端 UUID，承载 A06 指定的工位和待派时的正文。完成回放再迁入时，原 A02 请求按原正文重试、旧端回放请求按回放正文重试，都会返回同一张迁入单。已处理的 A05/A06 请求号不能开启业务写入。一直没有处理人、首次指定无法核对或旧回放回执缺失时，保留两边账本待核。[先建后指定演练](evidence/cutover-assigned-trial-order.json)

首次派发前已有工位的单，也可凭原 A05 修订或 A06 转派回执生成回放计划。待派时以最后一次有效路由决定和最终指令为准，保持旧队首。已验收时按首次实际接管的工位恢复旧端归档。更早的预定工位及变更原因留在私有档案。曾生成派发号的单仍须按完整派发链核验。只有已证实从未派发的单才能进入待派回放。[首次派发前修改与转派演练](evidence/cutover-preoffer-rollback.json)

### 回退已验收、已取消和已处置的池中工单

同一派发收到第二次显式取消时，Router 在 C01 指令和 W01 的 `pending_cleanup` 中给出 `cleanup_id`。自动重试沿用原清理号。Worker 从指定宿主的收件记录取该号，用 `cancel_confirmed-<cleanup_id>` 作为 W04 原请求键。新的控制号和回执不得合并。首次无歧义的旧取消保持旧报文。旧版本若已留下多次待核的无号取消，升级后由管理端发一条新取消，等带号 W04 到账。已经成功的旧回执仍能按原键查回。 Router 只有在写入带号指令时才扩展清理表。只启动并读回原单时，旧状态库逐字节不变。离线回退对账只在临时参照库补同一清理列，仍核对试用库的完整结构和写入。`audit-handoff` 可查 `scoped_cleanup_reports`。切换前，该项和 Router 的待清理项须清零。换回旧运行版要按快照与回退手册处理，不能让旧版接管带号的未确认清理。[前版升级演练](evidence/reissued-cancel-upgrade.json)


此入口覆盖旧队列迁入及试用期间新建的工单。原处理人按迁入记录、试用建单或首个有效 A06 确定。正文须能从原请求和后续回执重建。先建后指定的单还要核首次派发是否对应 A06 与其后的修订。已验收时用原指定工位和原建单正文绑定旧端归档请求，A02 原请求及 A06 证据保留在私有档案。已验收单须有最后一位处理人的正式 W04 和溪流的验收回执。已取消单须有正式取消操作和对应派发的释放证据。转派前在途或已入无法完成池均可纳入。原派发需清理时，必须读到对应的清理 W04。候选材料、受阻报告和最终成果按各自的原派发及附件保留。所有试用投递须有原接收方确认。可以同时带回试用中新建且从未派过的工单。工位仍有持单或状态查询，以及回执、通知或附件无法对证时，命令会停止并保留输入。

停止试运行写者后运行 `reconcile`，把 `trial_sha256` 和逐单核对过的 `closed_assignments` 写入审核文件。`closed_assignments` 的键是新工单号，值是正式 W04 对应的派发号。审核文件必须覆盖这批已结单的全部变更，包括 `closed_new_tasks` 列出的试用新单。新单须有唯一的原建单回执，旧端支持原描述和请求号。若已转派，审核时填最终正式成果的派发号。工具逐段核对原处理人与后续接手人，旧端归档仍以原处理人和原请求定位。

有已取消单时，在同一审核文件加入 `cancelled_assignments`。键是新工单号，值取 `work_orders` 中的 `cancelled_assignment_id`。从未派发的填写 `null`。若这批只有取消单，`closed_assignments` 填空对象。试用新建的取消单还会出现在 `cancelled_new_tasks`。已发出但未被 W03 接管的派发，须核对原派发传输收件和未接管事实。已接管的在途单或 Router 入池单，须核对原派发的取消 W04。Worker 已以受阻 W04 自行释放的单，核对原释放和之后的取消操作。

Worker 已交受阻 W04 并释放派发的单，可在明确池中结单或失败归档后带回。审核文件的 `pool_archives` 逐条列出新工单号，值包含 `assignment_id` 和最终 `state`，分别从对账结果的 `pool_archive_assignment_id` 和 `trial.state` 查回。状态为 `closed` 或 `failed_archived`。试用新单另列于 `archived_pool_new_tasks`。原管理动作、明确选中的材料及说明保存在私有档案。旧端读取 `pool_closed_on_rewrite` 或 `failed_on_rewrite`，成果字段为空。核实 W04 接收、原处理权已释、候选附件私有且不重派后才切回。

如果同时有 `queue_candidates`，把它们全部加入同一审核文件的 `execution_modes`，逐张确认值为 `"goal"`。`blocked_new_tasks` 必须为空。审核有遗漏时不生成任何回退目录。

```sh
python3 -m rewrite.cutover stage-reviewed-rollback --snapshot ./snapshot --trial ./trial --review ./review.json --destination ./reviewed-recovery
```

目标目录中的 `legacy.db` 用旧 Router 正常打开，旧附件在 `legacy.db.attachments/`。已验收的原单在恢复库标记为 `reviewed_on_rewrite`，留有指向私有档案的 `recovery_reference`。原单号、原建单幂等键以及尚未执行的旧队列顺序保留。旧 Router 再查原单能看到归档状态，重试原建单只回原单号。

试用新建且已验收或已取消的工单直接写入旧库归档。旧端可查到新旧编号映射及归档标记。标题、原指令、成果及附件从私有档案查询。已取消单标记为 `cancelled_on_rewrite`，旧 HTTP 的成果字段为空。只有存在对应取消 W04 时，`confirmed_by_worker` 才为真。`cancel_key`、取消原因和适用的 `cancel_w04_key` 从私有档案查阅。该单的旧兼容请求保存在 `private-review/manifest.json` 对应工单的 `legacy_creation_request` 字段，与其旧编号一起保存。重放该请求会回同一旧单号，改动正文再用同一个请求号会得到 409。无需发送这个请求来创建或归档工单。映射若与已有旧单或待回放工单的请求号冲突，准备会停止。回退库中的归档请求映射随数据库备份保存，旧 HTTP 仍只显示归档。以后用该旧库再次试迁时，重做 Router 会核对旧单号和归档引用，同一条验收或取消归档对应的原 Hermes 请求号和映射后的 UUID 都返回 409 与旧归档标识。映射不一致时停止准备。[试用新单验证](evidence/cutover-trial-born-archives.json)

`private-review/manifest.json` 保存正文、原 W04 和验收请求键、通知凭据、旧新编号及附件摘要，`private-review/files/` 保存逐块校验过的新成果附件。`private-review/trial-router.db` 是带校验摘要的完整试运行库副本。旧 Router 的通用凭据只能查看归档标记和原已授权附件。试用的新成果从私有回退包查阅，原通知无需重发。发生转派时，`assignment_history` 按派发保存接管、候选材料、受阻附件、转派与清理回执。正式成果仍由工单条目的 `artifact_ids` 与 `w04_key` 定位。旧派发的迟到清理回执须与当时的新派发状态相符。当前覆盖离线入池、进度询问无答复、Worker 主动受阻后的转派，以及工作中的直接转派。进度询问无答复时，需核对首次询问、三次重试和原搁置通知的收件。取消重试耗尽入池后，第一次取消与后续管理动作分别对账。首次取消、三次重试和搁置需有原收件及空闲观测。再次取消或转派需有该次指令的收件，以及原工位随后提交的清理 W04。转派单仍按最后一次正式报告和验收归档。[取消重试回退实测](evidence/cutover-reviewed-cancel-retry.json) 首次取消期间因离线超时入池时，原取消及已发重试的收件须早于首个池中管理动作。离线计时没有搁置帧。新取消或转派仍需独立传输收件和后续清理凭据。[离线回退实测](evidence/cutover-reviewed-offline-cancel.json) 工作中多次发起取消时，按管理操作顺序核对清理号、C01 和空闲重试。已替换的取消各有原收件，但不会作为最终确认。最后一条取消或转派的独立 W04 按原派发保留。[在途取消实测](evidence/cutover-working-cancel-rounds.json) 同一次入池中多次取消时，各次 A06 都要对应当时的工单版本、提醒期限及独立 C01，编辑或挂起可发生在最终 W04 之前。最终取消或转派凭据与早期取消分开核验。[同池重复取消](evidence/cutover-pooled-cancel-rounds.json) 取消仍待原工位确认、无法确认清理凭据来自哪次取消、无处理人的试用新单、Router 主动入池后结单或失败归档、缺原处理人释放凭据的续办、续办后仍在途、未经上述规则核对的待派修订单和其他未对账试用写入均需人工对账。已复现离线入池后仅凭候选材料结单，此时 Router 没有正式 W04，原工位仍持单且宿主忙。该场景拒绝发布回退目录。

带回新工单时，同一次操作还会生成私有的 `queue-replay.json`。首次打开旧 Router 前，核对文件 SHA-256 与私有清单的 `queue_replay_sha256`，核对预回放旧库摘要与 `restored_legacy_sha256`。保留这份初始目录并从副本启动隔离旧 Router，在整个回放窗口保持 Worker 断开。按文件原序提交给旧端 `POST /tasks` 并保存新旧编号对应。部分请求成功后中断时，用**同一已写入的旧库**重开，从第一条按原请求重送。已落库项应返回 200 和原单号，后续项返回 202 并留在队列。出现 409、任何实际派发或编号不一致时停止，保存当前库和回执对账。放开 Worker 前，检查已验收、已取消及已处置的池中原单仍归档，旧队首及相对顺序保留，新工单各出现一次，正文逐字相同。[中断后从头回放](evidence/cutover-mixed-rollback.json)

复跑 `python3 -B -m unittest -v rewrite.test_cutover`。实际旧 Router 和重做 Router 都经隔离 HTTP 运行。Worker 模型使用夹具。待验收或通知未确认时未生成回退库，另验损坏附件及新入站消息使准备阶段停止。[旧端归档和附件读回](evidence/cutover-reviewed-rollback.json)

取消复跑使用 `python3 -B -m unittest -v rewrite.test_cutover.Cutover.test_cancelled_orders_roll_back_without_replaying_work_or_inventing_results`。旧与重做 Router 均实际提供隔离 HTTP。Worker 使用宿主夹具和可控时钟。测试包含迁入单未派取消、已派未接取消、在途重试后确认、主动受阻后取消、掉线入池后确认和转派后新工位未接即取消。旧端按原号读回，五张试用新单的原建单请求重送均去重。再次迁入时，两种请求号均返回 409。[取消实测](evidence/cutover-reviewed-cancellation.json)

池中归档复跑使用 `python3 -B -m unittest -v rewrite.test_cutover.Cutover.test_pool_archives_only_return_after_the_worker_released_the_assignment`。隔离旧端读回原两种归档及同一请求号。候选附件从私有回退包逐字节比对，旧 HTTP 返回 404。离线入池后管理端明确归档但原工位仍忙时，演练拒绝发布。[池中归档实测](evidence/cutover-pool-archives.json)

已挂起的单后续转派、取消、池中结单或失败归档，可按各自的最终处置审核。核对原操作版本、每次挂起时长、当前期限和实际通知的接收确认。替换前尚未到期和归档时撤销的提醒均留在历史中。已发的提醒按所属派发与原期限识别，在私有包保留原消息和投递标识。旧端无需重播通知。缺接收确认、时间早于原期限或通知出现在下一派发之后，停止生成回退目录。若因时钟回拨而不能重建原顺序，保留两套库核对。复跑对应的转派、取消和池中归档测试。[提醒与回退实测](evidence/cutover-pool-reminders.json)

Worker 已以受阻 W04 释放原派发时，`resume` 可从受阻或挂起起步。同一工单和工位获得新派发，解决办法附在正文后面。工具从原库或建单请求逐版重建正文，核对原 Worker 的释放 W04、溪流的续办回执和后续派发版本。等待工位后的派发和当场派发都通过实测。试用新单回旧端时按最初建单正文计算指纹，同请求重送回原单号，带续办后的正文重送返回 409。私有档案保留两版正文、续办动作和最终成果。原 Worker 掉线入池且无释放凭据、新派发正文与记录不符都拒绝准备。[续办回退实测](evidence/cutover-pool-resumed.json)

直接修订标题或说明的已处置工单，按原建单、修订版本、派发和最终处置一起核对。派发前的修订改变首次派发正文。已派未接时，旧派发可有传输收件，仍须能证明无 W03。新版派发须对应修订回执。执行中的修订须有 `task.updated` 送往原派发、原工位，以及该工位的传输接收确认。池中、等待续派或成果待验时的修订也逐版核对。这几种修订保留在私有档案。旧 Router 按最初请求校验去重，停写库不重放修订或工单。版本、派发或传输确认有缺口时拒绝准备。演练另核了测试宿主的实际收件。复跑 `python3 -B -m unittest -v rewrite.test_cutover.Cutover.test_reviewed_edits_preserve_original_requests_and_fence_old_offers`。[修订回退实测](evidence/cutover-reviewed-edits.json)

工单内已答复的问题随原派发写入私有档案。逐题核对 W05、管理端答复、原问题通知和原工位答复帧的传输确认。原问题的选择、文本和跳过都保留。转派后或结单后的答复仍归原提问人。旧 Router 只恢复正式归档，原两工位没有待答题或重新派发。未答、未确认传输或与原派发不符的问答会停止回退。演练另用宿主夹具核对了实际投递。复跑 `python3 -B -m unittest -v rewrite.test_cutover.Cutover.test_reviewed_questions_keep_the_original_worker_after_transfer_and_rollback`。[问答回退实测](evidence/cutover-reviewed-questions.json)

Worker 请求继续时，回退以同一派发内的续做回执和原空闲观测划分询问轮次。每轮独立核首次询问和最多三次重试。最后一轮无答才允许对应入池和搁置通知。每条发出记录、观测和回执留在私有包，重开旧端无需再派。缺轮次回执或观测与原工位、忙闲和派发不符时停止。复跑 `python3 -B -m unittest -v rewrite.test_cutover.Cutover.test_reviewed_inquiries_preserve_continue_rounds_and_release_only_the_original_assignment`。[询问轮次实测](evidence/cutover-reviewed-inquiries.json)

### 试用新增私聊的回退

同伴私聊可与已验收工单一起回退，也可以单独审核。停写后用 `reconcile` 读取 `trial_peers`，逐条核对原发件人、收件人、消息号、回复关联、传输投递号及收件方登记的宿主回执。对账输出不包含私聊正文与附件。审核文件绑定本次 `trial_sha256`，并增加 `"peer_receipts":{"原消息号":"宿主回执号"}`。没有已验收工单时使用 `"closed_assignments":{}`。用同一个 `stage-reviewed-rollback` 命令生成新回退目录。只有传输已确认、宿主回执已登记、附件及权限均能对账的消息才可归档。任何一条无法核实，整个回退目录都不会发布。

私聊内容及原回执保存在 `private-review/peer-history.db`。旧 Router 仍只用回退目录里的 `legacy.db` 和 `legacy.db.attachments`。需要查阅档案时，从重做版包独立启动只读 TLS 服务，使用审核结果中的 `peer_archive_sha256`。认证文件单独列出各原参与人的 `worker/<id>` 身份及各自的令牌。

```bash
python3 -B -m rewrite.service peer-archive \
  --db "${RESTORE}/private-review/peer-history.db" --sha256 "${PEER_ARCHIVE_SHA256}" \
  --identities "${PEER_ROLES}" --cert "${CERT}" --key "${KEY}"
```

用参与人身份访问 `GET /v1/history/peers` 列来源，`GET /v1/history/<source_id>/peers/<message_id>` 取原文、传输号和宿主回执，再按返回的附件号读 `GET /v1/attachments/<attachment_id>`。正文支持分页，附件支持 HTTP Range。旧 Worker 的内置收件箱没有这个独立查询入口。已回退时使用只读 HTTPS 入口，回到重做版后可用原生 `worker_peer_inbox` 查询，也能沿原消息号回复。

再次迁入时，在原 `prepare` 参数上加 `--peer-archive "${RESTORE}/private-review/peer-history.db" --peer-archive-sha256 "${PEER_ARCHIVE_SHA256}"`。试用目录会保存校验后的 `peer-history.db`，后续对账、排空迁移和回退都应携带。所有原消息留在带原参与人权限的归档，零重投。无新写入的回退、只新增待派单的回退均逐字节保留原档案。再次审核后回退时，新私聊会并入原档案。

复跑 `python3 -B -m unittest -v rewrite.test_cutover -k reviewed_private_peers`。实际旧 Router HTTP 以原共享身份查询新私聊和附件均返回 404，已验收工单仍归档。独立 HTTPS 服务实际核对原双方可读全文及跨块附件，第三工位和非参与的溪流身份返回 404。回到重做版后，原两条各有原回执，重送原号返回 409，新增回信只产生一次宿主收件。第二次回退保留原两条和新增回复。宿主与 W04 模型使用夹具。[回退与回迁实测](evidence/trial-peer-fallback.json)

同批可以保留旧台账上其他工位的空闲观察证据。审核核对试用时该工位的无持单登记、每次传输入站确认、原会话的空闲回报和最后的心跳或适配器连通回执。核准的工位列在私有清单的 `idle_observers`，恢复的旧库仍按原状态启动。未在旧台账登记的旁观工位、没有查询回报、曾见忙碌或仍持单时，需要继续对账。

试用只有已核空闲观察时，也可用 `stage-reviewed-rollback`。审核文件保留 `trial_sha256` 和空的 `closed_assignments`。另有未经派发的新单时，加入 `reconcile` 核出的 `execution_modes`，并依正式回放步骤操作。实测只有空闲观察时逐字节恢复旧库。有一张新待派单时，旧 HTTP 首次接收返回 202，按原键重试返回 200 和同一单号。复跑 `python3 -B -m unittest -v rewrite.test_cutover -k quiet_presence`。

### 旧队列迁入后改过、仍未派发的单

用 `reconcile` 的 `work_orders` 核对原旧单号、最终工位和试用修订。只有两次迁移之间始终未派发、没有 W03、报告或候选材料的原待派单可按本节处理。审核过的 `review.json` 在已有字段外加入 `replace_migrated_queue`。每项以试用单号为键，值为 `{"legacy_task_id":"原旧单号","revision":最终修订号,"execution_mode":"goal"}`。只有这类替代时写 `closed_assignments: {}`。审核绑定当前 `trial_sha256`，使用同一个 `stage-reviewed-rollback` 命令。可与已验收、已取消及试用新建待派单同批核对。

旧端工单记录是追加式的。恢复副本在原旧单上追加 `queue_replaced_on_rewrite` 归档和替代单号，替代单有自己的旧端 ID 与请求 UUID，占原队列序号。无需向旧 Router 重放这个建单请求。原单保留原正文，无成果、无派发。按原旧请求重试读回原历史单，按替代请求重试读回替代单。开启旧端写者之前，依私有清单里的 `migrated_queue_replacements` 核对两单链接、接手工位、原队列位置和恢复后数据库摘要。替代单保留原执行模式与原路由上下文。编辑若去掉迁入时的目标段落或上下文，或旧端无法逐字表示新正文，回退会拒绝。原、新两端快照都须保留。

演练覆盖旧 HTTP 读回、两个原请求重试、正文冲突返回 409、实际旧 Router 仅派一次、回退后再次迁入，以及与正式 W04 归档和新待派单同批准备。旧单号收到取消时返回原归档回执，替代单继续排队。对替代单的管理操作须用其自己的旧端工单号。再次迁入只将替代单放回队列，已经使用的 A05/A06 请求键仍受历史记录保护。复跑 `python3 -B -m unittest -v rewrite.test_cutover -k approved_replacement`。使用测试 Worker 连接和宿主。[实测记录](evidence/cutover-migrated-queue-replacement.json) · [取消及三轮回退](evidence/queue-rollback-release-rehearsal.json)

### 在途工单先排空再回退

旧 Router 保持停写。先停试用写者并采集[五组件快照](#重做版五组件的离线恢复)，保留迁入时的旧快照和 `baseline.json`。从新的目录恢复这套重做组件，使用原 Worker 身份、原适配器账本及原生会话。启动重做 Router 时，在现有启动参数上加 `--drain-dispatch`，核对启动回执中 `dispatch_draining` 为 `true`。每次重开都须保留这个参数。此模式停止分配新派发号，已有派发、重连对账、业务回执和定时规则继续按原契约处理。冻结外部试用入口，避免排空时又发生无关写入。

核对原工单号、派发号、Worker 持单和宿主收据。已有可核的宿主收据时只接回原会话。W04 回执未到时保留原 outbox，由 Worker 以原幂等键恢复；Router 只有成果待验时仍由溪流实际验收。关停排空实例后再次采集，核对 `audit-handoff` 中的持单、待确认报告和宿主账本。保留每次采集的摘要。将最后一次 Router 库的离线副本作为 `router.db`，连同原来的 `baseline.json` 放在新的试运行目录，核对该库 SHA-256 与排空快照中 Router 成员一致，再使用上面的 `stage-reviewed-rollback`。切回旧端前核对原单归档、原队首和在途清单。

宿主接收未知、原模型仍无法接续、派发已转移或回报与原任务不符时，保留新旧两侧并逐笔对账。排空开关不会暂停离线入池，也不会隐去已在网络上的原派发。回退命令继续拒绝未验收或不符合上述回迁范围的写入。

复跑 `python3 -B -m unittest -v rewrite.test_cutover.Cutover.test_inflight_rollback_drains_same_assignment_before_old_router_can_resume`。隔离重做 Router、Worker 以及旧 RouterStore 实跑，宿主、模型和时钟使用夹具。第一次恢复原持单，第二次重开按原 W04 键读回丢失的回执，整个过程中下一张单保持无派发号。测试管理身份确认验收后才生成旧版回退副本。[排空与回执实测](evidence/cutover-inflight-drain.json)

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

### 迁入已对账的旧订阅积压

先停止该订阅的旧投递和源端写入，独立保存旧消费者最后确认的游标、实际使用的等级过滤、宿主收件凭据及旧离线归档。在出计划时为同一个旧 Trigger 库选定一个来源 UUID，之后为该来源始终沿用。下面以原消费者实际使用 `event,notify` 为例。

```sh
python3 -B -m rewrite.trigger_handoff plan --snapshot ./trigger-snapshot --manifest-sha256 "$TRIGGER_SHA256" \
  --subscriber "$LEGACY_SUBSCRIBER" --consumer-cursor "$OLD_CURSOR" --level event --level notify \
  --source-id "$LEGACY_SOURCE_UUID" > ./handoff-plan.json
```

计划按旧序号列出可读未确认项。已经确认、丢弃及日志档案均留在旧档。消费者游标与旧账本不合时停止。逐条查原消费者最后一段持久收件，回执未知时先核证。审核文件含 `plan_sha256` 和 `events`，事件表的键用计划中的旧序号。已读但旧确认丢失的条目写 `{"already_received":"原宿主收件凭据"}`。需补送的条目写 `{"level":"Notify","evidence":"原消费者收件账本核对结果"}`，等级需在该项的 `allowed_levels` 内。旧 Info 只能按 Info 迁入。旧 Event 未具备唤醒条件时最多按 Notify 迁入。其他活跃订阅会列在计划里，须分别核对消费归属。

```sh
python3 -B -m rewrite.trigger_handoff stage --snapshot ./trigger-snapshot --manifest-sha256 "$TRIGGER_SHA256" \
  --saved ./handoff-plan.json --review ./handoff-review.json --destination ./trigger-trial \
  --attachments ./snapshot/attachments
python3 -B -m rewrite.service trigger --db ./trigger-trial/trigger.sqlite3 --name legacy-handoff send \
  --connection "$ISOLATED_HANDOFF_CONNECTION"
```

仅含无旧附件引用的事件可以省略 `--attachments`。有引用时从已停写的旧 Router 内容寻址存储按 SHA-256 查本地文件，并核旧记录里的大小与摘要。超过 200 字的正文以原字节附档，旧结构化元数据保留在旧档。若消费逻辑依赖旧业务字段，应先盘清再迁。新 Router 使用仅绑定计划内 `instance_id` 的专用 Trigger 连接。预备目录及 `handoff.json` 按私有档案保留。T04 回执丢失时继续送同一库，从同一计划重建也复用消息及上传幂等键。推进旧游标前，逐项核对 T04、Hermes 接收回执与宿主收据。

实测已用真实旧 TriggerStore、重做 Router HTTP 和 HermesAdapter 核五条旧未读。两条 Info 只供查询，一条 Event 和一条 Notify 各送一次，原消费者已有收件的一条留在旧档。丢失 T04 后重开和重建均回同一回执，旧附件与长文逐字节一致。模型、宿主与时钟使用夹具。发送后如需退回旧消费者，先保留两侧库并逐笔对账。旧确认是累计游标，只能推进到连续核实的最后一个序号。复跑 `python3 -B -m unittest -v rewrite.test_trigger_archive`。[迁入回执](evidence/trigger-unread-handoff.json)

### 试用发送后退回原订阅

保持新旧发送端停写，用[组件快照](#重做版五组件的离线恢复)采集同一次停写窗口内的 Router、迁入专用 Trigger、Hermes 和原宿主适配器账本。保留其他组件和收件记录。用迁入时返回的 `journal_sha256` 校验原 `handoff.json`，核实待切回的旧端仍使用计划里同一订阅及等级过滤。若其中任何账本曾被替换、旧源在停写后又有新记录，先比对两侧现状再选择回退基线。

```sh
python3 -B -m rewrite.trigger_return audit \
  --snapshot ./trigger-snapshot --manifest-sha256 "$TRIGGER_SHA256" \
  --journal ./trigger-trial/handoff.json --journal-sha256 "$HANDOFF_SHA256" \
  --bundle ./trial-state --bundle-sha256 "$TRIAL_STATE_SHA256" \
  --trigger-instance handoff --manager-instance main --host-session "$ORIGINAL_HOST_SESSION"
```

`--trigger-instance` 和 `--manager-instance` 对应快照成员名里的实例部分。核对报告中的 `verified_cursor` 及逐项状态。确认送进新宿主的事件需要匹配 T04、Router 投递和 C02、Hermes 队列及原宿主收件号。旧消费者已收的条目沿用迁入时审核。旧 Info 在新档里可查时，需另有原消费者确认已经查阅。可提交 `--info-review`，文件含 `plan_sha256` 及 `archive_reads`，后者按旧序号填写实际查阅凭据。降为 Info 的旧 Event 或 Notify 仍留待原订阅。只剩有完整账本且尚未送进宿主的尾部事件时，它们继续在旧订阅待取。遇到未知的宿主回执、已收件但前序有缺口或有飞书提示未对账时，审计停在缺口前。

报告确认 `safe_to_stage` 后，将同一命令的 `audit` 换成 `stage`，加 `--destination ./trigger-return-copy`。输出旧归档字节副本及私有的 `ack-replay.json`。保持源端、订阅消费者和试用发送端停写，在隔离旧 Trigger 副本先校验现有旧游标为 `initial_cursor`，只在 `request` 非空时按原样执行 `POST /ack`。回包的 `last_id` 须等于 `verified_cursor`。若已执行但回执丢失，在同一副本重新发送原请求，按实际游标与待取列表核对。用相同的等级过滤读 `GET /events`，只应看到 `remaining_event_ids`。如消费程序另存本地游标，启动前同步到已核的旧游标。仍在新系统的其他来源与平台回执另行交接。

实测让后序 Event 先到宿主，前序 Notify 只有 C02 时禁止准备回退。Notify 进入宿主后模拟丢回执，仍禁止回退。沿原投递号查回后，原旧序号从 1 推进到 8。真实旧 Trigger HTTP 重开后重投确认，新增确认数为零，仅保留从未送出的尾项。Info 经实际查询及测试管理审核后才计入连续前缀。测试宿主和时钟使用夹具。[累计确认实测](evidence/trigger-return-prefix.json)

### Router 与来源同批回退

用同一次停写窗口采集 Router、Hermes、Hermes 宿主及所有参加本批回退的 Trigger 库。用于 `stage-reviewed-rollback` 的试用 `router.db` 必须与该组件快照中的 Router 成员逐字节一致。先运行 `reconcile`，从 `trial_source_messages` 核对每个消息号、原来源身份和等级。正式工单仍按派发与 W04 回执审核。

将独立来源审核的参数放进一份来源计划。路径相对于计划所在目录，或使用绝对路径。每个 Trigger 实例只列一次。

```json
{
  "bundle": "trial-bundle",
  "bundle_sha256": "<组件快照清单摘要>",
  "manager_instance": "main",
  "host_session": "<原 Hermes 宿主会话>",
  "sources": [
    {
      "kind": "subscription",
      "trigger_instance": "handoff",
      "legacy_snapshot": "old-trigger-snapshot",
      "legacy_snapshot_sha256": "<旧 Trigger 快照清单摘要>",
      "journal": "trigger-handoff/handoff.json",
      "journal_sha256": "<原交接日志摘要>",
      "info_review": "observed-info.json",
      "info_review_sha256": "<Info 审核文件摘要>"
    },
    {
      "kind": "feed",
      "trigger_instance": "mail",
      "legacy_cursor": "old-mail-cursor.json",
      "review": "mail-source-review.json",
      "review_sha256": "<采集审核文件摘要>"
    }
  ]
}
```

旧订阅的该批没有需确认查阅的 Info 时，可省去 `info_review` 及其摘要。采集项也适用于游戏来源，审核输入沿用原游标和来源证据。Router 审核文件加入 `source_plan_sha256`，填写来源计划的文件 SHA-256，并保留 `trial_sha256`、`closed_assignments` 及本批其他审核字段。[旧采集来源交接](#旧采集来源交接)

```sh
python3 -B -m rewrite.cutover stage-reviewed-rollback \
  --snapshot ./old-router-snapshot --trial ./stopped-trial --review ./rollback-review.json \
  --source-plan ./source-plan.json --destination ./combined-return
```

旧 Router 恢复副本和来源回交计划一起发布。所有来源材料保存在 `private-review/source-returns/`，含来源计划原件、Hermes 两份对账库及各实例的原试用库。私有总清单给出各来源的输出摘要。旧订阅按实例目录中的 `ack-replay.json` 恢复累计确认，旧采集按实例目录中的游标恢复。核对后再分别执行旧接口。单独完成旧端的一个步骤后若中断，从已经落账的同一份旧库或游标查回。

试用已收到 T04 但未送到原生宿主的旧订阅尾项仍由旧订阅处理，无论 Hermes 是否已确认传输。已取得且核对了宿主回执的项目按原连续前缀确认。缺源端 T04、缺宿主回执、已送宿主但应发的来源飞书提示未取得回执、漏列的来源、摘要不符或来源证明与试用 Router 不同批时，整包不发布。同一旧 Trigger 归档的多个订阅如需并回同一库，仍须另做合并演练。

复跑 `python3 -B -m unittest -v rewrite.test_source_rollback`。实跑隔离 Router、旧 Trigger HTTP 和恢复副本。验证一张已验收工单归档、旧订阅 Event 与 Info 的确认、邮件与游戏采集游标推进，以及两条仍待原订阅处理的尾项。实际旧端重放确认返回零新增。宿主、模型、飞书、邮件源和游戏 SSE 源使用夹具。[证据](evidence/source-router-rollback.json)

### 来源回退后再次迁入

同批回退保存 `private-review/source-history.db`。其摘要在私有清单和旧恢复库中各有记录。沿用同一批的旧库与档案，打包之前核对摘要。旧 Router 和旧 Trigger 按原入口运行，档案单独保管。对回退后的旧库重新 `backup`，在隔离端运行原 `prepare` 命令时加上这两个参数：

```sh
--source-archive "${RETURN}/private-review/source-history.db" --source-archive-sha256 "${SOURCE_ARCHIVE_SHA256}"
```

缺档、错批或摘要不一致会阻止生成试用库。对停写试用库做对账或回退时，随 `router.db` 与 `baseline.json` 保留该目录中的 `source-history.db`。无新来源写入的回退逐字节保留原档。再次审核新的来源记录时会两批并档，旧库内的档案绑定随之更新。

溪流可在 C03 或 `router_info` 按原消息号查回正文、来源和原附件。来自旧批次的条目另带 `source_history`，显示原批次摘要、核对结论、传输回执及已有的宿主回执。原 Trigger 也可按原上传号查询其附件，其他身份无该访问权。已入档且处理完的 T04 同键重试返回原回执，换键重试同文只返回重复确认。在新提交中借用旧附件会被拒绝。已交回旧订阅的尾部使用旧接口消费，重做版 T04 对尾部返回 `409 source_returned_to_legacy`。它们如需再次交接，应先停原消费者，核实旧游标和实际收件，再另行审核。当前恢复不会重新派送这些尾项。

复跑 `python3 -B -m unittest -v rewrite.test_source_rollback`。实际隔离 HTTP 查回六条原消息、原上传与附件字节，历史消息零重投，尾部原键和换键都回 409。新采集的 Event 投递一次。二次同批回退后第三次迁入，六条历史和后一条 Event 仍分别按原批次可查。宿主、模型、平台与来源使用夹具。[再次迁入实测](evidence/source-history-reentry.json)

### 旧 Worker 的实时订阅

旧 Router 的 `GET /subscriptions?active=1` 用于只读盘点配置。旧 `GET /subscriptions/pending` 会确认已取记录，并推进两边游标。隔离测试只取 HTTP 状态后丢弃响应正文，旧 Router 和实际旧 Trigger 的游标均从 0 到 2，过滤掉的事件也有确认，随后旧队列返回零条。全过程 Worker 连接为零。旧侧确认只能证明上游游标已经走过，宿主是否收到须另查原工位记录。

核对旧 Codex 或 DSH 时，在停写副本中先用旧插件的 `subscriptionMessageId` 从来源名及旧事件序号取回消息号，再查原工位的常驻收件账本或 `receipt-archive/<消息号>.json`。可将原 `origin=subscription`、事件头及正文、目标会话、原生回合号和宿主实际输入逐一对上。只有拉取、飞书提示或收件状态待核时，不能推断宿主已收到。旧插件提交 `f399148df99ed58568f4931aed414e3a83fa7a25` 的两套订阅用例各实跑一遍，同号两次到达只形成一条输入及一张测试提示，均无工单成果。该复验使用旧运行模块、HTTP 与宿主替身，未读取生产工位收件档。复跑该仓的 `node --test --test-name-pattern='subscriptions declare, poll, serialize' Plugins/Codex/tests/router-runtime.test.mjs Plugins/DSH/tests/router-runtime.test.mjs`。

用三个冻结副本运行订阅审计。保留断开旧 Worker 连接之前的 Router 副本，旧 Router 会在断开连接后删除活跃订阅，重开后查不到记录无法证明旧能力已退役。Trigger 用原事件库副本，Worker 用已经确认归属的 `desktop-manager-state.json`。分别记录原件摘要。若有已压入档案的旧回执，可另传 `--receipt-index` 和 `--receipt-index-sha256`。索引为消息 UUID 到回执文件 SHA-256 的 JSON 对象，使用原文件名存于索引旁的 `receipt-archive/`。

```sh
python3 -B -m rewrite.legacy_subscription \
  --router-snapshot ./old-router --router-sha256 "${ROUTER_DB_SHA256}" \
  --trigger-snapshot ./old-trigger --trigger-sha256 "${TRIGGER_MANIFEST_SHA256}" \
  --worker "${OLD_WORKER}" --subscription-id "${OLD_SUBSCRIPTION_ID}" \
  --worker-state ./old-worker/desktop-manager-state.json --worker-state-sha256 "${WORKER_STATE_SHA256}"
```

报告给出两端游标、原 Trigger 确认和每条消息的宿主证据。只有请求正文、固定会话、原生回合、已观察输入及其摘要一致才记 `input_observed`。该状态只证明原生会话有过这条输入。表内缺回执、仅 RPC 接受、回执冲突分别列出，无法据此认定从未发送。报告省略正文。审计只读，且仍由原切换门槛拦截活跃订阅。隔离真实旧 HTTP 覆盖了确认后丢弃响应和 ACK 失败仍返回事件两种情况。用旧 Codex、DSH 管理器及宿主替身实测，同一受理回执在原生读回前为待核，读回后能核到同一个输入。复跑 `MOWULINK_TEST_LEGACY_WORKER_ROOT="${PINNED_LEGACY_WORKER_SOURCE}" python3 -B -m unittest -v rewrite.test_source_rollback`。旧 Worker 源码取上面核对过的版本。[订阅审计实测](evidence/legacy-worker-subscription-audit.json)

旧 Router 为 Worker 分配的 `router:<worker>` 和 `router:<worker>:<订阅号>` 拒绝进入通用 Trigger 迁入。隔离复现中，旧订阅只返回 4 条，通用迁入却另列了一条已被旧工位过滤的事件。以前留存的通用计划也会被拦截，原库和原工位继续保留。

发现活跃的旧 Worker 订阅时，离线准备仍会停在 `active_subscriptions`。先核对原订阅配置、两端游标、过滤项和原宿主收件，再决定沿独立来源把消息交给溪流，或继续保留旧路径。复跑 `python3 -B -m unittest -v rewrite.test_source_rollback -k legacy_worker_subscription`。[旧端读回证据](evidence/legacy-worker-subscription-ack.json)

## 旧采集来源交接

在演练窗口准备好来源游标副本和专用配置。交接检查旧采集端已处理到的最后一条、待上行队列和新实例标识。游标导入只保存位置与摘要。仍在旧采集端等待发送的事件需要在接管前另外对账。

```sh
python3 -m rewrite.feeds mail --config ./mail-trial.json --import-legacy-cursor ./old-offline/mail-cursor.json --confirm-legacy-mailbox
python3 -m rewrite.feeds game --config ./game-trial.json --import-legacy-cursor ./old-offline/game-cursor.json
```

旧邮件游标只记文件夹、UIDVALIDITY 和末尾 UID。使用 `--confirm-legacy-mailbox` 前，核对旧配置的服务器、端口、账号和文件夹确实相同。首次采集读回原 UIDVALIDITY，且服务端最新可读位置不小于旧 UID 时，才验证成功。游戏游标必须和配置的桥地址相同，首次观测到序号比旧游标小时会中止。旧桥缺少启动标识，若切换前桥已重启并重新超过旧游标，仅凭序号无法判定同一实例。核对连续运行状态后再用该游标。

新版游戏采集在同一 SQLite 事务保存源序号、消息和最后一帧的原始载荷摘要。再次连接时从当前序号的上一位请求 SSE，等同一帧读回并比对摘要后才接后续序号。即使新会话的序号和聊天正文相同，只要原始载荷中的时间戳等字段不同，已导入的交接就会记为连续性中断。首次观测到低于已导入序号时也会保留这一状态，重新超过旧序号或重开进程都不能解除。上一帧暂缺时，保留旧位置，归档和待交队列可查，回退游标准备会等该帧重新获得核对。

这项核对覆盖已保存原始帧摘要的续接窗口。旧游标没有原始帧或启动标识，首次迁入仍需独立确认桥持续运行。读回便于识别序号复用，源端两次返回完全相同的载荷仍无法凭此区分。[序号复用实测](evidence/game-source-overlap.json)

相同旧游标重导入只返回原摘要与验证状态。目标库已开始工作或保存过不同游标时拒绝覆盖。隔离复跑 `python3 -m unittest -v rewrite.test_feeds`。邮件用协议夹具，游戏走临时 HTTP 流。生产来源与订阅继续待验。

### 补录旧游标越过的记录

旧来源游标表示读取位置。切换时若该段可能遇过上报失败，先在停写窗口保存旧游标、旧 Trigger 归档和仍可取得的原始来源记录。邮件按原账号和文件夹以读取方式枚举 UID；游戏同时核桥连续运行、玩家及数字 SSE 帧号。源端缺失的资料和无法解释的账号或会话变更需保留为待核。

在私有的 `source.json` 保存以下字段：

| 字段 | 填写 |
| --- | --- |
| `kind`、`scope`、`epoch` | 邮件填 `email`、`[服务器,端口,账号,文件夹]` 和 UIDVALIDITY 字符串。游戏填 `mc_chat`、`[桥地址]` 和 `"game"`，另提供原 `player`。 |
| `after`、`through`、`legacy_cursor_sha256` | 审核区间为左开右闭。下界用已有对账位置，上界必须等于旧游标文件的当前位置。保留旧游标原件及摘要。 |
| `source_id`、`observed_at`、`continuity_evidence` | 同一来源同一连续代际用同一 UUID。记录原资料冻结的带时区时间和账号或桥连续性的核对依据。 |
| `records` | 原位置到文件 SHA-256 的对象，例如 `{"15":"…"}`。记录放在邻接的 `records/`，邮件以 `<UID>.eml` 保存原邮件，游戏以 `<序号>.sse` 保存含数字 `id:` 和结束空行的原帧。邮件只对实际枚举到的 UID 出结论。游戏中间缺帧会拒绝准备整段。 |

```sh
python3 -B -m rewrite.feed_recovery audit --snapshot ./old-trigger \
  --trigger-sha256 "${LEGACY_TRIGGER_SHA256}" --legacy-cursor ./old-cursor.json \
  --source-manifest ./original-input/source.json --source-sha256 "${SOURCE_MANIFEST_SHA256}"
```

审计只输出标识和处理结论。已入旧库、旧库记录为丢弃、原客户端的自发消息过滤均留在原裁定。邮件按邮件头、文件夹、UID 和旧库保留的正文预览对证，超出旧预览的正文仍需原始邮件证明。游戏用帧上原样的编号查旧收件，保留前导零。同号但旧内容无法核对时保持待核。对每一条 `missing` 附上原来源核对依据，在审核文件中保存 `plan_sha256` 及 `events`。例如位置 15 选存档可写 `{"15":{"level":"Info","evidence":"原始输入与旧 Trigger 对账记录"}}`。邮件仅可选 Info。原游戏 Event 可审核为 Info、Notify 或 Event。已从另一个旧消费者独立核实收件的，可填 `{"already_received":"该消费者原回执"}`，保留审核结论，跳过补录。

将 `audit` 换成 `stage` 并加 `--review ./recovery-review.json --destination ./feed-staged`。输出 `feed.sqlite3` 和 `recovery.json`。采集库保留原游标，只有审核补录的记录进入待交队列。首次接管时用这个新库做正常来源验证，按恢复清单登记该 Trigger 实例的隔离连接。通过已有的 `python3 -B -m rewrite.service trigger --db ./feed-staged/feed.sqlite3 --name email send --connection <测试连接文件> --message-id <原补录消息号>` 交付。游戏把名称换成 `mc_chat`。重试沿用同一份计划和已登记实例。已在运行的试用库另保留和对账。

回退按下面的来源收件步骤审核。补录的记录会单列 `recovered` 和原审核计划摘要，所需的 T04、附件与宿主收件规则沿用普通来源。旧游标保持已核位置，补录内容保留在回退档。再次迁入可按原消息号读回，发送重试沿用原回执。旧路径不能读到的邮件 UID、丢失的游戏原帧和现役消费者需独立核查。[补录实测](evidence/legacy-source-recovery.json)

### 把试用采集位置交回旧客户端

保留导入时的原游标文件。停两边采集器和发送端，从同一停写窗口保存试用 Router、采集 Trigger、Hermes 和原宿主适配器账本。先核试用期间的全部捕获。邮件 Info 必须有匹配的 Router 收件并保留完整私有档，游戏 Event 还须有原宿主的收件号。旧归档副本不会自动取得这些新数据，回退时一起保管试用档案与 Router 快照。仍有待确认回执时，先在试用侧查回或完成原投递。

审核文件包含 `snapshot_sha256`、`legacy_cursor_sha256`、`source` 和 `filtered_positions`。在 `source` 中保存同一来源的 `epoch`、已观测的 `head` 及独立核对的 `evidence`。邮件的 `epoch` 用 UIDVALIDITY 字符串，`head` 用 UIDNEXT 减一，并核原服务器、账号、文件夹。游戏的 `epoch` 为 `"game"`，`head` 为 `lastSeq`，证据要能证明原游戏桥未重启、玩家和过滤口径一致。游戏桥只报数字序号时，头部数字变大不足以证明连续。已观测的回退会留在新账本，数字后来恢复到较高值也不能解除。若最近一次续接缺少前帧读回，先查回同一帧。回退报告的 `game_overlap` 会列出实际核对过的末帧位置及摘要。游戏试用期缺号时保持原位置，待缺号补齐后才能继续。各条源端过滤在 `filtered_positions` 按原序号写核对依据。

```sh
python3 -B -m rewrite.feed_return audit --bundle ./trial-state --manifest-sha256 "$TRIAL_STATE_SHA256" \
  --legacy-cursor ./old-offline/email-cursor.json --review ./mail-return-review.json \
  --feed-instance mail --manager-instance main --host-session "$ORIGINAL_HOST_SESSION"
```

源名称使用快照里对应 `trigger:<实例名>` 的实例部分。得到 `safe_to_stage` 后，将 `audit` 换成 `stage` 并加 `--destination ./mail-return-copy`。输出经核对的原格式游标、私有试用档案及摘要清单。旧客户端在隔离副本先用该游标起一次，核首个 UID 或游戏序号。游戏旧客户端续接时用 `--lookback 0`。早期已验证的导入库若没有记录原导入代际，按原始来源与账本逐笔对账。

实测旧邮件客户端从 13 续到 14，旧游戏客户端从 17 续到 18。试用已收件和已审核过滤的事件没有重发。原生宿主、游戏桥和邮件服务使用夹具，旧客户端、旧游戏投递 HTTP 与重做 Router 实跑。复跑 `python3 -B -m unittest -v rewrite.test_feeds`。[源端续接实测](evidence/feed-source-return.json)

## 重做版五组件的离线恢复

有了验证过的五组件快照后，可先逐项核对持单。

```sh
python3 -m rewrite.state_bundle audit-handoff --snapshot "$SNAPSHOT" --manifest-sha256 "$MANIFEST_SHA256"
```

`worker:<ID>` 和 `agent:<ID>` 的 ID 使用 Router 上的准确拼写。命令只读取离线快照，比较 Router 持权、Worker 持单及会话、原生适配器收件凭据、待核的 W04 和原派发清理记录。输出 `needs_review: true` 时用隔离副本逐项对账。检查结果只覆盖上述账本，不能据此判断生产可切换。

启用 `assignment_container` 的 DSH 工位或启用 `--assignment-workspace` 的 Codex 工位，先用新版适配器核到原绑定的宿主活动状态，再停写拍快照。适配器会登记原工作区账本的文件系统身份。含该登记的快照采用格式 3，用新版工具读回。保持原适配器及宿主停止，准备 `dsh-runtimes.json`，按工位 ID 指向当时的原私有运行目录，例如 `{"oguri":"/isolated/oguri/dsh-runtime"}`。Codex 沿用同一个映射参数，将其值填为原 `workspace.json` 的 `runtime`。在能读取该原目录的机器上，用同一个参数执行审计和换绑。

```sh
python3 -B -m rewrite.state_bundle audit-handoff --snapshot "$SNAPSHOT" --manifest-sha256 "$MANIFEST_SHA256" \
  --dsh-runtimes ./dsh-runtimes.json
python3 -B -m rewrite.state_bundle stage-native-handoff --snapshot "$SNAPSHOT" --manifest-sha256 "$MANIFEST_SHA256" \
  --dsh-runtimes ./dsh-runtimes.json --worker-id oguri --source-session "$ORIGINAL_SESSION" \
  --target-session "$TARGET_SESSION" --destination ./trial-native-binding
```

`native_session_pending.dsh_assignment_workspaces` 计入原账本尚未回收的派发。未提供原目录、目录身份不符、账本无法读取或原宿主仍运行时，该项记为未知并且换绑不发布。换绑生成目录前再次读账本，确认在准备期间没有变化。W04 和持单均清零后，尚有容器也会阻止换绑。重开原宿主及适配器，凭原回执收尾，再停机检查。早于账本登记的快照需先在原工位补采一次活动观测后重新备份。五组件数据恢复仍可接回原工位。[DSH 换绑与回退](evidence/dsh-offline-workspace-handoff.json) · [Codex 工作区停机对账](evidence/codex-assignment-workspace.json)

运行可写工位的 DSH 插件在私有目录使用 `dsh-workspace.writer` 协调写入。离线程序复核 Unix 或 TLS 本机监听，读取 `dsh-link.json` 时仍检查文件权限。新宿主注册写者后，换绑和版本回退程序持有同一把锁直至目录发布。重开工位和发布目录不能同时写原账本。保留原私有运行目录及启动配置，不在停写期间移除端口配置或锁文件。使用没有写锁的旧插件时，维持原宿主确实停机，并保持端点配置供检查。[双向 TLS 锁与回退](evidence/dsh-tls-workspace-handoff.json)

`native_session_pending` 另列换绑前需要核对的离线账本。包括持单、Router 已保留的派发、未处理收件、私聊与队列、同伴收据、问题、压缩、清理以及没有原生关闭回执的命令审批。缺少的旧库表显示未知。只有这些记录均为零且前述持单检查无误，`native_session_ledger_clear` 才为真。原会话重开可继续未确认的工作，换到别的会话前要把这些记录留在原会话对账。实际忙闲、原生目标和权限须在独立宿主核验。

启用 Codex 审批的新版适配器另报 `codex_approval_closures`。它包含原生审批已到适配器、Router 尚未收到登记，以及 Router 已写关闭但适配器尚未核到回执两种情况。单工位与其纳入快照的托管会话均计数。未出现这项时，较早版本或未开启转接的工位需核对实际配置与原宿主记录。出现待核时保留原会话和账本。重开后原绑定 Worker 可查回 Router 已保存的关闭回执。已经保存原生关闭、且 Router 保有原登记时可补交关闭。宿主空闲无法填补缺失的原生关闭凭据。运行中换绑也会查询已登记的原适配器账本。运行中的待核审批恢复使用原会话，无需重放旧审批或改变权限。[运行中换绑](README.md#飞书切换会话)

`python3 -m unittest -v rewrite.test_state_bundle` 在临时端口跑一张已持有的单、一个还没送到宿主的通知、一个待交原附件和一个等待投递的飞书私聊。关闭这套服务，将七个数据库作为一批备份，再从新目录重开。Worker 保留原会话和派发，宿主按原投递返回原凭据；平台沿用原卡游标，私聊只进入一次；Trigger 保留实例号，恢复后上传的 332800 字节与原件全部相同。两端原生记录和飞书云端由独立夹具保留，HTTP、SQLite 和恢复程序实跑。

1. 在隔离工位停写并关闭服务，确认宿主保留了原生会话和输入记录。启用了 DSH 隔离容器时，一并核对原引擎和私有运行目录中的 `assignment-containers`、`read-only-admissions`。工作目录在原引擎的内存里，跨机复制账本无法恢复该目录。同机冷恢复已用真实 DSH、独立 Worker 和适配器核实。在途单写入后关停三个进程，原绑定在停写账本的副本上恢复，原容器继续可读。W04 已落库但回执丢失时再重开，按原幂等键取回回执，原容器随后回收。核对原会话、原派发和原容器启动标识，并确认验收通知只有一条。见[实测证据](evidence/dsh-workspace-cold-recovery.json)。需要保留的结果先用原派发交附件与 W04，再核对释放回执和容器移除。待核容器随原工位保留，换绑前核对回收。若工位使用项目快照，一并保留已审核的归档与摘要，供原派发的未确认初始化查回。使用独立工位的工作区执行选项时，保留原派发记录，到工作区回收后再应用新配置。旧派发的容器按记录里的挂载方式核对并移除。引擎已重启、工作目录丢失时，凭快照只能初始化新派发。为这次演练列出要备份的每个数据库。跨机时先在各自的停写窗口取完整离线副本，所有工位停写区间须重叠。要从本机集中快照，源数据库使用 SQLite 的 DELETE 日志模式，待处理的 WAL 和事务需先由原程序正常关闭并检查。程序遇到占锁或活动日志会退出。
2. 准备 `sources.json`，用 `角色:实例名` 指向每个数据库的绝对路径，例如 `"worker:oguri":"/staging/oguri/worker.db"`。角色为 router、worker、agent、hermes、trigger；使用独立宿主投递账本或飞书时，分别加入 hermes-host、feishu。每个 Worker 与对应 agent 的实例名相同；一个以上来源或工位各占一行。核对清单与各机器实际配置。凭据、连接配置和宿主登录态由各自所有者保管。缓存可由已备份的原附件重建。
3. 执行下列命令，单独保管采集输出的源码版本、快照号和清单摘要。恢复目标必须是新目录。用返回的角色与文件映射重新连接隔离组件并验证原身份、持单、原生会话和未回执队列。

```sh
python3 -m rewrite.state_bundle capture --sources sources.json --revision "$SOURCE_SHA" --destination ./snapshot
python3 -m rewrite.state_bundle verify --snapshot ./snapshot --manifest-sha256 "$MANIFEST_SHA256"
python3 -m rewrite.state_bundle restore --snapshot ./snapshot --manifest-sha256 "$MANIFEST_SHA256" --destination ./recovered
```

快照只覆盖清单中的重做版状态，原生宿主和飞书已发生的外部动作不会随数据库回退。原生会话不可核、平台已过幂等时限或快照后存在新的业务写入时，应先隔离恢复目录，对照两端记录后再启用写者。旧系统的待派单转换仍用上节的旧 Router 演练流程。

DSH 本机双向 TLS 只读工位的原生限制记录在宿主运行目录的 `dsh-admissions.sqlite3`。停止宿主写者后，将它和原生会话日志配套保留，放到目标工位已限制访问的运行目录。若留有 SQLite 回滚日志，先用支持此模式的版本恢复并正常关闭，再取离线副本。原主线快照清单不会替宿主收这两件。回退到不支持这份限制记录的版本前，先在原工位核清挂起或转派的旧派发，以及 Router 已落库但 Worker 未确认的清理回执。Unix 套接字工位与双向 TLS 工位使用各自的宿主记录，跨两种方式迁移前先核清原派发。

DSH 本机双向 TLS 工位另将前台题调用指纹存入 `dsh-questions.sqlite3`。停写并正常关闭宿主后，将这份库连同原生会话日志和适配器库一起备份。恢复到受保护的新目录后，先用原题号查回原生回执，再开放新的答复入口。独立验证复制原生会话和指纹库后返回同一关闭回执，原卡没有再次更新，模型未重跑。更换传输方式或退到不认识该题目记录的版本前，先核清原卡和宿主待答题。主线快照不会代收原生会话与题目指纹库。

DSH 托管会话依原有流程备份登记关系及子会话库，并把原生会话日志与它们一同保留。试用恢复时使用新的私有名册目录并核对启动时的路径与绑定。自动回调端口由当前适配器启动后重选，`dsh-worker-port.json` 可由适配器重建。回退旧版本前核对该版本支持当前工位的托管与传输配置。

需要退回原版本时，保留新系统试运行的全部库，将其映射写成 `trial.json`；可直接使用 restore 输出的映射。使用下列命令，将试运行库全部锁定并与原快照核对。任一库有写入、角色清单变化或写者仍占锁，都会拒绝生成回退目录。

```sh
python3 -m rewrite.state_bundle rollback --snapshot ./snapshot --manifest-sha256 "$MANIFEST_SHA256" --trial-sources ./trial.json --destination ./previous-state
```

含 DSH 工作区登记的格式 3 快照，版本回退还要加 `--dsh-runtimes ./dsh-runtimes.json`，在能读取原私有运行目录的机器上执行。原宿主仍在运行、原账本未核验、容器尚未回收时，回退目录不会发布。准备期间账本发生变化也会中止。需要先保留原库来接回同机同引擎时，用上面的 `restore` 生成数据副本，仍从原工作区回收容器。回收完成并再次停机后，才能准备旧版的状态目录。

已用两版安装包复验。新版先确认 W04 已落库、Worker 清零，实测容器仍活跃时拦回退。按原回执回收容器后，从同一快照准备旧版数据。启动旧版 Router、Worker、DSH 适配器和 DSH 宿主，读回相同成果和附件。重发一条同号私聊，原会话只新收一次，原工单未重跑。复跑命令见[DSH 工作区验收](README.md#dsh-固定会话与飞书卡片)，[实测证据](evidence/dsh-offline-workspace-handoff.json)。


版本级复跑为 `python3 -m rewrite.release_check --repository . --previous b31cee5da7c8dc74a76accdd78d8a6c5cb49484c --new 5de36ad66df26dc34fe3832ed14e77b1d9f19551`。两个已提交版本在各自新目录启动真实 Router，以专用临时 TLS 和身份验同一张待派单。旧包→新包→旧包后只查到原单一张，新试用库字节未变。该演练在 macOS 跑过；Windows 与跨机宿主尚待验收。

### 飞书输出回执与换绑

拍快照时同时保留 `feishu:<工位 ID>` 和对应的 Worker、宿主及外部消息记录。使用与原来相同的飞书应用、会话和工位身份恢复。在 `audit-handoff` 的 `native_session_pending` 核对 `platform_display_unseen`、`platform_display_attempts`、`platform_input_notices`、`platform_known_receipts_uncommitted` 和 `platform_image_uploads_unconfirmed`。第一项是 Worker 已存、飞书尚未开始处理的展示。后两项分别计入已取得消息号、待 Worker 确认的展示，以及仍缺图片资源编号的展示。上述待核项清零前，保持原绑定。

审计用同一快照里的 Worker 展示队列逐条核对飞书游标之前的事件和消息号。游标领先、缺前序收件或同序号正文不符时会显示 `platform_display_ledger_mismatch`，换绑会中止。先选回同一停写窗口内的两份库。只有尚未尝试的尾项时，按原绑定恢复平台并确认送达，再重新停写、采集和审计。

准备发送时保存完整的目标、正文和幂等号。显示名或渲染参数变化后，恢复仍使用这份请求。取得飞书消息号后先落盘，再确认 Worker 的菜单关联。只缺 Worker 回执时，恢复会沿用原消息号补录。若平台新消息回执仍未知，超过程序的 55 分钟重试窗口即保留待核。已知目标卡的内容更新可向原卡重试同一正文。

图片先上传取得资源编号，再发到会话。上传前记录原图摘要和最早重试时间。回执丢失时在 5 秒、10 秒、20 秒后逐步重试，间隔最多 5 分钟。重开仍按该进度等候，图后的展示一并等待。重试只上传已核对的原始字节，可能多占平台资源。首次取得可用编号即落盘，同一展示只按原消息 UUID 发送。已知编号时恢复直接复用它。飞书公开会话历史未提供发送 UUID 对账字段，无法用相同正文确认超时的原消息。[飞书上传图片](https://open.feishu.cn/document/server-docs/im-v1/image/create.md) · [飞书会话历史](https://open.feishu.cn/document/server-docs/im-v1/message/list.md)

旧版若只有已尝试、无消息号的记录，证据不足时保留原库。能确认原版本、原配置和重试窗口时，再从原版隔离恢复核对。退回尚不会读取 `view_writes` 的软件前，先在能正确读取的版本收齐回执。切回仍用本次已对账的状态副本，避免已发出的试用消息在回退中丢失本地收据。

复跑 `python3 -B -m unittest -v rewrite.test_feishu_output rewrite.test_state_bundle.Bundle.test_feishu_restore_reuses_original_card_and_keeps_known_provider_receipt_after_deadline`。隔离 Worker HTTP 与 SQLite 实跑，飞书服务和宿主使用夹具。换显示名恢复后只有原卡，模拟超过一小时且已有飞书消息号时仅补录 Worker 回执，同条私聊仍只有一次宿主输入。未知平台回执过期与旧包未确认记录均未重发。[实测](evidence/feishu-receipt-restore.json)

图片上传可用 `python3 -B -m unittest -v rewrite.test_state_bundle.Bundle.test_feishu_image_upload_backoff_survives_snapshot_without_reordering_messages` 复跑。快照恢复后，平台替身前两次保存了图片但未返回编号，之后实际展示仍是图前卡、单图、图后卡各一条。切回未读取 `view_image_uploads` 的旧包前先核清该项，以免旧包把仍待退避的上传立刻重试。

用 `python3 -B -m unittest -v rewrite.test_state_bundle` 复验换绑。回环中留一条飞书尚未轮询的旧会话输出，第一次拒绝生成换绑副本。恢复后仅发送一次，重新采集后才允许生成副本。错配 Worker 库的正文和游标也各测了一次。[展示队列审计](evidence/feishu-handoff-display-tail.json)

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

回退到 `3e3293e074d80767f4c74545ca5a90cba349be43` 时，先核 `pending_cleanup` 中因断联入池后发出的取消。该版 Router 以 SQLite 副本实测将这类 W04 拒为 `409 stale_assignment`。若试用产生此类未确认取消，由当前版收清原派发的 W04，再核对工单与部署前快照。有试用业务写入时按对账流程处理。隔离 DSH 清理实测见[原派发取消](evidence/dsh-offline-pooled-cancel.json)。

独立 Codex 只读工位另外核 `audit-handoff` 中的 `cleanup_waits` 和 `router_cleanup`。若原生已请求确认，先让原控制回合结束，由当前版核完整原生记录并收到 Router 回执。复验两项归零后再换绑。旧版没有这条自动确认路径，回退前保留待清理记录、原适配器和宿主记录，按试用写入对账处理。[只读工位实测](evidence/codex-readonly-cleanup.json)

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
