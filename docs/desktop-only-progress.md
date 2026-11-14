# Desktop-only Worker 整合进展

目标：收回鲁道夫补丁，正式整合 Desktop 随启停与 autoStart 开关，删除独立 CLI 入口和回退，保留飞书/router/审批、原会话、共享业务同构，修跨平台测试并实测 router 回传。

隔离工作树：mowulink-desktop-only，分支 feat/worker-desktop-only。原工作树仍承载当前收信进程，发布前不删除其入口。

- 补丁回收完成：router t-940e38b343 / t-fceff78b72；收集分支 collector/rudolf-desktop-lifecycle-20261113，提交 4a5003bb5b4088508cb2d8a81b8e063c61b0e44d（基准62fbdf7），已fetch审阅。原patch SHA256 d86ead7f3e5b67aa7f67344151a52526768e2f997a8216a92dc64e0279d8b992。保留实际退出等待/IPC生命周期思想，舍弃CLI分支与缺配置回退。
- 已核对：现有 Desktop 监督器只启动 server.mjs，router 在 cli.mjs；正式版缺 autoStart。t-a87d62569a 带显式 deadline_s=180，原生回合未结束时到期。
- 实现分工：父代理整合生命周期、插件业务入口与移除 CLI；Ohm 只负责新 router-runtime 和测试；Avicenna 只负责两侧权限断言测试。
- 验收待做：实际包冷启动、开关阻断回退、同会话收发、Desktop 完整退出/重开、跨平台测试、Windows/router 原样标记回传。
- 不把单元测试或接口在线代替实机验收；补丁和机器凭据分开，凭据不入库。

## 新增需求与验证状态

- router 普通 worker 互聊：独立持久消息队列/ACK/去重/回复关联，不创建 task 或目标；Parfit 负责 server API，Carson 负责 worker-messages；父代理负责客户端与MCP整合。
- 正式项目用一张工单并默认要求原生目标模式；后续需求/纠正归同一单。工具不支持目标时明确能力边界，不伪称已有目标。
- 所有平台相关测试考虑 Mac/Windows，分别列原生实测与分支模拟证据；用户要求已写入 worker/AGENTS.md。
- 权限测试72项通过，原始输出 /tmp/mwp-permission-results-5wqc1uxz.txt；POSIX仍0600，Windows检查Node可表示位，不冒充ACL验证。
- CLI与其专属引擎适配在隔离工作树移除；生产原路径暂未更换，待完整包与正常Desktop生命周期验证。

## 当前恢复点

- 核心整合测试 /tmp/desktop-only-integrated.tap：131 tests，130 pass，0 fail，1 skip（仅原生Windows检查）。含真实disabled-MCP包冷启动、两OS分支模拟、热交接、router模块与互聊模块。
- router 工单/互聊自测 /tmp/mowulink-router-workorders-selftest.log：130/130 PASS；正式 /tasks 支持可选UUID幂等、execution_mode=goal；普通 /messages 独立持久化。
- 生命周期已支持Mac ps与Windows CIM、autoStart false/非法值前置守卫、重启代际0.3.0、现有状态目录指针 worker-instance.local.json。Rudolf CLI分支未保留。
- 接入已完成：mcp-business 持有 router-runtime；peerSSE/离线轮询到worker-messages；公共HTTP代理和MCP提供workers、peerchat、inbox、workorders。还需完整集成后复跑。
- 待核：全量测试、正式README/安装技能更新、实际Mac/Windows包冷启动及真正router回环；不把当前0.2.41线上CLI当成已切换。

- 真实隔离MCP→writer HTTP→Python router端到端：两侧12项通过（/tmp/desktop-only-plugin-router-e2e.tap）；Desktop引擎回调为测试数据，未调用模型，不冒充原生Mac/Windows Desktop完整重启。
- Windows准备任务 t-bf89fd2d53 已排队：Rudolf从22:59起离线，未取到新包原生验收结果。
- 全量首次运行发现 self-target 用例未释放异步门导致关闭永等，旧测试进程已由父代理核实并终止（仅其创建的测试子进程）；Bohr修用例门及图片排空夹具。另已替换删除的app-server专属进展测试为真实Desktop工具完成事件进展测试。

