"""开工前检查：把「这是一个点还是一类」变成可执行命令。

## 为什么需要它（用户 2026-09-28 的批评）

> 「你没有按照你的 skill 经验做事，① 修改点还是类，**在开发前你没考虑**，
>   所以一个问题（A11 还没教给他 prompt 怎么用）解决前后，
>   没有考虑一类问题（A12/A13-A16 还没教给他 prompt 怎么用）」

**实证**：白名单含 `cal:` 的 Agent 共 9 个，我只教了 A11 —— 剩下 8 个
在**下一轮**才补上。而 skill 里明明写着「先问这是一类还是一个点」。

**根因**：skill 是**事后可读**的提醒，而问题出在**开工那一刻**。
本脚本把那一刻变成一条命令。

## 三种模式（对应三类常见开工场景）

    # ① 我要给某个"概念"加东西 —— 它还在哪些地方出现过？
    uv run python scripts/preflight_check.py --keyword "<关键词>"

    # ② 我要新增一个"指标前缀" —— 有哪些贯通点必须逐个过？
    uv run python scripts/preflight_check.py --prefix "xxx:"

    # ③ 我要改某个文件 —— 谁在用它（影响半径）？
    uv run python scripts/preflight_check.py --file src/xxx/yyy.py

    # ④ 组合（推荐：开工时把三样都给它）
    uv run python scripts/preflight_check.py --keyword "解禁" --prefix "cal:"

## 设计原则

| 原则 | 做法 |
|---|---|
| **不硬编码清单** | 贯通点从 `supervisor.NEW_INDICATOR_TOUCHPOINTS` 读；白名单从 `_AGENT_DATA_WHITELIST` 读 —— 代码改了脚本自动跟上 |
| **自证** | 用一个"已知答案"的输入先验证脚本本身（AGENTS.md 硬约束） |
| **输出可执行** | 每条给"该看哪个文件 / 该怎么判"，不是泛泛提醒 |
| **快** | 纯 grep，无网络、无 LLM |
"""
from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except (AttributeError, OSError):
    pass

#: 扫描范围（代码 + 配置 + 测试 + 文档）
SCAN_GLOBS = (
    "src/**/*.py", "configs/**/*.yaml", "configs/**/*.yml",
    "tests/**/*.py", "scripts/**/*.py", "docs/**/*.md",
    ".trae/skills/**/*.md", "AGENTS.md",
)

#: 跳过
SKIP_PARTS = (".venv", "__pycache__", ".git", "node_modules", "data/")

#: 命中分类（按路径判定"这属于哪一层"）。
#: ⚠️ 必须**穷尽** `src/`：否则文件会掉进「⑧ 其它」黑箱，
#: 而"未分类"在输出里看起来就像"不重要"。（实测踩过：`src/domain/intel/` 整目录未覆盖）
LAYER_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("① 配置/元数据", ("configs/",)),
    ("② 代码-领域/基础设施",
     ("src/domain/", "src/infrastructure/", "src/core/")),
    ("③ 代码-编排/调度",
     ("src/orchestration/", "src/scheduler/", "src/intraday/")),
    ("④ 代码-接口", ("src/api/", "src/services/")),
    ("⑤ 测试", ("tests/",)),
    ("⑥ 文档/Skill", ("docs/", ".trae/skills/", "AGENTS.md")),
    ("⑦ 脚本", ("scripts/",)),
    ("⑧ 代码-其它", ("src/",)),  # src/ 兜底：宁可粗分类，不可无分类
)


@dataclass
class Hit:
    path: str
    line: int
    text: str
    layer: str = ""

    def __post_init__(self) -> None:
        for name, prefixes in LAYER_RULES:
            for p in prefixes:
                if self.path.startswith(p) or p in self.path:
                    self.layer = name
                    return
        self.layer = "⑨ 未分类"


#: 第几层先搜（**顺序即重要性**）。实测教训：文件按字母序排会让 `.trae/`
#: 和 `AGENTS.md` 顶到最前，一旦有 max_hits 上限，**整个 `src/` 层会被静默截断**
#: —— 使用者以为看全了"同类现场"，其实最关键的代码层一条没看到。
_LAYER_ORDER = (
    "src/", "configs/", "tests/", "scripts/", "docs/", ".trae/", "AGENTS.md",
)


def _sort_key(rel: str) -> tuple[int, str]:
    for i, prefix in enumerate(_LAYER_ORDER):
        if rel.startswith(prefix) or prefix in rel:
            return (i, rel)
    return (len(_LAYER_ORDER), rel)


