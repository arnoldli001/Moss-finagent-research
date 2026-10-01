"""散文缺口 → **指标 id**：逐条复核过的映射表（不是相似度兜底）。

## 为什么需要它

A17（决策 Agent）报缺口时报的是**人话**，例如：

```
北向资金日度净买额（2024-08-19起交易所停止披露），外资实时流向不可得。
限售解禁规模与家数。
美国失业率、非农就业等就业口径
招商银行个股财务明细、股息率、净息差与资产质量数据
```

这些进了 `gap_queue` 之后，**没人能拿它去取数** —— A19 要的是可判定的指标名，
批采要的是指标 id。实测：35 条 pending 全是散文，`attempts=0`（一条都没被补过）。

## 为什么不用相似度/模糊匹配

本项目已经**实测否决过**这条路（`电气设备→火电设备`、`铁路→钢铁`、
`保险→环保`、`白酒→啤酒` 全是错的）⇒ **错配比缺数据更危险**（数字看着有据）。
所以本表是**逐条复核**的：每条规则写明命中的关键词、目标 id、以及为什么。

## 三种结局

| 结局 | 含义 | 队列动作 |
|---|---|---|
| `map` | 认出了概念且有可达的指标 id | 展开成具体 id 入队，原散文条目标 `resolved` |
| `terminated` | **物理不可得**（源已停止披露） | 标 `skipped` + 原因**停止重试**（不是"我们没采"） |
| `unmapped` | 认不出，或认出概念但缺标的代码 | **保持 pending**（留在队列里可见，等人补规则） |

`unmapped` 刻意**不静默丢弃**：它表示"这张表还没覆盖到"，是下一轮该补的输入。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

ACTION_MAP = "map"
ACTION_TERMINATED = "terminated"
ACTION_UNMAPPED = "unmapped"


@dataclass(frozen=True)
class ProseRule:
    """一条复核过的映射规则。"""

    name: str
    keywords: tuple[str, ...]      # **全部**出现才算命中（AND；避免误吞）
    targets: tuple[str, ...] = ()  # 指标 id 或含 `{code}` 的模板
    needs_code: bool = False       # 目标里的 `{code}` 必须先解析出标的
    action: str = ACTION_MAP
    reason: str = ""
    evidence: str = ""             # 复核依据（terminated 必填）


#: ★ 逐条复核的映射表（2026-09-30）。
#:
#: 目标 id 全部**逐个验过**"登记在册"或"连接器认它"（护栏测试会自动复核，
#: 见 `tests/unit/test_prose_gap_mapping.py`）—— 所以这张表不会腐烂：
#: 哪天某个 id 被摘掉，测试立刻红。
PROSE_RULES: tuple[ProseRule, ...] = (
    # ---------- 物理不可得（源停止披露）----------
    ProseRule(
        name="北向资金日度净买额",
        keywords=("北向资金",),
        action=ACTION_TERMINATED,
        reason="沪深港通自 2024-08-19 起**停止披露**日度净买额 ⇒ 物理不可得",
        evidence=(
            "实测 `mkt:north_flow` 最后一条 period_date=2024-08-16（8 条），"
            "此后无任何新值；连接器 `NorthboundFlowConnector` 仍在登记面上，"
            "但源侧不再发数 ⇒ 继续重试只是每天白试一次"),
    ),
    # ---------- 解禁（现在有逐股表了）----------
    ProseRule(
        name="限售解禁规模与家数（逐股）",
        keywords=("解禁",),
        targets=("解禁计划:{code}",),
        needs_code=True,
        reason="已建 `app_db::unlock_plan` 逐股表 + `解禁计划:{code}` 连接器",
    ),
    ProseRule(
        name="限售解禁（全市场家数/市值）",
        keywords=("解禁",),
        targets=("cal:unlock:company_count", "cal:unlock:market_cap"),
        reason="全市场口径由投资日历作业（`cal:unlock:*`）提供，不需要代码",
    ),
    # ---------- 美国宏观（换源后的新 id）----------
    ProseRule(
        name="美国失业率",
        keywords=("失业率",),
        targets=("fred:UNRATE",),
        reason="东财 `macro_usa_*` 源停更 ⇒ 已换源到 FRED（`fred:UNRATE`）",
    ),
    ProseRule(
        name="美国非农就业",
        keywords=("非农",),
        targets=("fred:PAYEMS",),
        reason="同上换源；水平值（千人），「新增」需派生层算",
    ),
    ProseRule(
        name="美国核心 PCE",
        keywords=("PCE",),
        targets=("fred:PCEPILFE",),
        reason="同上换源；**指数**不是同比，同比需派生",
    ),
    ProseRule(
        name="美国 CPI",
        keywords=("美国", "CPI"),
        targets=("us_cpi_yoy", "fred:CPILFESL"),
        reason="同比走 `us_cpi_yoy`（源仍在更新），核心指数走 `fred:CPILFESL`",
    ),
    ProseRule(
        name="美联储利率与目标区间",
        keywords=("联邦基金利率",),
        targets=("fed:target_upper", "fed:target_lower", "fed:effr"),
        reason="已拆成三个**单值序列**（原先一个 indicator 同日三值会把下限当政策利率）",
    ),
    ProseRule(
        name="美联储点阵图/降息概率",
        keywords=("点阵图",),
        targets=("fed:rate_prob:next",),
        reason="CME FedWatch 本机不可达（环境事实），登记面保留；方向由 fed:* 支撑",
    ),
    # ---------- 个股（带代码）----------
    ProseRule(
        name="个股股息率",
        keywords=("股息率",),
        targets=("股息率TTM:{code}",),
        needs_code=True,
        reason="合成口径的 `股息率` 源间歇性失败已下架；`股息率TTM` 走本地行情仓",
    ),
    ProseRule(
        name="银行息差/净息差",
        keywords=("息差",),
        targets=("净息差:{code}",),
        needs_code=True,
        reason="派生指标（利息净收入 ÷ 生息资产平均余额），已落事实表攒趋势",
    ),
    ProseRule(
        name="个股估值水位",
        keywords=("估值",),
        targets=("估值水位:{code}",),
        needs_code=True,
        reason="平台已有权威实现（`估值水位` 连接器），不要另算一套分位",
    ),
    ProseRule(
        name="个股行情",
        keywords=("个股", "行情"),
        targets=("stock_close:{code}",),
        needs_code=True,
        reason="日线链（AkShare→腾讯→Tushare→baostock）",
    ),
    ProseRule(
        name="合规比率族",
        keywords=("合规",),
        targets=("商誉占净资产比:{code}", "有息负债占总资产比:{code}",
                 "货币资金占总资产比:{code}"),
        needs_code=True,
        reason="A12 的 6 个族已有生产者；缺的是**输入到手**（白名单与登记面已对齐）",
    ),
    # ---------- 市场面（不需要代码）----------
    ProseRule(
        name="两市/三市成交额",
        keywords=("成交额",),
        targets=("mkt:turnover:total",),
        reason="平台自定义口径（`mkt:*`），已并入月频兜底作业",
    ),
    ProseRule(
        name="全A换手率",
        keywords=("换手率",),
        targets=("mkt:turnover_rate:all_a",),
        reason="同上",
    ),
    ProseRule(
        name="两融余额",
        keywords=("两融",),
        targets=("mkt:margin_balance", "mkt:margin_net_buy"),
        reason="`MarginTradingConnector` 提供余额/融资买入额（交易所 T+1）",
    ),
    ProseRule(
        name="指数估值分位",
        keywords=("指数",),
        targets=("idx_val:snapshot:all",),
        reason="指数口径走 `idx_val:*`（模板带指数名，无具体指数名时取快照族）",
    ),
    ProseRule(
        name="行业拥挤度/分项估值",
        keywords=("行业",),
        targets=("行业拥挤度:银行",),
        reason="板块池口径；⚠️「银行行业分项估值」**当前无生产者**（见 PRD 已知缺口）",
    ),
    # ---------- 不是缺口，是**缺陷**（别在补采队列里空转）----------
    ProseRule(
        name="量能数值单位异常",
        keywords=("单位异常",),
        action=ACTION_TERMINATED,
        reason="这是**数据质量缺陷**（量能数值量级偏差），不是「缺数据」"
               " ⇒ 停止在补采队列里重试，转缺陷登记",
        evidence=(
            "A17 原话「量能MA5/MA10/MA50数值单位异常（万亿级偏差），环比缩量幅度"
            "不可用」—— 数据**取到了**、只是量级不对；补采一万次也不会变对。"
            "同类问题本项目登记过：`产权比率=0` 那种「源给了不成立的 0」也是缺陷不是缺口"),
    ),
)


@dataclass
class ProseVerdict:
    """一条散文缺口的判定。"""

    action: str
    indicators: list[str] = field(default_factory=list)
    rule: str = ""
    reason: str = ""

    @property
    def mapped(self) -> bool:
        return self.action == ACTION_MAP and bool(self.indicators)


def _default_code(text: str) -> str:
    """从散文里解析标的代码（复用既有实体别名，不自造一套）。"""
    try:
        from src.infrastructure.catalog.synonym_dict import (
            extract_code,
            resolve_entity,
        )
    except Exception:  # noqa: BLE001 字典不可用 ⇒ 当解析不出
        return ""
    code = extract_code(text)
    if code:
        return str(code)
    try:
        hit = [c for c in resolve_entity(text) if str(c).strip()]
    except Exception:  # noqa: BLE001
        return ""
    return str(hit[0]) if hit else ""


def normalize(text: str, *, resolve_code: Any = None) -> ProseVerdict:
    """散文 → 判定（`map` / `terminated` / `unmapped`）。

    Args:
        text: 队列里的原始散文
        resolve_code: `(text) -> code`，缺省用既有实体别名解析。
            注入是为了可离线单测（不依赖 5000 只票的名字表）。
    """
    raw = " ".join(str(text or "").split())
    if not raw:
        return ProseVerdict(ACTION_UNMAPPED, reason="空文本")

    # ★ 顺序即语义（两处，都踩过）：
    #   ① **terminated 优先** —— "北向资金…停止披露" 里也含"资金/行业"等词，
    #      先判物理不可得，避免被后面某条泛化规则错配成一个取不到的 id；
    #   ② **命中的规则取并集**，不是"第一条命中就返回" —— 一句人话常同时点几件事
    #      （实测「招商银行个股财务明细、**股息率**、**净息差**与资产质量数据」），
    #      只取第一条会把"净息差"整条丢掉。每条并进来的 id 都来自**复核过的规则**，
    #      所以这仍然是逐条复核，不是模糊匹配。
    ordered = sorted(PROSE_RULES,
                     key=lambda r: 0 if r.action == ACTION_TERMINATED else 1)
    targets: list[str] = []
    rules_hit: list[str] = []
    needs_code_missing: list[str] = []
    for rule in ordered:
        if not all(k in raw for k in rule.keywords):
            continue
        if rule.action == ACTION_TERMINATED:
            return ProseVerdict(ACTION_TERMINATED, rule=rule.name,
                                reason=rule.reason or "物理不可得")
        if not rule.targets:
            continue
        expanded = list(rule.targets)
        if rule.needs_code:
            code = ""
            try:
                code = str((resolve_code or _default_code)(raw) or "")
            except Exception as exc:  # noqa: BLE001
                logger.debug("散文映射：代码解析失败 %s", exc)
            if not code:
                needs_code_missing.append(rule.name)
                continue
            expanded = [t.format(code=code) if "{code}" in t else t
                        for t in expanded]
        for ind in expanded:
            if ind not in targets:
                targets.append(ind)
        rules_hit.append(rule.name)
    if targets:
        return ProseVerdict(ACTION_MAP, indicators=targets,
                            rule=" + ".join(rules_hit),
                            reason="；".join(
                                r.reason for r in ordered
                                if r.name in rules_hit and r.reason))
    if needs_code_missing:
        return ProseVerdict(
            ACTION_UNMAPPED, rule=" + ".join(needs_code_missing),
            reason=f"认出「{'、'.join(needs_code_missing)}」但**缺标的代码**"
                   " ⇒ 无法取数（留在队列里等人补上下文）")
    return ProseVerdict(ACTION_UNMAPPED,
                        reason="映射表未覆盖（下一轮该补规则，**不是**丢弃）")


def expand_prose_gaps(queue: Any, *, resolve_code: Any = None,
                      is_registered: Any = None) -> dict[str, int]:
    """把队列里的散文缺口**展开**成指标 id / 标成物理不可得。

    对每条 pending：`map` ⇒ 展开成具体 id 入队 + 原条目 `resolved`；
    `terminated` ⇒ `skipped`（**停止重试**，理由随条目落盘）；
    `unmapped` ⇒ 原样留着（可见，等人补规则）。

    Args:
        is_registered: `(indicator) -> bool`。**必须传**才认得出"裸名登记的 id"
            —— 实测 `利息净收入`（bank 连接器的具体字段，YAML 里以裸名登记）
            不传判据时会被当成散文，于是它既进不了取数侧、又被记成"映射表未覆盖"。
            传了之后它是**已知指标**，走正常的 catalog/resolver 出口。

    Returns: `{"mapped": n, "expanded": k, "terminated": m, "unmapped": u}`
    """
    from src.domain.agents.decision.gap_queue import looks_like_indicator_id

    out = {"mapped": 0, "expanded": 0, "terminated": 0, "unmapped": 0}
    for entry in list(queue.pending()):
        text = str(getattr(entry, "indicator", "") or "")
        if looks_like_indicator_id(text, is_registered=is_registered):
            continue                     # 已经是指标 id，不碰
        verdict = normalize(text, resolve_code=resolve_code)
        if verdict.mapped:
            added = 0
            for ind in verdict.indicators:
                if queue.enqueue(
                    ind,
                    reason=f"散文缺口展开（{verdict.rule}）：{text[:80]}",
                    source=f"{getattr(entry, 'source', '')}+prose_map",
                ):
                    added += 1
            queue.mark(entry, "resolved",
                       result=f"展开为 {len(verdict.indicators)} 个指标 id"
                              f"（新增 {added}）")
            out["mapped"] += 1
            out["expanded"] += added
            logger.info("[散文展开] %s → %s", text[:40], verdict.indicators)
        elif verdict.action == ACTION_TERMINATED:
            queue.mark(entry, "skipped",
                       result=f"物理不可得：{verdict.reason}")
            out["terminated"] += 1
            logger.warning("[散文展开] 停止重试（物理不可得）：%s ⇒ %s",
                           text[:50], verdict.reason)
        else:
            out["unmapped"] += 1
    return out


__all__ = [
    "ACTION_MAP",
    "ACTION_TERMINATED",
    "ACTION_UNMAPPED",
    "PROSE_RULES",
    "ProseRule",
    "ProseVerdict",
    "expand_prose_gaps",
    "normalize",
]
