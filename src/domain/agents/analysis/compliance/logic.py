"""A12合规爆雷：本地合规风险规则计算（防幻觉：旗标与等级由规则计算，LLM只做定性综合）。

规则三类：
1. 比率旗标：关联交易/商誉/质押/担保四类高危比率（阈值按百分数口径，如50即50%）
2. 存贷双高：货币资金与有息负债同时畸高，账面资金真实性存疑
3. 信息层事件：A06提取的litigation负面事件，含"立案/调查/冻结/处罚"升级为严重

等级：存在任一严重旗标，或普通旗标≥3 → 高；存在任一普通旗标 → 中；
      **量到了、且都没超阈值** → 无；**一个族都没量到且无事件** → **未量到**。

## ★★★ 2026-09-29：修掉一张「伪造的体检合格证」

### 症状（用户看得见的那一面）

A12 对任何个股都输出 `compliance_level = "无"`、旗标
`["未见明显合规风险信号"]`、`confidence = "high"`，
而且因为 `level == "无"` 会**跳过 LLM**（省 18,500 tokens/轮），
所以整条链路**没有任何一个环节会报错**。

### 根因：「没量到」与「量到 0」共用一个返回值

旧实现最后三行是：

    if severe_count >= 1 or len(flags) >= 3: level = "高"
    elif flags:                              level = "中"
    else:                                    level = "无"      # ← 两种完全不同的处境

`else` 分支同时收下了两种**语义相反**的处境：

| 处境 | 事实 | 旧输出 |
|---|---|---|
| 6 个族都量到了，值都没超阈值 | 真的没发现风险 | 「无」+「未见明显合规风险信号」 |
| **6 个族一条都没量到** | **什么都不知道** | 「无」+「未见明显合规风险信号」 |

第二行才是本仓库当时的**实际**状态 —— `关联交易`/`商誉`/`质押`/`担保`/
`货币资金`/`有息负债` 这 6 族在采集侧**一个生产者都没有**
（见 `tests/unit/test_contract_consistency.py::_KNOWN_UNPRODUCED_RULE_FAMILIES`），
于是 `_ratio_flags()` 与 `_double_high_flag()` 永远返回空，
`level` 永远是「无」，用户永远看到"未见明显合规风险"。

这是 AGENTS.md 明令禁止的那一类：**「没量到」被当成「量到 0」显示**，
而且它比"缺数据"更危险 —— 缺数据会被追问，一张合格证不会。

### 修法（两份判据，不是一个字符串）

1. `_RULE_FAMILIES` 显式列出 6 个族，逐族记录**有没有拿到值**
   （`None` = 没量到；`0.0` = 量到了、读数是 0 —— 两者必须分开）；
2. `has_input` 决定等级：无输入时给 `LEVEL_UNMEASURED`（「未量到」），
   **不再**复用「无」；占位旗标也从「未见明显合规风险信号」换成
   `FLAG_UNMEASURED`（明写"这不等于无风险"）。

`tests/unit/test_compliance_input_provenance.py` 是这条纪律的机器复现：
它断言 `evaluate_compliance([])` 的等级**不是**「无」、旗标里**没有**
「未见明显合规风险信号」，并且 6 个族逐个都能把"量到"翻成 True。

### 一处刻意的"不重构"

`_find_value(data_points, "货币资金")` 这类调用里的**字符串字面量必须保留**：
`tests/unit/test_contract_consistency.py::_consumed_rule_families()` 用 **AST**
扫这些字面量来**派生**"消费侧到底消费了哪几族"。把字面量抽成常量会让它扫不到，
于是那两条豁免被判成"消费侧已不再使用该关键词" —— **假红**，
而假红会诱使人去改一个没坏的东西。
所以族名清单另立一份（`_RULE_FAMILIES`），并由
`test_rule_families_cover_every_consumed_keyword` 断言两份清单一致
（同一判断只允许一份实现，两份就必须能被机器对上）。
"""

from __future__ import annotations

from typing import Any