## 发布候选验证

- macOS / Node 26.12.1 全量：Codex 694 tests / 686 pass / 0 fail / 8 skip；DSH 735 tests / 727 pass / 0 fail / 8 skip。日志 /tmp/desktop-only-{codexworker,dshworker}-release.tap。
- 8个skip为Windows原生进程/租约、script watchdog和真实MSIX Desktop冷启动；不能视为Windows实际通过。Mac/Win实现分支、真实Mac子进程关闭、带空格/Unicode路径、MCP重定位包、router真实HTTP/SQLite均有测试。
- Mac ps已消失PID返回1的处理与检查拒绝区分；Desktop关闭时只停止所属子进程，清理宽限不限制模型任务。
- 未知图文前缀保留并阻断同条后续图片/文字，独立回复仍继续；回归不降低20/4图片数量或授权断言。
- 已加GitHub Actions：macos-latest / windows-latest × Node22/26；交互Desktop专属检查仍需对应机器真实验收。
- 生产Mac当前仍旧CLI/共享实例；未替换运行版本；Rudolf离线，准备任务t-bf89fd2d53排队；生产router未重启。

- 真正Windows Server2025 / Node26 CI已运行：发现DSH测试写死POSIX分隔符（已改path.join）；首轮矩阵另有新router0.4.0与旧版本断言冲突（已改为核对实际源VERSION）。定向22/22通过，正在重跑四组CI。
- 技能真实包全量首次发现缺references/hermes-manager-protocol.schema.json；分发需连同此测试/协议依赖拷贝并记录哈希，不能只复制Worker目录。

- Windows Node22首轮另遇并发冷加载/CIM的夹具时限：CI改为2文件并发，冷加载预算与Windows MCP的60s一致；真实进程探针仅对系统杀掉的超时查询最多3次，不重试权限拒绝。全部安全/零网络/零原生写入断言保留。此为测试夹具期限，不给Worker任务设期限；定向108 pass / 0 fail / 2 skip。
- 实际分发包补协议schema后全量694 tests / 686 pass / 0 fail / 8 skip（/tmp/desktop-only-distribution-final.tap）；技能验证Skill is valid。Mac Desktop配置已备份，暂autoStart=false阻止与旧收信进程争用；已注册本地marketplace，未关闭旧实例。

## 实际安装与原生 CI 恢复点

- 5192c4d 与 ace8724 的 Windows Node22/26 均实际全量成功：Codex 694 tests / 687 pass / 0 fail / 7 skip；DSH 735 / 728 / 0 / 7。
- Mac 云端失败的真实 Python 堆栈为 socket.getfqdn → http.server.server_bind → RouterServer.__init__，不是引擎或 worker 卡死；已改直接绑定数字地址，回归禁止启动时反向 DNS。
- Mac 正常插件安装源为 CodexSyncArea；受管缓存 ~/.codex/plugins/cache/codex-sync-area/codexworker/0.3.0，123文件已只读核对与5192c4d相同。实际.mcp清单的隔离Node22 initialize/tools/list/binding_status成功，17工具、原主会话不变；并非真实Desktop宿主验收。
- 实际desktop-config备份 .bak-desktop-only-20261114-000832，暂autoStart=false；旧CLI PID55771 / 0.2.41仍ready，飞书与router均在线，未动生产服务。
- launchctl CODEX_CLI_PATH仍指向 ~/.codex/state/codexworker/desktop-shared/codex；迁移需正常恢复Desktop内置入口，不能直接删除仍用的wrapper或中断当前原生回合。
- 技能分发master仍有本轮未提交快照/文档；实际本地旧技能待候选验证后备份刷新。

## 源码与分发已推送（下一阶段从这里恢复）

