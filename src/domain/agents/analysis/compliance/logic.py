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

## ★★★ 2026-10-02：多标的（一次问句 ≥2 只票）不许拼出一张合格证

### 症状

`evaluate_compliance()` 原先把 `data_points` 当成**一只票**的数据在扫：

    _find_value(data_points, "商誉")      → 整个列表里的**首个**命中
    _find_value(data_points, "货币资金")  → 同上
    _find_value(data_points, "有息负债")  → 同上

而问句里点两只票是**常态**：`CHG-0216` 之后个股指标按 `resolved_codes`
**逐只**排（见 `supervisor.augment_plan_by_query_signals`），
所以 `data_points` 里本来就同时躺着 A、B 两只票的同名指标。于是：

  · `货币资金` 取到 A 公司、`有息负债` 取到 B 公司
    ⇒ 拼出一条**两家公司合起来才有**的「存贷双高」；
  · `compliance_families_measured` 是**跨代码并集**
    ⇒ A 量到 3 族 + B 量到 3 族 = 「已量到 6/6 族」，
      而**没有任何一只票**的 6 族齐过（最坏就是这张假合格证）；
  · 一个 `level` 收下全部标的，旗标文案里**没有代码/公司**
    ⇒ 用户看到一条**无归属**、可能张冠李戴的合规等级。

### 修法：按代码分组，每只票各自独立判定

数据点的 `indicator` 带 6 位代码后缀（`商誉占净资产比:601088`），
**归属从后缀就能拿到**，所以分组是确定性的、不需要动采集侧：

  · `_payload_codes()` 列出出现过的代码；`_find_value(..., code=X)`
    只在 X 自己的点里取值 ⇒ 比率旗标、存贷双高、族清单**一律不许跨代码**；
  · 每只票一份 `compliance_per_code` 条目；扁平字段是它的**汇总** ——
    等级取**最坏**、`compliance_families_measured` 取**交集**
    （"6/6 族"必须是**同一只票**的 6 个族，并集不是）；
  · 多标的时旗标文案带 `[代码]` 前缀；**单标的不加**（逐字兼容）；
  · 单标的（0 或 1 个代码）走**原样口径**：那时"整份输入"本来就等于
    "这一只票"，首个命中即该票的首个命中 ⇒ 输出与修复前逐字一致。

### 边界（必须知道，否则会把保守当故障）

· **事件的归属判不出来**：A06 的事件契约
  （`item_id/event_type/subject/direction/magnitude/event_date/`
  `evidence_quote/confidence`）**没有代码字段**。多标的时事件只在**汇总层**
  计入（`compliance_unattributed_event_flags`），**不摊派给任何一只票**
  —— 摊派就是又一次张冠李戴。单标的时事件照旧落在那唯一一只票上。
· `_payload_codes()` 认"**最后一段**里恰好一个 6 位数字"= 代码
  （`600519` / `600519.SH` / `SH600519` 三种写法都认）。实测登记表与
  planner 目录里**没有**任何以 6 位数字结尾的非个股指标
  （守卫见 `tests/unit/test_compliance_multi_target.py`）。
  万一将来出现，症状是等级偏保守（「未量到」），**不是**假绿。
· **认不出代码时不会退化回"跨标的合并"**：一只票都没有代码 → 走单标的口径，
  那时的输出仍然只描述"整份输入"，不会把两只票的数拼成一个等级。
  真正会退化的只有"多标的但都没带后缀"这种输入，而它在本仓库里不存在
  （5 个比率族都在 `supervisor._CODE_SUFFIX_INDICATORS` 里）。

### 一处刻意的"不重构"

`_find_value(data_points, "货币资金")` 这类调用里的**字符串字面量必须保留**：
`tests/unit/test_contract_consistency.py::_consumed_rule_families()` 用 **AST**
扫这些字面量来**派生**"消费侧到底消费了哪几族"。把字面量抽成常量会让它扫不到，
于是那两条豁免被判成"消费侧已不再使用该关键词" —— **假红**，
而假红会诱使人去改一个没坏的东西。
所以族名清单另立一份（`_RULE_FAMILIES`），并由
`test_rule_families_cover_every_consumed_keyword` 断言两份清单一致
（同一判断只允许一份实现，两份就必须能被机器对上）。

