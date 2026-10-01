# Copilot 指令（转发层）

> **只转发，不复制。** 唯一事实源是仓库根 `AGENTS.md`（含《需求—PRD 对账硬约束》等
> 硬约束段）与 `.trae/skills/*/SKILL.md`。复制内容必然漂移，本项目已实测过这类故障。

## 需求 ↔ 开发 PRD 文档对账（每轮对话）

把用户这次说出的需求与 `docs/PRD.md` 对一遍：**没有的补充进去，有差异的改掉**。
不依赖任何 AI 工具，任何环境都能跑：

```bash
uv run python scripts/prd_sync_check.py --keyword "<关键词>"   # OK / MISS_LEDGER / MISS_PRD / TODO
uv run python scripts/prd_sync_check.py --ledger               # 台账完整性，0 ERROR 才算达标
uv run python scripts/prd_sync_check.py --self-test            # 自证检查器本身可信
```

- 结论 `MISS_PRD`（仓库做了、PRD 查不到）→ 补写 `docs/PRD.md` 并登记台账；
- 口径冲突 → 改写 PRD，**保留原文并加 `⚠️ 已废止（CHG-xxxx）`**，禁止直接删；
- 落不进 PRD 的条目只能挂 `待办`（台账 `docs/REQUIREMENT_CHANGELOG.md`）。

完整纪律：`.trae/skills/requirement-prd-sync/SKILL.md` · 现行口径：`docs/PRD.md` §13。
