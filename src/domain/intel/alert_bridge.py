"""情报 → 事件告警的桥：把**该弹窗**的情报变成可弹窗的告警。

> 用户口径（2026-09-25）：
>
>   "为什么不把情报流、舆情热度 有明显利空或利多的信息，都通过事件告警弹出。
>    当前舆情热度监控没做好，不如直接融入到情报流里……"
>   "明确方向 + 信度达标 + 仅 high 级；跨源同文合并成一条；
>    非交易时段只进列表不弹。"

> 用户改判据（2026-09-25，**只针对知识星球**）：
>
>   "知识星球是不是弹窗和置信度无关，取决于抓取的信息内容"
>   "出现 孙潇雅、赵宇阳、武超则、陈果、刘晨明、洪灏，这些知名券商分析师的名字，
>    就告警弹窗，要求这些名字不能被本地大模型过滤掉。"
>   "另外是 8b 模型分析出是利空或利好时，就弹窗。"
>   "知识星球不看置信度，看内容里有没有'人'和'机构'，还有这条消息被本地模型
>    分析出是有利空或利多偏向的，都要告警。"

## 为什么是"桥"而不是"合并两张表"

告警与情报流的**生命周期不同**，硬合会毁掉告警最值钱的那部分：

    告警    事件驱动 + 去重冷却（event_key 同源 24h、content_key 跨源抑制）
            + 每人已读 + WebSocket 推送 + 邮件
    情报流  广谱 + 信度分层 + 倾向抽取

实测：pilot 库里 541 条事件只产出 27 条告警 —— 差的 95% 全是去重挡下来的。
如果把情报流直接灌进弹窗通道，那个比例会变成"每条都弹"。

所以这里只做一件事：**把已判定的方向翻译成告警引擎认识的字段**，
去重、冷却、等级、已读、推送全部交给既有告警系统。

## 闸门（**按来源分两套**）

| 闸门 | 其它来源 | 知识星球（`research_note`） |
|---|---|---|
| 内容触发 | ——（不看内容） | **分析师名 / 模型方向，任一命中即弹** |
| 明确方向 | `tone.has_tone` 且 ∈ {偏多, 偏空} | 不是必需（分析师命中即可） |
| 信度达标 | `credibility.score ≥ MIN_CREDIBILITY` | **不看**（用户明说"和置信度无关"） |
| 仅 high 级 | 分数映射到 `risk/opportunity_score`，靠引擎档位判定 | 同上（见 `FORCED_SCORE`） |
| 跨源同文合并 | `content_key` = 内容指纹（不含来源） | **同样保留** |
| 非交易时段不弹 | `service.in_trading_window()` | **同样保留** |

⚠️ **机构名（「XX证券」）不是触发条件，只是展示信息**（用户 2026-10-01 改口径：
"券商名不一定要告警，但是一定要前端输出信息"）。机构名由
`alert_rules.institutions()` 挂到**情报条目**上出接口（前端显示"机构：中泰证券"），
不进这里的放行判据 —— 一条只提到券商的笔记不弹窗。

## ⚠️ 知识星球这一路**放宽了**用户最初选的"仅 high 级"

必须写在这里而不是埋在代码里：用户最早的口径是"明确方向 + 信度达标 + **仅 high 级**"。
现在对知识星球取消信度闸门之后，这个来源的条目在信度维度上**不可能**够到
high 档（实测 15 条真实笔记全是 58 分，还有一条 18 分），所以：

    "仅 high" 对知识星球实际上变成了一句空话 —— 要么强制抬到 high 档，
    要么这个来源的告警一条都出不来。

用户已就此**明确确认**（2026-09-25）："知识星球不看置信度，看内容里有没有
'人'和'机构'……都要告警" —— 即这个来源不再要求引擎的 high 档资格。
实现上取"抬到 high 档"（`FORCED_SCORE`），与用户要的"就告警"一致：
告警照弹，且**理由里如实写明触发物**，用户随时能核对为什么弹。
其它来源一个字都没变（仍走 `MIN_CREDIBILITY` 与引擎档位判定）。

## 为什么倾向**复用**情报流那套，而不是让告警重新判

`tone.py` 的结论是"规则层与本地模型交叉验证"的产物。让告警的分析器
再用云端模型判一次，会出现同一件事两套方向（情报流说"偏多"、告警说"风险"）
—— 用户看到互相矛盾的结论，比"没有告警"伤害大得多。

而且**这条链路上一次模型调用都不发生**：抽取在 `tone_job` 里，
结果落 `tone_store`，这里只是读已算好的值 + 一遍正则（微秒级）。
"""