# (指标关键字, ((阈值%, 描述模板, 是否严重), ...))，高阈值在前取首个命中，避免重复旗标
_RATIO_RULES: tuple[tuple[str, tuple[tuple[float, str, bool], ...]], ...] = (
    ("关联交易", ((30.0, "关联交易占比{value:g}%超30%，存在利益输送嫌疑", False),)),
    ("商誉", ((30.0, "商誉/净资产{value:g}%超30%，存在商誉减值爆雷隐患", False),)),
    ("质押", (
        (80.0, "大股东股权质押比例{value:g}%超80%，平仓与控制权变更高危", True),
        (50.0, "大股东股权质押比例{value:g}%超50%，存在平仓与控制权风险", False),
    )),
    ("担保", (
        (100.0, "对外担保/净资产{value:g}%超100%，或有负债高危", True),
        (50.0, "对外担保/净资产{value:g}%超50%，或有负债风险", False),
    )),
)

# 存贷双高阈值（均为占总资产百分比）
_DOUBLE_HIGH_CASH = 40.0
_DOUBLE_HIGH_DEBT = 40.0

#: 规则消费的族清单（**等级的"有没有输入"判据**，见模块 docstring 末节）。
#: 前四项与 `_RATIO_RULES` 的键**同源**（派生，不重抄）；后两项与
#: `_double_high_flag()` 里的 `_find_value` 字面量对应。
_RATIO_FAMILIES: tuple[str, ...] = tuple(kw for kw, _bands in _RATIO_RULES)
_DOUBLE_HIGH_FAMILIES: tuple[str, ...] = ("货币资金", "有息负债")
_RULE_FAMILIES: tuple[str, ...] = _RATIO_FAMILIES + _DOUBLE_HIGH_FAMILIES

#: 「未量到」等级 —— **不是**「无风险」。任何展示层都必须把它与「无」分开渲染。
#: ⚠️ 不要把它折叠回「无」：那正是 2026-09-29 修掉的那张伪造合格证。
LEVEL_UNMEASURED = "未量到"

#: 「无」等级下的占位旗标：**真的量过了**，且都没超阈值。
FLAG_NO_SIGNAL = "未见明显合规风险信号"

#: 「未量到」等级下的占位旗标：一个族都没量到。措辞必须自带"这不是无风险"。
FLAG_UNMEASURED = (
    "合规判据未获得输入：6 类比率族（关联交易/商誉/质押/担保/货币资金/有息负债）"
    "一条都没量到，且无诉讼/监管事件 → 无法判定合规风险，"
    "此结果不等于「无风险」"
)

# 监管/诉讼严重关键词（命中即升级为严重旗标）
_SEVERE_EVENT_KEYWORDS = ("立案", "调查", "冻结", "处罚", "警示函", "退市", "违规")


def _find_value(data_points: list[dict[str, Any]], keyword: str) -> float | None:
    for p in data_points:
        if keyword in str(p.get("indicator", "")) and isinstance(p.get("value"), (int, float)):
            return float(p["value"])
    return None