def _iter_files() -> list[Path]:
    out: list[Path] = []
    for g in SCAN_GLOBS:
        for p in ROOT.glob(g):
            if not p.is_file():
                continue
            rel = p.relative_to(ROOT).as_posix()
            if any(s in rel for s in SKIP_PARTS):
                continue
            out.append(p)
    uniq = {p.relative_to(ROOT).as_posix(): p for p in out}
    return [uniq[r] for r in sorted(uniq, key=_sort_key)]


def search(keyword: str, *, max_hits: int = 0) -> list[Hit]:
    """在全部扫描范围内找关键词（字面匹配，大小写不敏感）。

    `max_hits=0`（默认）表示**不截断**。绝不默认截断：宁可输出长一点，
    也不能让"同类现场"清单静默缺层 —— 那等于把开工检查变成假绿。
    """
    hits: list[Hit] = []
    kw = keyword.lower()
    for p in _iter_files():
        try:
            text = p.read_text(encoding="utf-8-sig", errors="replace")
        except OSError:
            continue
        if kw not in text.lower():
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if kw in line.lower():
                hits.append(Hit(
                    path=p.relative_to(ROOT).as_posix(),
                    line=i, text=line.strip()[:120]))
                if max_hits and len(hits) >= max_hits:
                    hits.append(Hit(
                        path="(截断)", line=0,
                        text=f"已达 max_hits={max_hits}，结果**不完整**"))
                    return hits
    return hits


def group_by_layer(hits: list[Hit]) -> dict[str, list[Hit]]:
    out: dict[str, list[Hit]] = {}
    for h in hits:
        out.setdefault(h.layer, []).append(h)
    return dict(sorted(out.items()))


def by_file(hits: list[Hit]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for h in hits:
        counts[h.path] = counts.get(h.path, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: -kv[1]))


# ============================================================
# 贯通点检查（从代码的单一事实源读）
# ============================================================


def check_touchpoints(prefix: str) -> list[tuple[str, str, str]]:
    """检查该前缀在全部贯通点的状态。

    Returns: `[(贯通点名, 状态, 证据)]`，状态 ∈ {OK, MISS, TODO, N/A}。
    `TODO` 专用于**机器判不了、必须人来判断**的情况 ——
    既不能报 OK（假绿），也不能报 MISS（误报）。
    """
    rows: list[tuple[str, str, str]] = []

    # ① 贯通点清单（单一事实源）
    try:
        from src.orchestration.supervisor import (
            NEW_INDICATOR_TOUCHPOINTS,
            _AGENT_DATA_WHITELIST,
        )
    except Exception as exc:  # noqa: BLE001
        return [("(无法导入 supervisor)", "N/A", str(exc)[:80])]

    # ② indicators.yaml 是否登记
    yaml_hits = [h for h in search(prefix) if h.path.startswith("configs/")]
    rows.append((
        "configs/indicators.yaml",
        "OK" if yaml_hits else "MISS",
        f"{len(yaml_hits)} 处" if yaml_hits else "未登记"))

    # ③ 白名单（并列出**每一个**放行它的 Agent —— 这就是"一类"的清单）
    wl = [a for a, kws in _AGENT_DATA_WHITELIST.items()
          if any(str(k) == prefix or prefix.startswith(str(k))
                 for k in kws)]
    rows.append((
        "_AGENT_DATA_WHITELIST",
        "OK" if wl else "TODO",
        f"{len(wl)} 个 Agent：{', '.join(wl)}" if wl else
        "无 Agent 放行 —— 需人工判断：**给分析层用的数据漏改白名单**，"
        "还是本就是内部数据（可忽略）"))

    # ④ 每个放行它的 Agent，prompt 是否已教（★ 最容易漏的一层）
    # 无论白名单是否为空都要出一行 —— 整行不打印会让人以为"这项没检查"。
    if not wl:
        rows.append((
            "Agent system_prompt（贯通点⑩）", "N/A",
            "无 Agent 放行该前缀（不需要教；若本该有 Agent 用它，"
            "说明漏改 _AGENT_DATA_WHITELIST）"))
    elif prefix not in _PREFIX_KW:
        # 「没量到」≠「量到 0」：判据没登记时**不能**报 OK（假绿），
        # 也**不能**报 MISS（误报会训练人忽略警告）。单列 TODO。
        rows.append((
            "Agent system_prompt（贯通点⑩）", "TODO",
            f"{len(wl)} 个 Agent 放行，但该前缀未登记 prompt 判据关键词 "
            f"—— 请在 preflight_check.py::_PREFIX_KW 登记后重跑，"
            f"别默认它们已经会用了"))
    else:
        taught, missing = _prompt_taught(wl, prefix)
        rows.append((
            "Agent system_prompt（贯通点⑩）",
            "OK" if not missing else "MISS",
            f"已教 {len(taught)}/{len(wl)}"
            + (f"，**漏教**：{', '.join(missing)}" if missing else "")))

    # ⑤ scheduler 频率映射
    try:
        from src.scheduler.catalog_jobs import FREQUENCY_TO_CRON
        cron_n = len(FREQUENCY_TO_CRON)
        rows.append(("FREQUENCY_TO_CRON", "OK" if cron_n else "MISS",
                     f"{cron_n} 条映射"))
    except Exception as exc:  # noqa: BLE001
        rows.append(("FREQUENCY_TO_CRON", "MISS", str(exc)[:60]))

    # ⑥ capabilities（A17 是否知道这个能力）
    try:
        from src.domain.agents.decision.capabilities import CAPABILITIES
        rows.append(("capabilities.py", "OK" if CAPABILITIES else "MISS",
                     f"{len(CAPABILITIES)} 条能力登记"))
    except Exception as exc:  # noqa: BLE001
        rows.append(("capabilities.py", "MISS", str(exc)[:60]))

    # ⑦ 把单一事实源本身打出来（让开工者看到"要过几道门"）
    rows.append(("（贯通点总数，来自代码）", "N/A",
                 f"{len(NEW_INDICATOR_TOUCHPOINTS)} 个检查点"))
    return rows