from __future__ import annotations

import hashlib
import logging
from typing import Any, Final

from src.domain.alerts.models import (
    AffectedStock,
    Event,
    EventAssessment,
    EventEntities,
)
from src.domain.intel import alert_rules

logger = logging.getLogger(__name__)

#: 触发告警所需的最低可信度（**其它来源**）。
#:
#: 74 = "权威财经媒体"档（`configs/intel_source_levels.yaml`）。
#: 为什么卡在这里：54（财经自媒体）/38（论坛股吧）的倾向判断本身
#: 就不可靠，拿它去弹窗等于把噪音包装成信号。
#: 84（持牌机构研报）/94（官方披露）自然满足。
#:
#: ⚠️ 知识星球（`research_note`）**不过这道闸门**，见 `BYPASS_SOURCES`。
MIN_CREDIBILITY: Final = 74

#: 信度闸门**不适用**的来源类型（用户口径："知识星球不看置信度"）。
#:
#: 为什么按 `kind` 而不是来源名：出接口的 `source_alias` 已经被
#: `source_pseudonym()` 换成了稳定假名（`src-xxxxxxxx`），拿它比对来源名
#: 永远匹配不上（实测踩过这一类：分级表按中文属性词查，传英文标识就全落保守档）。
#: `kind` 是**我们自己**打的类型标签、是公开契约的一部分，且 `research_note`
#: 目前只有知识星球一个产出方（`service.KIND_LABELS` / `_kind_of` 可复核）。
BYPASS_SOURCES: Final[frozenset[str]] = frozenset({"research_note"})

#: `source_alias` 侧的同义判据（仅作 `kind` 缺失时的兜底）。
#:
#: 内部消费方给的是 `to_public()` 之前的形状时 alias 还是明文；一旦有人
#: 直接拿接口 dict 来调（那种情况下 `kind` 一定在），这里的兜底不生效也无妨。
#: ⚠️ 假名化后的 alias（`src-…`）**不在**这张表里 —— 它无法反查，
#: 也不该反查（那正是脱敏的目的）。
SOURCE_ALIAS_HINTS: Final[frozenset[str]] = frozenset({
    "research-note-zsxq", "zsxq", "research_note",
})

#: 知识星球告警的**强制分数**。
#:
#: ## 为什么必须强制（而不是沿用 58 分）
#:
#: 引擎有两道与本值相关的闸门（`core/config.py`）：
#:
#:     alert_confidence_min = 0.70     信度/100 = 0.58 时**直接不出告警**
#:     alert_opp_high       = 80       机会分 < 80 就不是 high 档
#:
#: 实测知识星球 58 分 ⇒ 置信度 0.58 < 0.70 ⇒ **引擎层就被拦掉了**。
#: 也就是说，桥里放行只是第一步，不抬分的话表现还是"什么都不弹"——
#: 而这一层没有任何日志（`evaluate` 静默返回 None），最难查。
#:
#: 取 80 而不是 70（刚过置信度门槛）：用户最初要的是"仅 high 级"，
#: 现在已确认这个来源不再要求 high 档资格；但**弹窗的语义**是"值得你现在看一眼"，
#: 抬到 high 档与之一致，也不会让这个来源的告警看起来比它的实际分量更弱。
#: 取 80 的两个后果都核对过：
#:
#:     偏多 → opportunity 80 ≥ alert_opp_high 80 → 机会/ high ✓
#:     偏空 → risk        80 ≥ alert_risk_high 75 → 风险/ high ✓
FORCED_SCORE: Final = 80