def _ratio_flags(data_points: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """返回 (全部旗标, 严重旗标)。同一指标按最高命中阈值只产一条。"""
    flags: list[str] = []
    severe: list[str] = []
    for keyword, bands in _RATIO_RULES:
        value = _find_value(data_points, keyword)
        if value is None:
            continue
        for threshold, template, is_severe in bands:  # 高阈值在前
            if value > threshold:
                desc = template.format(value=value)
                flags.append(desc)
                if is_severe:
                    severe.append(desc)
                break
    return flags, severe


def _double_high_flag(data_points: list[dict[str, Any]]) -> str | None:
    # ⚠️ 两个字符串字面量**必须原地保留**（`_DOUBLE_HIGH_FAMILIES` 是它的一份
    #    派生清单，由 `test_rule_families_cover_every_consumed_keyword` 对齐）：
    #    `test_contract_consistency.py::_consumed_rule_families()` 用 AST 扫它们。
    cash = _find_value(data_points, "货币资金")
    debt = _find_value(data_points, "有息负债")
    if (cash is not None and debt is not None
            and cash > _DOUBLE_HIGH_CASH and debt > _DOUBLE_HIGH_DEBT):
        return (f"存贷双高：货币资金/总资产{cash:g}%且有息负债/总资产{debt:g}%"
                "同时畸高，账面资金真实性存疑")
    return None


def _family_inputs(data_points: list[dict[str, Any]]) -> dict[str, float | None]:
    """逐族记录**拿到的值**：`None` = 该族没有输入（≠ 值为 0）。

    这是「没量到」与「量到 0」的分界线，也是本模块最重要的一次判断：
    质押比例 0% 是**有效读数**（真的没质押），
    而"上下文里没有质押这条指标"是**什么都不知道**。
    """
    return {kw: _find_value(data_points, kw) for kw in _RULE_FAMILIES}


def _event_flags(events: list[dict[str, Any]]) -> tuple[list[str], list[str]]:
    """信息层A06事件 → 合规监管信号。"""
    flags: list[str] = []
    severe: list[str] = []
    for e in events:
        if e.get("event_type") != "litigation" or e.get("direction") != "negative":
            continue
        quote = str(e.get("evidence_quote") or e.get("subject") or "未具名事件")
        desc = f"涉诉/监管事件：{quote[:50]}"
        flags.append(desc)
        if any(k in quote for k in _SEVERE_EVENT_KEYWORDS):
            severe.append(desc)
    return flags, severe


def evaluate_compliance(
    data_points: list[dict[str, Any]], events: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """汇总全部合规信号并给出本地爆雷等级（权威值，LLM不得修改）。

    ★ 返回值里的 `compliance_level_calc` 有**四个**取值，不是三个：

        「高」/「中」—— 有旗标；
        「无」       —— **量到了**、且都没超阈值（真的没发现风险）；
        「未量到」   —— **一个族都没量到**、也没有诉讼/监管事件（什么都不知道）。

    后两者**绝不允许**在展示层合并。溯源三件套
    （`compliance_measured` / `compliance_families_measured` /
    `compliance_families_unmeasured`）随结果一起下发，
    这样"这个结论准不准"在界面上是**可判**的，而不是靠信任。
    """
    points = list(data_points or [])
    inputs = _family_inputs(points)
    measured = sorted(kw for kw, value in inputs.items() if value is not None)
    unmeasured = sorted(kw for kw, value in inputs.items() if value is None)

    flags, severe = _ratio_flags(points)
    double_high = _double_high_flag(points)
    if double_high:
        flags.append(double_high)
        severe.append(double_high)
    event_flags, event_severe = _event_flags(list(events or []))
    flags += event_flags
    severe += event_severe

    # 「有输入」= 至少一个族量到了值，或至少有一条诉讼/监管事件。
    # ⚠️ 用 `event_flags` 而不是 `events`：一条"产品/正面"事件对合规判定
    #    没有任何信息量，把它算成输入会让等级又从「未量到」漏回「无」。
    has_input = bool(measured) or bool(event_flags)

    severe_count = len(severe)
    if severe_count >= 1 or len(flags) >= 3:
        level = "高"
    elif flags:
        level = "中"
    elif not has_input:
        level = LEVEL_UNMEASURED
    else:
        level = "无"

    if flags:
        placeholder: list[str] = []
    elif has_input:
        placeholder = [FLAG_NO_SIGNAL]
    else:
        placeholder = [FLAG_UNMEASURED]

    return {
        "compliance_flags": flags + placeholder,
        "severe_flag_count": severe_count,
        "compliance_level_calc": level,
        # —— 溯源：这次判定到底量到了什么（"没量到"与"量到 0"分开显示）——
        "compliance_measured": has_input,
        "compliance_families_measured": measured,
        "compliance_families_unmeasured": unmeasured,
        "compliance_event_flags": len(event_flags),
    }