- 冻结代码 d02a4d29dad8b0e3124f058a834e9cffed96782e 已推送 feat/worker-desktop-only；正式分发90929e4已推送CodexSyncArea master。原生产checkout的两个用户改动未动。
- 本机Mac Node22完整两套：1433 tests / 1417 pass / 0 fail / 16 skip，/tmp/desktop-only-mac-node22-frozen.tap；实际分发包Mac Node26：696 / 688 / 0 / 8，/tmp/desktop-only-package-frozen.tap。
- 当前代码原生CI https://github.com/KibyouMoe/mowu-link/actions/runs/43466713975 ：Windows22/26各自Codex696/689/0/7、DSH737/730/0/7；Mac26 Codex696/688/0/8、DSH737/729/0/8，router130/130；均成功。Mac22云端仍排队，本机22已实际通过，不能把排队称成功。
- 缺失router凭据不再拖掉飞书writer；新工单先核对health的idempotent-goal-v1能力，生产旧router目前不支持时明确拒绝POST而非重复派单。两组定向各26/26，router130/130。
- 新包123文件及router、schema依赖已逐字节/哈希核对；本地技能已备份到 ~/.codex/skill-backups/operate-codex-worker-20261114-002505，并刷新/验证。正常产品plugin add可在同版本下刷新缓存，已再次安装并核对当前123文件相同。
- 实际主会话仅01a1c413-6bd5-7e10-a388-665d44d2b6ec；切换前记录receipts347、feishuReceipts193、publicTextOutbox128、autoImages18（后续会增长）。旧CLI LaunchAgent ai.hermes.codexworker PID55771仍在线；autoStart暂false，生产DSH/router/gateway未重启。
- 尚未完成目标：真正Mac Desktop插件加载/退出重开与旧实例交接、Windows Desktop原生验收、真实router唯一标记结果/互聊。Rudolf仍离线，t-bf89fd2d53仍queued。
- 下一步先核对真实Desktop宿主的管道注入与进程归属；当前会话在shared daemon，CODEX_CLI_PATH还指向旧desktop-shared/codex。不要把模拟/隔离MCP通过当真实宿主已接管，不盲目重启客户端或后台、强抢租约或另建主会话。当前目标保持active，不标完成。

## Mac真实入口准备（后续以此覆盖上方旧状态）

- 官方app.asar证据：CODEX_APP_SERVER_USE_LOCAL_DAEMON=1、FORCE_CLI非1、无CODEX_CLI_PATH时走原生control socket；最低appServerVersion0.141.0。实际后台0.167.0-alpha.3.2满足；默认客户端CLI0.167.0-alpha.4.1，两次daemon version均返回同一running后台。无需另起引擎或换主会话。
- 已备份旧环境至 ~/.codex/state/codexworker/desktop-cli-environment.backup.local.json，并正常launchctl unsetenv CODEX_CLI_PATH；仅影响下一次Desktop启动，现有CLI/后台均未停止。
- Mac共享daemon不能只用MCP父进程查Desktop；047cd4c已增加宿主原管道的socket/同UID/lsof真实owner/ps校验，只接受Desktop自身持有者，拒绝非socket、跨UID、任意子进程、歧义或检查错误，不扫描或持久化Native端点。Windows仍祖先方式。
- 定向85 pass /0 fail /1 skip；真实只读helper返回Desktop PID76729与正确启动时间，未调用Native IPC。Node22两侧全量1443/1427/0/16，/tmp/desktop-shared-owner-full.tap。CI43470307712的Windows22/26与Mac26成功，Mac22排队。
- 尚需正常Desktop重开以获得真实MCP注入/工具验证；autoStart仍false，旧PID55771临时收信。核对新管道与原会话后，再正常停止/禁用旧LaunchAgent、启用新autoStart并验唯一writer。不得停止共享daemon或先删旧shim文件。

## 交互验收外部依赖核对