#: 明确的倾向标签（只有这两个会触发告警）
BULL: Final = "偏多"
BEAR: Final = "偏空"

#: 置信度下限（沿用告警系统的 `alert_confidence_min` 语义）。
#:
#: 为什么用 `credibility/100` 当置信度：一个 94 分的官方披露
#: → 0.94，一个 54 分的自媒体 → 0.54。**这不是凑数**，而是
#: "我们有多确定这条方向判断"的合理代理 —— 来源越权威、内容事实要素
#: 越全，方向判断就越可信。这样"信度达标"这条闸门由既有引擎统一裁决，
#: 不需要在桥里另写一套阈值（两套阈值必然漂移）。
CONFIDENCE_SCALE: Final = 100.0

#: 告警有效期（天）。与引擎既有 7 天一致。
EXPIRE_DAYS: Final = 7

#: 没有个股时如实标注的措辞。
#:
#: 用户口径："个股名比较可靠就按个股名，本身就是个股优先，目的就是找到那些股
#: 被唱多，唱空。" —— 一条说"利多"却**没有标的**的告警，与一条点名
#: `天岳先进 688234` 的告警，价值差一个量级。把它们显示成同一个样子，
#: 用户会以为"系统找到了票"，而实际上他只是拿到了一条方向判断。
#: 所以**必须写出来**，且写在 `summary` 里（它会成为告警的 `description`，
#: 也就是弹窗正文 —— 用户第一眼就看到的那句话）。
NO_STOCK_FLAG: Final = "未点名个股，仅方向判断，信号强度偏弱"


def _has_direction(tone: Any) -> bool:
    """有没有**明确的原文倾向** —— **判据的唯一实现**。

    ⚠️ 兼容两种形状：`item["tone"]` 是 dict（情报流），
    `alert_rules.direction_word()` 传进来的是 dict。
    旧行可能没有 `has_tone` 键 —— 那时按 `tone` 的字面值判，
    否则老数据会被判成"没有方向"而静默不弹（向后兼容要求）。

    ## 三条判据的位置不能调

    1. `neutral=True` → **一律不算方向**，哪怕 `tone` 写着"偏多"
       （中性是"我们没说它偏多偏空"的显式结论，它压过字面值）；
    2. 有 `has_tone` 键 → 以它为准（规则层与模型层一致的结论）；
    3. 没有该键 → 按字面值兜底（第二轮之前的老存储行）。

    ## ★ 2026-09-26：本函数成为**全局唯一**判据

    在那之前 `service._has_direction` 自己实现了一份"只看 `has_tone`"的
    版本 —— 与这里**不一致**：它漏了 `neutral` 压制与老行兜底。
    而前端那个【多】/【空】标记、以及这次新加的多/空筛选器，走的是
    **各自的**一份。三份并存时差异不会报错，只会表现为
    "筛选说这条偏多、列表上却没有【多】标记"这类自相矛盾。

    现在 `service._has_direction` 与 `service.direction_of` 都**委托到这里**，
    所以：**要改判据就改这一处**，不要再复制一份。
    """
    t = tone if isinstance(tone, dict) else {}
    if t.get("neutral"):
        return False
    if "has_tone" in t:
        return bool(t.get("has_tone"))
    return str(t.get("tone") or "") in (BULL, BEAR)


def direction_of(item: dict[str, Any]) -> str:
    """`偏多` / `偏空` / 空串（无明确方向）。"""
    t = item.get("tone") or {}
    if not _has_direction(t):
        return ""
    tone = str(t.get("tone") or "")
    return tone if tone in (BULL, BEAR) else ""


def is_bypass_source(item: dict[str, Any]) -> bool:
    """这个来源是否**不走信度闸门**（知识星球）。

    两道判据都要（`kind` 优先）：内部消费方传的是 `to_public()` 之后的形状
    （`kind` 一定在），而直接构造的测试/脚本 dict 可能两种都给。
    """
    kind = str(item.get("kind") or "").strip()
    if kind in BYPASS_SOURCES:
        return True
    if kind:
        # `kind` 明确给了、且不是被放行的那些 → 就是其它来源，
        # **不要**再拿 alias 去猜（猜错的表现是"快讯也被放行"，
        # 那等于把所有来源的信度闸门一起取消了）。
        return False
    return str(item.get("source_alias") or "").strip() in SOURCE_ALIAS_HINTS


