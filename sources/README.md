# sources

每个消息源一个目录：

```
sources/
├── mc_chat/     source.json（声明）+ 客户端（待迁入 client.py）
└── email/       source.json（声明）+ 客户端（待迁入 client.py）
```

约定：
- 声明（schema / policy / health / dedupe）放 source.json
- 客户端脚本负责：连接、归一化、业务过滤、publish
- 通用规则（限流/去重/优先级）不在这里，归 trigger

注：`server/trigger/sources` 是指向这里的软链，trigger 仍按原路径加载。