- 原生CI43470307712四组Mac/Windows × Node22/26已全部success，Mac22 job132040142536。源码/分发/安装缓存/本地技能已完成；不再重复这些通过的回归。
- 当前Desktop PID仍76729，未观察到正常退出重开；autoStart=false，旧Worker0.2.41/PID55771仍ready。Rudolf仍offline，准备任务t-bf89fd2d53仍queued。重开提问已发，未收到答案。
- 下一步必须获得真实Desktop环境：Mac重开后核对宿主注入和原会话，再交接旧服务；Windows须对应机器上线。不能把CI成功缩成目标完成，也不能跳过这两项。
- 外部阻碍连续审计：上一目标轮首次提出重开并等待；本轮再次核对仍未重开/离线，同时收齐最终CI证据。若下一轮同条件无变化且无可独立推进工作，按三轮阈值标blocked；不要擅自paused或complete。

## 真实重开发现的迁移缺口（优先处理）

- 训练员已正常重开，实际Desktop新PID42377，子进程42768却启动私有app-server：附加参数 features.code_mode_host=true 与 codex_app.enabled=true 使该构建跳过官方daemon路径。前述仅凭最低版本/环境判断可直接恢复默认入口不充分，已被真实验收推翻。
- 原会话仍由共享后台PID55381持有，所以新客户端显示会话被占用；未创建替代会话，旧receiver PID55771继续收信。
- 已从备份正常恢复launchctl CODEX_CLI_PATH到先前已验证的Desktop共用连接器；需要再次正常重开才应用，不能热改应用或kill共享后台。此为临时恢复，目标未完成。
- 下一步保留Desktop所需的连接适配（不等于恢复独立Worker CLI），移除对将被删除的原worker源码目录依赖，验证附加参数真实流程及Desktop-owned Worker。不能继续宣称默认native daemon入口在本机构建可直接使用。

## Desktop连接适配修复

- 新增 interface/desktop-connection/{entry,unix-websocket,prepare}.mjs：只接受真实Mac Desktop祖先，转接已有官方Unix后台；禁止独立模型/Worker命令，连接失败不启动第二个引擎，Windows拒绝Unix覆盖并继续原生管道。
- 真实附加启动参数保持为线程config，原请求覆盖优先；原文/原thread不变。补UTF-8跨chunk、EOF只关当前连接、异步写错误和配置/端点权限检查。
- prepare只将三个所需模块与启动器写入本机内容寻址私有目录，不启动进程或改环境；旧源码移除不再导致入口悬空。已有文件内容不符明确拒绝，不覆盖。
- Mac Node22定向77 tests /76 pass /0 fail /1平台skip（/tmp/desktop-connection-mac-node22.tap）；Mac Node26新增16/16（/tmp/desktop-connection-mac-node26.tap）。真实只读父链探针确认当前Desktop PID42377；不等同于实际Desktop重开验收。
- 共享后台55381和主会话保持，未重启DSH/router/gateway。下一步安装新适配并正常重开Desktop，确认接回原后台后再交接旧receiver；Windows实际Desktop仍待上线验收。

- 适配源码fc35e1a与技能分发682e947均已推送并核对远端。实际正常plugin add刷新后129文件逐字节匹配，分发包适配16/16、Skill is valid，本地技能同步。
- 已从受管包的prepare正常生成本机私有desktop-connection/710c6ed6…/codex（四文件，sh/node语法检查通过），备份旧环境至desktop-connection-activation.backup.local.json，正常launchctl setenv将下一次Desktop入口指向它；未启动或关闭任何已有进程。旧连接器仍保留可恢复。
- 目前仍是Desktop42377/私有app-server42768与共享后台55381，修复只在下一次正常完整退出/重开Desktop后生效。autoStart仍false，旧receiver继续临时收信，不将文件安装称为新生命周期已接管。
- 新CI43475103068：Mac22排队，Mac26/Windows22/Windows26运行中，不能套用前一提交全绿结果。下一步先等正常Desktop重开，核对原会话/实际后台/插件工具，再交接旧receiver；目标保持active。