def _score_of(item: dict[str, Any]) -> int:
    raw = item.get("credibility") or {}
    try:
        return int(raw.get("score"))
    except (TypeError, ValueError):
        return 0


def reason_of(item: dict[str, Any]) -> alert_rules.AlertReason:
    """一条情报**为什么**该（或不该）弹窗 —— 两条触发条件的落地情况。

    两条是 **OR**（用户 2026-09-25 强调"并列，不是必须同时满足"）：
    分析师名 / 模型方向，任何一条成立即弹。判据本身在 `alert_rules`，
    这里只负责把"条目"喂给它并附加个股（个股优先，见 `stock_entries`）。

    ⚠️ 机构名会出现在返回值的 `hits` 里（供展示），但**不参与 `fires`** ——
    用户 2026-10-01 改口径："券商名不一定要告警"。
    """
    try:
        return alert_rules.AlertReason(
            hits=tuple(alert_rules.content_hits(item)),
            direction=alert_rules.direction_word(item.get("tone")),
            stocks=tuple(stock_entries(item)),
        )
    except Exception:  # noqa: BLE001 规则层失败退化成"没有理由"，绝不抛给调度
        return alert_rules.AlertReason()


def is_alertable(item: dict[str, Any],
                 *, min_credibility: int = MIN_CREDIBILITY) -> bool:
    """这条情报该不该触发告警（正向闸门；理由见模块 docstring 的表）。

    ## 两套判据，按来源分

        知识星球        命中分析师 **或** 模型给出方向 → 弹，**不看信度**
        其它来源        方向明确 **且** 信度 ≥ 门槛 → 弹

    ⚠️ 知识星球这一支**不能**顺手加回信度判断：实测该来源 15 条真实笔记
    全是 58 分，加回去就是"这个来源永远不会弹"—— 那正是这次要修的缺陷本身。

    ⚠️ 也**不能**把机构命中写成放行条件（见 `reason_of` 的说明）。
    """
    if is_bypass_source(item):
        return reason_of(item).fires
    if not direction_of(item):
        return False
    return _score_of(item) >= min_credibility


# ======================================================================
# 标的（**个股优先**）
# ======================================================================

def _bucket_stocks(item: dict[str, Any]) -> list[dict[str, str]]:
    """从抽取结果的**方向桶**里取个股（名字 + **词表给的**代码）。

    ## 为什么用方向桶而不是顶层 `codes`

        顶层 `codes`    模型给/规则扫的代码，**不知道属于哪一侧**
        方向桶 `stocks` `tone.validate_entities` 已逐条校验过：
                        名字逐字在原文里、代码**只从词表或原文明确配对取**
                        （模型编的代码一律不采用）

    用户要找的是"这些股被唱多、唱空"，方向信息只存在于方向桶里 ——
    所以个股优先的落点就是这里。

    ⚠️ **两种布局都读**（item 级优先，tone 里兜底）：
    情报流把桶挂在 item 上（`service.build_feed` 的 `it["tone"]["bullish"]`…
    实际是顶层），而 `tone_store` 的存储行是**扁平**的。读取侧要多认一种形状，
    否则表现是"抽取明明抽到了个股，告警里却没有标的"—— 静默、且很难查。

    ## ⚠️ 代码解析**不做**兜底

    桶里的名字查不到词表时**留空**，绝不退回顶层 `codes` 去配一个代码 ——
    那会把"这条里出现过的某个代码"安到这只票头上，而用户核对不出来
    （他看到的是一只有名有码的标的）。空代码在界面上就是"只有名字"，
    比一个可能错的代码诚实。
    """
    from src.domain.intel import vocab

    def _bucket(key: str) -> Any:
        side = item.get(key)
        if not isinstance(side, dict):
            side = (item.get("tone") or {})
            side = side.get(key) if isinstance(side, dict) else None
        return side if isinstance(side, dict) else {}

    out: list[dict[str, str]] = []
    for key in ("bullish", "bearish"):
        side = _bucket(key)
        for raw in (side.get("stocks") or []):
            if not isinstance(raw, dict):
                continue
            name = str(raw.get("name") or "").strip()
            code = str(raw.get("code") or "").strip()
            if not name and not code:
                continue
            if name and not code:
                try:
                    code = vocab.stock_code(name)
                except Exception:  # noqa: BLE001 词表不可用不该让整条作废
                    code = ""
            out.append({"name": name, "code": code})
    return out


