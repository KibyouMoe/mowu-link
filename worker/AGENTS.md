# Worker 开发与验收

- CodexWorker 只通过 Desktop 插件入口启动；禁止恢复独立 CLI/headless 启动或在 Desktop 交接拒绝后改道。
- 共享业务模块与 DSH 对应文件保持逐字节相同；引擎差异放 interface/。
- 每次设计测试都考虑 macOS 与 Windows；平台无关逻辑共用测试，平台分支分别覆盖。
- 涉及进程、文件权限、路径、IPC、启动器或客户端生命周期时，分别记录两平台原生实测与尚未验证项，不能用 Mac 结果宣称 Windows 已通过。
- Windows 不用 Unix mode=0600 代替 ACL 验证；POSIX 仍保留权限约束。
- 正式项目走工单/目标模式，项目补充归同一单；普通 worker 互聊走 router 消息通道，不创建工单或目标。
- 不自动替换主会话，不重放未知原生写入，不在两台机器并行修改同一批正式文件。