## 后续核验与router发布准备

- 续接后核对：Desktop仍42377，旧Worker0.2.41/PID55771仍ready，autoStart=false；下一次Desktop入口已指向710c6ed6…独立适配。Rudolf仍offline，实际桌面验收尚未发生。
- 新CI43475103068实际日志：Mac26 Codex717/709/0/8、DSH742/734/0/8，router130/130；Windows22与26各Codex717/702/0/15、DSH742/735/0/7，均success。Windows新增8个skip为Mac专用Unix/权限/安装测试；Windows原生管道未被Unix适配替代。Mac22 job132056795002仍queued且无runner，不能宣称通过或启动第二个重复run。
- 生产router只读health仍0.3.1，无新版工单能力；未重启。已在 ~/.hermes/releases/router/fc35e1ab… 准备server.py、attachments.py及保留原参数的plist草案，实际 --version=0.4.0；仅暂存，未替换现有LaunchAgent。
- 本机备份 ~/.hermes/backups/router/20261114-014540：SQLite backup生成983040字节一致快照，138 tasks/14 workers，quick_check=ok；原plist SHA仍173fe5a8be2b27b681cddc76dabaa7be827153057df563211510388e83767e79。离线WAL快照只读验证需immutable=1，未将此参数用于在线数据库。
- 仍需正常Mac重开/唯一writer交接、Windows真实Desktop、允许加载新版router后实际工单和peer回流；当前不修改或重启生产router/DSH/gateway，不将CI或准备包冒充外部闭环完成。

## 整机重启后的真实启动冲突（2026-11-14 09:27）

- 训练员整机重启后Desktop1260启动私有后台2135；旧LaunchAgent ai.hermes.codexworker 又启动CLI/PID2061与共享后台2494，并把CODEX_CLI_PATH恢复旧路径。Desktop早于该setenv启动，故整机重启仍出现原会话占用。CLI还重写desktop-config，移除了暂存autoStart/publicJournalRoot/router配置。
- 当前模型在共享后台，其MCP没有原生Desktop通道；实际Desktop后台2135及其插件MCP3765已收到宿主通道，真实监督器3778已启动但被旧writer挡住。这两者不能用同一条bridge_probe的失败混为一谈。
- 一次性迁移计划已只读核验：仅在当前回合idle持续12秒后，备份配置/绑定/账本与旧plist，正常disable/bootout旧Worker并移出LaunchAgents，清除旧启动覆盖，启用已经在运行的Desktop插件；原threadId不变，不新建/归档，不停止共享daemon或其他服务。官方thread/read不订阅，最后旧连接离开后有默认60秒空闲卸载宽限；等待真正交接结果，不再以重复重启代替。
- 维护脚本 /tmp/oguri-desktop-cutover.mjs，--plan 输出plan_verified（旧writer2061/Desktop1260/原thread active）；尚待启动一次性执行器，本轮final后才能触发idle。日志为 ~/.codex/state/codexworker/desktop-only-cutover-events.jsonl。
- 官方稳定control socket实际是同UID/private0700目录里的symlink，目标为同UID0600 Unix socket；连接层修正为验证私有link目录再验证resolved socket，不扫描其他端点。新增真实symlink成功/非法目录与目标拒绝测试，Mac定向17/17通过。

- 一次性交接执行器已启动：PID11607 / exec session49354，实际输出waiting_for_current_turn_end；当前回合结束且idle持续12秒后才停旧Worker。句柄保存在 ~/.codex/state/codexworker/desktop-only-migration.local.json。它的15分钟仅限制维护排程，持续有对话则deferred不改服务，不限制/终止模型任务。
- 修复源码ef50ef7、技能43e7aea已推送；Node22/26适配各17/17，正常plugin add后129文件与分发逐字节相同。本地技能已同步。尚未看到Desktop_writer_ready，下一轮先用session49354或持久日志核对实际交接，不能根据“已启动”声称迁移完成。