def stock_entries(item: dict[str, Any]) -> list[AffectedStock]:
    """受影响标的：**个股优先** —— 方向桶里的个股在前，无方向的代码殿后。

    | 来源 | 名字 | 代码 | 说明 |
    |---|---|---|---|
    | 方向桶 `stocks` | 模型抽、原文逐字校验过 | **只从词表/原文配对取** | 最可靠 |
    | 顶层 `codes` | 空（不编名字） | 原文里真出现的 | 兜底，不知道方向 |

    接口与界面拿到的 `affected_stocks` 就是它（`Alert.affected_stocks`
    原样出 `alert_to_public`），所以"个股优先"在这里落一次就够了。
    """
    out: list[AffectedStock] = []
    seen: set[str] = set()
    for stk in _bucket_stocks(item):
        key = stk["code"] or stk["name"]
        if not key or key in seen:
            continue
        seen.add(key)
        out.append(AffectedStock(code=stk["code"], name=stk["name"], impact=""))
    for code in (item.get("codes") or [])[:3]:
        c = str(code or "").strip()
        if not c or c in seen:
            continue
        seen.add(c)
        out.append(AffectedStock(code=c, name="", impact=""))
    return out


def _bucket_industries(item: dict[str, Any]) -> list[str]:
    """从抽取结果的**方向桶**里取行业名（去重、保序）。

    ⚠️ 行业**不当标题**但它不该丢：用户口径是"个股优先"，不是"不要行业"。
    一条点名了 `天岳先进` 又标了 `第三代半导体` 的告警，比只有个股的更好读
    —— 只是行业不能成为**判定依据**（实测 8B 的行业分类在两次运行之间会漂，
    见 `alert_rules` 模块 docstring 记的那条实测）。
    """
    out: list[str] = []
    for key in ("bullish", "bearish"):
        side = item.get(key)
        if not isinstance(side, dict):
            side = (item.get("tone") or {})
            side = side.get(key) if isinstance(side, dict) else None
        if not isinstance(side, dict):
            continue
        for raw in (side.get("industries") or []):
            s = str(raw or "").strip()
            if s and s not in out:
                out.append(s)
    return out


def _event_key(item: dict[str, Any]) -> str:
    """来源级去重键。用 `content_hash` —— 它就是"这条内容"的指纹。"""
    h = str(item.get("content_hash") or "")
    if h:
        return h
    raw = f"{item.get('title')}|{item.get('published_at')}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def _content_key(item: dict[str, Any]) -> str:
    """跨源内容键 —— **不含来源**，这样同一件事被 5 家转发只弹一次。

    这是用户说的"跨源同文合并成一条"的落点：告警引擎的
    `last_content_alert_time` 会按它做冷却抑制。
    """
    title = "".join(str(item.get("title") or "").split())
    stamp = str(item.get("published_at") or "")[:10]
    return hashlib.sha1(f"{title}|{stamp}".encode()).hexdigest()[:16]


def _event_type_of(item: dict[str, Any]) -> str:
    """情报条目 → 告警系统的 `EventType`（个股优先）。

    ⚠️ 枚举里**没有** "news"（只有 policy/sector/stock/calendar），
    直接传 "news" 会被 pydantic 拒掉（第一版就是这么写的）。
    按"有标的 → 个股、有行业 → 板块、都没有 → 政策"降级映射：
    这三档决定了告警列表上显示的标签，也是用户筛选告警的依据。

    ⚠️ 判据里**必须**算上方向桶：用户口径是"本身就是个股优先"，
    而一条只抽到个股、顶层 `codes` 为空的笔记若被判成 `sector`，
    列表标签就与内容相反（用户按"个股"筛会漏掉它）。
    """
    if stock_entries(item):
        return "stock"
    if str(item.get("industry") or "").strip():
        return "sector"
    return "policy"


