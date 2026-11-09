"""worker 共用能力：飞书双向桥、看门狗、订阅。

这个包被 ``worker/dsh/worker.py`` 与 ``worker/codex/worker.py`` 共用，
不包含任何 Router 服务端代码。依赖（lark_oapi）只在真正建立飞书连接时
才导入，保证没有装 lark 的机器仍能跑 worker 的基础功能。
"""