#: agent_id → (模块, 类名) —— 与 test_indicator_prefix_wiring 的映射保持一致
_AGENT_MODS: dict[str, str] = {
    "A08_macro": "src.domain.agents.analysis.macro.agent:MacroAnalysisAgent",
    "A09_meso": "src.domain.agents.analysis.meso.agent:MesoAnalysisAgent",
    "A10_micro": "src.domain.agents.analysis.micro.agent:MicroAnalysisAgent",
    "A11_fin_risk": "src.domain.agents.analysis.risk.agent:RiskAnalysisAgent",
    "A12_compliance":
        "src.domain.agents.analysis.compliance.agent:ComplianceAnalysisAgent",
    "A13_tech": "src.domain.agents.industry.tech.agent:TechIndustryAgent",
    "A14_consumer":
        "src.domain.agents.industry.consumer.agent:ConsumerIndustryAgent",
    "A15_cyclical":
        "src.domain.agents.industry.cyclical.agent:CyclicalIndustryAgent",
    "A16_pharma": "src.domain.agents.industry.pharma.agent:PharmaIndustryAgent",
}

#: 前缀 → prompt 判据关键词（只认机器可读的标识，不认文案）
_PREFIX_KW: dict[str, tuple[str, ...]] = {
    "cal:": ("解禁", "unlock"),
}


def _prompt_taught(agents: list[str],
                   prefix: str) -> tuple[list[str], list[str]]:
    """逐个检查 Agent 的 system_prompt 是否提到了该前缀。"""
    import importlib

    kws = _PREFIX_KW.get(prefix)
    if not kws:
        # 调用方应先判 `prefix not in _PREFIX_KW` 并报 TODO；
        # 走到这里说明判据缺失，**绝不返回"已教"**（那就是假绿）。
        return ([], list(agents))
    taught: list[str] = []
    missing: list[str] = []
    for aid in agents:
        spec = _AGENT_MODS.get(aid)
        if spec is None:
            missing.append(f"{aid}(无类映射)")
            continue
        mp, cls = spec.split(":")
        try:
            sp = str(getattr(importlib.import_module(mp), cls).system_prompt)
            (taught if any(k in sp for k in kws) else missing).append(aid)
        except Exception:  # noqa: BLE001
            missing.append(f"{aid}(导入失败)")
    return taught, missing


# ============================================================
# 输出
# ============================================================


def _print_hits(title: str, hits: list[Hit]) -> None:
    print(f"\n{'─' * 88}")
    print(f"{title}（{len(hits)} 处）")
    print("─" * 88)
    if any(h.path == "(截断)" for h in hits):
        print("\n  ⚠️ 结果被截断，**不完整** —— 不要据此下'同类现场已看全'的结论")
    for layer, group in group_by_layer(hits).items():
        print(f"\n  {layer}")
        for h in group[:12]:
            print(f"    {h.path}:{h.line}  {h.text}")
        if len(group) > 12:
            print(f"    …还有 {len(group) - 12} 处")