⚠️ 同一约束也管住了 2026-10-02 这次改动：代码过滤是 `_find_value()` 的
**第三个参数**，函数名与那两个字符串字面量都**原地保留**
（改了函数名 = AST 扫不到 = 上面那条断言假红）。
"""

from __future__ import annotations

import re
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

#: 多标的（≥2 只票）「未量到」的占位旗标 —— 与单标的的 `FLAG_UNMEASURED`
#: **必须分开**：单标的那句说的是"6 个族一条都没量到"，而多标的时通常只是
#: **其中一只**没量到，复用同一句话会让读者以为"全体都没数据"。
#: ⚠️ 同样不许折叠：多标的的汇总等级偏保守（宁可疑，不许假绿）。
FLAG_UNMEASURED_MULTI = (
    "多标的合规判据未获齐输入：至少一只标的的比率族一个都没量到"
    "（逐只明细见 compliance_per_code）→ 无法给出统一合规结论，"
    "此结果不等于「无风险」"
)

# 监管/诉讼严重关键词（命中即升级为严重旗标）
_SEVERE_EVENT_KEYWORDS = ("立案", "调查", "冻结", "处罚", "警示函", "退市", "违规")

#: 数据点没有代码后缀时的归属标签（只在多标的模式下会用到）。
UNLABELED_TARGET = "未标注标的"

#: 代码认领：**指标名的最后一段**里恰好一个 6 位数字。
#: 兼容 `600519` / `600519.SH` / `SH600519` 三种写法。
#: ⚠️ 不用"整串搜索 6 位数字"：那样 `ind:sw_third_pe_ttm:all` 之外的同形状段
#: （期号、集合名）会被误判成代码；误判代价是等级偏保守（「未量到」），
#: 可接受但没必要 —— 认领失败时的兜底是"单标的原样口径"，不会跨标的合并。
_CODE_IN_SEGMENT_RE = re.compile(r"^[^\d]*(\d{6})[^\d]*$")


def _point_code(point: dict[str, Any]) -> str:
    """从 `indicator` 的后缀里认领标的代码：`商誉占净资产比:601088` → `601088`。

    返回 `""` = 这个点没有标的归属（`PE`、`mkt:turnover:total` 这类）。
    """
    indicator = str((point or {}).get("indicator", ""))
    if ":" not in indicator:
        return ""
    tail = indicator.rsplit(":", 1)[-1].strip()
    matched = _CODE_IN_SEGMENT_RE.match(tail)
    return matched.group(1) if matched else ""


def _payload_codes(data_points: list[dict[str, Any]]) -> list[str]:
    """整份数据点里出现过的标的代码（**排序**，与点的先后顺序无关）。

    排序而不是首次出现顺序：同一份数据换个返回顺序不该换一份结论
    （本项目实测过"两次运行给出不同子集"那类不可复现问题）。
    """
    found: set[str] = set()
    for point in data_points:
        code = _point_code(point)
        if code:
            found.add(code)
    return sorted(found)


def _has_unlabeled_family_point(data_points: list[dict[str, Any]]) -> bool:
    """有没有"带规则族关键词、却没有代码后缀"的点（多标的模式下才会问）。"""
    return any(
        not _point_code(p) and any(kw in str(p.get("indicator", "")) for kw in _RULE_FAMILIES)
        for p in data_points
    )


def _tag_for(code: str) -> str:
    """多标的模式下的旗标归属前缀（`[601088] `）。

    单标的模式**不加**前缀 —— 加了就破坏"单标的输出逐字不变"这条硬约束。
    """
    return f"[{code or UNLABELED_TARGET}] "


def _find_value(
    data_points: list[dict[str, Any]], keyword: str, code: str | None = None
) -> float | None:
    """取该关键词的**首个**命中值（`None` = 没量到）。

    `code is None` ⇒ 在**整份**点列表里取首个命中（单标的/无后缀的既有口径，
    逐字不变）；
    `code = "601088"` ⇒ **只在该代码自己的点里**取首个命中。

    ⚠️ 多标的时**必须**传 `code`（`evaluate_compliance` 按代码分组来做这件事）：
    不传的话 `货币资金` 可能落在 A 公司、`有息负债` 落在 B 公司，
    两条拼出一条**两家合起来才有**的「存贷双高」——
    这正是 2026-10-02 修掉的那个缺陷。
    """
    for p in data_points:
        if code is not None and _point_code(p) != code:
            continue
        if keyword in str(p.get("indicator", "")) and isinstance(p.get("value"), (int, float)):
            return float(p["value"])
    return None


def _ratio_flags(
    data_points: list[dict[str, Any]], code: str | None = None, tag: str = ""
) -> tuple[list[str], list[str]]:
    """返回 (全部旗标, 严重旗标)。同一指标按最高命中阈值只产一条。"""
    flags: list[str] = []
    severe: list[str] = []
    for keyword, bands in _RATIO_RULES:
        value = _find_value(data_points, keyword, code)
        if value is None:
            continue
        for threshold, template, is_severe in bands:  # 高阈值在前
            if value > threshold:
                desc = tag + template.format(value=value)
                flags.append(desc)
                if is_severe:
                    severe.append(desc)
                break
    return flags, severe


def _double_high_flag(
    data_points: list[dict[str, Any]], code: str | None = None, tag: str = ""
) -> str | None:
    # ⚠️ 两个字符串字面量**必须原地保留**（`_DOUBLE_HIGH_FAMILIES` 是它的一份
    #    派生清单，由 `test_rule_families_cover_every_consumed_keyword` 对齐）：
    #    `test_contract_consistency.py::_consumed_rule_families()` 用 AST 扫它们。
    #    第三个参数 `code` 是 2026-10-02 加的**同一只票**约束：两个数必须同源。
    cash = _find_value(data_points, "货币资金", code)
    debt = _find_value(data_points, "有息负债", code)
    if (cash is not None and debt is not None
            and cash > _DOUBLE_HIGH_CASH and debt > _DOUBLE_HIGH_DEBT):
        return (f"{tag}存贷双高：货币资金/总资产{cash:g}%且有息负债/总资产{debt:g}%"
                "同时畸高，账面资金真实性存疑")
    return None


def _family_inputs(
    data_points: list[dict[str, Any]], code: str | None = None
) -> dict[str, float | None]:
    """逐族记录**该标的**拿到的值：`None` = 该族没有输入（≠ 值为 0）。

    这是「没量到」与「量到 0」的分界线，也是本模块最重要的一次判断：
    质押比例 0% 是**有效读数**（真的没质押），
    而"上下文里没有质押这条指标"是**什么都不知道**。
    """
    return {kw: _find_value(data_points, kw, code) for kw in _RULE_FAMILIES}


def _event_flags(
    events: list[dict[str, Any]], tag: str = ""
) -> tuple[list[str], list[str]]:
    """信息层A06事件 → 合规监管信号。"""
    flags: list[str] = []
    severe: list[str] = []
    for e in events:
        if e.get("event_type") != "litigation" or e.get("direction") != "negative":
            continue
        quote = str(e.get("evidence_quote") or e.get("subject") or "未具名事件")
        desc = f"{tag}涉诉/监管事件：{quote[:50]}"
        flags.append(desc)
        if any(k in quote for k in _SEVERE_EVENT_KEYWORDS):
            severe.append(desc)
    return flags, severe


def _evaluate_scope(
    data_points: list[dict[str, Any]],
    events: list[dict[str, Any]],
    *,
    code: str = "",
    scan_code: str | None = None,
    scope_tag: str = "",
) -> dict[str, Any]:
    """在**一只标的**的范围内跑完整套合规规则（比率族 → 存贷双高 → 事件）。

    这是等级的**唯一实现**：单标的与多标的都走它，区别只在
    `scan_code`（`None` = 不按代码过滤，即单标的既有口径）。
    两份实现必然漂移，所以刻意不做成两支（见模块 docstring 的"一处刻意的'不重构'"）。
    """
    inputs = _family_inputs(data_points, scan_code)
    measured = sorted(kw for kw, value in inputs.items() if value is not None)
    unmeasured = sorted(kw for kw, value in inputs.items() if value is None)

    flags, severe = _ratio_flags(data_points, scan_code, scope_tag)
    double_high = _double_high_flag(data_points, scan_code, scope_tag)
    if double_high:
        flags.append(double_high)
        severe.append(double_high)
    event_flags, event_severe = _event_flags(events, scope_tag)
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
        # 归属前缀只加在"哪只票没量到"这句话上：多标的时它就是用户唯一能读到的线索。
        placeholder = [scope_tag + FLAG_UNMEASURED]

    return {
        # —— 这一只票的判定 ——
        "code": code,
        "label": code or UNLABELED_TARGET,
        "level": level,
        # `risk_flags` = 真实风险旗标（给 `red_flags` 用）；`flags` 含占位旗标
        "risk_flags": list(flags),
        "flags": flags + placeholder,
        "severe_flag_count": severe_count,
        "measured": has_input,
        "families_measured": measured,
        "families_unmeasured": unmeasured,
        "event_flags": len(event_flags),
    }


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

    ★★ 多标的（`data_points` 里出现 ≥2 个代码）时**逐只判定**，见模块
    docstring 的 2026-10-02 一节。此时：

      · `compliance_per_code` = 每只票一份 `{code, label, level, flags,
        families_measured, families_unmeasured, measured, severe_flag_count}`；
      · 扁平字段是**汇总**，不是任何一只票的读数：
        等级取最坏、族清单取**交集**、`compliance_measured` 要求**每只票**都有输入；
      · 单标的（0/1 个代码）走原样口径 ⇒ 输出与 2026-10-02 之前**逐字一致**。
    """
    points = list(data_points or [])
    event_list = list(events or [])
    codes = _payload_codes(points)
    multi = len(codes) >= 2

    if not multi:
        # ★★ 单标的：整份输入本来就属于这一只票 ⇒ 不按代码过滤，
        #    首个命中即它自己的首个命中（与修复前同一口径，逐字不变）。
        only = codes[0] if codes else ""
        per_code = [_evaluate_scope(points, event_list, code=only)]
    else:
        # ★★ 多标的：每只票只看**自己的**点；事件归属判不出来（A06 契约无代码字段）
        #    ⇒ 一律不摊派，只进汇总层。
        per_code = [
            _evaluate_scope(points, [], code=c, scan_code=c, scope_tag=_tag_for(c))
            for c in codes
        ]
        if _has_unlabeled_family_point(points):
            # 带族关键词却没代码后缀的点：**不许丢**（丢了就是静默少算一族），
            # 也**不许**摊派给某一只票（那就是张冠李戴）⇒ 单列一条。
            # 它会把汇总等级压向「未量到」，方向是保守的（宁可疑，不假绿）。
            per_code.append(_evaluate_scope(points, [], code="", scan_code="",
                                           scope_tag=_tag_for("")))

    # —— 汇总层 ——
    #
    # ⚠️ 汇总**不是**把各只票的族清单并起来：并集会让"A 量到 3 族 + B 量到 3 族"
    #    显示成"已量到 6/6 族"，而没有任何一只票的 6 族齐过 —— 那就是假合格证。
    agg_flags: list[str] = [f for entry in per_code for f in entry["risk_flags"]]
    agg_severe = sum(int(entry["severe_flag_count"]) for entry in per_code)

    # 事件的归属：单标的时全落在那一只票上（既有口径）；
    # 多标的时判不出归属 ⇒ 只在这里计入（不丢，也不摊派）。
    if multi:
        un_events_flags, un_events_severe = _event_flags(event_list)
    else:
        un_events_flags, un_events_severe = [], []
    agg_flags += un_events_flags
    agg_severe += len(un_events_severe)

    measured_sets = [set(entry["families_measured"]) for entry in per_code]
    common = set.intersection(*measured_sets) if measured_sets else set()
    families_measured = sorted(common)
    families_unmeasured = sorted(set(_RULE_FAMILIES) - common)

    # 汇总「有输入」= **每只票**都量到了，或有判不出归属的事件。
    # 少一只票没量到 ⇒ 汇总不许报「无」：那会替那只票签一张合格证。
    all_measured = all(entry["measured"] for entry in per_code)
    has_input = all_measured or bool(un_events_flags)

    if agg_severe >= 1 or len(agg_flags) >= 3:
        level = "高"
    elif agg_flags:
        level = "中"
    elif not has_input:
        level = LEVEL_UNMEASURED
    else:
        level = "无"

    if agg_flags:
        placeholder: list[str] = []
    elif has_input:
        placeholder = [FLAG_NO_SIGNAL]
    else:
        placeholder = [FLAG_UNMEASURED_MULTI if multi else FLAG_UNMEASURED]

    all_event_flags, _ = _event_flags(event_list)

    return {
        "compliance_flags": agg_flags + placeholder,
        "severe_flag_count": agg_severe,
        "compliance_level_calc": level,
        # —— 溯源：这次判定到底量到了什么（"没量到"与"量到 0"分开显示）——
        "compliance_measured": has_input,
        "compliance_families_measured": families_measured,
        "compliance_families_unmeasured": families_unmeasured,
        "compliance_event_flags": len(all_event_flags),
        # —— ★ 2026-10-02：per-code 维度（多标的的唯一诚实表达方式）——
        "compliance_per_code": per_code,
        "compliance_codes": list(codes),
        "compliance_multi_target": multi,
        "compliance_unattributed_event_flags": len(un_events_flags),
    }
