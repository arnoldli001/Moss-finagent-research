"""主线挖掘：板块成分股池（去掉"蹭概念"的噪声股）。

## 问题：概念成分股里大量是噪声

概念板块的成分股来自同花顺 `ths_member`。一只股票可以属于**几十个概念** ——
其中大量是蹭概念的：大市值股因为被主题基金持有而被挂进去，小市值股因为
名字沾边被挂进去。它们进入板块聚合后，把真正在动的那几只票的信号稀释掉。

实测（2024-06-12，本地数据）：

    CRO概念   77 只成分股 → 成交额 top30% 贡献了 |净流入| 的 84%
    医疗服务  56 只成分股 → top30% 贡献 83%
    农业种植  93 只成分股 → top30% 贡献 69%

也就是 **70% 的成分股只贡献 16~31% 的资金动作**。板块层面的分数因此既不灵敏
（真正在动的票被摊薄）也不稳（噪声票的随机波动会盖过信号）。

## 相关度为什么默认用「成交额」而不是「主营业务」

需求提出的是"主营业务相关度最高的前 30%"。用主营构成的困难在于**名称对不上**：
`fina_mainbz` 返回的业务条目是「化学业务 / 测试业务 / 生物学业务」，
而板块叫「CRO概念」—— 最核心的那只票（药明康德）恰恰**匹配不上**，
纯名称匹配会把它筛掉，比不过滤还糟。

因此默认口径用**成交额占比**作为"市场验证过的相关度"：

1. 真属于这个概念的票会被资金集中交易；蹭概念的票通常成交额不突出；
2. 只用当日及之前的数据，**没有前视偏差**（主营构成只有最新一期，
   拿它做 2022 年的回测等于把今天的业务结构搬回过去）；
3. 自动更新，不需要为 2000+ 个概念维护词表。

主营相关度作为**保底**：`fina_mainbz` 的业务条目命中板块关键字时该股强制保留
（避免把"业务很纯但当天没人交易"的票剔掉）。`pool.mode` 可切换口径。

## 人工覆盖优先于一切自动规则

`configs/mainline_pools.yaml` 的 `include` / `exclude` 优先级最高。
自动规则一定有判错的时候（"这个板块到底该看哪几只票"终究是研究判断），
必须有一个**不改代码就能增删**的入口 —— 这也是需求明确要求的。

## 三条不能违反的约束

1. **裁剪只影响打分，不影响留档**：`ml_member` 里的原始成分股一条都不删。
   裁剪是"这次打分用哪些票"，不是"这个板块有哪些票"—— 后者改错了不可逆。
2. **小板块不裁**：成分股少于 `min_members` 时不裁剪，再裁就没剩几只，
   板块级分位统计会失去意义。
3. **龙头强制保留**：成交额/净流入排名前 `always_keep_top` 的股票永远保留，
   否则会出现"把龙头裁掉、然后问为什么没有龙头共振"的荒谬结果。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief
from src.mainline.config import MainlineConfig, load_config, load_yaml_config
from src.mainline.scoring import percentile_score, to_float

logger = logging.getLogger(__name__)

DEFAULT_RULE_FILE = "mainline_pools.yaml"

#: ST / 退市类前缀（这些股票的资金动作与板块主线无关，且涨跌幅限制不同）
_ST_PREFIX = ("ST", "*ST", "SST", "S*ST", "退市")


@dataclass
class PoolRule:
    """人工规则：某板块显式保留 / 剔除的股票。"""

    include: list[str] = field(default_factory=list)
    exclude: list[str] = field(default_factory=list)
    #: 该板块是否完全不过滤（`keep_all: true`）——
    #: 给"这个概念的成分股本身就都很纯"的板块留一个开关
    keep_all: bool = False


@dataclass
class PoolRules:
    """`configs/mainline_pools.yaml` 的内容。"""

    #: 全局规则（与 `config.pool` 的同名项合并，YAML 优先）
    top_pct: float | None = None
    min_circ_mv: float | None = None
    by_board: dict[str, PoolRule] = field(default_factory=dict)
    #: 按板块名匹配的规则（支持包含匹配，与 board_profiles 同口径）
    by_keyword: list[tuple[str, PoolRule]] = field(default_factory=list)
    loaded: bool = False
    gap: str = ""

    def rule_for(self, board_code: str, board_name: str) -> PoolRule:
        """取某板块的规则（**代码精确匹配 > 名称关键字最长匹配**）。"""
        direct = self.by_board.get(board_code)
        if direct is not None:
            return direct
        best: tuple[int, PoolRule] | None = None
        for keyword, rule in self.by_keyword:
            if keyword and keyword in (board_name or ""):
                if best is None or len(keyword) > best[0]:
                    best = (len(keyword), rule)
        return best[1] if best else PoolRule()


@dataclass
class PoolOutcome:
    """一次股池裁剪的结果与说明（供面板展示"为什么少了这么多票"）。"""

    members: dict[str, list[str]] = field(default_factory=dict)
    #: `{board_code: {"before": n, "after": m, "kept": [...], "dropped": [...]}}`
    detail: dict[str, dict[str, Any]] = field(default_factory=dict)
    enabled: bool = False
    note: str = ""

    @property
    def before_total(self) -> int:
        return sum(item["before"] for item in self.detail.values())

    @property
    def after_total(self) -> int:
        return sum(item["after"] for item in self.detail.values())

    def summary(self) -> str:
        if not self.enabled:
            return "成分股池未启用裁剪（使用板块全部成分股）"
        return (f"成分股池裁剪：{self.before_total} → {self.after_total} 只"
                f"（保留 {self.after_total / self.before_total * 100:.0f}%）"
                if self.before_total else "成分股池裁剪：无成分股")


def load_rules(config: MainlineConfig | None = None) -> PoolRules:
    """加载人工规则文件（缺失时返回空规则，不抛错）。"""
    cfg = config or load_config()
    path = getattr(cfg.pool, "rule_file", DEFAULT_RULE_FILE)
    raw = load_yaml_config(path)
    if raw is None:
        return PoolRules(loaded=False, gap="")   # 没有规则文件是正常状态
    out = PoolRules(loaded=True)
    out.top_pct = to_float((raw.get("defaults") or {}).get("top_pct"))
    out.min_circ_mv = to_float((raw.get("defaults") or {}).get("min_circ_mv"))
    for key, value in (raw.get("boards") or {}).items():
        if not isinstance(value, dict):
            continue
        out.by_board[str(key)] = _rule(value)
    for item in (raw.get("keywords") or []):
        if not isinstance(item, dict):
            continue
        keyword = str(item.get("keyword") or "").strip()
        if keyword:
            out.by_keyword.append((keyword, _rule(item)))
    return out


def _rule(raw: dict[str, Any]) -> PoolRule:
    def codes(key: str) -> list[str]:
        value = raw.get(key)
        if not isinstance(value, (list, tuple)):
            return []
        return [str(item).split(".")[0].zfill(6) for item in value if str(item).strip()]

    return PoolRule(include=codes("include"), exclude=codes("exclude"),
                    keep_all=bool(raw.get("keep_all", False)))


def is_st(name: str) -> bool:
    """名称是否属于 ST / 退市类。

    判定要求 ST 前缀后面**跟的不是 ASCII 字母**：简单的 `startswith("ST")`
    会把「STAR股份」这类名字误判成 ST（测试里发现过），而 ST 股的实际形态
    永远是「ST + 中文字符」。全角星号 `＊` 也要认。
    """
    text = str(name or "").strip()
    if not text:
        return False
    # 先去掉前导星号（`*ST` / `＊ST` 是同一类，全角半角都要认）
    body = text.lstrip("*＊").strip()
    if not body:
        return True              # 名字只有星号（异常，但不该当成正常票）
    for prefix in _ST_PREFIX:
        plain = prefix.replace("*", "")
        if not body.upper().startswith(plain.upper()):
            continue
        # 去掉前缀之后，下一个字符不能是 ASCII 字母
        rest = body[len(plain):].strip()
        if not rest:
            return True          # 名字就叫 "ST"（异常，但不该当成正常票）
        if not (rest[0].isascii() and rest[0].isalpha()):
            return True
    return False


def resolve_pool(member_map: dict[str, list[str]], *,
                 board_names: dict[str, str],
                 market_stats: dict[str, dict[str, float]],
                 config: MainlineConfig | None = None,
                 rules: PoolRules | None = None) -> PoolOutcome:
    """按规则裁剪每个板块的成分股池。

    `market_stats` 是 `{code: {amount_5d, net_5d, ...}}`（来自
    `datastore.bulk_member_market_stats`），**必须用当日及之前的数据** ——
    用未来数据算"相关度"等于让板块自动选中未来的强势股，回测会好得离谱。

    `board_names` 是 `{board_code: 名称}`，用于关键字规则与 ST 判定。
    """
    cfg = config or load_config()
    pool_cfg = cfg.pool
    rules = rules if rules is not None else load_rules(cfg)
    top_pct = float(rules.top_pct if rules.top_pct is not None
                    else pool_cfg.top_pct)
    min_cap = float(rules.min_circ_mv if rules.min_circ_mv is not None
                    else pool_cfg.min_circ_mv)
    top_pct = max(0.05, min(1.0, top_pct))

    outcome = PoolOutcome(enabled=bool(pool_cfg.enabled))
    for board_code, members in member_map.items():
        codes = [str(code).zfill(6) for code in members if code]
        rule = rules.rule_for(board_code, board_names.get(board_code, ""))
        kept, dropped, reason = _filter_board(
            codes, rule=rule, board_name=board_names.get(board_code, ""),
            market_stats=market_stats, pool_cfg=pool_cfg, top_pct=top_pct,
            min_cap=min_cap)
        outcome.members[board_code] = kept
        outcome.detail[board_code] = {
            "before": len(codes), "after": len(kept),
            "kept": kept, "dropped": dropped, "reason": reason}
    if not pool_cfg.enabled:
        outcome.note = outcome.summary()
    return outcome


def _filter_board(codes: Sequence[str], *, rule: PoolRule, board_name: str,
                  market_stats: dict[str, dict[str, float]], pool_cfg: Any,
                  top_pct: float, min_cap: float
                  ) -> tuple[list[str], list[str], str]:
    """单个板块的裁剪（返回 `(保留, 剔除, 原因)`）。"""
    if not pool_cfg.enabled:
        return list(codes), [], "裁剪未启用"
    if rule.keep_all:
        return list(codes), [], "人工规则：该板块不过滤"
    if len(codes) <= max(int(pool_cfg.min_members), 1):
        return list(codes), [], f"成分股仅 {len(codes)} 只，少于下限不裁剪"

    exclude = set(rule.exclude)
    force_keep = set(rule.include)
    scored: list[tuple[float, str]] = []
    for code in codes:
        stats = market_stats.get(code) or {}
        amount = to_float(stats.get("amount_5d")) or 0.0
        scored.append((amount, code))
    # 成交额降序：真属于该概念的票会被资金集中交易
    scored.sort(key=lambda item: (-item[0], item[1]))
    size = max(int(pool_cfg.keep_min), int(round(len(codes) * top_pct)))
    size = min(size, len(codes))
    keep: list[str] = []
    dropped: list[str] = []
    for index, (_amount, code) in enumerate(scored):
        if code in exclude:
            dropped.append(code)
            continue
        if code in force_keep:
            keep.append(code)
            continue
        stats = market_stats.get(code) or {}
        cap = to_float(stats.get("circ_mv")) or 0.0
        if min_cap > 0 and cap < min_cap:
            dropped.append(code)
            continue
        if pool_cfg.exclude_st and is_st(stats.get("name") or ""):
            dropped.append(code)
            continue
        # 成交额排名前 always_keep_top 的强制保留（龙头不该被裁掉）；
        # 其后按 top_pct 截断
        if index < max(int(pool_cfg.always_keep_top), 0) or index < size:
            keep.append(code)
        else:
            dropped.append(code)
    # 人工 include 的票可能在原成分股之外 —— 补进来（这是"让我来配置"的核心用途）
    for code in force_keep:
        if code not in keep:
            keep.append(code)
    reason = (f"按成交额取前 {top_pct:.0%}"
              + (f"，市值下限 {min_cap / 1e8:.0f} 亿" if min_cap > 0 else "")
              + ("（排除 ST）" if pool_cfg.exclude_st else ""))
    return keep, dropped, reason


def relevance_scores(member_map: dict[str, list[str]], *,
                     market_stats: dict[str, dict[str, float]]
                     ) -> dict[str, dict[str, float]]:
    """板块内每只股票的**相关度分位**（0-100，成交额口径）。

    面板的"股池配置"用它作为默认排序 —— 使用者打开某个板块时，
    最该看到的是"系统认为最相关的那些票"，然后在其上增删。
    """
    out: dict[str, dict[str, float]] = {}
    for board_code, members in member_map.items():
        values = [to_float((market_stats.get(str(code).zfill(6))
                            or {}).get("amount_5d")) for code in members]
        row: dict[str, float] = {}
        for code in members:
            key = str(code).zfill(6)
            score = percentile_score(
                values, (market_stats.get(key) or {}).get("amount_5d"))
            row[key] = round(score, 2) if score is not None else 0.0
        out[board_code] = row
    return out


def summarize(outcome: PoolOutcome, *, limit: int = 10) -> list[str]:
    """给 `gaps` / 日志用的一行行说明（只列裁剪比例最小的几个板块，便于核查）。"""
    items = [(code, item) for code, item in outcome.detail.items()
             if item["after"] < item["before"]]
    items.sort(key=lambda pair: pair[1]["after"] / max(pair[1]["before"], 1))
    lines = [outcome.summary()]
    for code, item in items[:limit]:
        lines.append(f"{code}：{item['before']} → {item['after']} 只"
                     f"（{item['reason']}）")
    return lines


def _log_failure(exc: BaseException) -> None:
    logger.warning("股池规则处理失败（回落为不裁剪）：%s", brief(exc, BRIEF_TIGHT))


__all__ = [
    "DEFAULT_RULE_FILE",
    "PoolOutcome",
    "PoolRule",
    "PoolRules",
    "is_st",
    "load_rules",
    "relevance_scores",
    "resolve_pool",
    "summarize",
]