def _print_file_impact(path: str) -> None:
    """影响半径：谁 import 了这个模块。"""
    mod = path.replace("/", ".").removesuffix(".py")
    if mod.startswith("src."):
        pass
    else:
        mod = f"src.{mod}"
    stem = Path(path).stem
    hits = search(stem)
    importers = [h for h in hits if h.path != path
                 and ("import" in h.text or "from" in h.text)]
    print(f"\n{'─' * 88}")
    print(f"影响半径：谁引用了 `{stem}`（{len(importers)} 处 import）")
    print("─" * 88)
    for h in importers[:20]:
        print(f"    {h.path}:{h.line}  {h.text}")
    if len(importers) > 20:
        print(f"    …还有 {len(importers) - 20} 处")
    if not importers:
        print("    （无 import —— 可能是入口文件或仅被动态加载）")


def _self_check() -> None:
    """自证：喂一个**已知存在**的词，确认搜索能命中且**没漏层**。

    AGENTS.md：「自己的检查脚本必须先自证」。
    本项目实测过"grep 没命中 → 得出'从没发生过'的错误结论"。
    这里额外断言 `src/` 必须出现 —— 实测过排序错误导致整个代码层被截断。
    """
    hits = search("_AGENT_DATA_WHITELIST")
    assert hits, "自证失败：连 _AGENT_DATA_WHITELIST 都搜不到 —— 扫描范围有问题"
    layers = {h.layer for h in hits}
    assert any(h.path.startswith("src/") for h in hits), (
        "自证失败：已知存在于 src/ 的词却没搜到 src/ —— "
        f"排序或范围有问题，命中层={layers}")
    print(f"✅ 自证通过（已知词 `_AGENT_DATA_WHITELIST` 命中 {len(hits)} 处，"
          f"覆盖 {len(layers)} 层，含 src/）")


def main() -> int:
    ap = argparse.ArgumentParser(
        description="开工前检查：这个改动是「一个点」还是「一类」？",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  uv run python scripts/preflight_check.py --keyword 解禁\n"
            "  uv run python scripts/preflight_check.py --prefix cal:\n"
            "  uv run python scripts/preflight_check.py "
            "--file src/domain/agents/analysis/risk/agent.py\n"
        ))
    ap.add_argument("--keyword", default="", help="要改的概念（关键词）")
    ap.add_argument("--prefix", default="", help="要新增/改动的指标前缀")
    ap.add_argument("--file", default="", help="要改的文件（看影响半径）")
    args = ap.parse_args()

    if not (args.keyword or args.prefix or args.file):
        ap.print_help()
        return 2

    print("=" * 88)
    print("开工前检查（preflight）")
    print("=" * 88)
    _self_check()

    if args.keyword:
        kw_hits = search(args.keyword)
        _print_hits(f"同类现场：关键词 `{args.keyword}`", kw_hits)
        counts = by_file(kw_hits)
        print(f"\n  ★ 涉及 {len(counts)} 个文件，Top10：")
        for p, n in list(counts.items())[:10]:
            print(f"      {p}  ({n} 处)")

    if args.prefix:
        print(f"\n{'=' * 88}")
        print(f"贯通点检查：前缀 `{args.prefix}`")
        print("=" * 88)
        print(f"  {'检查点':<34}{'状态':<8}证据")
        print("  " + "-" * 84)
        miss = todo = 0
        for name, status, evidence in check_touchpoints(args.prefix):
            if status == "MISS":
                miss += 1
            elif status == "TODO":
                todo += 1
            print(f"  {name:<34}{status:<8}{evidence}")
        if miss:
            print(f"\n  ❌ {miss} 个贯通点未通过 —— **开工前就要补齐**，"
                  "不要等测试红")
        elif todo:
            print(f"\n  ⚠️ {todo} 个贯通点**无法自动判定**（不是通过，也不是失败）。"
                  "先补判据，再下结论。")
        else:
            print("\n  ✅ 全部贯通点已覆盖")

    if args.file:
        _print_file_impact(args.file)

    print(f"\n{'=' * 88}")
    print("开工纪律（来自 .trae/skills/requirement-closure-and-impact）")
    print("=" * 88)
    print("""
  1. **点 vs 类**：上面的「同类现场」就是"类"的全集。逐个给结论（修/不修+理由）。
  2. **影响半径**：改动会穿过几层？上面列出的每一层都要有独立证据。
  3. **怎么证生效**：每层一条可执行证据（不是"我改了"）。
  4. **防复发**：这次要加什么护栏？（断言 / 结构清单 / 审计脚本，至少一个）
  5. **交付自问**：下一个 AI 只读仓库、不读对话，能不能不犯同一个错？
""")
    return 0


if __name__ == "__main__":
    sys.exit(main())
