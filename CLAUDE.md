# CLAUDE.md

> **本文件只是转发层，不是规则本体。** 唯一事实源是 `AGENTS.md` 与
> `.trae/skills/*/SKILL.md`；**不要在这里复制内容** —— 复制必然漂移
> （本项目实测过：同一 key 写在 3 处，只改一处 → 情报 5 个端点对所有人 403）。

@AGENTS.md

## 与开发工具无关的三条命令（需求 ↔ PRD 对账）

`AGENTS.md` 的《需求—PRD 对账硬约束》要求**每轮对话**把用户说的需求与
`docs/PRD.md` 对一遍：**没有的补进去、有差异的改掉**。它不依赖任何 AI 工具：

```bash
uv run python scripts/prd_sync_check.py --keyword "<关键词>"   # 三态：OK / MISS_LEDGER / MISS_PRD / TODO
uv run python scripts/prd_sync_check.py --ledger               # 台账完整性（交付前必须 0 ERROR）
uv run python scripts/prd_sync_check.py --self-test            # 自证：检查器本身还可信
```

纪律全文：`.trae/skills/requirement-prd-sync/SKILL.md`
（Trae 侧路径；其它工具按本文件的方式转发，不要另存一份）。
台账：`docs/REQUIREMENT_CHANGELOG.md`；证据基座：`docs/PRD_ALIGNMENT_AUDIT_20260928.md`。
