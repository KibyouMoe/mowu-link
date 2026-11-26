# 现役工具替换进度

外层单 t-124f6357c5。程序契约版本 9db82a32。此表逐项记录可替换条件。「实测」限定到表中指定的宿主、平台和接口。只有全表核验、迁移回退演练和溪流独立复验齐全，才能提请生产切换。

## 检查点与证据

独立 Codex 工位可给原派发开启 Linux 可写容器。实际 App Server 的原生工具在容器内写入 691200 字节，经 W04 导出。验收端逐字节读回，重复导出得到原附件号。Router 写入后故意丢回执，Worker 保留原持单和容器，按原报告重试后得到一次通知并回收。另用两张隔离单实测取消和转派。两条原回合都在容器里留下脱离进程组的子进程，原生中止后等待控制回合终止，凭原派发移除旧容器，再交回 W04。转派保留新工位持权。三张单各有一个原生工单输入和一次写命令登记，停机后原账本的待回收数均为零。模型和接替工位由本地脚本驱动。[实测证据](evidence/codex-assignment-workspace.json) · [启用及复跑](desktop-check.md#隔离可写工位)

修复了首次原生工具抢在适配器收件落库前到达的竞态。若相同投递已在原生会话，由适配器核对原输入、原正文和回合后补记凭据。缺证据时保留待核。

另在成果 W04 回执丢失后关停独立 Codex 适配器和 Worker 接口。重建 Worker 后按相同报文与幂等键取回原报告，只有一次验收通知。工作区由原引擎持有，五组件格式 3 快照此时拒绝发布回退状态。原会话和适配器重开后，无新增模型请求，原容器成功回收。再次关停后，同一快照发布五组件回退状态，数据库逐字节与恢复副本一致。离线审计探测由实际容器账本写入进程应答，进程结束前保持待核。从 `7c53afa41d6639b43c808f872f0c318620cebbd4` 构建的旧包启动 Router、Worker 和只读 Codex 适配器。旧版接回同一原生会话，查回原工单输入、待验状态和成果文件摘要，测试私聊同号重投仅发起一次原生模型请求。[冷恢复与两版回退](evidence/codex-assignment-workspace.json)

独立 Codex App Server 可显式启用只读派发清理。原工单写入前登记派发、会话、投递号与内容摘要，按原生只读沙箱运行。对端 Agent 发 W04 后，Worker 持久保存清理请求，原控制回合仍在运行时只回待核。控制回合结束后审计完整原生记录，随后以原派发结清取消或转派。工单取消场景重开 Worker SQLite 后继续等待，转派场景保留新持单。两类反例覆盖原工作回合运行命令、控制回合提出 W04 后又运行命令，Router 都保留旧派发待清理。模型和接替工位为夹具。[原生证据](evidence/codex-readonly-cleanup.json) · [启用及复跑](desktop-check.md#独立-app-server-的-w04-协议验收)

另在独立进程遇到原生已收、凭据未落库的取消竞态。现用原投递查回匹配的输入后再中止。中止观察暂缺时允许重试尚未写入的控制指令，完成后原工单输入仍为一条。通过临时 TLS 启动完整适配器，核对开启 `--read-only-assignments`、查回原收件和 W04 后 Router 标已取消。[进程证据](evidence/codex-readonly-cleanup-service.json)

在同一隔离流程里，等原控制回合结束后先停适配器，此时 Router 仍保持待清理，由 Worker 库保留原请求。从同一 profile 和两个原账本冷开后核对同一控制回合，自动交回该派发的 W04。[冷恢复回执](evidence/codex-readonly-cleanup-service.json)

实现提交 `5c39dcdd1966299659b79fa48906f614f9e73d1a`。macOS 全量 286 项通过，Linux arm64 187 项通过、99 项按环境跳过。254 文件发布包的 SHA-256 为 `d96f87560f2c89b26c5a21398352f84f4759603bf20709accbd34e124a6da613`。安装目录内 41 项专项通过，逐文件回读一致。由前版 `9c3ab5a8ef0cfa1ab92266dea51d371325ae7ca0` 复跑 `rewrite.release_check --component worker-pair`，两版独立进程回退通过，宿主为夹具。Codex 可写工位仍待接。现有工作区模块已在普通 Node 进程内直接调用，隔离 Linux 容器成功写文件、按摘要导出并回收。[工作区模块](integrations/dsh_host/assignment-container.mjs)


Linux 虚拟机内的 Worker 和 macOS 上的 Codex App Server 分别启动，Worker 向 Router 和适配器、适配器回调 Worker 都走经认证的 TLS。停止原 Worker 进程期间，原生回合继续写完测试文件，首次 W04 待确认。新 Worker 进程复用原库、原端口和会话，仍持原派发。同一原生回合重报后只记一次正式成果和一次通知，Router 保持待验收，原生输入一条。无信任证书的客户端连接失败，平台身份查询运维状态返回 403。模型由本地脚本驱动，物理跨机和飞书云端另验。[证据](evidence/codex-linux-worker-restart.json) · [复跑](README.md#codex-独立工位的断联与恢复)

演练提交 `5fab4d63f965f3d13dfa10400a94b8e75a77470b`。macOS 启用跨虚拟机及 DSH 环境后全量 285 项通过，Linux arm64 187 项通过、98 项依环境跳过。251 文件的发布包摘要为 `595300264b224974198c899b178fe4ad41b1ffb8e03af44295458ab5824f3f5a`。从安装目录实跑包括跨虚拟机重启、两套原生断联的 39 项回归，逐文件摘要回读一致。

Codex 侧 A10/A11 用桌面安装包内的真实 App Server、独立 profile 与隔离 Router 验证。两种断联期间同一原生回合都实际完成测试文件写入。断 Router 后首次 W04 在 Worker 存为待确认。原回调因 TCP 重置无 HTTP 响应，现返回可重试的 `503 worker_report_unconfirmed`。原派发入池后，重连重放得到 `stale_assignment`，无正式报告或成果通知。只断适配器时 Router 继续看到原持单，恢复后同回合正式提交，Router 待验收，仅一份成果和一条通知。两次各一条原生输入、一张测试飞书断联卡。模型、计时和飞书均为夹具。另用短断链证明原 W04 可从待发队列交回，只通知一次。[证据](evidence/codex-worker-link-outage.json) · [复跑](README.md#codex-独立工位的断联与恢复)

实现提交 `c9dc01d37b0fd4d153eb3b1722cc0c55a9e4f2e9`。该检查点 macOS 284 项通过，Linux arm64 187 项通过、97 项依环境跳过。与前版 `e233f831f583268dfeab7962d4e490a104a51923` 的 `rewrite.release_check --component worker-pair` 隔离两版回退通过，同一私聊各阶段只保留一个原生输入，宿主为夹具。

修复失联入池后取消漏发。修复前隔离 Router 立即变成 `cancelled`，原工位 W01 回 `no_task` 且没有停止指令。修复后真实 DSH 原容器继续运行，Router 只记录旧派发待清理。到重连时，原生回合因取消中止、容器移除，Agent 调用 W04 后 Router 才从 `blocked` 变为 `cancelled`。保留原工单输入一条。另用隔离协议测试核原回执同号重放只发一次通知，迟到清理只能影响旧派发并保留新持单。原生 DSH、Linux 引擎和 Router 实跑，模型、时间和飞书为夹具。[取消记录](evidence/dsh-offline-pooled-cancel.json) · [复跑入口](README.md#dsh-固定会话与飞书卡片)

实现提交 `2c0d3dfb60d908bec55cf2d9815a8c5c96b8edce`。macOS 全量 282 项通过，Linux arm64 186 项通过、96 项依环境跳过。248 文件的包摘要为 `ae41fee8c5363d5490a768186eb077d283c760978750858bbe511c72ebbbef6a`，安装目录专项 36 项通过，逐文件摘要回读一致。用前版 `3e3293e074d80767f4c74545ca5a90cba349be43` 复跑 DSH 版间回退通过。另装同一前版 Router 对暂存的入池取消实测 W04 返回 `409 stale_assignment`，当前版对同一派发接受回执。回到该前版前须由当前版收清这类待确认取消。[回退步骤](router-rehearsal.md#worker-和适配器逐个升级)

Worker 篇 A10/A11 用安装版 DSH 原生回合、Linux 容器和隔离 HTTP 实测。仅断 Router 时，原回合继续调用工作区工具并写入文件。Router 离线满 10 分钟入池，Worker 从确认断联起满 20 分钟重连，原容器及进程保持工作。之后明确转派，旧回合才中止并交原派发清理回执，新持单保持原样。仅断适配器时发现 W03 已接单、Router 目录仍为空的问题。W03 现与工单接管原子记录持单，实测断联期间本体持续在线、忙闲未知且原持单可见。恢复后同一回合可继续写入，明确取消才停止和清理。两项各只有一次原生工单输入和一张测试飞书断联卡。模型、时钟、飞书及接替工位为夹具。[实测](evidence/dsh-worker-link-outage.json) · [复跑入口](README.md#dsh-固定会话与飞书卡片)

实现提交 `4af754a6547f90aa0efd64fc88e64d264cbe9896`。macOS 全量 280 项通过，Linux arm64 184 项通过、96 项依环境跳过。247 文件的包摘要为 `3557f3a8b7a6de78e6e7f750cad493e9654cb42d9427cf0463d83f35013b4147`。安装目录专项 34 项通过，逐文件摘要回读一致。用前版 `2a96a3dc717394b95c72852d24a4e9a1497ad67b` 复跑 `rewrite.release_check --component dsh-workspace-rollback`，原工作区回收后前版接回原会话、成果附件和一次私聊成功。

版本回退也核对原 DSH 工作区。W04 已确认且持单为零时，实测活容器、宿主未停、遗漏原目录及提供不符目录均拒绝发布旧版状态。普通数据恢复可保留同机冷恢复的副本。真实 DSH 接回原会话按已确认 W04 回收后，原快照逐字节恢复五组件状态。随后从旧版包启动 Router、Worker、DSH 适配器和 DSH 宿主。旧版原工单、单次通知和成果附件读回一致，新私聊同号重送仅一次原生收件，原工单工具没有重跑。实际 Linux 容器及 HTTPS 进程实跑，模型由本地脚本驱动。准备途中新增工作区记录的反例同样中止回退。[两版实测](evidence/dsh-offline-workspace-handoff.json) · [复跑入口](README.md#dsh-固定会话与飞书卡片)

复跑用旧版 `a469849b7815aaaf9eb6e9be504afac3c2ae4e4b` 和实现版 `4159eed4b66eb0fcbf18a2c215608e458924d493`。原工单使用隔离 HTTP，回退后旧进程使用隔离 HTTPS。两版发包、安装及运行检测连续通过 3 轮。命令行另核格式 3 缺原运行目录时返回 1，核过原目录后发布状态目录。实现版发行包含 246 文件，SHA-256 为 `c2a0121747394a4f62e4dedd79b2913158704faca766ff6c013293130b4928ba`。安装目录专项 38 项通过，文件摘要回读一致。macOS 全量 279 项通过，Linux arm64 184 项通过、95 项跳过。另用上一提交与实现版复跑原有 `--component worker-pair`，夹具宿主的收件回退也通过。


离线换绑补查 DSH 原工作区账本。用真实 DSH 和 Linux 容器复现 Router 成果已落库、Worker 已放回持单、容器仍运行。新审计识别缺原目录、错误目录、符号链接记录、未回收容器及原宿主仍运行。原会话重开后查到旧 W04 并移除原容器，无新增模型调用。宿主再停机后，原 SQLite 快照无需重拍即可经当前账本核验准备新绑定。另用账本夹具证明准备途中出现新记录会撤销输出。启用工位的适配器账本登记后，快照用格式 3 以免旧版换绑工具误放行。真实引擎、宿主和 HTTP 实跑，模型为本地脚本。[实测证据](evidence/dsh-offline-workspace-handoff.json) · [复跑与操作](router-rehearsal.md#重做版五组件的离线恢复)

从 `a469849b7815aaaf9eb6e9be504afac3c2ae4e4b` 安装的旧审计工具读取格式 3 实测返回 1，原因为 `unsupported snapshot`。新版命令行在缺原目录映射时报告 `dsh_workspace_journal_required`，传入停机原目录后读回零待回收项并生成目标绑定。复制 SQLite 库本身仍由恢复命令处理，活工作目录继续由原引擎持有。

实现提交 `588f81193df8a7ba5bb7a6b81cd99c29a897f720`。macOS 全量 279 项通过，Linux arm64 184 项通过、95 项按环境跳过。离线换绑和准备期变化两个测例各连续复跑 3 次通过。以实现提交打包 246 文件，SHA-256 为 `a220c60625c7ab6c40ffa8bd4b8e7154fa23e3e777b671adbadb4147bb6ca722`，安装目录专项 38 项通过，逐文件回读一致。另复跑 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous a469849b7815aaaf9eb6e9be504afac3c2ae4e4b --new 588f81193df8a7ba5bb7a6b81cd99c29a897f720`，夹具宿主的两版安装回退通过。



DSH 独立项目工位可显式授权新派发的 `/workspace` 运行本地构建产物。实际 DSH、Linux 容器、C 编译器和隔离 Router 跑两张对照单。默认工位生成 ELF 后直接执行返回 126，W04 进入受阻。显式启用的工位运行返回 0，验收人读回逐字节相同的 ELF，W04 待验收。两单交权后各回收原容器。既有派发在启用和停用两种方向的配置变更下均拒绝继续写入，可暂存原产物并按原挂载记录完成取消清理。容器以普通用户运行，使用只读根、无网络和原资源上限。模型由本地脚本驱动。[编译与挂载实测](evidence/dsh-workspace-compiled-project.json) · [复跑入口](README.md#dsh-固定会话与飞书卡片)

实际从 `256db0bf87aa46322df70e837f91c1d041e594b4` 和 `132311abb9b5c56b96b28c08f9b2d58b9ddacfc8` 打包安装两版 DSH 容器管理模块。旧版拒绝确认仍在运行的新挂载容器，路线恢复为新版导出 ELF 后按原容器移除。恢复旧版配置后新派发可编译，直接执行返回 126，清理返回 `removed`。此项用两版真实模块和同一隔离引擎直测。正式工单的宿主调用另见上述 DSH 原生回环。

实现提交 `132311abb9b5c56b96b28c08f9b2d58b9ddacfc8`。macOS 全量 277 项通过，Linux arm64 实跑 183 项，按环境跳过 94 项。从此提交打包 244 文件，SHA-256 为 `170ec0b814ee85c474ceaab3ed9f77c38ca20559fa7cc1daf88ea7508d2f22eb`，安装目录专项 36 项通过，摘要回读一致。另复跑 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous 256db0bf87aa46322df70e837f91c1d041e594b4 --new 132311abb9b5c56b96b28c08f9b2d58b9ddacfc8`，两版 Worker 与适配器在夹具宿主上可按原收件回退。


同机冷恢复演练使用真实 DSH 会话持久化、Linux 引擎、HTTPS Worker 与适配器进程和独立 Router。一张正式单写入项目文件后保留原持单，关停三个进程并复制两份停写账本。重开后同一派发只收到一条工单更新，模型导出原容器中的改动并交 W04。Router 落成果，Worker 此时回执待核。三个进程再重开，Worker 以原报告和原幂等键确认，DSH 回收原容器。原始写入工具只跑一次，最后重开后的模型请求为零。验收端只收到一次通知。宿主、引擎和网络实跑，模型为本地脚本，飞书云端未使用。[冷恢复证据](evidence/dsh-workspace-cold-recovery.json) · [复跑入口](README.md#dsh-固定会话与飞书卡片)

演练提交 `ca032a7854771a6614d90ecaac468de6fc27ed6d`。macOS 全量 275 项通过，Linux arm64 实跑 183 项，按环境跳过 92 项。冷恢复场景连续复跑 3 次通过。243 文件的发行包摘要为 `7001d16bf9ced99a406d43c59dfb79312da7ac73d01da00fdbc17ea513cc6588`，安装目录专项 34 项通过，文件摘要回读一致。用 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous e16429e1ae3b2e60448ae27b225753bd810c58e0 --new ca032a7854771a6614d90ecaac468de6fc27ed6d` 补验两版安装和回退。该命令的宿主使用独立夹具，原生 DSH 参加上面的同机冷恢复。


另外用同镜像、普通用户、只读根文件系统、无网络和相同资源上限的两个一次性容器编译同一个测试 C 程序。当前工作区参数挂载为 `noexec`，编译成功，直接执行返回 126。显式给一次性工作区加 `exec` 后，运行返回 0 并输出 `isolated executable works`。正式 DSH 工位的可执行构建流程仍待接入和实测。


隔离 DSH 工位从经审核的 Git 归档给两张工单分别创建可写项目副本。首次导入的 CLI 成功回包丢失后，原工具报告待核，重试在同一容器内使用已经完整导入的目录。两张单都从原始内容开始，执行快照中的测试脚本，交回改动文件，并按各自的 W04 回执回收容器。源目录未修改，待验收各有一条通知。独立反例核对摘要不匹配、越界路径、符号链接和硬链接均不发布项目目录。重启原容器后，写入和导出都被拒，未发成果，原派发按取消收尾。实际 DSH 宿主、Linux 引擎、Git 及 Router 实跑，模型为本地脚本。[项目快照与故障实测](evidence/dsh-workspace-project-seed.json) · [复跑命令](README.md#dsh-固定会话与飞书卡片)

实现提交 `6f86d4da953408ed1122234de4a7b18247425c54`。macOS 全量 274 项通过。Linux arm64 实跑 183 项，按环境跳过 91 项。导入丢回执与引擎重启分别连续复跑 3 次通过。以该提交构建 242 文件的发行包，SHA-256 为 `873f10222be0f82896112859bbd4d6431c2a7ae48fa5a88bd03a5d0fc240bfac`，安装后 33 项通过，安装树摘要回读一致。用 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous 0d9f82b675c8d0d4a724733a34ca75d9e26168b8 --new 6f86d4da953408ed1122234de4a7b18247425c54` 跑通隔离升级和回退，夹具宿主各阶段保持一条原生收件。限定项目规模与类型见运行说明。当前引擎挂载工作区实测为 `rw,nosuid,nodev,noexec`，运行项目脚本可走镜像内解释器，依赖直接执行构建产物的项目仍需验证。


DSH 容器工位通过原生工具导出 691200 字节的二进制文件。第一块上传回执丢失后按原偏移续传，验收人只在 W04 写入后读到文件，字节和 SHA-256 均匹配。符号链接被拒。Router 的 W04 已记账、Worker 回执在途中丢失时，容器保持运行，原绑定不能切换。重建 Worker 后以原报告和原幂等键取回回执，Router 仍为待验收且只发一次通知，原生输入仍为一条，原容器在回执落库后移除。真实 DSH AgentLoop、Linux Docker、SQLite 和测试 HTTP 服务实跑，模型由本地脚本驱动。[原生证据](evidence/dsh-workspace-artifacts.json) · [复跑入口](README.md#dsh-固定会话与飞书卡片)

实现提交 `f33f68f6ee7ddf5f403f335c426497643aa2388a`。macOS 全量 271 项通过。Linux arm64 实跑 183 项、按环境跳过 88 项。原生附件测试连续复跑 4 次通过。从该提交构建并安装 240 文件的发行包，校验摘要为 `5a420fc9ae9457ac4ce307de9d5d4ea067e256c94a0f16862fa0ad9701cb707d`，安装后专项 31 项通过，回读安装包校验一致。用 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous c448c2ff36a40e679d9c6968912e56299c158c34 --new f33f68f6ee7ddf5f403f335c426497643aa2388a` 验证 Worker 与适配器的隔离升级和回退，原生收件始终一条。此演练的宿主是夹具。仍需核对普通项目工作目录、实际测试账号、飞书云端和跨机工作区恢复。



DSH 的可选 Linux 容器工位按派发隔离可写工作目录。真实 DSH 调用原生工具写入文件并留下脱离命令进程组的子进程。实际 Router 发取消或转派后，只移除旧派发的容器，DSH 用 W04 确认原回合停止与清理。另一条私聊仍入原会话一次；转派后的新处理权保留。另验证明字段里的引擎身份不符时，原回合中止，Worker 保留待核，旧容器与文件未被清理。宿主、Linux Docker 引擎、Worker 和 Router 实跑，模型和新接单端由夹具驱动。[原生证据](evidence/dsh-assignment-container.json)

复跑 `MOWULINK_DSH_CONTAINER_IMAGE=<已有镜像完整ID> python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_dsh_workspace_container_cancel_and_transfer_remove_owned_processes`。容器工作目录在内存里。普通项目读写工作树和生产切换仍待验。

取消、转派和引擎身份不符三种情况连续通过五轮。macOS 全量 270 项通过，包含在本机 Linux Docker 上复跑此验证。Linux arm64 通过 183 项、跳过 87 项。以 `6a709a552b3812455fbccef71eadcb43a130e007` 构建 239 份文件，SHA-256 为 `97d5c4947be0dd9bb545e140d9e5ae49b37e392cd5adfff93b990db5ac4ca397`。从安装目录通过 DSH 原生停止、只读及容器清理、快照和发布包共 37 项，文件摘要一致。以 `b88a3b548f4d45cb8bd6da8d07308f721aefb109` 为旧版，默认 Worker/适配器在隔离 HTTPS 演练分步升级与回退，三阶段各保留同一条夹具宿主收件。复跑 `python3 -B -m rewrite.release_check --repository . --component worker-pair` 并指定上述新旧 SHA。运行中的容器工位须先收尾后才可回退。

Codex 已把收到的原生审批关闭通知保存到适配器账本。基线 `b4f8c521da38620cadd5260ad662c9ff2d9dc8b1` 的原生测试回合已经中止，Router 仍只有登记，重开八秒后仍未补交。新版用原 Worker、原会话和登记摘要补原关闭，第一次补交回执丢失后按原号查回，实际只有一次补交、零新增通知、零原生决定重放。已落库的另一条只读原回执。丢失原生关闭通知的一条继续待核，三个回合的原生历史一致。隔离 App Server、Worker、Router 与数据库实跑，模型、时钟、网络和事件丢失使用夹具。[原生关闭实测](evidence/codex-durable-approval-resolution.json)

复跑 `python3 -B -m unittest -v rewrite.test_service.Service.test_recovered_codex_resolution_requires_the_original_registered_request rewrite.test_codex_app_server.CodexAppServer.test_cold_adapter_reads_committed_approval_closure_without_native_replay`。同时验证错会话、错回合、错条目或无原登记均拒绝补交。Router 从未登记、适配器也已退出时仍缺可核原请求，继续待核。

macOS 全量通过 269 项，Linux arm64 通过 183 项、跳过 86 项。两个专项复跑连续通过五轮。并行测试曾有一次四秒等待未收到原生终止事件，回归改用十秒并附失败诊断。以 `fb0f35cb7131e1edb3b48b6910b13c1e4814d348` 构建的独立包包含 237 个文件，SHA-256 为 `2f05796ba0993a4c9d71bef65bf639022c7a8266e789223c283f086b7b7faed7`。安装目录通过审批、切换、快照及服务的 44 项复验，文件摘要一致。用新旧安装包在同一隔离 SQLite 审批库上演练升级、回退并写入另一条原始审批、再升回去，三条原始审批和一条关闭凭据均保留。库升级演练注入原生回执。实际隔离 App Server 的独立发布演练中，三个阶段均只有原先的三条输入，待验通知 `943b13c0-ddc7-4e39-94b9-aa91d4bda44a` 保持不变，使用本地脚本模型。复跑 `python3 -B -m rewrite.codex_release_check`，指定上述提交、旧版 `b4f8c521da38620cadd5260ad662c9ff2d9dc8b1` 和本机 App Server 二进制。

Codex 适配器冷重开可从原 Worker 查回 Router 已保存的审批关闭回执。基线 `18238d2bd40892d4f74ac06cb8053383971ee0fd` 的真实 App Server 测试在 8 秒内仍未补回已落库的关闭。新版用原 Worker 注册、原生会话、审批号和请求摘要取回相同摘要。另一条丢在 Router 落库前的关闭仍待核。两条原生回合均未重跑，重开后新增原生决定和审批通知均为零。实跑独立 App Server、Worker 与 Router，模型、时钟和丢包由测试驱动。另验错绑、错误摘要、已更换的 Worker 注册和其他身份均不能读到回执。[冷重开证据](evidence/codex-cold-approval-reconcile.json)

复跑 `python3 -B -m unittest -v rewrite.test_service.Service.test_lookup_returns_only_original_closed_approval_receipt_digest rewrite.test_codex_app_server.CodexAppServer.test_cold_adapter_reads_committed_approval_closure_without_native_replay`。对关闭尚未写入 Router 的扩展，见本页最新检查点。

两项连续通过 5 轮。macOS 全量通过 268 项，Linux arm64 通过 182 项、跳过 86 项。以 `3eab3960c393cfab0d839f91a37f47d79c986b76` 构建 236 份文件的独立包，SHA-256 为 `b39dab9971ecd361f7f83aadeb2f172437bdbbe8d8ce63051ff2224e5c1fc892`。安装目录复跑审批、切换、快照与服务 42 项通过，文件摘要一致。另在临时 TLS 端口演练旧 Router 写入并关闭审批 `5a4014ae-fe00-40f3-9437-cf92aa3e596e`。将停写库复制给新版，按原审批号核到关闭摘要 `473bb1c31a48f91768ce0678f432eb9c98ce6cca1b8f715c0f3c56b710f6b6a1`。在原试用库上起旧包后仍读回同一关闭。三阶段各一条原通知，原库和回退试用库的摘要均未变。现场使用两版锁定安装包与合成审批，无原生动作。

运行中换绑核对 Codex 和 DSH 适配器的原生审批账本。基线 `a3d421ad737b6179789b3138c64f4444b47e6b18` 在两个隔离反例中都写入了新绑定。一类是原生已空闲但 Router 尚未收到登记，另一类是派发暂停后才收到原审批。改版在第一类从未请求暂停，第二类以原交接号解除，丢解除回执后重开仍保留原绑定。旧适配器不支持审批观测时保留普通聊天并提示先升级。真实 Codex App Server 两会话实测，审批补记关闭后，新切换后的目标只新增一次输入。真实 DSH SDK 同样验证未登记和已登记但回执丢失的原审批，源宿主恢复并核到关闭后，允许新切换。DSH 目标宿主、模型、飞书、审批人和故障均为隔离测试夹具。[原生证据](evidence/native-handoff-approval.json)

复跑 `python3 -B -m unittest -v rewrite.test_worker_resume rewrite.test_codex_app_server.CodexAppServer.test_runtime_switch_waits_for_unregistered_native_approval_to_close rewrite.test_dsh_native_loop.NativeLoop.test_dsh_unacknowledged_approval_recovers_without_a_late_notification`。原生应用授权等未接入的审批仍需另验。

五个场景连续通过 5 轮。macOS 全量通过 266 项，Linux arm64 通过 181 项、跳过 85 项。以 `6cc50b6cf5a29346e8b7f4a1b65df6f3cf83ca8e` 构建的独立包包含 235 份文件，SHA-256 为 `e186cda8b4b1834ac7992c94ed1c03a23bb69524183d375db9a1c25decee73ba`。安装目录复跑切换、暂停派发、快照与两种原生审批共 33 项，逐文件摘要一致。以 `a3d421ad737b6179789b3138c64f4444b47e6b18` 为旧版，真实隔离 Codex App Server 执行混合版本、升级与回退。各阶段仍为三条原生输入，待验通知保持 `93b45c7c-e9cd-46e8-a189-17debb40e33e`，快照摘要一致。发布演练使用脚本模型，复跑 `python3 -B -m rewrite.codex_release_check` 并指定这两版和本机 App Server 二进制。

从当前本机 App Server 实际导出的协议模式（含实验项）核到 `serverRequest/resolved` 为通知，未列出按审批请求号查询关闭结果的客户端方法。Codex 宿主与适配器同时重开后，若此前无可确认的关闭回执，仍保留待核。此处的 Router 已保存关闭场景见本页最新检查点。

离线换绑会核对 Codex 适配器已收到的原生审批。隔离实测，原生文件审批已到、Router 尚未收到登记时，适配器持久保存待核记录。重开账本仍拒绝生成换绑副本。Router 已关闭但回执丢失时记录继续待核，按原号查回才消除。独立 App Server、Worker 和 Router 实跑，模型与断联由本地测试驱动。快照换绑的夹具复验另查了输出中无命令目录。复跑 `python3 -B -m unittest -v rewrite.test_state_bundle.Bundle.test_handoff_refuses_codex_approval_never_registered_with_router rewrite.test_codex_app_server.CodexAppServer.test_native_approval_deadline_survives_lost_intake_and_late_router_accept`。[断联证据](evidence/codex-approval-router-outage.json)

两项连续通过 5 轮。macOS 全量通过 262 项，Linux arm64 通过 178 项、跳过 84 项。以 `79623b2bb412ffa5f9dc80901707e39a201ef68d` 构建的独立包包含 234 份文件，SHA-256 为 `1eaf80916d55246b3be409c4c1d69a83a5ad1698aeab5348f8e91ec175b2a245`。安装目录复跑快照、发布包和 Codex 断联审批共 17 项，文件摘要一致。以 `a2cc41fbfe65e0cdf1a98c13bbbcdbbdc826987c` 为旧版，在真实独立 Codex App Server 演练混合版本、升级与回退，各保留三条输入，新增为零，待验通知 `e794961d-f566-47fe-a710-854fc6b94211` 原号不变，旧快照摘要一致。使用本地脚本模型，审批转接专项另测。复跑 `python3 -B -m rewrite.codex_release_check`，指定上述新旧提交与本机 App Server 二进制路径。

DSH 的普通 Bash 审批支持省略 `workdir` 和相对目录。旧版 `b88116e7cfd68765f41ecf098c1724ed43a1af35` 的同一默认目录请求在六秒内未到 Router。新版按同一次原生准备、审批与执行定位，保存原参数与宿主解析的绝对目录。连续六轮核实默认、相对和带符号链接父级的真实 `pwd` 输出。等批准期间换目录，原生拒绝执行。审批已允许但进执行前换目录，原生回合留下一次工具错误，未启动 Bash。将目录绑定到本次调用后，再改会话目录时实际命令仍在获准目录内。无法核实的目录仍走宿主审批。实跑 DSH AgentLoop、ApprovalService 与 Bash，模型、审批人与目录竞态由隔离测试提供。[原生证据](evidence/dsh-approval-native-working-directory.json) · [复跑](README.md#独立宿主命令审批)

macOS 全量通过 261 项，Linux arm64 通过 177 项、跳过 84 项。以 `1d8cce41f11be4b64af26d7dfc258ab716ffd5ce` 构建 234 文件独立包，SHA-256 为 `ad53a5c92485c8eff4d4383e2ccc5c33f4d89559c82ee74e78e42d49a1e3fd96`。安装目录复跑 DSH 原生审批、断联恢复、Router 服务与发布包共 15 项，逐文件摘要一致。

DSH 冷开后按原生凭据补记审批关闭。旧版 `d73e9585bbb224f397dbca2b0a53e94aa64abe2e` 在原生审批 `ccf9fe3b-c6b4-48ae-8750-392013f7558c` 已取消后仍发出通知 `44380a98-ef8e-4249-9bf1-62ab2e212711`。新版连续六轮分别验到登记从未送达、登记已落账但回执丢失。重开 DSH 与适配器后，Router 一次保存同号原生取消凭据。关闭回执丢失可按原号重试，迟到登记不生成新通知。第二种情况保留既有允许决定，原生结果仍为取消，两种均无 Bash 执行。DSH AgentLoop、落盘日志、Worker 与 Router 实跑，使用本地模型、操作人和断联夹具。[实测与命令](evidence/dsh-approval-router-outage.json) · [复跑](README.md#独立宿主命令审批)

macOS 全量通过 260 项，Linux arm64 通过 177 项、跳过 83 项。从 `536ae99fed5a41166772745f74c734faa6a38463` 构建 233 文件独立包，SHA-256 为 `2acac7736cdfa9705d8269dbf78bfd91ba36fdec9c19c98207188ac135d21560`。安装目录复跑原生审批、Router 服务和发布包共 14 项，逐文件摘要一致。升级实际上一版 Router 创建的临时库，原审批逐字段一致。新版写入关闭凭据后，用上一版读取仍为已关闭，同号登记没有补发通知。这次库升级的原生凭据由夹具提供。

Codex 审批断联后由原生适配器独立计时。基线 `b8d986558dffbf89cab8c80f90266aee85ec576b` 在收到文件审批后，Router 登记受阻，模拟十分钟到点又观察三秒，宿主仍未结束。新版连续六轮在原 HTTP 请求仍被冻结期间中断原回合，文件仅保留原字节，命令没有执行。文件审批从未登记，恢复后按原号直接补记已关闭，丢关闭回执再提交只读原记录，迟到登记零通知。另一组故意扣住 Router 的允许回执，恢复后保留操作人原决定和宿主超时取消两份记录。使用真实独立 App Server、Worker 与 Router，模型、操作人、时钟推进和故障为隔离测试输入。[断联证据](evidence/codex-approval-router-outage.json) · [复跑](README.md#独立宿主命令审批)

macOS 全量通过 258 项，Linux arm64 通过 176 项、跳过 82 项。以 `8d6ee21b9fca1aac2e9c60b16928c43604c422d1` 构建 232 文件独立包，SHA-256 为 `321c3af25a21f4896126251c4c5966f1c51e17c69ed0848a673d08615cfb82a7`。安装目录复跑原生审批、服务和发布包共 14 项，逐文件摘要一致。

Codex 文件审批已接上原生条目与 Router 单次决定。独立 App Server 连续六轮实测，授权 CLI 读到同一回合的完整差异，正确摘要允许两个临时文件各改一次。错摘要、其他工位和会话级允许被拒。缺差异或目录授权请求按原号取消，未进 Router，飞书展示链未收到差异。模型和操作人用本地夹具，缺差异与目录授权由测试注入。[实测与复跑](evidence/codex-app-server-file-approval.json) · [操作入口](README.md#独立宿主命令审批)

macOS 全量通过 256 项，Linux arm64 通过 175 项、跳过 81 项。以 `3846ff95ecc19c21c61481992189267cce1d5475` 构建 231 文件独立包，SHA-256 为 `132cf201de7ef89d5d946411396e92c695ff1f93903691b6a428e93d85e4129c`。安装目录复跑原生审批和服务共 11 项，逐文件摘要一致。另从安装包启动三段隔离 HTTPS，用实际 Codex 适配器的 `--relay-file-approvals` 收到审批 `e11518cc-2ee7-49fe-848c-d3a85b1068d6`，CLI 放行后冷读原生回合 `01a2188a-8718-7e90-b526-86fb73a9b7e9`。等待审批时首个聊天回执为空，批准后按原消息号找回同一输入，重投未新增回合。使用本地模型和独立测试身份。Desktop 应用授权、原生 Hook 与额外权限待验。

Hermes 插件的 Router 收件与原生输入独立推进。旧版在断开 Router 后 23 秒内没查回已收的 Event。新版连续三轮在 Router 仍断开时卸载并重载插件，从隔离 Hermes 的原生台账查回带附件 Event 的原收件号。每轮已入箱的 Notify 等原会话空闲 10.89–10.92 秒后送达，Info 零注入。已核实的附件元数据可以重用，仍缺附件的 Event 暂缓，同批普通 Event 可先交。使用实际 Hermes 插件、会话台账及 Router HTTP。模型用夹具，断链由隔离测试注入。[实测](evidence/hermes-router-outage.json) · [复跑](README.md#hermes-专用会话)

macOS 全量通过 255 项，Linux arm64 通过 175 项、跳过 80 项。以 `e54f9c825487d75c3b32bcb773a6bbd4c0c70850` 构建 229 文件独立包，SHA-256 为 `3c369309622bb1a30efd74df6e6e193532e0caf334434f058c5f83cca3474dd2`。安装目录通过队列、附件、旧订阅回退与组件快照等 32 项，实际 Hermes 插件另通过一次断联重载，找回原收件 `6083f780923848ec9870625d094450ba`，文件摘要复验一致。旧版本回退夹具取自 `30f61e671465db0b5bba221ba8c9fc2194a6e43d`，用 `MOWULINK_TEST_LEGACY_ROOT` 指定。

运行中切换在探测目标前持久记下原请求号。目标暂不可达时，重开仍查得到交接号，授权运维可用原号撤回。实测原工位在等待期间接到一张正式单，旧宿主回调仍有效，Router 暂停接口调用为零。撤回后迟到的原命令只读原取消回执，等待的私聊只到原会话一次。目标会话零收件，重复撤回或迟到的开始请求沿用原回执。实际 Router、Worker HTTP 与 SQLite，飞书和宿主用夹具。[实测](evidence/feishu-resume-preflight-cancel.json) · [复跑](README.md#飞书切换会话)

复跑 `python3 -B -m unittest -v rewrite.test_worker_resume.WorkerResume.test_cancel_unreachable_switch_before_router_fence_survives_restart`，连续通过 10 轮。macOS 全量通过 254 项，Linux arm64 通过 174 项、跳过 80 项。以 `2e12d73ff5ab167b76f9c0afb46f25a06eeb7ce4` 构建 228 文件独立包，SHA-256 为 `36e9d19b69d2b22b2f5e631161d110d56e569be4f5672905e53232b3a13c6568`。安装目录通过切换、组件快照、回退和飞书收发共 52 项，文件摘要一致。旧版本回退夹具来自 `30f61e671465db0b5bba221ba8c9fc2194a6e43d`，用 `MOWULINK_TEST_LEGACY_ROOT` 指定。

飞书入箱的换绑命令按收到顺序确认。旧版复现目标会话暂不可达时，后发消息误进原会话。新版在入箱中留住后续消息，旧会话已存的回复卡照常送出。重开后仍等待原切换命令，目标恢复后后发消息只进目标宿主一次。另验早于切换的重试消息先送原宿主，以及从旧库和回退后旧版写入记录中重建切换顺序。实际 Router、Worker HTTP 与 SQLite，飞书和宿主使用夹具。[实测](evidence/feishu-resume-inbox-order.json) · [操作说明](README.md#飞书切换会话)

两项新回归连续通过 10 轮。macOS 全量通过 253 项，Linux arm64 通过 173 项、跳过 80 项；Linux 另跑官方 SDK 收发和重试耗尽恢复。从 `112805e7ac4e18c0f1464d03bf924f4377cfc0e5` 构建 227 文件独立包，SHA-256 为 `0b6a522b7ddc193ae3df3d35aa8d8b8d88a5aa90b588d7632ea0bfdca5a2bcaf`。安装目录通过 `rewrite.test_state_bundle`、`rewrite.test_worker_resume`、`rewrite.test_feishu_inbound`、`rewrite.test_feishu_output`、`rewrite.test_question_cards` 的 42 项及 SDK 两项复验，文件摘要一致。

离线换绑现在核对 Worker 展示队列和飞书游标。旧审计在 Worker 已写旧会话回复、飞书尚未轮询时误报账本已清。隔离复验把同一条尾项按原绑定补投一次，积压从 1 变 0，然后才生成换绑副本。配对库同序号正文不一致或飞书游标领先时均报错。实际 Worker HTTP 与 SQLite，飞书和宿主用夹具。[实测](evidence/feishu-handoff-display-tail.json) · [恢复与复跑](router-rehearsal.md#飞书输出回执与换绑)

换绑与错配场景连续通过 10 轮。macOS 全量通过 251 项，Linux arm64 通过 171 项、跳过 80 项。以 `02de55e3798e74964a02e433c3672ed67643fdc2` 构建 226 文件独立包，SHA-256 为 `25c1de0dbf6cdf47496c19cda89ea02f496b83d086ab3fc5a904c3f76461c1f6`。安装目录运行 `python3 -B -m unittest -q rewrite.test_release rewrite.test_cutover rewrite.test_state_bundle rewrite.test_feishu_output`，通过 38 项，文件摘要一致。旧版本回退夹具来自 `30f61e671465db0b5bba221ba8c9fc2194a6e43d`，用 `MOWULINK_TEST_LEGACY_ROOT` 指定。

有限重连次数用完时，飞书收件进程记一条不可用日志，等待至少 30 秒且不短于平台下发的间隔，再经原 SDK 重新连。隔离端点两轮均回 HTTP 503，随后同一进程成功连上。断线前的消息已经送进宿主，平台后来重发只查回原 Worker 收件凭据，新消息也只投了一次。明确下发零次重连时仅有首次取址，日志标明停止收件。官方 SDK 1.7.5、实际本地 HTTP/WebSocket 与 Worker 路径，平台和宿主使用夹具。[实测](evidence/feishu-sdk-retry-exhausted.json) · [复跑](README.md#飞书入口)

该场景在 macOS 连续通过 10 轮，在 Linux arm64 复验通过。另验原 SDK 对照、平台主动停止、单纯 TCP 断链与取址无响应。macOS 全量通过 249 项，Linux arm64 通过 169 项、跳过 80 项。以 `239e5d59391f6bdb849104fec2f17c49dcdd4ab3` 构建 225 文件独立包，SHA-256 为 `8e7368f3b4b49c7e9473a72c4e2785677c6fb517df211c5e38ff2e6d424a3703`。独立安装目录内通过 SDK 收件、重连专项与 31 项回归，复核文件摘要一致。飞书云端消息号与真实 Codex W04 仍待验收。

飞书 SDK 断线后，重连取地址的 HTTP 请求没有响应时，原始 SDK 1.7.5 进程仍活着，连续观察 3 秒没有发出下一次请求。配置连接和读超时后，在本地以 0.5 秒测试值重连，同一进程继续收件。旧消息发两次，新消息发一次，三次均回 200，Worker 宿主共收到两条原输入。实际 SDK、HTTP、WebSocket、Worker 和 SQLite，飞书平台及宿主为夹具。保留飞书下发的重试间隔，平台进程使用 15 秒的连接和读超时。未验飞书云端重连策略。[断线证据](evidence/feishu-sdk-discovery-timeout.json) · [复跑](README.md#飞书入口)

macOS 的取址恢复连续通过 10 轮，间隔 0.501–0.504 秒，Linux arm64 独立复验 0.508 秒且原事件仍只入队一次。SDK 题卡夹具经实际发卡路径建立存档，重复点卡只留一条答案，关卡后提交原表单被拒。macOS 全量通过 249 项，Linux arm64 通过 169 项、跳过 80 项。从 `1461cde39fc17ca612e97a89d63107aa6d6d3b0b` 构建 224 文件独立包，SHA-256 为 `e510f7163015034e84e5e4c157ee052228e872b1238120f0fc5b0a6e56d02c3a`。安装目录通过 SDK 收发和断线复验及 31 项相关回归，逐文件摘要一致。

原 SDK 对照中，本地端点设定只重试一次并回 HTTP 503，SDK 后台抛出 `ServerUnreachableException` 后进程仍存活。随后观察 3 秒，没有第三次取址。[SDK 重连实现](https://github.com/larksuite/oapi-sdk-python/blob/v1.7.5/lark_oapi/ws/client.py)

飞书图片资源上传与会话展示分开确认。上传开始前保存原图摘要和退避时间，快照恢复后沿用同一等待进度。平台替身连续两次存图但丢编号，之后会话仍只有图前卡、单图、图后卡各一条。待核上传从 1 变 0。三次调用上传了相同原始字节，平台可能留下两份未引用资源。隔离 Worker HTTP 与 SQLite 实跑，飞书、宿主和时钟用夹具。该场景连续通过 10 轮。[实测](evidence/feishu-image-upload-restore.json) · [恢复与复跑](router-rehearsal.md#飞书输出回执与换绑)

macOS 全量通过 249 项。Linux arm64 通过 169 项、跳过 80 项。从 `61b3c6a98202074a6f90f68e047e2646ff1b1536` 构建 223 文件独立包，SHA-256 为 `ff30560ab7239f2286a529c60ad8962fd9651853eece061a74b739bf4e65df9a`。安装目录通过 `python3 -B -m unittest -q rewrite.test_feishu_output rewrite.test_feishu_inbound rewrite.test_question_cards rewrite.test_state_bundle` 的 31 项，文件摘要一致。另查到飞书 SDK 1.7.5 的长连接地址发现请求未传超时，现有 TCP 断连复验未覆盖 HTTP 连接仍开着而一直没有响应的情形，待做隔离复现。[SDK 地址发现实现](https://github.com/larksuite/oapi-sdk-python/blob/v1.7.5/lark_oapi/ws/client.py#L261-L265)

飞书输出的原请求和平台消息号分阶段落盘。隔离恢复改显示名后，丢回执的发卡沿用同一正文和幂等号，找回同一张卡。飞书已回消息号而 Worker 菜单确认丢失时，模拟一小时后恢复只补录原消息号，无新平台请求。同一私聊仍只进宿主一次。旧版未留原请求的未知卡和已超时的未知创建仍待核。实际 Worker HTTP 与 SQLite，飞书和宿主用夹具。[实测](evidence/feishu-receipt-restore.json) · [恢复步骤](router-rehearsal.md#飞书输出回执与换绑)

回执恢复场景连过 10 轮。macOS 全量通过 248 项，Linux arm64 通过 168 项、跳过 80 项。从 `9c896b80675a84cf6ec12c95caf70c56978748d0` 构建 222 文件安装包，SHA-256 为 `c17f3e7d5328d241a0571b30c89c686e6b045c61336984c15d6b4d95b3ba3e2f`。安装目录复验 32 项，覆盖飞书收发、问题卡、组件快照以及两种菜单回执丢失，文件摘要一致。`/resume` 和 `/model` 各核发卡一次，仍要求原菜单绑定确认后才接受编号。

试用采集可核清后交回旧客户端。邮件重开并查回 T04，两个 Info 保存在私有试用档。游戏先核 Event 的原宿主收件，并保存 Info 和已审核过滤项。实际旧邮件客户端只读 UID 14，实际旧游戏客户端从 17 续取，只上报事件 18。试用未收回执、缺号或桥序号回退会阻止交接。桥的数字后来恢复也不会解除已记录的回退。邮件服务、游戏桥与 Hermes 宿主使用夹具。[源端实测](evidence/feed-source-return.json) · [旧客户端交接步骤](router-rehearsal.md#把试用采集位置交回旧客户端)

双来源回退连过 10 轮。macOS 全量通过 245 项，Linux arm64 通过 165 项、跳过 80 项。从 `e808c18c2d0a6e6978673fe03372a965d0893772` 构建 221 文件安装包，SHA-256 为 `18e452861f7336d4c07b39396e964c8be4acf47357e43897a79d0d9a64aaaf18`。安装目录通过采集、旧归档、Trigger 和组件快照共 26 项，文件摘要一致。旧 Router、Trigger、邮件客户端固定取 `30f61e671465db0b5bba221ba8c9fc2194a6e43d`。

旧 Trigger 可用同一停写快照的四类收件账本决定回退游标。回退工具只允许旧 `/ack` 确认连续核实的序号。模拟后序 Event 越过前序 Notify、前序进宿主后掉回执，两次均拒绝生成回退副本。按原投递号查回后，旧 Trigger 通过 HTTP 推进 1→8，重开并重投确认后新增确认数为零，原未投递尾项仍可取。Info 另核管理端查阅证据。宿主与时钟使用夹具。[实测回执](evidence/trigger-return-prefix.json) · [回退步骤](router-rehearsal.md#试用发送后退回原订阅)

累计游标回退场景连过 10 轮。macOS 全量通过 244 项，Linux arm64 通过 164 项、跳过 80 项。以 `babbeea16f52cb1a987c087a07c40381acb6a8b2` 构建 219 文件安装包，SHA-256 为 `fed53595bda54eb9c4808a0cf988bf3ad55b3481e5baf36984f527e43eae0613`。安装目录通过归档、Trigger 和组件快照回归共 19 项，文件摘要一致。测试用旧 Router 和 Trigger 源码取自 `30f61e671465db0b5bba221ba8c9fc2194a6e43d`，通过 `MOWULINK_TEST_LEGACY_ROOT` 指定。

旧 Trigger 积压可按消费者游标、有效等级和原收件凭据审核迁入。实际旧 TriggerStore 查到五条未读，新 Router 留档四条，已有原消费者收件的一条留在旧档。隔离 HermesAdapter 只送一条 Event 和一条 Notify，两条 Info 可查且未唤醒。T04 丢失后重开和按同一计划重建均取回原回执，两份附件字节一致。缺旧附件、附件摘要不符、游标不合或审核不全时不生成迁入库。旧档与游标原样保留。宿主和时钟由夹具驱动，试用发送后退回旧消费者仍须按原序号对账。[实测回执](evidence/trigger-unread-handoff.json) · [交接步骤](router-rehearsal.md#迁入已对账的旧订阅积压)

迁入场景连过 10 轮。macOS 通过全量 243 项，Linux arm64 通过 163 项、跳过 80 项。从 `c2f6292a7b24c6783d863c23ab077b252f6b9216` 构建 217 文件安装包，SHA-256 为 `1408088d49bd6eccdacc7b4de7619c833a74ab6283ff63321a3b877bf7e1aa6a`。安装目录的旧归档、迁入、Trigger、采集和独立 TLS 状态回归共 15 项通过，文件摘要一致。旧 Router 和 Trigger 测试源码由 `30f61e671465db0b5bba221ba8c9fc2194a6e43d` 提取，通过 `MOWULINK_TEST_LEGACY_ROOT` 指定。

Router 以 `--drain-dispatch` 启动时冻结新的派发号。隔离试运行中，一张持单从六库快照恢复至原 Worker 和原生收据。W04 已落库但回执丢失，第二次恢复按原 W04 键取回，只有一条正式派发、一次宿主输入、一条验收通知。经过 12 秒模拟空闲，下一张单仍未获派发号。测试管理身份显式验收后，旧 RouterStore 读回原单归档、原队首不变。仍在执行或待验收时均未生成旧端库。运行重做 Router HTTP 及真实旧库，宿主、模型与时钟使用夹具。[证据](evidence/cutover-inflight-drain.json) · [复跑与回退步骤](router-rehearsal.md#在途工单先排空再回退)

在途恢复和回退连过 10 轮。macOS 全量通过 242 项，Linux arm64 通过 162 项、跳过 80 项。以 `b59d1e1521114440ba18ca10232a89a8d963c081` 构建 215 文件安装包，SHA-256 为 `fa86a847c76db8f08e7e9c242fe78d2f4d6459c5e626928e560c7b83685a65e8`。独立安装目录通过回退、状态快照与 Router 排空启动回归共 25 项，安装文件摘要一致。旧 Router 测试源码取自 `30f61e671465db0b5bba221ba8c9fc2194a6e43d`，用 `MOWULINK_TEST_LEGACY_ROOT` 指定。

已验收的迁入原单与试用中新建的未派工单可用一份审核同时回退。隔离旧 Router 首次只收到两张新单中的第一张，重开后按原计划从头提交。第一张返回原单号，第二张新入队；改正文同号重送返回 409。读回顺序为旧队首、两张新单各一次，原已验收单仍归档，未派发，试用库和私有成果附件摘要一致。审核漏一张单时没有生成回退目录。此演练实际运行新旧 Router 和 HermesAdapter，Worker 模型为夹具。[回放回执](evidence/cutover-mixed-rollback.json)

复跑 `python3 -B -m unittest -v rewrite.test_cutover`，该组 13 项通过，混合场景连过 10 轮。macOS 全量通过 241 项，Linux arm64 通过 161 项、跳过 80 项。以 `941c5f2287d00f526f4e14fb9f6f4ce6c839b140` 构建 214 文件安装包，SHA-256 为 `fdd5c988f89baed4928f7425317d876df51417d4ec3453ea277ed562163feffd`。安装目录以固定旧 Router `30f61e671465db0b5bba221ba8c9fc2194a6e43d` 复跑 `python3 -B -m unittest -q rewrite.test_cutover rewrite.test_state_bundle`，23 项通过，安装文件摘要一致。

已验收的迁入工单能带成果回退。先以实际旧 Router 建单，在重做 Router 完成 W03、经 Worker 报 W04，再由溪流验收。恢复副本保留原工单号和建单回执，旧 Router 重开后读回归档，原请求重试不会再次入队，队首顺延到原本下一单。原附件继续从旧端下载。试用成果正文、附件和完整新库保存在私有包里，旧通用凭据不能下载新增成果附件。脚本模型驱动 Worker。复跑 `python3 -B -m unittest -v rewrite.test_cutover`。[回退操作](router-rehearsal.md#回退已验收的迁入工单) · [实测记录](evidence/cutover-reviewed-rollback.json)

普通回退继续拒绝覆盖有新写入的试用库。已结单但通知未确认、待验收、审核摘要过期、附件损坏以及出现无关的试用消息均未生成旧端副本。不能按上述步骤排空或回迁的试用写入仍需逐笔对账，保留新旧两侧。

待验收与已归档两个场景各连过 10 轮。macOS 源码回归全过 241 项，Linux arm64 通过 161 项、跳过 80 项。以 `c30c272d13ceb09e897c65ec3bcdc889bf8081af` 构建 213 文件安装包，SHA-256 为 `4e5a3d97157079545f2d9e3b4642447e17a33a2280bd5eced63483a112fb6288`。安装目录复跑 `python3 -B -m unittest -q rewrite.test_cutover rewrite.test_state_bundle`，23 项通过，安装文件摘要一致。旧 Router 测试源码由 `30f61e671465db0b5bba221ba8c9fc2194a6e43d` 的 `server/router` 提取到临时目录，通过 `MOWULINK_TEST_LEGACY_ROOT` 指定。

旧远程维护入口由 Router 共用凭据控制固定 launchd 白名单。现有溪流的 `gateway-restart` 说明明确引用该接口，Codex 运维说明也用它验证 `codexworker` 的标签与桌面进程是否同一来源。重做版改为独立、显式启用的 `host-ops` HTTPS 服务。只读身份能查清单，单服务维护身份才能对明确代际发起操作。目标表仅登记当前用户的已加载 LaunchAgent。运行期间 plist 改动与加载参数不符都使服务代际待核，拒绝操作。[维护入口](README.md#macos-独立维护入口)

实际 macOS 临时 Router 收到一次维护请求，客户端主动断开后按原 UUID 查回。旧 PID 退出，新 PID 启动，另一测试服务的 PID 不变。新 Router 从原库查回同一待派工单，原建单幂等键仍只返回一张单。维护服务重开后原回执可读。同场跑了已加载停机项恢复。另用测试队列复验维护进程中途中断留下 `unconfirmed`，旧请求不会重跑。复跑 `python3 -B -m unittest -v rewrite.test_host_ops`。三个临时 LaunchAgent 已卸载，未接现役服务。[隔离运维证据](evidence/host-ops-launchd.json)

运维三项连过 8 轮。macOS 全量通过 240 项，Linux arm64 通过 160 项、跳过 80 项，其中维护权限和请求恢复已过，LaunchAgent 实测仅在 macOS。从 `e0211fef2f3d0babafe7682af9b83f378a268b7e` 构建 211 文件安装包，SHA-256 为 `88606f358468eb0671af3d82f13a1277c0f0e0c53a3338d95c1eb1d9b9af489b`。安装目录通过维护、身份与服务进程复验共 12 项，文件摘要一致。另在隔离 HTTPS 端口执行 Router 新旧包冷升级与回退，同一待派工单仍为一张，试用库摘要一致。复跑 `python3 -B -m rewrite.release_check --repository . --component router --previous 7de8f934925d24d5b6fb53ce163a2e79352b75a2 --new e0211fef2f3d0babafe7682af9b83f378a268b7e`。

没有原生题窗时，实际 DSH 与独立 Codex App Server 可通过 Agent 工具 `worker_ask_trainer` 发布独立题卡。发卡只登记原会话与原回合。选项、自填和逐题跳过回到同一会话的新回合；等待期间不占正式工单处理权。实测 DSH 忙时未写入，改到空闲后收到一次。Codex 原生已投但回执丢失时，重开 Worker 后只查原号。挂到空适配器库仍待核，使用原账本后查回单次原生收件并更新原卡。两端均未创建工单。宿主原生运行时、隔离 Worker HTTP 和 DSH 私有 IPC 实跑，模型与飞书用夹具。[独立题卡证据](evidence/standalone-trainer-questions.json)

复跑 `python3 -B -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_native_tool_publishes_standalone_question_and_accepts_answer_once rewrite.test_dsh_native_loop.NativeLoop.test_dsh_standalone_question_defers_answer_while_native_session_is_busy`。同一原生调用或同轮的原请求号重试只留原卡。修改题文、在新回合重用显式请求号、错绑会话及非训练员点卡均有拒绝证据。故意提交原生题窗关闭凭据也不能关闭独立题。

两项连过 10 轮。macOS 全量通过 237 项，Linux arm64 通过 158 项、跳过 79 项。另跑 `python3 -B -m rewrite.devloop`，回合结束后隔离工单仍在工作中，W04 后等验收，显式验收后结单。从 `ea4624e33fdac14b12e363160aa208a57bb6a016` 构建 208 文件安装包，SHA-256 为 `41bd4f325842abc26607fbf94edbfa8d5f5f6fe7f0d173bb4264eaa3065be64e`。安装目录复跑独立题、原生题与卡片 16 项通过，文件摘要一致。新旧 Worker/适配器在隔离 HTTPS 的分步升级及回退各保留同一个私聊收件 `fdf840d1-0136-4eb0-b946-0aae3bde522c`，此迁移测试使用宿主夹具。复跑 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous a5ffe7751f71c9777b3bad9be3651c646ab06681 --new ea4624e33fdac14b12e363160aa208a57bb6a016`。

独立 DSH 工位可启用前台题转接。真实 AgentLoop 的同一个原生请求由原客户端与飞书共用。两端先答各测一次，迟到的不同答案不会覆盖已入场的答案。Worker 回执丢失后用原生工具结果查回，原生会话重新打开仍能找到同一凭据。前台超时后同一卡可续答。原回合被取消时，原卡标已关闭且无答案，拒绝后来点卡。四个场景均只创建一张测试卡、更新一次。模型、原客户端答题者和飞书均为本地测试替身。[前台题回执](evidence/dsh-foreground-question-relay.json)

复跑 `python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_foreground_question_uses_one_native_answer_and_one_original_card`。初版试跑中原生持久化拒读未经声明的扩展事件。最终版的转接标记写入宿主私有目录，原生会话日志保持 SDK 原格式。原生的原题、工具结果、续答消息及回合结束构成关闭凭据。

四个场景连续通过 8 轮。macOS 全量通过 235 项，Linux arm64 通过 158 项、跳过 77 项。从 `9c001418b4e7df465c8b525b327cd731d574492a` 构建 207 文件的独立包，SHA-256 为 `44364c63afe6defe0aa074f78a82303300030725aaca9bf714322406f64113da`。安装目录复跑原生题和平台卡共 15 项，逐文件摘要再次核对一致。关闭前台题转接时，用默认设置演练同机隔离 HTTPS 的分步升级与回退。三个阶段各保留一条原生收件，原回执 `701d3aa6-3289-4964-8cd8-09f21bd264fa` 一致。复跑 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous a7f14002fb3be1d6a940367c31912ed35144ffa5 --new 9c001418b4e7df465c8b525b327cd731d574492a`。

本机 DSH SDK 的 `dsh-experimental-worktree` 随附说明和实现只在宿主归档后尝试移除干净的专属工作树。该删除会保留有订阅会话或改动的工作树，也不查询在其中运行的进程。读写工位的旧派发仍需专属运行环境和可核验的停止与清理能力，继续标为待核。

用本机 DSH SDK 的 `dsh-subprocess-local` 在临时 Git 工作树启动前台命令并落下实际目录标记。普通 `git worktree remove -- <目录>` 已成功，SDK 托管的子进程仍未退出。直到停掉这次测试独占的 subprocess provider，SDK 才观察到退出。验证只覆盖 Git 移除和独占 provider 的行为。日常 DSH provider 可能同时承载别的会话，仍缺分配到单一旧派发的进程与工作树归属凭据。

启用只读模式的独立 DSH SDK 工位已经能用真实原生记录确认原派发停止并清理。取消或转派到达后，宿主撤下同一派发的排队更新，中止确切的原回合，另一个排队的私聊仍收到一次。Worker 取得清理证明后，由原生 `worker_report` 经 W04 清除旧派发。已转派的工单仍由新派发持有。Router 实际记账后丢回执，Worker 重开沿用原派发查回，清理不会生成正式成果。通用未启用模式和出现未核实工具的回合仍待核，测试写工具被原生执行前的限制挡下，文件未产生。宿主为实际安装的 DSH SDK，模型为脚本驱动。读写工作区的清理仍待实测。[清理与回执证据](evidence/dsh-read-only-cleanup.json) · [待核反例](evidence/dsh-native-cleanup-refusal.json)

复跑 `python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_read_only_dsh_acknowledges_original_cancel_or_transfer_and_recovers_w04_reply rewrite.test_dsh_native_loop.NativeLoop.test_native_dsh_stop_alone_or_unverified_tool_keeps_cleanup_pending`。

两项 DSH 原生用例连续通过 12 轮。macOS 全量通过 234 项，Linux arm64 通过 158 项、跳过 76 项。从 `7c0f90d67cf328097aa798b8e479ccf33b9cefe5` 构建 206 文件的独立包，SHA-256 为 `0402b4249daaa0670644bb320f55c2ff45c55ef248b34d678a4ca831caca5225`。安装目录内通过 DSH 与 Codex 的原生取消、派发竞态及快照审计共 17 项，安装文件逐份复验一致。默认配置下的隔离 HTTPS 升级与回退在三个阶段各保留一条原生输入，原收件 `76e88422-d0bd-47de-bf92-38f7751bdc83` 一致。复跑 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous 4faf8e277945a512b37b1884ac8437e32883f645 --new 7c0f90d67cf328097aa798b8e479ccf33b9cefe5`。

DSH 的 `/new`、`/clear`、`/fork` 已经走 Worker 的正式交接。原生空会话用工位默认配置，分支保留父会话标识、已落盘的历史边界和当前模型力度。写入与登记分别按原操作留证。隔离 SDK 实测原生已落盘而 IPC 回执丢失，重开查回同一会话。原生未写入且父会话已变化时，拒绝按旧边界建分支。Worker 收到拒绝后保留当前绑定并解除派发暂停。[原生回执证据](evidence/dsh-native-preparation-recovery.json)

登记回执丢失时 Router 仍暂停派发，重开后沿用原登记。旧会话回调被拒。新会话和分支各接一张正式单，每张单两次原生 `worker_report` 调用只留一条待验通知。分支的父历史没有变成新的原生输入。三个子会话的适配器账本从快照恢复和回退均逐字节一致。宿主是本机安装的独立 DSH SDK，TLS、Router 和 Worker 实跑，模型及飞书为夹具。真实 DSH 客户端、飞书与跨机原生历史仍待验。[换绑、W04 和快照证据](evidence/dsh-managed-native-sessions.json)

复跑 `python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_managed_dsh_creates_and_forks_through_the_worker_and_recovers_a_lost_reply rewrite.test_dsh_native_loop.NativeLoop.test_dsh_preparation_reopens_the_exact_native_child_after_ipc_ack_loss`。

两项原生 DSH 用例连续通过 12 轮。macOS 全量通过 232 项，Linux arm64 通过 158 项、跳过 74 项本机宿主用例。从 `e1f9a49ec3fdb77b30151f913ddf03c31fb2a553` 构建 203 文件的独立包，SHA-256 为 `416e1d0baace48a5188f3f5c1d6a114aa68ad4b4106063c735f2494575643f4a`。安装目录通过 DSH、Codex 和快照的 15 项测试，逐文件复验一致。关闭自动登记时，分步升级与回退沿用同一条私聊原生收件 `281e6f01-3fca-4542-b94e-4588662e7403`。复跑 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous 392a68db1b211f5cc6236a3e9ead201739697d62 --new e1f9a49ec3fdb77b30151f913ddf03c31fb2a553`。

自动登记工位的离线快照已收录全部子会话账本，核对 Worker 操作号、原生准备记录、子库会话和回调摘要。仅使用原私有密钥在目标目录重建回调身份。漏登记目录、注册表或子库被占用、错父会话、错密钥及试用库已变动均会拒绝。恢复运营前可审计子库的原收件和待核项。[恢复与回退操作](router-rehearsal.md#codex-自动登记工位的恢复与回退)

隔离 Router 与真实 Codex App Server 从落账后丢登记回执的快照恢复，找回同一操作和会话。前一版适配器 `4e7c646e060ceb4a3abf7430e0ac3f4b49a72f09` 从回退目录接回同一持单，重投不产生第二条原生输入。原单更新后，两个原生工具回包只落一条成果和一条待验通知。带新成果的试用库拒绝覆盖回退。前一版适配器实跑 12 轮。原生 profile 保留在同机独立目录，模型为本地夹具，飞书线上未验。[快照、派发、回合与通知证据](evidence/codex-managed-bundle-rollback.json)

复跑 `python3 -B -m unittest -v rewrite.test_state_bundle rewrite.test_codex_app_server.CodexAppServer.test_managed_bundle_restores_pending_enrollment_and_rolls_back_a_held_assignment`。调试前一版适配器时，可把独立安装目录设为 `MOWULINK_MANAGED_ROLLBACK_PACKAGE` 并附其 `MOWULINK_MANAGED_ROLLBACK_REVISION`。macOS 全量通过 230 项，Linux arm64 通过 158 项、跳过 72 项本机宿主用例。

从 `fb6b03054e2bcc370533839a38bea1f08f292592` 构建 198 文件的独立包，SHA-256 为 `9e94ac1be8d2d8ffbb39ab36773fa0301106abd92cdcb92d1393823916c9d7e9`。安装目录通过恢复、快照审计、老版本适配器接回持单及自动换绑等 12 项，文件摘要复验一致。

独立 Codex App Server 已跑通 `/new`、`/clear` 和 `/fork` 的自动登记与换绑。重放原飞书测试消息仍得到原绑定，旧会话凭据冒用新会话被拒。主动丢弃第一次登记回执并重开 Worker 与适配器后，Router 仍保持派单冻结，Worker 查回同一登记，再启用并确认新绑定。另一次重开只恢复当前分支，旧会话保持冷态。普通私聊继续进入当前分支。

两张新派发分别进入新会话和分支，各产生一次新原生输入和一个回合。各回合两次调用原生 `worker_report`，Router 各留一条待验通知，Worker 各释放一次持单。分支继承的父输入没有成为新派发收件。实测宿主为独立原生 App Server，模型和飞书为本地夹具；多会话登记目录及密钥的备份和回退仍待演练。[会话、派发、原生回合与通知证据](evidence/codex-managed-native-sessions.json)

复跑 `python3 -B -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_managed_native_commands_bind_without_manual_enrollment_and_restore rewrite.test_state_bundle.Bundle.test_capture_refuses_to_leave_native_session_registry_outside_the_backup`。macOS 全量 228 项通过。Linux arm64 157 项通过，另 71 项本机宿主用例跳过。

从 `149788b782232b4fe75d4c5ba9e3d8f4af2502e1` 构建的独立包包含 195 份文件，SHA-256 为 `d6ee315bcb6b6010a7f09e187beccd5ed6484b8f6e86b237a6bbff215b3586aa`。安装目录通过真实原生自动换绑、创建待核、快照防漏与旧方式换绑四项，逐文件摘要复验一致。复跑时设置 `PYTHONDONTWRITEBYTECODE=1`，使测试子进程也不写字节码缓存。不开启自动登记时，隔离 HTTPS 的旧版、混合版、新版和回退阶段保留同一条原生私聊回执 `3542952d-dfcb-4633-8845-7e4f41abb816`，升级前、试用和回退各只有一条原生输入。复跑 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous 7d16090452d0e2bd8895e75584eeac191caf72da --new 149788b782232b4fe75d4c5ba9e3d8f4af2502e1`。

Codex 独立只读工位已能经授权固定会话接口准备新会话或分支。实测沿用原工作区、模型和思考强度。空会话准备不增加模型请求，原生分支保留原父会话及历史。命名回执丢失时使用已落账的原生 ID 继续确认。创建回执丢失时，同一操作重试与原生进程重开均保留待核，未再发创建。当前安装的 App Server schema 中 `thread/start` 和 `thread/fork` 无供调用者提交幂等号的字段。离线停写换绑已能核对这类待核记录。[原生准备与接入证据](evidence/codex-native-thread-preparation.json)

用准备端返回的原生 ID 显式登记独立适配器，再经现有交接将空会话从冷态启用。正式单仅产生一个原生回合，模型夹具两次调用 `worker_report`，Router 只留一条待验通知。飞书 `/new`、`/fork` 的自动适配器接入和换绑尚待实现，本轮未向真实用户会话投递。

复跑 `python3 -B -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_thread_preparation_retains_native_identity_and_never_retries_an_uncertain_creation rewrite.test_codex_app_server.CodexAppServer.test_prepare_sessions_cli_uses_authenticated_pinned_adapter_and_replays_after_restart rewrite.test_state_bundle.Bundle.test_handoff_refuses_to_copy_an_unconfirmed_native_thread_preparation`。两项原生用例与停写审计连续通过 12 轮。macOS 全量 226 项通过，Linux arm64 156 项通过，另 70 项本机宿主用例跳过。

从 `040dad32906b187514f9599f0eef4eb15b2d0595` 构建 193 文件独立包，SHA-256 为 `895e3adf85fd4ab108ab13d0a3a407639b6fda745810495a840233782559160d`。安装目录专项复验通过 4 项，重读文件摘要一致。关闭准备功能的旧版、混合版、新版和回退保留同一条原生私聊回执 `756c061f-e82c-445e-870e-ca6a96d42d45`。复跑 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous cee3a234fa832afea0c417d50940b6ef05541619 --new 040dad32906b187514f9599f0eef4eb15b2d0595`。

DSH 已接入运行中 Worker 的冷会话恢复。真实 DSH SDK、TLS 适配器进程和隔离 Router 实测，目录浏览时原生代理保持关闭，Router 冻结后恢复原会话。首次恢复回执丢失时按原号查回，测试控制器仍只调用一次原生 AgentRegistry 恢复。原生执行一次正式派发，两次 `worker_report` 回同一结果，仅一条待验通知和一份最终显示。旧历史未重播。关闭原 Agent 后，原插件拒绝换成新对象。宿主和适配器一起冷重开后，Worker 以同一交接号恢复同一会话和历史，新增模型调用为 0。保有活动原生目标的冷会话在唤醒前被拒。控制器装配、飞书与模型为夹具。[DSH 冷恢复实测](evidence/dsh-cold-resume.json)

复跑 `python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_cold_dsh_reopens_only_the_authorized_session_and_recovers_lost_activation_reply rewrite.test_dsh_native_loop.NativeLoop.test_cold_dsh_requires_explicit_activation_and_keeps_active_goals_unloaded rewrite.test_dsh_native_loop.NativeLoop.test_closed_native_binding_never_resumes_or_sends_to_a_new_agent`。三个场景连续通过 12 轮。macOS 全量 223 项通过，Linux arm64 155 项通过，另 68 项本机宿主用例跳过。

从 `80a952263b83432f40e128a753df4d436eb41f54` 构建含 191 文件的独立包，SHA-256 为 `c6a0c60d1a213056052521c9c82f9bb0271199815517ac6963a4855fc363e108`。安装目录通过 DSH 冷恢复、原生目标、关闭对象关联、双宿主热切、Codex 冷恢复和 TLS 配置六项，安装文件摘要复验一致。未启用冷恢复的两版 Worker 与适配器可混合运行和回退，各阶段各保留一条私聊原生输入，原收件号 `f1a371af-c044-490a-a269-fa93b02e0bd4` 一致。复跑 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous 2017df4f1469e3a81043bfae92e57cbeeb0e76fb --new 80a952263b83432f40e128a753df4d436eb41f54`。

新会话入口预查中，隔离 Codex App Server 以 `thread/start` 建立并命名未有对话的会话 `01a217ad-1f09-7891-a1ae-3e17346c39b2`，模型调用数为 0。重开后按 ID 的 `thread/read` 查到 `notLoaded`，同工作区的状态目录未列出这条记录。后续接入 `/new` 时需保存实际返回的会话 ID，单靠目录查不到这类空会话。

已配置的 Codex App Server 适配器可通过 `--resume-on-handoff` 保持原生会话冷态。隔离 TLS 进程在 Router 确认冻结后才恢复指定会话。实测丢弃 Adapter 回执后，Worker 沿用原交接号，查回这同一个会话，释放窗口后才发正式单。原生回合收到一个正式输入，模型夹具触发两次 `worker_report`，Router 仅一条待验通知。适配器冷重开后恢复已提交的绑定，没有额外模型请求或卡片回放。另在真实 App Server 丢弃原生恢复响应，第二次只核历史和状态。活动原生目标会保留在原宿主，Worker 回到原绑定。飞书与模型均为测试夹具。[冷会话与两层丢回执](evidence/codex-cold-resume.json)

复跑 `python3 -B -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_cold_codex_activates_only_after_router_pauses_and_recovers_lost_reply rewrite.test_codex_app_server.CodexAppServer.test_native_cold_activation_checks_goal_and_recovers_a_missing_app_server_reply rewrite.test_worker_resume.WorkerResume.test_cold_target_with_active_goal_keeps_source_even_when_release_reply_is_lost`。三个场景连续通过 12 轮。源码目录 macOS 通过 221 项，Linux arm64 通过 155 项，另 66 项本机宿主用例跳过。DSH、Desktop 的冷启动和真实飞书验收仍待接。

从 `cb3953c4fa3b9781117aa9d8f35fb751fa80560e` 构建 190 文件独立包，SHA-256 为 `56bcf267ed6b45e8b11901470f2de664ecfc46fca67ca950389637538b68b4d7`。从安装目录复跑 221 项后，复验文件摘要一致。两版冷恢复功能关闭时，隔离 TLS 的旧版、混合版、新版和回退每阶段各保留一条私聊原生输入，原收件号 `1170899e-6e8c-4f41-90d4-7f345eeb2692` 一致。复跑 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous c9edeb5c3acacb330f049a61e37da2b410a1845d --new cb3953c4fa3b9781117aa9d8f35fb751fa80560e`。

`/resume` 的目录按旧版从 0 编号。翻页复用当次目录，刷新和搜索生成新目录，选编号需已确认的卡片。隔离实测卡片送达回执丢失、重复发送、目录重排、翻页后回复错页与回复刷新前的旧卡，均未误切。完整标题重复时需用 ID，目录内未配置的冷会话只可浏览。原生联调从真实 Codex App Server 生成的目录卡按编号切到 DSH，完成私聊和正式工单，再按 ID 切回。对应两组场景连续通过 12 轮，模型与飞书为夹具。[目录与原生回执](evidence/native-resume-catalog.json)

复跑 `python3 -B -m unittest -v rewrite.test_worker_resume.WorkerResume.test_number_uses_the_confirmed_catalog_and_old_card_cannot_redirect_it rewrite.test_dsh_native_loop.NativeLoop.test_platform_resume_switches_live_codex_and_dsh_sessions`。macOS 218 项通过，Linux arm64 154 项通过，另 64 项本机宿主用例跳过。冷目录中自动打开宿主仍待接。

从 `3e7e7c2028deaf9f351752a36cf097609b43fe92` 构建并校验 188 文件的独立包，SHA-256 为 `93ca1d51faa9e332755feb37b8f6f1c84d582a1114e1246ebe696a43aed78aa4`。安装目录通过目录、双宿主切换、Codex 和 DSH 冷目录只读、TLS 配置五项，复验安装文件一致。

另在独立 Codex App Server 原生接口查证，临时会话 `01a21799-ed4c-7f43-aec1-62549209fd59` 从 `notLoaded` 恢复到 `idle`。查询目标未加载会话，显式 `thread/resume` 后仍只有原回合 `01a21799-ed53-73b2-8c79-cf9646412bea`，新增模型请求为 0。此项只验证宿主入口，Worker 的冷会话交接尚未接入。

`/resume 会话ID` 可将同一 Worker 切到已配置、已运行的独立适配器。`/switch 会话ID` 使用同一条链。运行中的 Codex App Server 与 DSH 隔离会话已往返切换。重投老消息、旧切换指令均读回原回执。原 Codex 一次私聊，DSH 一次私聊加一张工单。DSH 的两次原生 W04 返回同一结果，Router 仅一条待验通知。切走后，旧适配器即使把新会话 ID 写进回调正文也会被拒。模型和飞书是本地夹具。真实飞书、生产宿主、冷目录和编号选择仍待核。[原生与回执证据](evidence/native-live-resume.json) · [运行入口](README.md#飞书切换会话)

切换、Router 开始与结束丢回包、提交前目标断开三个场景连续 12 轮通过。最后一项确认目标断开 80 秒期间 Worker 仍在线且派发暂停，授权运维按原号解除后继续投给原会话。适配器进程和 Worker 各自用独立账本，CLI 配置测试核对 Worker 重开后保持新会话。复跑 `python3 -B -m unittest -v rewrite.test_worker_resume rewrite.test_service.Service.test_runtime_resume_uses_opted_in_connection_map rewrite.test_dsh_native_loop.NativeLoop.test_platform_resume_switches_live_codex_and_dsh_sessions`。

从 `f9c8859d0243733a7aa7c296b806b8361658a3d8` 构建 186 文件的独立包，SHA-256 为 `e314a066d61fd559637137c97e1e4ff918ef5dba6b7055c782367ec85a251c41`。首轮安装后 14 个迁移测试未找到旧实现。按固定提交单独供应旧 Router 与 Trigger 测试夹具后，macOS 安装目录 217 项通过，复验安装文件摘要一致。Linux arm64 通过 153 项，另 64 项本机宿主用例跳过。[安装目录全量复跑](README.md#本地复跑)

用 `python3 -B -m rewrite.release_check --repository . --component worker-pair --previous 2cd3b465a462a0253476b3469cd7eaa0d821fa9c --new f9c8859d0243733a7aa7c296b806b8361658a3d8` 演练隔离 TLS 旧版、混合版本、新版和回退。未启用运行中切换时，各阶段均只有一条原生输入，原收件号 `3cbe257a-b295-466a-a286-8febcd12e122` 保持一致，原快照未改变。运行中切换的恢复另由上述丢回执、目标断开和切回原会话用例覆盖。

Worker 可经独立交接授权建立 Router 交接窗口。冻结和预约在 Router 内以互斥事务处理，已有预约或持单时拒绝。开始回包丢失后，Worker 重开沿用原操作号恢复冻结。解除回包丢失也能查实。提前解除会留记录，迟到的冻结不会重新打开。实测等待超过在线租期仍在线，私聊和同伴信留在原队列，解除后各收一次。Router 重新采样并等满连续空闲 10 秒，才派新单。真实隔离 HTTP 和 TLS 进程，飞书与原生宿主为夹具。[回执证据](evidence/native-handoff-admission.json) · [操作入口](README.md#原生交接窗口)

复跑 `python3 -B -m unittest -v rewrite.test_handoff_admission rewrite.test_service.Service.test_three_process_worker_chat_and_assignment`。三个竞态与续接场景连过 18 轮，另用三个 TLS 进程验证隔离交接命令和解除后正式派单。macOS 全过 210 项。Linux arm64 通过 147 项，另 63 项本机宿主用例跳过。

从 `ec22dad2b1e14c33f6aad0c5543b56c38d22954e` 构建 182 文件发布包，SHA-256 为 `8189f3ee84f4841315faa39aabf8ba9b4f93877da3d295276303ca6e7017c027`。安装目录复跑四项通过。以 `52a79e0c674f089b66d5cddc7dd122239df28920` 为旧版，未开启交接窗口时演练分步升级、混合版本与原快照回退，原生账本各阶段均为一条。复跑 `python3 -B -m rewrite.release_check --repository . --component worker-pair`，指定上述新旧 SHA。

同一 Worker 从 Codex 冷切到 DSH 再切回。两端分别收到同样的图文并返回各自的原生收据，图片摘要一致，每次私聊只生成一张对应回复卡。两端原生 `worker_report` 各调用两次，均回同一派发凭据，各留一条待验通知。切回后按原消息号重放，卡片、原生输入和成果均未增加。Router 和 Worker 使用相同接口。真实原生运行时均为同机隔离测试，模型和飞书用夹具。[跨引擎证据](evidence/cross-native-handoff.json) · [复验命令](router-rehearsal.md#隔离工位冷换原生会话)

跨引擎验收连过 12 轮。macOS 全量复跑 207 项通过，Linux arm64 通过 144 项，另 63 项本机宿主用例跳过。首轮 macOS 回归发现压缩夹具把「原生已受理」断言成即时可读历史；调整后分别核对持久受理标记和冷开后的同一压缩凭据，原生发送仍只一次。

从 `bdaf14d34513d5f429cd258f8965c14852933d1b` 构建 178 文件发布包，SHA-256 为 `b703466ec23839693ad88bda4f8157b935634847e27c973edf9b87435b255e20`。安装目录复跑跨引擎换绑及压缩恢复两项通过，安装文件再次验过。

已确认的会话绑定写入 Worker 与适配器状态。直接改掉 Worker 的固定会话或复用另一会话的适配器库会拒绝。停写换绑检查 Router 已预约派发、待收业务帧、未了原生收件、答题与平台入站，只有账本核对通过才在新目录生成换绑副本。原宿主是否确实空闲仍需由宿主侧确认。[换绑命令与回退流程](router-rehearsal.md#隔离工位冷换原生会话)

实际隔离 Codex App Server 从原会话冷换到原生分叉。重放父会话私聊只返回原收据。子会话收到一条新私聊、一张正式工单，原生工具两次 W04 返回同一原派发回执，Router 留一条待验通知。继续从试用后状态切回，保留新成果和两端收据，模型请求数不增加。共 12 轮，模型和飞书用夹具。macOS 通过 206 项，Linux arm64 通过 144 项，另 62 项本机宿主用例跳过。飞书 `/resume` 仍为目录查询。[原生交接证据](evidence/codex-native-session-handoff.json)

实现提交 `cc5caff4d769f3cb94c3e0785edb5aa48a19a890` 构建 177 文件发布包，SHA-256 为 `f0ec0c2cd2cdffbee69f419940cdec048d8ad6679b6ca3989a78598c33681c3e`。安装目录复跑 8 项交接、备份与回退测试均过。另从 `058f7b81955bd34b2c7c431d8c6a0024223d8ac1` 实跑同会话的混合版本、新版和回退。三阶段均保留三个原生输入与同一成果通知 `a495c798-c1d1-4064-8e02-cf22fc534bae`，未新增原生输入，原快照未改变。复跑入口为 `python3 -B -m rewrite.codex_release_check`，指定这两个提交及已安装的 Codex App Server 路径。[发布演练](router-rehearsal.md#保留实际-codex-测试工位的升级与回退)

来源留档和 Router 投递分别有只读状态入口。临时 TLS 实测首次 T04 回执丢失时来源仍有三条待核，Router 已有一帧。按原号重试后来源剩零条，Router 共两帧，Info 仅留档。测试接收者先确认第二帧，连续游标仍为 0；Router 重开后补第一帧，游标到 2。来源不能查询接收者，另一工位只能查看自己的零积压，查询不返回正文或推进游标。复跑 `python3 -B -m unittest -v rewrite.test_service.Service.test_source_receipts_and_consumer_cursor_are_observed_independently`。[计数证据](evidence/trigger-delivery-status.json) · [操作入口](README.md#来源留档与通知积压)

积压与续接场景连续 10 轮通过。使用隔离 TLS Router 和测试接收者确认，未调用真实 Hermes 或飞书。本版 macOS 全过 204 项，Linux arm64 通过 143 项，另 61 项本机宿主用例跳过。

从 `367ef4a8acb89253e31ccafd8689fcc5d43d0f54` 构建并核验了 176 文件的独立发布包，归档 SHA-256 为 `b579021ace26f58005598b55842a6b2db03654870278a38cdbaba03e8aae0024`。在安装目录内复跑积压、Codex 批量题、DSH 续答和 DSH 阻塞题边界，4 项通过。

从 `a61b77fe5f37bdf5d39a8fc9ad2e311044caef97` 演练 Router 冷升级和回退。旧包、新包、旧包依次在临时 HTTPS 回环启动，均查回工单 `9c8ace6e-31e9-4f40-aa77-530b459b8ceb`。回退后一张原单，试用库摘要一致。复跑 `python3 -B -m rewrite.release_check --repository . --previous a61b77fe5f37bdf5d39a8fc9ad2e311044caef97 --new 367ef4a8acb89253e31ccafd8689fcc5d43d0f54`。

训练员题卡可一次提交同一原生请求里的多道公开题。Codex App Server 按原题 ID 回填单选、自填和跳过。任一题标为密题时整组留在宿主。DSH 已进入续答状态的计时题支持多选、自填与逐题跳过。选项顺序取原题，同样的选择换顺序重提仍只留一次答案。飞书测试卡收到原生关闭凭据后才更新同一张卡。两端各连过 8 轮，模型与飞书均用夹具，未启动日用会话。[Codex 原生记录](evidence/codex-app-server-native-question-batch.json) · [DSH 原生记录](evidence/dsh-native-question-batch.json)

批量卡以输入框收选项序号。此处的续答证据使用 DSH SDK 已超时的题；原生前台答题见本页最新检查点。飞书实际显示、真人点击与同时打开的桌面原卡留待独立测试身份验证。

另用 DSH 随附 SDK 起阻塞式原生题，确认 `user-questions/request` 交给原答题者时，`continued(agent)` 仍为空。适配器按同一会话、回合与调用号查询和尝试作答均未得到凭据。原答题者给出测试答案后只生成同一工具调用的结果，没有第二条回合输入或飞书题卡。连续 12 轮通过，以隔离测试答题者代替客户端 UI。复跑 `python3 -B -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_native_foreground_question_remains_with_its_original_answer_provider`。[阻塞题证据](evidence/dsh-native-foreground-question.json)

全量回归在 macOS 通过 202 项，Linux arm64 通过 142 项，另 60 项本机宿主用例跳过。复跑 `python3 -B -m unittest discover -q -s rewrite -t .`。

DSH 命令审批已用实际 SDK 的 AgentLoop、ApprovalService 和 Bash 验证。带绝对工作目录的请求沿用原生审批号，测试审批人允许后只执行一次隔离 `pwd`，拒绝及超时均未执行。原生结果已写盘但关闭回执被扣住时，重开适配器及宿主可从原会话补回，原生输入、决定和工具结果不新增。该次验收版本在省略工作目录时交由宿主原生审批。连续 16 轮通过。模型和审批操作者用测试夹具。[原生审批记录](evidence/dsh-native-command-approval.json) · [操作入口](README.md#独立宿主命令审批)

三段 TLS 和 DSH 私有 Unix 回调接口另用独立进程实跑。审批 CLI 允许 `adb32960-a5f8-4ef2-9153-f831352283e9`，原生回合回读同号、`allowed-once`，仅一次 Bash 结果。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_dsh_native_command_decisions_match_persisted_approval_ids`。

同版 macOS 201 项全过。Linux arm64 通过 141 项，60 项因本机宿主依赖跳过。

从 `3d36e488e7879a060786e4a773fe8ba81edbdb19` 构建并安装了发布包，再从安装目录运行 DSH 命令审批回归，校验 171 个文件。归档 SHA-256 为 `782a474c4150ccfbe4f892a03eb18daa3095c4c4b730eea1b719ea9017b20e51`。

独立 Codex App Server 的命令审批通过 Worker 按原请求号到 Router。请求与单次决定分别持久记录，C01 通知仅给该工位获准的审批身份。同号重投没有第二条通知。一次故意丢掉 Router 闭合回执，适配器取回同一记录；另一次丢原生关闭回执，Router 保留决定已登记，未误记关闭，也未重发原生决定。第三次推进隔离 Router 时钟，超时后原生请求只收到一次取消，没有操作人决定。连过 12 轮。模型和人机操作使用夹具，实际授权流程仍待验。[原生审批记录](evidence/codex-app-server-native-approval.json) · [审批入口](README.md#独立宿主命令审批)

另用正式服务入口启动三个隔离 TLS 进程。获准 CLI 取消 `de39f540-e4ad-4c87-95a0-32b4aa591566` 后，独立宿主冷开回读同一回合已中断，且无命令执行项。复跑 `python3 -m unittest -v rewrite.test_service rewrite.test_codex_app_server.CodexAppServer.test_native_command_approval_cancels_exact_request_and_does_not_infer_missing_proof`。

同一回归加入单次允许，隔离 Codex App Server 连续九轮通过。示例审批 `9513f08a-ecd8-4e99-ad9c-1daeee74b2ff` 对应原生回合 `01a2187d-6794-7b82-890e-929f10650f88`。用其他请求的摘要提交会被拒绝。正确摘要获准后原回合只执行一次隔离目录的 `pwd`，退出码为 0，Router 回到 `native_closed`，原决定重投返回同一回执。两次取消及一次超时均无执行记录。实际 App Server、Router 与 Worker，模型及审批者用本地夹具。[允许与原生命令证据](evidence/codex-app-server-approval-accept.json)

macOS 全量通过 255 项，Linux arm64 通过 175 项、跳过 80 项。从 `b7d4682209edcea619d3b166ce2e3d343f9e99d6` 构建 230 文件独立包，SHA-256 为 `675963c342da8fcaf55e48037715da3f102f84943a79b08a051032eb22360fc1`。安装目录以同一命令复跑原生审批和服务的 10 项，文件摘要复验一致。

同版 macOS 200 项全过，Linux arm64 通过 141 项，另 59 项因缺本机宿主跳过。

分叉校验与离线审计进入新发布包 `02dbb460da5cc21cc0627498e01773344c32783e`。从 `2a8f081841310259a48ea9f077297d1440857c56` 出发，用独立原生 Codex 工位实跑混合版本、升级及回退。三阶段都保留原会话的三条输入，仅原成果待验通知 `98014442-52d8-49cf-8da1-3fa821499a78`，快照摘要未变。新包 SHA-256 为 `a6bb3dff99add0aacc2670740ca2801ed5f2c0068d028f4751b3a3cd4e518389`。复跑方法见[发布演练](router-rehearsal.md#保留实际-codex-测试工位的升级与回退)。macOS 197 项全过，Linux arm64 通过 139 项、另 58 项因缺本机宿主跳过。

换原生会话前可用 `state_bundle audit-handoff` 对照未核的原生私聊、下一轮队列、派发、题目、压缩和清理。隔离试验中，原宿主已收到私聊但回包丢失，同时有一条排队消息。正式持单账本一致，原生会话账本仍显示待核；从原会话恢复后查回私聊并交付队列，各只有一次原生输入。审计仅返回计数。复跑 `python3 -m unittest -v rewrite.test_state_bundle`。[停写窗口与恢复](router-rehearsal.md#重做版五组件的离线恢复)

Codex App Server 的原生分叉会复制父会话的输入号、投递号和回合号。新会话首次接入时保存已存在的投递号，旧号既不能证明在子会话新收件，也无法再次写入。临时改接子会话后，原消息有回执时读回父会话的回执，缺回执时仍待核。子会话只收到一条新消息，故意丢收件后冷开可按原号查回。连续 20 轮通过。[原生分叉记录](evidence/codex-app-server-native-fork.json)。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_native_fork_cannot_confirm_or_resend_the_parent_delivery`。换绑由测试显式选择，飞书 `/fork` 与 `/resume` 换绑仍待接。

正式 Codex 长目标在独立 App Server 内连续 12 轮通过。模型夹具通过原生工具建目标，宿主自动续办并完成。续办回合虽与派单同源，但没有单独的原生输入，不能凭原单号代报。原派发继续持有，直到 Router 发出询问、得到新回合的原生收件后，模型经 W04 交回。两次调用回同一回执，仅一条待验通知。正式派单与询问各有一个原生输入。[长目标证据](evidence/codex-app-server-native-formal-goal.json)。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_native_goal_finishing_keeps_original_assignment_until_bound_app_server_report`。

同版 macOS 195 项全过。Linux arm64 通过 138 项，另 57 项因缺本机宿主跳过。Codex 仍使用无账号的独立原生 App Server、本地脚本模型及隔离 Router。

运维 `worker-status` 可查看当前已核对的会话所声明的八项能力。隔离 TLS 中仅运维角色可查，空能力与断链未知可区分。实际 Codex App Server 报出目录能力。模拟两次查询之间换宿主会话，Worker 拒收混合观测，Router 的忙闲询问也没有生成样本。复跑 `python3 -m unittest -v rewrite.test_link_health rewrite.test_mainline rewrite.test_service`，30 项通过。

Antigravity 官方 CLI 的桌面对话恢复属于克隆。Remote Control 可供人工访问原对话。两条路径均未找到 Worker 后台写回既有本地桌面会话和停止的依据。远程托管和 SDK 可作为独立宿主评估，原会话的验收仍待补。[接口对照](antigravity-compatibility.md)

完整发布包完成实际隔离 Codex 工位升级和回退。旧包内真实 App Server 用脚本模型自主调用 W04，只有一条待验通知。在复制的 Worker 库模拟丢私聊回执，依次切新适配器、升级 Worker、用原状态回退。三轮独立演练中始终保留同一原生线程和三个输入，无新增模型请求或通知，快照及包摘要核对一致。复跑见[版本演练](router-rehearsal.md#保留实际-codex-测试工位的升级与回退)及[原生收件与版本证据](evidence/codex-native-release-cycle.json)。

Linux arm64 在提交 `38d4ee4a3693dd030631a7597f2eb74c583b8ae5` 上通过 137 项、56 项宿主依赖跳过，校验 161 个发布文件。首轮发现隔离子进程单测把临时脚本当可执行文件启动，Linux 临时卷拒绝；改由系统 Python 读取脚本后通过。隔离跨系统 TLS 复核只见原生夹具输入和待验通知各一条，错误身份与证书被拒。

独立 Codex A06 已通过原生 `view_image` 验批准目录的原图。测试飞书在第一张过程卡完成后单发原图，再开后续过程卡并更新。Worker 已写原图但返回回执丢失，原文件删去以后同号查回，仍只上传一次。目录外及符号链接拒绝发布。实际原生历史曾暂报中断，已以同回合完成通知和历史一致为发最终答复的条件，回归另注入同样的暂态。连续 20 轮通过。[图片证据](evidence/codex-app-server-native-image-split.json)。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_native_approved_image_splits_the_platform_card_and_replays_the_original_upload`。本地原图、模型及平台均为独立测试用。

独立 Codex A07 已接单个公开原生题。测试平台选择、自填和跳过实际进入同一个 App Server 原题，关题回执按原请求号匹配，测试平台仅更新原卡一次。错人、错卡、密题、同原生题再次注册均拒绝。故意丢原生关题回执，模型只收到一次原答案，卡保持待核。连续 10 轮通过。[题卡证据](evidence/codex-app-server-native-trainer-question.json)。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_native_trainer_question_closes_original_card_only_after_app_server_receipt`，模型与飞书用独立夹具。

完整回归曾见隔离 App Server 的原生辅助进程晚于主进程退出，暂存目录仍有写入。已用单独进程组停止该试验服务及其辅助进程，单测记录辅助进程收到终止信号。macOS 全套 192 项通过。真实宿主服务由各自的发布窗口验收。

独立 Codex A05 工具卡已核。实际 App Server 在只读试验目录运行命令期间，Worker 测试平台依次收到原生输入、公开过程发言、工具预览三张标准卡。原生命令仍在等待。故意丢工具写入回执，重复提交仍是同一事件。释放命令后只追加独立回复卡，完整参数、终端内容和内部工具未进入卡片。连续 12 轮通过。[卡片与回执](evidence/codex-app-server-live-tool-card.json)。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_codex_tool_card_arrives_during_native_execution_and_final_stays_distinct`。模型与飞书是夹具。

独立 Codex `/model`、`/effort` 已通过指定提供方目录和原生线程设置接通。实际下一次模型请求由 Sol 改至 Astra，`max` 透传。丢回执重投保持待核，后续 `low` 冷开读回且进入下一次请求。原生选择 `ultra` 时本地 Responses 请求为 `xhigh`，真实账号的请求值仍需实测，卡片只确认客户端设置。旧模型的力度菜单、未送达菜单、其他提供方均拒绝写入。连续 12 轮通过。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_native_model_and_effort_commands_change_next_request_and_persist_cold`。[设置与请求记录](evidence/codex-app-server-native-selection.json)。测试模型、平台使用夹具。

独立 Codex `/goal` 通过原生查看、建立、暂停和继续。带授权平台身份的显式指令才走原生用户写入，适配器角色发起同一路由被拒。已有目标无法覆盖，实际无自设 token 预算。丢继续回执后按原消息号重投，最后的原生暂停仍生效。连续 15 轮通过。[原生目标记录](evidence/codex-app-server-native-goal.json)。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_platform_goal_uses_native_user_origin_and_does_not_replay_after_lost_ack`，模型、平台使用夹具。

独立 Codex `/compact` 已走原生 `thread/compact/start`。收到 App Server 确认、返回 Worker 的回执丢失时，冷开后凭原压缩回合和记录查回，只发一条成功提示。若 App Server 原确认丢失，指令保持待核，即使历史后来出现压缩，其关联也不确认。两次实际原生调用各产生一个压缩回合，同消息号重投无第三次。连续 15 轮通过。[原生回执](evidence/codex-app-server-native-compaction.json)。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_app_server_compact_recovers_native_turn_without_restarting_uncertain_request`。平台与模型使用夹具。

macOS 全套复测曾遇到第一次原生历史读取被 App Server 拒绝。已将可重试的历史错误保留为待核，按原标识复查。隔离回归实际写一次原生输入、在首次读回注入拒绝，随后读回原号，仍由同一工位持单。最终 macOS 187 项全过。Linux arm64 锁定 `c52c30c9a17892cfdf6924996ac020931c990e16`，136 项通过、49 项因缺本机宿主跳过，校验 151 个文件，跨系统 TLS 回环使用夹具。

隔离迁移后已有正式进展的回退对账已实测。旧待派单按原号映射到试运行库，模拟 Agent 经实际 Worker 提交带附件 W04，重投只留一条通知。对账读回旧新单号、状态、原派发和成果附件，无正文和文件名；原样回退被拒，两份库保留。连续 8 次通过。复跑 `python3 -m unittest -v rewrite.test_cutover`。[回退边界证据](evidence/cutover-reconcile-formal-report.json)。已完成或仍持单的业务事实须逐笔核准，仍未自动回灌原旧端。

实际 DSH 从搁置通知暂存 640000 字节，原生工具仅以附件另交候选材料。暂存后管理端读不到，候选入池后按原附件号读回相同字节，下一位 Worker 仍无读权。原 W04 首次迟交仍回 409，转派后同号只回读原材料号，另交新材料被拒，无成果或凭候选释放持单。连续 12 次通过。[原生材料记录](evidence/dsh-native-candidate-artifact.json)。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_shelved_dsh_assignment_submits_candidate_without_changing_ownership`。模型、时钟和第二工位使用夹具。

隔离 Codex `/resume` 已用原生索引完成目录和搜索。冷开后只恢复绑定线程，查询可见同目录的另一会话，归档会话和其他目录都不显示。历史会话查询前后未加载，无新增模型请求或工单，原会话仍绑定，选择编号明确提示换绑未接。连续 12 次通过。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_resume_catalog_uses_only_current_app_server_workspace_without_loading_history`。[原生目录记录](evidence/codex-app-server-native-catalog.json)。

隔离 App Server 的 `/queue` 已接通。Worker 观察为空闲后宿主突然开始普通对话，原生写前明确拒绝队首投递，另两个消息仍排队。空闲后成功写入，故意丢回执则查原输入。两个排队消息按顺序各进一次新回合，原普通回合也只有一条输入。连续 16 次通过。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_app_server_next_turn_queue_never_steers_busy_turn_or_replays_lost_ack`。[原生队列记录](evidence/codex-app-server-native-queue.json)。平台与模型为夹具。

Linux arm64 已复验 `827eedcc65273a0897b7e697e9a4a9582a498135`，通过 135 项，47 项依赖本机 Codex 或 DSH 跳过，校验 147 个发行文件。独立系统上的隔离 TLS 只留一条正式成果通知，身份和证书反例通过，宿主为夹具。

隔离 Codex App Server 已接平台 `/stop`。Worker 持单时拒绝，适配器也独立核对原生单输入与私聊收件账本。普通聊天停后回读原回合和输入号。故意丢停止回执，同号重投仍是待核，第三个真实回合继续运行直至接到自己的停止命令。连续 20 轮通过。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_platform_stop_interrupts_only_the_proven_private_app_server_turn`。[试验记录](evidence/codex-app-server-private-stop.json)。本地平台和模型夹具。

`codex-provision` 已实测新建独立原生测试工位。一次初始化输入后退出并冷开 App Server，线程、回合和输入号全部读回。独立 TLS 适配器使用该线程接工单，原生工具调用回同一 W04 凭据且仅一条成果通知。连续 10 轮通过。默认无授权登录时在建线程前拒绝，脚本模型尝试在初始化时调用工具也被拒绝，无成功凭据。复跑 `python3 -m unittest -v rewrite.test_codex_app_server`。[初始化试验](evidence/codex-native-provision.json)。模型为本地脚本。

独立 Codex 的 `/model` 和 `/effort` 读到指定原生会话当前为 test_wire/gpt-5.4、high，与本地模型请求一致。`/context` 从原生用量事件和回合历史读到第 1 回合输入 11、总计 16、模型窗口 258400。连续 10 轮通过。原生模型目录没有标明各项所属提供方，当前只展示会话选择，切换仍待核。[回读证据](evidence/codex-app-server-native-context.json)。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_model_effort_and_context_read_native_app_server_observations`。模型为本地脚本。

Linux arm64 已复验 `c10762113fa53737338acefd16af0cfee915e9e7`，通过 135 项，44 项依赖本机 Codex 或 DSH 跳过，核对 143 个发行文件。同机跨系统 TLS 复核原生收件及正式完工通知各一条，宿主与模型使用夹具。

同机两个隔离宿主已实测 Codex App Server 向 DSH 发原图、DSH 按原消息号回复。两端都由本机原生工具执行器产出请求，原图摘要一致，Router 记录两端宿主凭据，重投不增输入，未建工单。连续 10 轮通过。[跨宿主记录](evidence/codex-dsh-native-peer.json)。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_codex_and_dsh_roundtrip_native_peer_image_and_linked_reply`。双方模型为脚本，两进程同在 Mac。

在实际 App Server 发现活跃时调用 `turn/start` 会复用当前回合，后续历史可见两条输入。重做适配器已在正式写入前确认空闲，原生工具调用也核对该回合只存在一条用户输入。真实 Router 取消原派发时，已核只中止该原生回合。另一个回合活跃时续用原控制号等待，它仍保持活跃，随后空闲才收到取消。资源清理无凭据时不释放持单。两个场景各 12 次通过。[取消证据](evidence/codex-app-server-router-cancel.json)。复跑 `python3 -m unittest -v rewrite.test_codex_app_server`，模型为本地脚本。

独立 App Server 的工单提问已从原生 `worker_ask` 走 W05。管理端的原回答经同一工单、派发和题号回到实际线程，原生工具回报完工后才释放。原答复重投只返回原回执。适配器只接受已知 Worker 工具名，拒绝任意工具、错线程和错回合。连续 12 次通过。复跑 `python3 -m unittest -v rewrite.test_codex_app_server`。[问答证据](evidence/codex-app-server-native-question.json)。本地脚本模型、隔离管理驾驶，无飞书云端。

独立 Codex 适配器已把新结束回合接入原 Worker 的卡片投影。首次绑定前的试验回合零抄送。Worker 先记回复再丢回执时，适配器重投同一事件，前后确认的序号一致。用另一 App Server 进程读取同一历史和账本，原输出未重复。连续 10 轮，每轮原卡形态的工单和回复各一张、Router 成果通知一条。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_codex_adapter_process_handles_native_report_without_controller_dispatch`。[出向证据](evidence/codex-adapter-output.json)。飞书和模型由夹具提供。

新增独立 `codex-adapter` 启动入口。用专用 profile 核原生线程与工作目录后恢复线程，在 TLS 上接真实隔离 Worker。App Server 的原生请求由适配器直接转交 W04，控制器未代调。连续 12 次，两次原生工具调用均只给一条 Router 通知，错工作目录、错会话和第二个 profile 写者都被拒。复跑 `python3 -m unittest -v rewrite.test_codex_app_server.CodexAppServer.test_codex_adapter_process_handles_native_report_without_controller_dispatch`。[原生进程记录](evidence/codex-adapter-process-w04.json)。模型用本地脚本，Desktop 插件及真实模型待授权。

独立 Codex App Server 已用 `localImage` 接图文与纯图。飞书夹具经实际 Worker HTTP 上传，原生模型请求保留测试原图 SHA-256，图文正文保留换行与空格。模拟平台丢失 Worker 回执后只查原输入，原生各一条、Router 无工单。连续 12 轮通过。复跑 `python3 -m unittest -v rewrite.test_codex_app_server`。[图文记录](evidence/codex-app-server-native-media.json)。飞书与模型均为夹具。

新增离线快照持单核对。实测 Router 已接收 W04、Worker 丢回执，命令报持单不一致和一条待核报告。隔离恢复后读回原成果，只留一条通知。互换同会话名的适配器账本时，Router 与 Worker 即使持单一致，命令仍指出缺原收件证据。原始快照校验前后一致，输出不含工单正文。复跑 `python3 -m unittest -v rewrite.test_state_bundle`，CLI 见[迁移演练](router-rehearsal.md#重做版五组件的离线恢复)。

独立 Codex App Server 的指定回合可生成原 Worker 样式的飞书卡。原生普通输入和最终答复各一张，原空格及换行保持，重投无新增。同套原生 W04 记录只投影公开答复，不抄工具参数或桥接正文。连续 12 次通过。复跑 `python3 -m unittest -v rewrite.test_codex_app_server`。[卡片记录](evidence/codex-app-server-native-display.json)。原生 App Server 实际运行，输入与模型由驱动生成，飞书替身。

独立 App Server 以原生 `turn/interrupt` 停止一个已确认的原回合。错输入由适配器拦下，错回合由原生 API 拒绝，正确回合由历史证实已中止。Router 取消待清理，持单不释放、无成果通知。连续 15 次通过。复跑 `python3 -m unittest -v rewrite.test_codex_app_server`。[宿主证据](evidence/codex-app-server-native-interrupt.json)。控制由测试驱动发起，模型为本地脚本。

独立 Codex App Server 在 W04 工具请求待响应时中断。清除适配器宿主凭据后，由新进程重开同一测试会话，查回同一宿主输入。原派发重投未产生第二个输入或模型请求，Router 保留原持单且无成果通知。连续 20 次通过。复跑 `python3 -m unittest -v rewrite.test_codex_app_server`。[原生恢复记录](evidence/codex-app-server-native-reconnect.json)。真实进程使用本地脚本模型，无生产调用。

Linux arm64 以 `98097e25e6d3632d6fe7360d3b67e9f3829fe7ab` 复验通过 133 项，36 项因缺少本机 Codex 或 DSH 宿主跳过，校验 132 个发行文件。同机跨系统 TLS 试验仍只记录一个原生输入和一条成果通知，宿主与模型用夹具。

独立 Codex App Server 的原生动态工具协议已接通隔离 Router。原生线程从 Worker 收一条正式派发，重投无第二条。实际 `item/tool/call` 两次调用 W04 回同一结果、只发一条待验通知；错线程和错回合均拒绝。首次查历史偶发暂不可用，保留待核，仍按原投递查回。连续 30 次通过。复跑 `python3 -m unittest -v rewrite.test_codex_app_server`。[原生记录](evidence/codex-app-server-scripted-w04.json)。原生进程实际运行，模型是本地脚本，无独立登录身份。Desktop 测试项目缺工具的原证据仍有效。

DSH 原生工具执行器已走通带附件的 W04。563,200 字节测试文件由隔离适配器预先登记，原生回合调用暂存工具。最后一个分块回执丢失后，按原上传号查回，三块字节均只写一次。W04 前管理端取不到文件，正式回执后读取的字节和摘要与原件一致，只有一条 Notify。普通私聊和只获读权的训练员附件不能用此入口暂存。连续 12 次通过。[原生证据](evidence/dsh-native-artifacts.json)。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_native_w04_stages_private_artifact_before_sharing_it_with_manager rewrite.test_work_files`。模型用本地驱动，测试不连接真实飞书。

工单更新与取消实测续用各自的投递号查回。同会话空账本均收零条，接回原账本后原输入数分别保持一条。取消回执未知时仍保留持单；另一个场景模拟原投递途中断线，收到 Router 随后发出的新取消指令才进入宿主。复跑 `python3 -m unittest -v rewrite.test_link_health rewrite.test_management rewrite.test_question_cards`。临时 Router 和 Worker 实际运行，宿主采用夹具。

版本 `d9ebc890f5605feb7511fea787e00a5d48f2db64` 再跑 Worker 与适配器分步安装。旧包先运行，先升适配器、再升 Worker、最后回退，四阶段查询同一原收件号。三个原生账本各只有一条输入，原快照摘要一致。Linux 通过 131 项，34 项因 macOS DSH 跳过。复跑 `python3 -m rewrite.release_check --repository . --previous 3e0eba5426f8c0f1344f195929ecae84b1c546b5 --new d9ebc890f5605feb7511fea787e00a5d48f2db64 --component worker-pair`。旧版写者已停止，宿主为夹具。

隔离启动增加按组件状态库生效的进程锁。实际 TLS Router 留一张待派单，另一个进程试用同一数据库，退出码 1，首实例仍可查原单。终止首个试验进程后保留原锁文件，新进程成功打开同一数据库。备份遇仍持锁的组件立即拒绝，未生成目标目录。复跑 `python3 -m unittest -v rewrite.test_service rewrite.test_state_bundle`。锁覆盖新版独立启动入口，老程序仍须按迁移手册停写。

Linux arm64 以 `3e0eba5426f8c0f1344f195929ecae84b1c546b5` 复验通过 130 项，34 项因缺少 macOS DSH 跳过，校验 128 个发行文件。macOS Worker 与 Linux 隔离 Router、宿主夹具用不同 TLS 端口，核对原生输入与正式完工通知各一条，拒绝未知身份和不受信证书。物理机相同，无真实模型或真人飞书参与。

W03 后断在首次原生收件回程时，Worker 已保留原派发与宿主待核状态。隔离测试切到同名空账本，重复轮询两次无新输入；恢复旧账本后查原收件号，工单仍由原 Worker 持有。TLS 查回端口核对收件号、会话及原派发内容。实际 DSH 在宿主尚未写入前明确拒绝忙时派发，原私聊结束后才收到正式单；若先取消，只收到取消。复跑 `python3 -m unittest -v rewrite.test_mainline rewrite.test_service rewrite.test_dsh_native_loop.NativeLoop.test_native_race_after_w03_never_prequeues_a_formal_task`。

`/queue` 实测在本地 HTTP 发出前已将投递标为待核。原宿主收件后丢回执，换到同会话号的空账本后两次复核均无写入，接回原账本查到原输入号。仅各有一条待核与恢复提示。真实 DSH SDK 的排队测试同时验证两条消息依原次序进入独立回合。复跑 `python3 -m unittest -v rewrite.test_worker_commands rewrite.test_dsh_sdk`。平台及模型使用夹具。

Worker 后台遇到临时 SQLite 写入错误会继续轮询。隔离测试注入一次锁错误，原线程恢复后接管待派工单，宿主只有一条输入。另用实际 HTTP 返回非 JSON、缺会话、数组和损坏的 SSE，核对宿主状态标为待核，Router 连接保持，恢复后可继续接单。Router 暂断时同伴状态保留本地 Router 收件时间，对端两层留空。复跑 `python3 -m unittest -v rewrite.test_link_health rewrite.test_peers`。Router 与 Worker 运行于本机隔离环境，故障与宿主用夹具。

飞书入站及出站循环遇到暂时数据库锁、无效上游响应仍会继续运行。隔离回归先注入入箱读取锁和显示响应错误，再让实际 Worker 写入但返回缺回执的数据。平台保留待核，退避后查回原凭据，宿主只有一次输入，题卡队列仍继续。复跑 `python3 -m unittest -v rewrite.test_feishu_inbound.PlatformInput.test_platform_queue_recovers_after_locked_inbox_and_a_malformed_worker_receipt`。故障为注入，Worker 传输实际运行，未连飞书云端。

DSH 原生 `worker_peer_inbox` 已验证从自己的收件箱补查原消息号和回复关系。隔离回归核了待回执游标、确认后移出待办、原样重复读取、历史不含正文。原生 DSH 在收件回合先查历史再发回复，只产生一条回复。复跑 `python3 -m unittest -v rewrite.test_peers.Peers.test_inbox_cursor_and_history_are_read_only_and_keep_reply_correlation rewrite.test_dsh_native_loop.NativeLoop.test_dsh_peer_image_and_native_reply_travel_on_the_same_private_chain`。

飞书官方 Python SDK 1.7.5 通过实际回环 WebSocket 验证 TCP 中断后自愈。测试直接断开连接，无 WebSocket 关闭帧。SDK 第二次取地址并重连，三次事件回包为 200，其中一条是断线前事件重发。共享入箱只有两条，另一个进程重新打开入箱并交给实际 Worker，宿主输入也只有两条。连续 6 次通过。复跑带 SDK 的 `python3 -m rewrite.feishu_reconnect_check`。[传输记录](evidence/feishu-sdk-reconnect.json)。未连真实飞书，原生宿主用夹具。

两套独立原生 DSH 进程已在同机跑通同伴图文及关联回信。双方 AgentLoop 都通过宿主工具执行器调用 `worker_send_peer`，Router 两条原消息分别有接收方原生凭据。输入原图摘要一致，回复带原消息号，两端重读都无重复输入，Router 无工单。连续 10 次通过。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_two_real_dsh_sessions_roundtrip_a_linked_peer_message`。[双宿主原生记录](evidence/dsh-native-peer-two-hosts.json)。两端模型由本地脚本驱动；不涉及真实飞书或跨物理机器。

Linux arm64 回归使用提交 `318e905a241229c2de276bd5917293fc969507bc`，通过 125 项，33 项因依赖 macOS DSH 跳过，核对 125 个发行文件。macOS Worker 经独立 TLS 接入 Linux Router 和测试宿主，只见原输入和通知各一条。该跨系统试验在一台物理机上运行，模型和两宿主用夹具。

同伴私聊的接收绑定现在也随投递保存。隔离实测在原生收件后丢回包，依次更换为另一会话、同会话但空账本的适配器，再接回原账本。前两步保留待核，原会话始终只有一条输入，另一会话为零。接回时查原凭据并回告 Router。远端 TLS 查证路径拒绝错误接收方。复跑 `python3 -m unittest -v rewrite.test_peers.Peers.test_uncertain_peer_receipt_never_writes_to_another_session_or_a_fresh_ledger rewrite.test_service.Service.test_pinned_worker_rejects_same_authenticated_adapter_when_session_differs`。

DSH 原生 `worker_list_peers` 已验证可读到登记的对端，再用原生工具回复对方。独立 Router 验了分页、在线筛选、未登记身份拒绝，以及查询结果无工单或持单字段。管理查询权限照旧。复跑 `python3 -m unittest -v rewrite.test_peers.Peers.test_only_registered_peers_see_worker_ids_without_task_ownership rewrite.test_dsh_native_loop.NativeLoop.test_dsh_peer_image_and_native_reply_travel_on_the_same_private_chain`。[原生目录与私聊记录](evidence/dsh-native-peer.json)。模型为本地驱动。

真实 DSH 收到 Router 的搁置通知后，通过原生 `worker_submit_material` 留下一份文字候选。同一工单的迟到 W04 仍回 409，正式报告为空，交材料时持权留在 Router。转给另一测试工位后，原调用只取原材料号，新调用拒绝，第二派发仍由新工位持有，待清理仍指向第一派发。连续 12 次通过。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_shelved_dsh_assignment_submits_candidate_without_changing_ownership rewrite.test_pool rewrite.test_work_files`。[原生记录](evidence/dsh-native-material.json)。第二工位宿主与模型为夹具。附件候选由独立协议回归覆盖。

DSH 原生 `worker_peer_status` 回读 Router 收件、Worker 入箱及原生回执。真 DSH 工具实测，原生回包丢失时先见前两层，第三层为空；后台续办后才有原生确认。另以系统级夹具从发送前断线复跑，工具返回原消息号和 pending，对端读不到该未入账记录。重复查询不补发。复跑 `python3 -m unittest -v rewrite.test_peers.Peers.test_agent_can_read_pending_outgoing_message_without_claiming_acceptance rewrite.test_dsh_native_loop.NativeLoop.test_dsh_peer_image_and_native_reply_travel_on_the_same_private_chain`。

发件 Worker 已能自动恢复待确认的同伴私聊。实测 Router 提交前断线、提交后丢回包两支，重开原 Worker 后不再调用发送工具，待发送队列分别用原键补交或查回，对端各只收一条。未登记工位的拒绝保存在本地，后台不重复发送。复跑 `python3 -m unittest -v rewrite.test_peers.Peers.test_outgoing_peer_send_recovers_both_sides_of_router_accept_without_a_new_call`。Router、Worker 和 HTTP 实跑，宿主与时钟为夹具。

实际 DSH AgentLoop 收到同伴的一条带原图私聊，经原生工具执行器调用 `worker_send_peer` 回复原消息。隔离 Router 可分别观察三层收件。原生回执丢失时按原投递查回，同一工具回调重投返回同号，换正文拒绝，发送端只收一次回复。原图摘要吻合，第三工位读不到消息，没建工单。连续 20 次通过。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_dsh_peer_image_and_native_reply_travel_on_the_same_private_chain`。[实测记录](evidence/dsh-native-peer.json)。接收端是实际 DSH，模型和另一工位宿主是夹具。

新版派发在 W03 前固定原生会话。隔离 Router 接管成功后立即换到另一个已认证的适配器，旧单保持 working，另一会话没有收到它；单独给另一会话构造同号输入后，那里发出的 W04 被拒。重开原绑定后，原派发只有一次原生输入，正式 W04 才释放持单。复跑 `python3 -m unittest -v rewrite.test_mainline.Mainline.test_assignment_keeps_original_native_session_across_replacement`。Router、Worker 及传输实际运行，宿主为夹具。

首次确认宿主会话前断链时，普通私聊先留在 Worker 入箱。隔离测试在探测过程能读回已提交的消息，此时没有原生输入。重开 Worker 后同号继续首次投递，回执可重查，原生账本一条。复跑 `python3 -m unittest -v rewrite.test_mainline.Mainline.test_private_chat_pending_before_adapter_probe_resumes_once`。宿主为夹具，此处只验证入箱到首次写入的边界。

已加独立工位的只读运维检查。三进程 TLS 中，测试平台身份读状态被拒，专用运维身份读到预期和确认的同一会话及当前持单。两个真实回环断链试验分别核 Router 重连倒计时及宿主忙闲未知，另一链保持可观察。复跑 `python3 -m unittest -v rewrite.test_link_health rewrite.test_service`。原生宿主为夹具。

旧飞书私聊增加离线专用导出。用实际旧 RouterStore 写两工位记录，按一工位分页、筛选，并逐项比对旧查询的原 ID、方向、消息号、原会话、时间和文本。另一工位记录未导出，原库摘要未变，坏快照拒绝，不触发新 Router 或平台。复跑 `python3 -m unittest -v rewrite.test_cutover.Cutover.test_export_legacy_chat_keeps_original_fields_and_private_scope`。未取生产私聊日志；旧在线旁听的现有使用方另核。

用两版发布包实跑 Worker 与适配器逐个升级、回退。旧包、先升适配器、升完整套、旧包回退返回同一原生回执；升级版 Worker 已保存从原账本查到的会话和回执。各阶段原生账本只记一条，三份原快照摘要未变。[复跑命令](router-rehearsal.md#worker-和适配器逐个升级)与[原始输出](evidence/worker-pair-release-trial.json)。独立进程和回环 TLS 实跑，宿主与平台为夹具。

DSH 真正关闭原生 Agent 后，旧适配器的忙闲、输出游标和目录检查均报 503。原生注册表没有新增实例，解析器没有再调。宿主明确用同会话 ID 恢复出另一个 Agent，旧适配器写入仍被拒，新实例为零输入。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_closed_native_binding_never_resumes_or_sends_to_a_new_agent`。实际安装包的 AgentRegistry、持久会话与私有 Unix 套接字，测试控制器调用原生恢复。没有触发真实模型、飞书或生产会话。

普通私聊经 Worker 重启或更换适配器时，同飞书消息号已确认为原会话则返回原回执；首次投递状态未知时只查回。隔离实测更换为另一会话，三条旧消息（含无绑定的历史记录）都未写入新会话，只有新消息进入。即使原会话的适配器账本空了也没有二次写入；恢复原账本后查到两条原回执。复跑 `python3 -m unittest -v rewrite.test_mainline.Mainline.test_chat_replay_stays_with_original_native_session_after_restart rewrite.test_service.Service.test_pinned_worker_rejects_same_authenticated_adapter_when_session_differs`。SQLite、HTTP/TLS 与重启接续实测，宿主为夹具。主动换绑仍待验。

Worker 支持用 `--adapter-session` 固定 HTTPS 目标会话。专用请求路径会在服务端逐次核会话；适配器地址及认证相同但原生会话换了，忙闲和发送均回 409，错误会话零输入。指定正确测试会话才取得本机收件。复跑 `python3 -m unittest -v rewrite.test_service`。这里验证的是进程、TLS 与绑定拒绝，宿主为夹具。

DSH 在 W03 的回包之后、原生收件之前突然变忙时，会在入原生队列前明确拒收。适配器只对明确没发起写入的状态允许后续重试。真宿主复跑两支：自然空闲后原派发只收一次；此间先收到 Router 取消，宿主只收取消指令，普通对话照常完成，原派发执行从未开始。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_native_race_after_w03_never_prequeues_a_formal_task`。[原生事实](evidence/dsh-native-admission-race.json)。实际 DSH 与隔离 Router，本地模型。

[验收对照](acceptance-matrix.md) 汇总旧入口、Worker 篇 A01–A11、原生宿主证据与切换前剩余条件。

DSH 图文与纯图已通过真实 AgentLoop 的输入边界。分别给飞书夹具一条带原始空格和换行的图文及一条纯图，原生模型传输各读到同一原图的完整字节和摘要。两次重送共两条原生输入，飞书夹具各一张最终回复。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_actual_dsh_loop_gives_the_same_feishu_caption_and_original_image_to_its_provider`。[原生图文证据](evidence/dsh-native-media.json)。模型是读取实际字节的本地夹具，图像语义识别和飞书云端待验。

正式工单的 DSH 长目标已在隔离环境走通。原生工具创建目标，宿主跑一轮原生续办并标记完成；此时 Router 仍为 working，Worker 仍持原派发。Router 空闲后发原单询问，模型在该原生回合自行调用 `worker_report`。W04 后才释放，Router 待验收。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_native_goal_finishing_releases_formal_work_only_after_its_router_probe_w04`。[长任务证据](evidence/dsh-native-formal-goal.json)。模型为本地驱动。自动续办轮本身的 W04 来源仍缺独立授权链，本路径从 Router 的正式询问取得原派发。

旧邮件和游戏游标已有离线导入。邮件从原 UID 10 接续，只收 11 与 12；错误 UIDVALIDITY 保留 10，没进新档。游戏核对原地址，序号落后时不打开流；恢复后从 15 接续，只收 16 与 17。重导入后位置不退。复跑 `python3 -m unittest -v rewrite.test_feeds`，邮件协议夹具、真实临时 SSE 传输。[交接规程](router-rehearsal.md#旧采集来源交接)。旧游戏桥没有启动标识，生产游标与旧队列在授权窗口对账。

旧 Trigger 0.7.0 的归档已通过隔离保全与回退测试。用原版 TriggerStore 建 5 条不同处置的事件及订阅，确认源文件摘要未变、恢复档案逐字节相同、真实旧程序仍只查到原来两条未读记录。活跃日志和被篡改的成员会阻止恢复。Mac mini 只读盘点确认仍有 1,492 条记录及 1 个 active 订阅，[聚合证据](evidence/legacy-trigger-status.json)。未复制生产原文，未启用订阅。复跑 `python3 -m unittest -v rewrite.test_trigger_archive`，操作见[旧 Trigger 归档](router-rehearsal.md#旧-trigger-历史和订阅账本)。

工位收到原单更新、取消、转接和搁置即保存飞书提示。原生收件有独立凭据，控制提示也不会当作已经停止或清理。隔离实测，更新输入已进宿主但回包丢失、取消还没进宿主时，飞书待发队列均已有对应正文。恢复后原生各一条，飞书无重复，Router 仍待取消清理。复跑 `python3 -m unittest -v rewrite.test_link_health.LinkHealth.test_task_cards_use_worker_acceptance_independently_of_host_receipts`。真实工单与 Worker，宿主和飞书为夹具。

DSH 的正式题已跑通原生 `worker_ask`、Router 回题和原派发续办。答复回包故意丢失，原生会话只有一条答复输入；`continue_requested` 保留同一个派发和持单。溪流更新原单后，下一原生回合自行用 W04 交成果，Router 待验收，工位释放。飞书展示中的两条题卡事件均归溪流，训练员无作答入口。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_native_formal_question_returns_to_its_assignment_and_keeps_ownership_until_completion`。[正式题答复与回执](evidence/dsh-native-work-question.json)。真实 DSH 工具执行器和隔离 Router，模型、经理输入为测试驱动。

DSH `/stop` 已走真实 AgentLoop。普通回合留下原生中止记录，原生排队内容在下一次输入后照常入场。丢回包时显示待核，同号重投不影响之后的回合。实际隔离 Router 下发正式单后，飞书命令和宿主端私聊停止入口都拒绝停止工单，原 Router 取消路径仍可中止并保留清理待核。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_stop_targets_only_current_private_turn_and_never_replays_a_lost_result`。[原生停止记录](evidence/dsh-native-stop.json)。宿主真实，模型为本地夹具。

DSH `/resume` 已通过宿主原生查询读取当前会话和显式获准的历史目录。隔离测试读回 12 个条目，其中 11 个是已关闭的会话。验证两页、按标题或 ID 搜索、缺失记录提示及另一 Worker 收 403。未配置的会话连标题都没有进入适配器回应。浏览前后仅原会话处于活动，无原生输入、无新工单。换绑仍待接。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_resume_browses_only_configured_native_cold_sessions_without_activating_them`。[原生目录证据](evidence/dsh-native-catalog.json)。

DSH 原生计时题过前台等待期后，受支持的单题可接入训练员飞书题卡。原生工具在隔离 DSH AgentLoop 里自行运行并记录待回答状态。测试平台分别提交选择、自定义与跳过，验证原回合和原生题号、原卡更新、非绑定用户和非原卡拒绝。选择回包丢失后只从原生会话查回，三题各一次原生答复，多选保持在 DSH。另测开卡回包丢失，原生入口先选第一项，迟到的飞书第二项被拒，原卡按实际入场的答复更新。Router 无工单。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_dsh_native_timed_question_closes_the_original_feishu_test_card`。[原生记录](evidence/dsh-native-questions.json)。真实 DSH 工具及回合，模型为本地夹具，飞书为测试平台；原生前台阻塞题和真人飞书待验。

DSH `/compact` 已从绑定的 Worker 指令入口调用安装包的原生压缩服务。隔离测试丢弃首次提交回包，压缩期间和 Worker 服务重新初始化后重查原号，只观察到一次摘要请求、一组原生开始/摘要/替换/结束记录和一条压缩结果卡。DSH 在维护期间仍返回 idle，适配器会将自己发起的在途压缩报为忙碌。无可压缩历史时显示实情，缺证超时为待核。Router 无工单。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_compact_reconciles_a_lost_start_reply_against_the_native_session rewrite.test_worker_commands`。[原生压缩证据](evidence/dsh-native-compact.json)。真实 DSH 压缩引擎，模型为本地夹具。

DSH `/goal 目标正文` 在绑定会话通过原生服务建立目标，并使用宿主默认轮次。实测保留换行、同号重送原回执、已有未完成目标时拒绝替换。轮次驱动的验收改为由此入口建立目标后再暂停和恢复，Router 始终无工单。证据覆盖真实 DSH 循环与本地测试模型。[目标回执与两轮记录](evidence/dsh-native-goal.json)。

DSH 原生目标续跑已通过隔离测试。安装包自带的轮次驱动实际入场第 1 轮。用 Worker `/goal pause` 后第 1 轮中止，`/goal resume` 后同一目标的新版本入场第 2 轮并完成该回合。两条旧指令重送只取原回执。Router 始终无工单、无通知，Worker 无持单。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_native_goal_rounds_pause_and_resume_without_becoming_router_orders`，macOS 全套 133 项通过。模型传输为本地夹具。

DSH 的飞书 `/goal` 查询、暂停和继续接到原生目标服务。隔离回归查到同一目标的版本变化，暂停和继续各一条原生记录。丢失继续回执时保留待核，重送旧消息没有重复变更。同一会话未出现普通模型输入，Router 未建单。未加载目标服务的会话提示暂未接入。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop.NativeLoop.test_goal_query_pause_and_resume_use_the_bound_native_goal`，macOS 全套 132 项通过。本项核验目标服务与持久状态，自动续跑另验。

提交 `ab112dc8a6ee60e89b0af5bba184d9526750bbf6` 的 Linux arm64 复验通过 111 项，20 项 macOS DSH 测试跳过。跨 TLS 测试复核 105 个发行文件，原生宿主夹具只记一条输入、Router 只新增一条成果通知，认证反例通过。

Antigravity 桌面兼容层新增接口对照。本机 App 为 2.32.1。Google 文档提供工作区钩子、原生会话及转录路径和 MCP 工具入口。MCP App 的消息能力从 2.34.0 起，当前仍缺按指定会话后台唤醒和取消的实测。先留隔离验收入口，不以日志或资料推断飞书全链已通。[Antigravity 兼容层核对](antigravity-compatibility.md)。

Hermes 插件已能向单独登记的飞书会话投递收件提示。模拟 Notify 在 Hermes 忙时成功入箱，正文等空闲 10 秒，飞书只收到来源与等级。丢失平台回执后，重启续用同一消息号。超出安全重试窗口的提示保留待核，更换绑定也不会转发旧提示，后续 Event 照常进入宿主。复跑 `python3 -m unittest -v rewrite.test_manager_notices` 和带 SDK 的 `python3 -m rewrite.feishu_sdk_check`，macOS 全套 131 项通过。Router 与 Trigger 走实际 HTTP，通知测试的 Hermes 宿主及飞书返回为夹具。另跑真实 Hermes 隔离 LOCAL 输入，等待 11.5 秒后交 Notify，Event 续跑查回同一原生收件。正式飞书会话另验。

派单飞书卡现在与本体 W03 接管一起入库，卡片明确写「已接单，正在交给宿主」。此前宿主回执丢失会留下持单、漏掉派单卡。隔离回归主动抛弃原生回执，本体仍产生一张工单卡。查回原回执后只有一次宿主输入和一张派单卡。复跑 `python3 -m unittest -v rewrite.test_mainline rewrite.test_link_health rewrite.test_dsh_native_loop`，macOS 全套 129 项通过。原生控制指令仍各自按实际宿主回执显示。

DSH 的真实原生工具执行器已通过隔离 Worker 向 Router 报完工。复跑时发现首调用可能早于适配器写宿主收件凭据。修正后，Worker 只查询已存在的投递意图和指定原生输入，并核对原回合再放行。隔离测试主动丢弃原生投递回执，两个原生 `worker_report` 调用仍取同一业务回执。发出 W04 时 Worker 持原单，收到回执后清持单，Router 只发一条通知并停在待验收。回读宿主只存在一条工单输入。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop`，同场景连续 30 次通过，macOS 全套 128 项通过。实测使用实际 DSH 运行循环和工具执行器、临时 Router/Worker 及本地测试模型。真实账户和 Codex 原生 W04 另验。[原生与业务回执](evidence/dsh-native-w04.json)。

DSH 隔离环境已使用安装包中的真实 AgentLoop、原生工具执行器和 V4 会话存储。回合 1 在模型流中止，已排队的普通聊天和取消指令分别进入回合 2、3。控制投递重试仍返回回合 3 的原凭据。原生模型请求及后续两条请求均带已选 `high`。复跑 `python3 -m unittest -v rewrite.test_dsh_native_loop`，macOS 全套 127 项通过。本项采用进程内测试模型及直接适配器调用。工作环境清理仍需独立凭据。[原生回合记录](evidence/dsh-native-loop.json)。

DSH 原生停止请求限定到已确认过的工单回合。适配器将原派发的输入号和回合号交给插件。在原生会话仍处于这一回合、只有该工单输入且会话位置一致时，插件发出保留原收件箱的停止请求。普通私聊回合保持运行。隔离回归中丢弃取消指令的本地回执后，插件从原生日志查回同一收件，停止调用仍只有一次。相关测试用实际 DSH SDK 读写原生日志，执行停止的 Agent 与模型为夹具。停止请求、取消指令送达及原工作区清理确认分别核验。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk`，macOS 全套 126 项通过。

DSH 取消的待核分支已复跑。隔离 SDK 先记原回合中止，再向同一会话发送原派发的取消请求。两次试报均因缺少工作环境清理凭据返回 `native_cleanup_unconfirmed`，Router 保留原派发与清理记录，Worker 保留持单并只发一张待核卡。另用通用宿主夹具提供可核凭据，验证 Router 回执后才发确认卡。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk.DshSdk.test_cancel_stops_reporting_at_missing_native_cleanup_even_after_an_aborted_turn rewrite.test_cleanup`，macOS 全套 124 项通过。DSH 停止及工作区清理的正向实机验收仍待补。

DSH 的目录翻页和数字选择已接入。菜单产生后要等飞书返回消息号，Worker 才接受编号。模拟飞书已收、Worker 尚未记菜单回执时，旧编号不会误选，重投原卡仍只有一个平台消息号。力度菜单打开后手动换到另一模型，即使两模型都支持同一编号和强度，旧菜单也被宿主拒绝。重新查询后可换到 high。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk.DshSdk.test_numbered_model_choices_use_the_last_published_page_and_effort_checks_its_route`，macOS 全套 123 项通过。实际 DSH SDK 和 HTTP 已运行，飞书和目录为夹具。

回退回放同时验证了真实管理工具生成的 `hermes-…` 建单请求号。旧 Router 只接收 UUID，演练按来源生成稳定的旧请求号，并在计划中保留原号。两次生成计划一致，经旧 HTTP 重送仅入队一次。前一版 `8ecfd7ce9418862cb98eea0cb1df69d7b15af498` 在 Linux arm64 复核了 98 个发行文件，108 项通过，14 项 macOS DSH 测试跳过。跨 TLS 复跑仍只有一条夹具宿主输入和一条成果通知。

仅增加未派工单的回退分支已完成隔离回放。用真实新 Router 建两单，审核两条均能用旧目标模式，在全新目录恢复旧库，再经真实旧 Router HTTP 以原请求号回放。两条首次均为 202，重复取到原旧单号，改正文为 409，原队首保持，新增两单标题、正文和顺序一致。新试运行库与原快照保留，macOS 全套 122 项通过。已派发、已产生成果或其他新写入仍需对账。[复跑入口与审核方法](router-rehearsal.md#试运行只增加未派工单时的回退演练)。

旧 Router 回退新增只读对账。在停写副本用同版重建基线后核对每张表。实际隔离 Router 新建一单时可定位原请求号；W03 接管并交宿主后，该单退出候选，差异列出 Worker 与投递记录，直接回退仍拒绝。无处理人及旧端未知工位也分开标明。对账前后试运行库摘要一致，篡改基线被拒。复跑 `python3 -m unittest -v rewrite.test_cutover`，macOS 全套 121 项通过。已有新写入的实际回迁仍待演练。

DSH 已在独立会话验证 `/model 提供方/模型` 与 `/effort 强度`。原生 SDK 写入并读回 `model/selection`，指令回执带会话、选择和原生序号。忙碌或不支持的选项被拒。模拟原生写入后丢回包、Worker 落回执前中断，原消息均留待核。手动选了新值以后重送同号，新选择未被覆盖。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk`。SDK、原生日志与两段 HTTP 实跑，模型目录及飞书为夹具，Worker 默认值同步、数字选项、真实模型新请求仍待验。

DSH 飞书 `/model` 和 `/effort` 已核对绑定会话当前选择、最近持久化的模型请求配置及目录。真实 SDK 中会话已选 high、最近请求仍为 medium 时，卡片显示两份状态。目录最多展示 200 项，每页十项，单个提供方失败时保留其余条目。回放原事件保持原卡，观察过程没有新增回合或模型选择记录。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk.DshSdk.test_model_and_effort_distinguish_live_selection_from_recorded_request`，macOS 全套 118 项通过。目录、模型请求和飞书由测试驱动提供，切换和默认配置同步待接。

历史私聊提交 `c99fa4353eaa7102c5e7c24604d36673590343a0` 已在 Linux arm64 再验。发行包 97 个文件复核通过，回归 106 项通过，11 项 macOS DSH 测试跳过。macOS Worker 跨 TLS 交接后的宿主夹具输入仍为一条，W04 只生成一条通知。

旧 Router 私聊迁移保留原消息号、回复关联、工单号、确认或放弃时间、分段正文和附件。测试由实际旧 RouterStore 写入数据，两个新端 HTTP 入口查回，原图逐字节一致。第三方和平台入口无法读取，收件人直接用历史附件引用提交 W04 时被拒，持单仍在。原号重送未唤醒宿主，未确认消息或正文中间缺段会阻止生成试运行库。复跑 `python3 -m unittest -v rewrite.test_cutover`，macOS 全套 117 项通过。飞书私聊记录与订阅另验。

DSH 的飞书 `/context` 已接入绑定会话的版本 4 原生日志。隔离测试的第一条模型回包为输入 46、总计 51、窗口 32768；下一条改用另一路由且缺用量，三个数均显示「未记录」，同号重送仍读回第一张卡。错误 Worker 身份被适配器拒绝。真实 DSH SDK、SQLite、Unix 套接字和两段 HTTP 已运行，模型事件与飞书为测试输入；真实账户消费另验。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk.DshSdk.test_context_command_reads_only_committed_usage_for_the_same_route`。macOS 全套 115 项通过。

同伴消息和宿主回执按单条恢复。故障复现时，首条私聊的错误连续三轮阻挡后续私聊和新工单，接收工位原生输入为零、工单一直待派。修正后，同一时序下第二条私聊与正式派发均送达；首条凭据指向错误会话时保持待核。重启并查回原凭据后，首条只进宿主一次。一条上行回执暂时失败时其他回执照常提交。测试使用真实 Router、Worker、SQLite、HTTP，宿主与时钟为夹具。复跑 `python3 -m unittest -v rewrite.test_peers`，七项通过，macOS 全套 114 项通过。

macOS Worker 与 Linux arm64 Router、宿主适配器已通过两个独立 TLS 端口核验。两端安装同一提交 `2025c300`，Linux 复核 92 个发行文件。重投后宿主日志仅一条输入，回合结束仍为 working，夹具 Agent 报告后转待验收，只发一条通知。Linux 回归 102 项通过、10 项因 macOS DSH SDK 缺席跳过。实测使用同机 Linux 来宾，跨物理设备、真实模型调用另验。[原始记录](evidence/linux-cross-os.json)和[复跑说明](README.md#跨系统验收)。

| 范围 | 当前结果 | 复验入口与原始证据 | 下一项 |
| --- | --- | --- | --- |
| 正式主线 17fccee | 真实临时 HTTP Router 与 Worker；双宿主及 Agent 为夹具，14 项 | `python3 -m rewrite.devloop`、`rewrite/test_mainline.py` | 扩充 A/W 接口 |
| Desktop 指定会话 f4f7ba7 | macOS 原生输入确认，原派发重投 0 条额外输入；原生回合结束仍持单 | `desktop-check.md` 中的实际会话、原生输入与命令；`rewrite/test_codex_desktop.py` 5 项 | 独立测试会话工具注册 |
| W04 工具入口 2f69249 | macOS Desktop 当前隔离项目加载受阻。重做 MCP 子进程、私有套接字、Worker、Router 和 SQLite 联通；5 项新增，其中同回合多输入拒报。工具调用者和原生元数据为夹具 | `evidence/desktop-w04-probe.json`；`python3 -m unittest -v rewrite.test_project_report` | 取得可加载工具的专用测试宿主，再取原生工具调用项和业务回执 |
| A01/A03/A05、A06 assign（bb1a4e5） | 真实隔离 HTTP 已覆盖名单、工单列表分页、原单指定与修订。旧派发指令已变时，旧帧不能让宿主执行过时内容；已接管的修改用原派发通知一次。新增 4 项，累计 28 项 | `python3 -m unittest discover -s rewrite -t . -v`，`test_management.py` | 转派与旧清理 |
| 无法完成池、提醒与材料（5d7fd19） | 真实隔离 Router、SQLite、Worker，原生宿主和时钟为夹具。5 项新增，累计 33 项。覆盖续办保留持单、主动报阻、离线确认后连续 600 秒入池、上线清零重计、延后提醒、独立材料幂等及与恢复竞态。材料由认证 Worker 调用 `POST /v1/tasks/{task_id}/materials`，附件待接。 | `python3 -m unittest -v rewrite.test_pool`；`test_stale_completed_is_not_material` 与 `test_new_dispatch_permanently_excludes_new_old_material` | 各宿主接入 |
| 转派、取消与旧清理 | 两个独立身份的 Worker、HTTP Router 与 SQLite 实跑。甲离线期间乙接管后，甲迟报完工被拒。补旧清理不动乙，旧材料仍可查。取消先留持单，收到停止与清理的宿主证据后才确认。宿主的清理凭据由测试替身提供。3 项新增，累计 36 项。 | `python3 -m unittest -v rewrite.test_cleanup` | 原生清理凭据、询单与取消的三次重试 |
| R8 询单及取消重试 | 真实隔离进程通信，宿主与时钟为夹具。两类请求分别累计；首次加三次重试仍无答复才入池并通知搁置。忙闲切换重新计时，继续请求保留原派发。3 项新增，累计 39 项。 | `python3 -m unittest -v rewrite.test_inquiries`；转派基线 259a196 | 真宿主停止、清理和原生回执仍待验 |
| 原单多题 W05/A08/A09 | 真实隔离 HTTP 与 SQLite，发问、作答方为测试驱动。每题由 Worker 分配 GUID；丢收件回执后原调用查回原题。选择、文本、跳过各有精确校验，转派后仍向原提问人送答案，答题不能收回工单。累计 43 项。 | `python3 -m unittest -v rewrite.test_questions` | 原生工具注册、真人双端题卡；R8 重试提交 2a0a757 |
| T01–04、C03 和分级收件 | 两个临时 Trigger 身份、SQLite、HTTP、文件下载实际运行。空文件、小图和 SQLite 文件逐字节核对；大文件跨分块 Range 成功。丢分块回执后按偏移续传、丢消息回执后只入账一次，超长或缺附件整条拒收。Event/Notify/Info 用受控宿主验证。累计 48 项。 | `python3 -m unittest -v rewrite.test_trigger`，问答基线 d0fe4dd | 真实来源接入、真实 Hermes、Worker 成果附件与双端界面 |
| 独立启动与 HTTPS | macOS 两个真实进程、临时证书和身份。Trigger 可离线记件，独立 Router 重启后仍能查到同一信息与附件。错误身份返回 401，未知签发者拒连。传输使用回环网卡，尚未作为跨机器实测。累计 50 项。 | `python3 -m unittest -v rewrite.test_service`；命令 `python3 -m rewrite.service router --help`、`trigger --help`；附件基线 dfc1c31 | 独立 Worker/宿主链、远端测试设备 |
| Worker 本体和远端适配器 | 三个真实独立进程分别启动 Router、Worker、测试宿主适配器，以 HTTPS 和各自身份对接。重复平台私聊只有一条宿主输入；平台身份不能调用原生回报，普通私聊的回合不能代报，正式回报重试只通知一次。宿主和调用者仍是替身，测试在 macOS 回环网卡上执行。 | `python3 -m unittest -v rewrite.test_service.Service.test_three_process_worker_chat_and_assignment`，累计 51 项；TLS 基线 991ccc9 | 独立平台实机、真实宿主证据 |
| 同伴私聊 | 两个 Worker 通过 Router 互发与关联回复；回执丢失重试后目标只收一次。同伴输入不能代报正式成果，查询不建单。真实 HTTP，宿主输入为夹具。 | `python3 -m unittest -v rewrite.test_peers`（2 项）；三进程基线 0111cc1 | 真实工位对端、飞书镜像 |
| W04 成果附件和候选附件 | Worker 上传的文件可随正式报告授权给溪流。一份跨身份文件使整笔事务回滚；改正后成果、通知各一次。仅附文件的晚到候选只记材料。 | `python3 -m unittest -v rewrite.test_work_files`（2 项），同伴基线 c962875 | 用户界面下载和生产身份待验 |
| 全量功能 | 下列旧入口已确认存在；迁移和各平台回退尚待实测 | 入口表对应源文件及注册表 | 双端题卡、运维验收 |
| 飞书展示 A05/A06 | 专用适配器身份将指定会话的公开事件交给 Worker。隔离平台记录显示工具卡、增量更新原卡、独立图片、图后新卡和最终回复顺序正确。图片发送回执丢失后沿用原图片与消息键；超过平台去重时限保留待核。4 项测试使用平台替身。 | `python3 -m unittest -v rewrite.test_feishu_output`；[飞书发送消息与 UUID 有效期](https://open.feishu.cn/document/uAjLw4CM/ukTMukTMukTM/reference/im-v1/message/create?lang=en-US) | 实际平台消息 ID、各原生宿主事件接入另验 |
| 飞书私聊入站 A02–A04 与普通文件 | 实际飞书 Python SDK 1.7.5 在空白环境解析两次同一模拟回调，队列仅存一条。隔离 Worker 通过 HTTP 分块收文件；模拟宿主收到同一输入中的原文和原始图片字节。丢回执重试不重复输入。测试还覆盖纯图、Markdown 文件、非训练员和机器人消息。文本专用宿主明确报无法交图。累计 67 项全量回归通过。 | `python3 -m unittest -v rewrite.test_feishu_inbound`；带 SDK 的测试 Python 运行 `-m rewrite.feishu_sdk_check` | 飞书云端收发、原生识图及界面待验 |
| 旧 Router 离线迁移演练 | 实际旧 RouterStore 在临时目录产生归档、排队和附件。备份校验、稳定队列映射和零新写入回退均实跑。旧程序重开恢复库，原下一张单、归档理由与附件均吻合。有在途、摘要损坏或新库已写拒绝覆盖。 | [复跑与回退界线](router-rehearsal.md)，`python3 -m unittest -v rewrite.test_cutover`（4 项） | 整体多组件、带新增业务回迁及新端历史查询另验 |
| A07 业务消息留档 | 完工、提问、入池、提醒和取消确认与各自的通知同时入账，按原 ID 可查。历史投递缺接收时间时返回空。查询不生成新帧，不确认宿主收到。 | `python3 -m unittest -v rewrite.test_archive`（2 项），成果附件基线 488b996 | 平台与宿主可见性另验 |

## 宿主与来源复验

同伴收件的三种观测可由发件工位按原消息号查询。第一次查询只有 Router 收件时间，接收 Worker 记入收件箱并确认传输后可见 `worker_received`，此时宿主收件号仍为空。指定宿主交回独立凭据后，Router 单独保存来自接收 Worker 的确认号。丢失这次回执再重开接收 Worker，查回同一号，原生输入仍只有一条。同伴消息的另一端可查，第三个工位及管理端被拒。复跑 `rewrite.test_peers.Peers.test_sender_distinguishes_router_worker_and_host_even_after_receipt_loss`。宿主用夹具，数据库、权限及两段 HTTP 实跑。

同伴私聊现可转交附件。发送 Worker 到 Router 的首块回执丢失，接收 Worker 又在第二块断开；两个方向均从已确认的字节偏移续传。接收工位重开后只生成一个原生输入，文件摘要吻合，双方无持单。空文件经过零字节读取验证；混入无权访问的附件会拒绝整条消息，未向管理端授权附件。复跑 `python3 -m unittest -v rewrite.test_peers`。两个 Worker 接口、Router HTTP、分块存储与本机文件实际运行，接收宿主使用夹具。累计 111 项回归通过。

旧 Router 的真实 HTTP 建单暴露出迁移夹具遗漏的差异。旧端给每张普通单写入默认目标元数据，原转换器把它一律拦截。现已在临时端口从旧 HTTP 派单入口产生待派单及已归档单，旧建单重试只记一份；迁到新 Router，原键仍查回同单，修改正文被拒，已归档单用旧键也无法复活。Worker 取得同一新单号，测试宿主读到目标约束、完整正文与原上下文。早期快照仍可读。复跑 `python3 -m unittest -v rewrite.test_cutover`，6 项通过；两端 Router、HTTP 与数据库实跑，宿主为夹具。原生目标执行待验。

飞书入站异常已有独立回执。隔离 DSH 在写入用户消息后抛错，Worker 收到 503，重试从原会话查回同一条输入。分别模拟宿主回执与平台回复回执丢失，训练员原消息先得到一条待核提醒，查回后得到一条确认；超出去重窗口的回复保留待核，其他输入仍能收到反馈。SDK 1.7.5 以模拟返回值核过话题父消息和消息卡参数。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk rewrite.test_feishu_output` 与 `python3 -m rewrite.feishu_sdk_check`。全量 108 项；实际飞书平台与模型未参与。

DSH 的 A05/A06 已从 SDK 的 V4 会话日志取证。Worker 接收四条有序展示事件，飞书替身分别生成原生输入、过程、工具与回复卡。丢 Worker 展示回执后重建发布器，平台仍仅收四张。另一次展示依次收到图前文字、单独图片、图后文字；平台收到的图片和 SDK 实际读取的对象 SHA-256 相同。对非图片记录或错误记录摘要的取图均拒绝。中断回合只出过程输出，正式单即便已有最终聊天文本，仍保留原持单。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk`。仅覆盖已持久化的用户及模型公开内容和工具调用；模型生成、实时逐字流、真实飞书仍待验收。

DSH 图文输入已用随附 SDK 的 `LocalAttachmentStore` 实测。飞书入站夹具提供一张可解码的 1×1 PNG 和原留言，原生用户消息保留两块内容；SDK 独立读回图像对象，归一化后的 SHA-256 与宿主引用相符。另发的 368,000 字节 Markdown 文件由 SDK 按文件读取，摘要与上传原件一致。清空适配器凭据和原图缓存后，重取原图查回同一原生收件号，未再生成输入。损坏图或只接收文字的测试模型均无收件。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk rewrite.test_feishu_inbound`。DSH SDK 及本机附件库、进程和 HTTP 实跑；飞书事件、Agent 和模型路由是夹具，视觉理解尚未验收。全量回归 101 项。

迁移后已实测溪流按来源 UUID 和旧工单号查询历史。真实旧 RouterStore 生成的成果、归档原因及时间通过新 Router 与 Hermes 管理工具查回，排队单能查到新单号。历史归档单未重派，查询前后试运行库摘要完全相同。复跑 `python3 -m unittest -v rewrite.test_cutover` 共 5 项，全量回归 93 项。人工整理旧格式记录以及试运行后的新增业务回迁另验。

Hermes 本地专用会话 H01/H02 已取得两条独立原生收件号。Event 进入指定会话后，将插件两份收件记录清空，仍由 Hermes 台账找回同号，宿主只执行了一次。测试回合释放后 10.94 秒收到 Notify，Info 没有新增输入。来源附件通过引用和原消息授权取回并验 SHA-256。执行记录在 [Hermes 原生台账](evidence/hermes-local-input.json)，源码版本 464bd0dd659f336bd0bf468bf02cad8aac211136。模型处理函数由夹具代替。普通回归另测待核 Event 与 Notify 队首，以及两次观察之间出现新回合时重新计时。此项覆盖独立 LOCAL 会话，飞书云端和日用网关未验。

A07 附件与 H02 文件交接在隔离 Router 实跑。用 `router_info` 同时指定消息号和附件号，可取得校验过的本机文件。测试在 640,000 字节原件的第二段断开，重建插件后从第 262,144 字节续取；原件和空文件各自的 SHA-256 均与宿主夹具的独立读取相符。两条 Info 即使共用缓存，交换消息号也无法取另一条的附件。复跑 `python3 -m unittest -v rewrite.test_manager_files`，全量 90 项通过。宿主收件仍为夹具，真实 Hermes 输入回执另验。

版本包的构建、安装、核验和升级入口见 [隔离安装](README.md#隔离安装与升级)。从 b31cee5 升到 5de36ad，再退回前版，三个实际临时 Router 进程先后启用同一张待派单，仅原单一张。原始工单号 `2bd9d658-d841-4a7e-9f28-e52086a56c64`；两版源码包 SHA-256 分别为 `8ca669d7142031d022a5a37ae124aaa8f739113c70c40fab2eaf120c6c954d25`、`318fa45ec72c8ab1fa1edde64f4bd56bc4d10c17d8f5e9be33aff43abb316a37`。复验命令在 [回退演练](router-rehearsal.md)。七库试运行出现新业务写入时，整组回退另外实测拒绝。

五组件离线备份的复跑入口为 `python3 -m unittest -v rewrite.test_state_bundle`，步骤见 [离线恢复](router-rehearsal.md)。七库从同一停写点恢复，原持单、未交的私聊和通知、待上传附件都实跑续接。一次已有写锁、错误角色或清单摘要不匹配会阻止发布恢复目录。真实云端状态及跨机器停写窗口另验。上一关 DSH 提交 b31cee5，完整回归 84 项。

DSH 的适配器可在独立宿主进程加载 `integrations/dsh_host/index.mjs`，只对显式指定的会话注册 `worker_ask` 和 `worker_report`。宿主插件与同机适配器经用户私有目录里的 Unix 套接字通信；适配器通过 HTTPS 与 Worker 通信。接入参数是会话 ID 和绝对目录，启动器说明见 `python3 -m rewrite.service dsh-adapter --help`。宿主侧不持有 Router 凭据。

复跑 `python3 -m unittest -v rewrite.test_dsh_sdk`。本机 DSH 随附 SDK 0.2.2-rc.5 在全新临时用户目录里加载插件，由 SDK 自己提供工具表、调用分发、会话存储和 V4 日志查回。清掉适配器收件凭据后，SDK 日志恢复了原凭据，输入仍为一条。问答和完工分别到达隔离 Router，同一模型调用号的重放只形成一次成果通知；私聊、同回合有手动插话均拒报。Agent 输入、模型输出及工具调用是受控测试驱动生成，未使用测试账号或真人会话。DSH 真实模型、附图、原生清理和平台展示仍未验。Desktop 的两项阻点独立保留。

来源采集复验入口为 `python3 -m unittest -v rewrite.test_feeds`。Python 自带的 IMAP 客户端实际通过 TLS 连接临时邮箱服务器，使用 EXAMINE 和 BODY.PEEK 只读取件。真实 SSE 连接从测试游戏桥取回聊天；本人消息过滤，紧急聊天交 Event，普通聊天、邮件入 Info。每条消息留档与推进读取位置同事务，写入中断重开时保留旧位置，重见同序号按内容核对。两项通信运行于本机回环，邮箱、游戏数据和服务端均为测试替身。旧订阅、来源限流及游戏桥无启动标识时的同序号重启识别另验。

Worker 篇 A07/A08 双端题卡的复跑入口为 `python3 -m unittest -v rewrite.test_question_cards`。实际 HTTP 和 SQLite 连接了 Worker 与独立适配器；飞书和宿主题窗由夹具代替。训练员选择、自填、跳过后，原卡均等原题关闭凭据到达才更新。宿主丢回执时只查原题，重试未再提交答案。问溪流的卡保留完整题面和选项，训练员的回调被拒；溪流回答后更新同一卡。飞书 SDK 1.7.5 收两次模拟按钮事件，答案队列只写一条。累计 78 项回归通过，真实人员点击、云端卡片及客户端题窗仍待专用工位验证。

Worker 篇 A01、A09–A11 的隔离复跑入口为 `python3 -m unittest -v rewrite.test_link_health`。真实回环 HTTP 断线期间，测试宿主的进展仍进入本体，原派发在本地保留。受控时钟满 20 分钟后重连，Router 已将原单标为受阻，Worker 据此解除持单。单断适配器时本体仍可达，Router 将宿主忙闲标为未知，只报一次警。工单卡在宿主收件后显示派发、更新和取消正文。适配器链路诊断接口只允许当前 Worker 会话报状态。共 74 项离线回归，原生与飞书界面另验。

### 溪流侧管理工具的独立验证

Hermes 源码版本 `a2de7418a3405307f8b3abce01a66d5d71351edc`。从 Git 跟踪的源码建立临时副本，用本机已有的独立测试 Python 3.14 环境、全新配置和测试身份加载插件。Hermes 自己登记全部 8 个管理工具，实际调用经过其工具分发和中间件，再通过 HTTP 建单。同原生调用号重试返回同单，其他会话收到 403。原始输出在 `evidence/hermes-manager-registration.json`。调用由测试驱动发起，未启动模型、网关或飞书。A07 业务存档提交 61d7721；本轮全量回归 59 项通过。

复跑用 `python3 -m rewrite.hermes_check --hermes-source <Hermes源码仓> --hermes-python <带完整依赖的独立测试Python>`。缺少运行依赖的 Python 3.12 副本在导入 ruamel.yaml 时失败。普通离线回归包括 `rewrite.test_hermes_tools` 和 `rewrite.test_archive`。

### Codex 两种测试入口

在新建的临时工作目录配置项目级 stdio MCP，再在相同目录打开独立测试会话。原生测试提示只要求枚举本回合的工具。回读得到空数组，本机同目录的 `codex mcp get mowulink_w04_test` 返回未找到服务，私有运行目录也没有握手或调用记录。仅凭以上现象无法区分该版本的项目加载策略和其他宿主配置原因。测试后已删除专用项目配置。宿主对项目配置有信任要求，Desktop 的共享配置会影响其他使用者。[Codex MCP 官方说明](https://developers.openai.com/codex/mcp)

`report_scope stage|clean`（`python3 -m rewrite.report_scope`）仅用于专用项目。测试启动器须先在仅属当前用户的临时目录建立监听。模型工具参数只有业务字段。入口读取 tools/call 的宿主线程、回合元数据，用适配器的已确认收件查回唯一输入，再到 Worker 逐项核对工单和派发。查不到、同回合有多个已知输入、会话不符或私聊均拒报。开发用 Unix 通道信任同一系统用户，不能证明同用户的任意本地进程由 Desktop 启动；生产认证和跨机传输另验。

另以该机 Desktop 随附的 `codex app-server --stdio` 建立一次独立原生协议连接，临时用户目录和配置均为空。初始化成功，协议提供线程级 `dynamicTools`，但 `account/read` 返回无账号、需要认证。该进程未发起模型回合。此路径可用独立测试帐号继续验原生模型，Desktop 项目加载仍需单验。

上面两次探测分别记录于 `evidence/desktop-w04-probe.json` 和 `evidence/app-server-empty-profile.json`。溪流已核实当前没有可用的已授权测试工位。真实 W04 继续列为未验；其他项目继续推进。

## 旧入口与替代条件

DSH 的 `/queue 正文` 已接入固定会话。飞书命令回执证明 Worker 已持久排队；宿主收件由独立输入号和回合号证明。忙时两条消息都留在 Worker，重建 Worker 后按原顺序进入两个独立回合。首条原生回执在传回途中丢失，复查原号后输入总数仍为三条（原回合一条和排队两条）。适配器拒绝将原消息交给另一会话。DSH SDK 在临时目录实际加载插件并落 V4 日志，Agent 驱动和飞书输入由夹具提供。旧宿主未声明下一回合投递能力时提示未接入。复跑 `python3 -m unittest -v rewrite.test_dsh_sdk rewrite.test_worker_commands`。

旧 Codex 的实际命令入口是 `Plugins/Codex/desktop-manager.mjs` 的 `message` 和指令分发器。除帮助列出的主要命令，还处理 `/start /commands /clear /switch /list /stop`。Antigravity 的 `interface/feishu-commands.mjs` 明确限定固定会话，历史切换另列。仓内 `client/desktop/README.md` 和 `client/mobile/README.md` 仅记未来客户端，本仓这两个目录没有可执行消费者。

重做版的纯文本指令现由绑定的飞书训练员交给 Worker。隔离 HTTP 实测 `/help`、`/status`、固定会话查询和同会话选择。状态卡分别显示两条连接、宿主最近忙闲、本地工单号与派发号。帮助别名为 `/start` 和 `/commands`。此时 `/resume` 仅能查看当前会话，同会话编号及 ID 可确认；历史翻页、检索、换绑仍待接。未知指令给出「暂不开放」。尚无执行回读的命令不会送入普通对话。重试丢失的 Worker 回执只出现一张指令卡；旧版已交给宿主的同号消息可查回旧收据。四项新增回归 `python3 -m unittest -v rewrite.test_worker_commands`，全量 97 项。飞书卡片使用平台替身，真实云端另验。

旧 Router、Trigger 以此分支的 `server/` 和 `sources/` 为依据；宿主插件以独立 MowuLinkWorkerSkill 的 `f399148df99ed58568f4931aed414e3a83fa7a25` 为依据。已安装的 Hermes `trigger-bus` 与当前维修会话的 MCP 工具表亦已核过。源码声明只证明存在。每行的迁移条件在对应使用端实测通过之前，保留旧入口。

| 旧入口与使用者 | 新入口／归属 | 已有证据；迁移条件 |
| --- | --- | --- |
| 溪流、客户端使用 Router 的 /workers、/tasks、/tasks/{id}、/tasks/{id}/cancel；Worker 用 /register、/heartbeat、/stream、/result、/tasks/{id}/deliverable（server/router/server.py） | A01–06、C01–02、W01–04 | 建单收件和验收已有主线；须逐项验证改派、取消、旧清理、晚报与管理查询 |
| 工位点对点消息 /messages、消息 ack；飞书与 Worker 的聊天记录 /chat、订阅与 notice | Worker 私聊与 Router 投递；训练员私聊由 Worker 对接 | 同伴收发、确认、附件和旧同伴历史已用隔离 HTTP 验证；宿主输入为夹具，旧飞书聊天记录、订阅与真人工位另验 |
| 工位问答 /questions、/questions/responses，审批 /approvals，远端命令 /commands、/commands/responses，运维 /ops/services | W05、A08–09；宿主授权及运维能力单列 | 验收要覆盖原题、原请求发起人和权限；保留独立审批边界，禁止用问答代批准 |
| Trigger 来源 sources/email/client.py、sources/mc_chat、server/trigger/clients/mc_chat_client.py；/trigger、/sources、/stats、/events、/drops、/blocked、/subscribe、/ack、/renew、/unsubscribe | 来源实例持久队列、T01–04，订阅、健康和运维另测 | 两来源都有登记；按启动状态另核。须复现断传、重发、按实例查发件与接收 |
| Hermes trigger-bus 本机已安装；register 挂 pre_gateway_dispatch、pre_llm_call、transform_tool_result、post_llm_call；旧附件获取 | HermesRouterAdapter C01–03、H01–02、A07 | Notify 空闲 10 秒已有宿主夹具；Event、Info、文件和真实 Hermes 单列 |
| Codex/DSH 私聊入出站、图文、文件、图片回传、流式卡、题卡、按钮（Plugins/{Codex,DSH}/feishu*.mjs、question-*.mjs） | Worker 与平台适配器 A01–A11 测例 | 回归须有消息与卡 ID、原始附件，工具进展和最终输出区分；真人卡验收条件另记 |
| Codex 飞书 /resume（搜索、分页、选会话）、/status、/context、/compact、/new、/fork、/model、/effort、/goal（查询/暂停/继续）、/queue（feishu-commands.mjs 与 business-runtime.mjs） | 各宿主会话与能力接口 | 每个命令分别核实际执行和回读；选中模型、力度不能只核菜单文字。DSH 和 Antigravity 独立核 |
| 当前 Codex 工具 bridge_probe、bridge_inspect_native_contract、bridge_bind_manager、bridge_binding_status、bridge_verify_manager_binding、bridge_start_local_manager、bridge_verify_native_write_path、bridge_reload_business_modules | 宿主适配器、安装与恢复入口 | 目标绑定及真收件有部分证据；其余逐项核失效回退 |
| bridge_register_current_task、bridge_create_work_order、bridge_work_order_status、submit_deliverable | Worker 工单接管、管理工具与 W04 | 旧工具表实见；新工具尚缺真模型调用 |
| bridge_message、bridge_send_image、bridge_publish_question、bridge_list_workers、bridge_send_worker_message、bridge_worker_inbox | 平台输出、问答、同伴联络 | 需用测试身份核回执、去重、接收方及权限 |
| DSH 插件 index.js、dsh-session-catalog.mjs；Antigravity/interface/sidecar.mjs、deliverable-mcp.mjs | 可替换 AgentAdapter | 各宿主分别登记支持、缺口及实测；Codex 成绩不能转记 |
| 旧附件 /attachments、订阅 /subscriptions、运维命令、Codex/DSH 的安装更新、Windows 启动器与 macOS 恢复脚本 | T01–03/C03、注册鉴权、隔离安装与升级回退 | 两平台均须有已登记工具消费者及恢复证据；既有停用巡检保持停用，确认退役另记 |
| client/desktop、client/mobile、host/apply.sh 等兼容使用方 | 管理和消息接口的客户端 | 尚待逐个核实际调用和版本覆盖，不能凭目录名判定退役 |

源码台账仍需实际使用者确认客户端独有入口、各来源使用状态及 DSH、Antigravity 的命令差异。隔离环境的迁移和回退已有实测；生产切换仍待独立复验及授权。
