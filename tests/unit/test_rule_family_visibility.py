"""★ **派生**判据：合规规则的每个**比率族**，消费方必须看得见。

## 为什么这个文件存在（这一层缺口的第 N 次复发）

同一个失败模式在本仓库已经复发过多次，只是每次换一层：

| 层 | 症状 | 已修的判据 |
|---|---|---|
| 取数 | 数据源取不到 | `SmartFetcher` |
| 落库/索引 | 库里有、目录里没有 | `audit_data_index.py` |
| **分析层可见性** | 库里有、**Agent 看不见** | `test_whitelist_coverage.py` |
| **规则输入** | 指标放行了、**规则不消费它** | 本文件 |

2026-09-29 实测的那一次：`compliance/logic.py` 的 `_RATIO_RULES`（商誉档）与
`_double_high_flag`（存贷双高，需要「货币资金」**与**「有息负债」两个输入同时到手）
**直接消费**这些族，而 `_AGENT_DATA_WHITELIST["A12_compliance"]` 里没有对应的
子串关键词 → `_filter_points_for_agent` 把它们全部挡掉 →
**商誉档永不触发、存贷双高恒不触发**，而 `compliance_level_calc` 照样给出等级。

## 为什么必须是**派生**判据（本文件与 `test_whitelist_coverage.py` 的分工）

`test_whitelist_coverage.py` 的方向是**生产者侧**：拿 `indicators.yaml` 里
**已登记**的 id 去问"有没有 Agent 认领"。它的盲区是
**"还没被登记的族"** —— 登记表没长出来，它就看不见。

本文件的方向是**消费者侧**：拿 `logic._RULE_FAMILIES`（**规则的输入契约**）
去问"消费方 A12 的**子串语义**放不放行它"。族清单**现读常量、不手写** ——
手写清单不会自己长大，这正是"下一个 PMI/GDP 式缺口就漏一次"的根因。

两侧合起来才闭环：生产者侧保证"登记了的东西有人看"，
消费者侧保证"**规则要的东西有人给**"。

## 这里**刻意没有**豁免表

任何"这一族先挂着"的登记表都会腐烂成永久豁免（AGENTS.md《护栏必须带过期语义》）。
本判据没有豁免入口：新加一族规则，就必须同步让消费方看得见 ——
没有第二种合法选择。（对照：`supervisor._FALLBACK_TRIGGER_EXEMPTIONS`
那种表之所以允许存在，是因为它只能让系统**更保守**；这里没有"更保守"的等价物：
看不见输入 = 规则静默不触发 = **伪造合格证**。）
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.domain.agents.analysis.compliance import logic  # noqa: E402
from src.orchestration import supervisor  # noqa: E402

#: 规则的**消费方**。写成常量是为了让"换消费方"这件事必须显式发生。
CONSUMER = "A12_compliance"

#: 对照组：一个**必然**不该被任何白名单放行的哨兵。
#: 有它才能让判据写成"**精确命中**"而不是"有没有出现" —— 本项目实测过
#: `_filter_points_for_agent` 的 0 命中兜底（返回前 N 条）会让
#: "看起来保留了"变成假绿，所以这里用 `fallback_limit=0` 关掉兜底。
_SENTINEL = "__NO_SUCH_RULE_FAMILY__"


def _visible_to(agent_id: str, family: str) -> bool:
    """该 Agent 的白名单放不放行这个族 —— **走生产的唯一实现**。

    不另写一份子串匹配：判据永远与 `_filter_points_for_agent` 的语义一致
    （AGENTS.md：「同一判断只允许一份实现」）。改动匹配规则时这条自动跟随。

    `fallback_limit=0` 是**必须**的：那个函数的"0 命中 → 取前 N 条"兜底
    会让 `family` **碰巧**混在返回值里 → 判据变成"有没有出现"（假绿）。
    同时放一个哨兵点，使返回值只可能是 `[family]` / `[哨兵]` / `[]`，
    于是"命中"这件事可以被**精确**断言。
    """
    kept = supervisor._filter_points_for_agent(  # noqa: SLF001 判据的唯一实现
        agent_id,
        [{"indicator": family}, {"indicator": _SENTINEL}],
        fallback_limit=0,
    )
    names = [str(p.get("indicator", "")) for p in kept]
    return names == [family]


def test_ratio_families_are_derived_not_hand_written():
    """族清单必须**从规则本身派生**（不是第二份手抄的清单）。

    这是上一条判据能"自己长大"的前提：如果 `_RATIO_FAMILIES` 是手写的，
    新加一档 `_RATIO_RULES` 而忘了抄进来，本文件就**看不见**新族 ——
    判据本身成了缺口。
    """
    assert logic._RULE_FAMILIES, "族清单为空 —— 判据的前提不成立"  # noqa: SLF001
    assert tuple(kw for kw, _bands in logic._RATIO_RULES) == logic._RATIO_FAMILIES, (
        "`_RATIO_FAMILIES` 与 `_RATIO_RULES` 的键不同源 —— 它被手抄了")
    assert set(logic._DOUBLE_HIGH_FAMILIES) <= set(logic._RULE_FAMILIES), (
        "存贷双高的两个族没有进族清单")


def test_every_rule_family_is_visible_to_its_consuming_agent():
    """★ `_RULE_FAMILIES` 里的**每一族**都必须被消费方 A12 放行。

    判据是**派生**的：族清单从 `logic._RULE_FAMILIES` 现读，
    所以 `compliance/logic.py` 里新增一档规则 → 这条立刻变红
    （而不是等某个 PMI/GDP 式的缺口在现场爆出来）。

    为什么必须点名消费方（而不是"某个 Agent 放行就行"）：
    数据点只落到**消费它的那一个** Agent 手里才有用 ——
    "A11 看得见商誉"不能替代"A12 看得见商誉"。
    """
    for family in logic._RULE_FAMILIES:                # noqa: SLF001
        passers = [aid for aid in supervisor._AGENT_DATA_WHITELIST
                   if _visible_to(aid, family)]
        assert passers, (
            f"规则族『{family}』没有任何 Agent 的白名单放行它 —— "
            f"本地规则会永远拿不到输入（而等级照样给结论）")
        assert CONSUMER in passers, (
            f"规则族『{family}』被本地规则消费，但消费方 {CONSUMER} 的"
            f"白名单**不放行**它（放行的是 {passers}）→ 该规则永不触发。"
            f"修法：在 `_AGENT_DATA_WHITELIST[{CONSUMER!r}]` 里补一个"
            f"**子串关键词**（匹配是子串，不认中文标签以外的写法）")


def test_the_sentinel_is_never_visible():
    """自证：哨兵对**任何** Agent 都不可见（否则上一条判据是假绿）。

    AGENTS.md《自己的检查脚本必须先自证》：喂一个"已知答案"的输入，
    确认它报 0。这里喂的是"一个必然不存在的族名"。
    """
    visible = [aid for aid in supervisor._AGENT_DATA_WHITELIST
               if _visible_to(aid, _SENTINEL)]
    assert visible == [], f"哨兵竟然被这些 Agent 放行了：{visible}"


def test_the_matcher_used_here_is_the_production_one():
    """判据用的必须是**生产那一个**匹配器（不是测试自己写的子串判断）。

    机器复现：拿一个**已知在白名单里**的关键词去问，必须可见；
    拿一个**已知不在**的，必须不可见。两条对照同时成立，
    才说明 `_visible_to()` 真的落在生产的判据上。
    """
    keywords = supervisor._AGENT_DATA_WHITELIST[CONSUMER]
    assert keywords, f"{CONSUMER} 的白名单为空 —— 判据的前提不成立"
    known = next((k for k in keywords if k.isascii()), keywords[0])
    assert _visible_to(CONSUMER, known), (
        f"已知关键词 {known!r} 竟然不可见 —— 判据没有落在生产匹配器上")
    assert not _visible_to(CONSUMER, _SENTINEL)