def build_event(item: dict[str, Any]) -> Event:
    """情报条目 → 告警系统的事件对象。"""
    from src.domain.alerts.normalize import now_iso

    stocks = stock_entries(item)
    return Event(
        event_id=f"intel_{_event_key(item)}",
        event_key=f"intel:{_event_key(item)}",
        content_key=_content_key(item),
        event_type=_event_type_of(item),
        title=str(item.get("title") or "")[:200],
        content=str(item.get("summary") or ""),
        # ⚠️ 这里是**内部**源名（`newswire-cls` / `research-note-zsxq`）——
        # 出接口时由 `routes/alerts.py` 的 `alert_to_public()` 过
        # `source_pseudonym()`。桥不做脱敏，避免出现第二处脱敏口径。
        source_name=str(item.get("source_alias") or ""),
        source_url="",
        publish_time=str(item.get("published_at") or ""),
        # `fetch_time` 是**我们什么时候取到的**（数据溯源规范要求），
        # 与 `publish_time`（对方什么时候发的）不是一回事。pydantic 必填。
        fetch_time=now_iso(),
        # 个股优先：有标的就把**名字**一起给出去（`entities.companies`
        # 原本只有代码，界面上只能显示一串数字）。名字来自方向桶，
        # 已过"逐字在原文里"的校验，不是编的。
        entities=EventEntities(
            industries=[str(item.get("industry"))] if item.get("industry") else [],
            companies=([s.name or s.code for s in stocks] or []),
        ),
        raw_data={},          # 白名单构造：raw_data 是泄漏高发区，一律空
    )


def _summary_of(item: dict[str, Any], reason: alert_rules.AlertReason) -> str:
    """告警正文（`description`）：**为什么弹 + 原始摘要**。

    ## 为什么理由要放在最前面

    弹窗上用户第一眼看到的就是这句话。他要能立刻回答两件事：
    "为什么弹我？"（命中谁/模型判了什么）与"这条有没有票？"。
    把理由放在 260 字摘要之后，等于让它默认看不见。

    ⚠️ 理由里的名字**逐字来自原文**（`alert_rules.ContentHit`），
    不是我们归纳的措辞 —— 与 `phrases` 同一条纪律：用户拿去原文里
    一定要能核对到。命中落在展示摘要之外时，`describe()` 会明说
    "正文后段，展示摘要未显示"，不让他白找。
    """
    parts: list[str] = []
    why = reason.describe()
    if why:
        parts.append(why)
    if not reason.stocks:
        # 无标的必须**明说**（见 `NO_STOCK_FLAG`）：一条"利多"却不点名个股的
        # 告警，与一条点名 `天岳先进 688234` 的告警不是一个量级的东西。
        parts.append(NO_STOCK_FLAG)
    head = "；".join(parts)
    body = str(item.get("summary") or "")
    return f"{head}。{body}" if head else body


