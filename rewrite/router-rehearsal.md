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