def build_assessment(
        item: dict[str, Any],
        reason: alert_rules.AlertReason | None = None) -> EventAssessment | None:
    """情报条目 → 评估结论。没有明确方向**且没有内容命中**时返回 None。

    `reason` 由调用方（`build_pairs` / `select`）算好后传入 ——
    这样"为什么弹"只算一次，且**与放行判据用的是同一份结果**。
    不传时按"方向-only"处理（既有调用点与既有测试的形状不变）。
    """
    if reason is None:
        reason = alert_rules.AlertReason(
            direction=alert_rules.direction_word(item.get("tone")),
            stocks=tuple(stock_entries(item)),
        )
    if not reason.fires:
        return None
    direction = direction_of(item)
    bypass = is_bypass_source(item)
    score = float(_score_of(item))
    if bypass and score < FORCED_SCORE:
        # 见 `FORCED_SCORE` 的说明：不抬到 80 的话，引擎的两道闸门
        # （置信度 0.70 / 机会档 80）会**静默**把这条告警吃掉。
        # 抬分只作用于**出告警的字段**，不改动情报流里显示的可信度。
        score = float(FORCED_SCORE)
    positive = direction != BEAR     # 命中分析师（无方向）时按机会侧出，见下
    return EventAssessment(
        event_id=f"intel_{_event_key(item)}",
        event_type=_event_type_of(item),
        # 告警系统的方向词汇是 positive/negative（不是偏多/偏空）——
        # 这里是**唯一的翻译点**，别处不要再转一次。
        #
        # ⚠️ 没有模型方向、只有分析师命中时判 `positive`：告警引擎的方向只有
        # 两档（风险/机会），没有第三档；而**告警必须出得来**（用户要的是
        # "出现这些名字就弹"）。选机会侧而不是风险侧是取保守：把一条
        # 方向未知的消息报成"利空"会误导用户卖出，报成"机会"至少与
        # 用户"找被唱多的股"的用法一致。所以理由文案必须同时带上
        # 命中的人（`impact_path` / `summary`），让用户一眼看出这条是
        # "点名了某位分析师"而不是"模型说它利多"。
        sentiment="positive" if positive else "negative",
        risk_score=0.0 if positive else score,
        opportunity_score=score if positive else 0.0,
        confidence=min(1.0, score / CONFIDENCE_SCALE),
        affected_stocks=stock_entries(item),
        # 行业：方向桶优先，其次条目自带的 `industry`（采集层给的）。
        # ⚠️ 它**不参与**放行判定（用户口径"个股优先"，且实测 8B 的行业分类
        # 在两次运行之间会漂）—— 只是让告警的可读性与情报流一致。
        affected_industries=_bucket_industries(item) or (
            [str(item.get("industry"))] if item.get("industry") else []),
        # `impact_path` 装**触发理由**：接口原样出它（`alert_to_public` 只抹 URL），
        # 所以用户能顺着弹窗看到"到底命中了谁"。这也是排障时唯一能回答
        # "这条为什么弹"的字段 —— 不写它就只能靠翻原文猜。
        impact_path=reason.describe(),
        summary=_summary_of(item, reason),
        model_used="intel_tone",
    )


def select(items: list[dict[str, Any]], *,
           min_credibility: int = MIN_CREDIBILITY,
           limit: int = 60) -> list[dict[str, Any]]:
    """从一批情报里挑出**该弹窗**的那些（最新优先）。

    ⚠️ 返回的是**条目本身**（不是 `(条目, 理由)` 对）—— 既有调用点
    （`scripts/_check_signal_alert.py`、`_intel_signal_alert`）按条目消费它。
    理由由 `build_pairs` 现算：它只花一遍正则，比到处传一个并行结构更不容易错位。
    """
    from src.infrastructure.connectors.intel_sources import sort_key

    hits = [x for x in items if is_alertable(x, min_credibility=min_credibility)]
    hits.sort(key=lambda x: sort_key(x.get("published_at")), reverse=True)
    return hits[:limit]


def build_pairs(items: list[dict[str, Any]]) -> list[tuple[Event, EventAssessment]]:
    """`(事件, 评估)` 列表，供 `AlertScanService.ingest_assessed()` 直接消费。"""
    pairs: list[tuple[Event, EventAssessment]] = []
    for it in items:
        a = build_assessment(it, reason_of(it))
        if a is None:
            continue
        pairs.append((build_event(it), a))
    return pairs


__all__ = [
    "BEAR",
    "BULL",
    "BYPASS_SOURCES",
    "CONFIDENCE_SCALE",
    "FORCED_SCORE",
    "MIN_CREDIBILITY",
    "NO_STOCK_FLAG",
    "SOURCE_ALIAS_HINTS",
    "build_assessment",
    "build_event",
    "build_pairs",
    "direction_of",
    "is_alertable",
    "is_bypass_source",
    "reason_of",
    "select",
    "stock_entries",
]
