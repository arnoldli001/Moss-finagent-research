"""**唯一的**实体词表：概念板块 / A 股个股 / 外部关键标的 / AI 关键词。

用户口径（2026-09-25）：

    "知识星球内容如果不涉及任何美股、AI、概念板块、股票的可以折叠。
     如果涉及股票、概念板块、美股、AI的…并且不折叠。"

要回答"这条涉不涉及"，就得有一份**能逐字对上的名单**；要把命中的词标绿，
还得知道**它出现在哪儿**。所以这份名单集中在本模块一处，四个来源合成**一张表**：

| kind | 来源 | 实测条数 | 说明 |
|---|---|---|---|
| `concept_board` | `ml_board`（`kind='concept'`） | **122** | 主线挖掘跟踪的板块，带板块代码 |
| `a_stock` | `quant_stock_directory` | **5562** | A 股名录，**代码只从这里取** |
| `overseas` | 用户手工清单 | **14** | 外部关键标的与关键词（含"加息"） |
| `ai` | 用户手工清单 | **6** | AI 关键词 |

⚠️ `concept_board` **曾是 138，2026-09-27 起为 122**：主线概念池冻结后
（`configs/mainline_frozen_pool.yaml`，同样 122 条），`ml_board` 里
`kind='concept'` 的目录与之**逐字相等**。这个数字是**文档表格**，
真正的哨兵在 `tests/unit/test_intel_vocab.py::test_real_table_matches_the_documented_sources`
（它对着真实库断言）—— 两处必须一起改，否则就是"文档说 138、库里有 122"的漂移。

⚠️ 顺序有意义：`concept_board` / `a_stock` 是**数据源**，`overseas` / `ai` 是
**手工维护的清单**（用户原话："不可能完全识别完的，漏掉就漏掉吧"），
后者**故意不完整**，不做模糊匹配、不做"猜个近似词"的兜底。

## 为什么是一张表 + 一次扫描，而不是每个功能各扫一遍

"哪些词要标绿"与"这条涉不涉及个股/板块"必须是**同一个判断**。分两处实现，
用户会看到"整条没被折叠、却一个绿字都没有"（或者反过来）—— 那种不一致
没有任何报错，只能靠人肉比对发现。所以：一张表、一个 `scan()`，
实体解析（抽取侧）与高亮（展示侧）都从它出。

## 匹配：滑窗 + 一张 dict，不用正则交替

    for i in range(len(text)):
        for L in range(min(max_len, len(text) - i), 1, -1):
            hit = index.get(probe[i:i+L])   # 命中就取，这就是"最长优先"

对比 `re.compile("|".join(5700 个词))`：

  · **成本可预测**：O(文本长度 × max_len) 次 dict 查找（1000 字 × 8 ≈ 8000 次），
    而 5700 分支的交替在不同输入上耗时差异极大，且编译一次就要付出代价
  · **没有回溯**：不存在灾难性回溯这类"某条正文让接口卡住"的风险
  · **每个命中都带位置**：高亮要的就是位置；正则只给匹配串，
    还得再 `find` 一次去定位（多一次扫描，且两处结果可能不一致）

两个正确性细节（都有具体反例）：

  1. **拉丁词忽略大小写**：`NVIDIA`/`nvidia`、`Tesla`/`tesla`、`GPT`/`gpt`
     在研报里混着写。查表键统一做**ASCII 小写**（`str.maketrans`）——
     刻意不用 `casefold()`：`"ß".casefold()` 会变成两个字符、**长度变了**，
     切片位置就会跟原文错位（高亮会标错地方）。
     条目本身的拼写保持原样，用于显示。
  2. **同一位置最长优先**：`AI应用`（概念板块）与 `AI`（AI 关键词）在同一个
     起点上必须只出一个 —— 长的那个。**不同起点**的重叠命中都保留
     （`存储芯片` 与它后面偏移 2 处的 `芯片概念` 互不影响）；高亮侧不受影响，
     因为前端按长度降序替换，长词先被吃掉、短词就不会被单独标出来。

## 匹配的是**清洗后**的文本

喂进来的必须是读者看到的那份文本：内部走
`intel_sources.strip_rich_tags`（剥 `<e …>` 富文本标签）与
`content_filter.strip_noise` 之后的。**在原始文本上匹配的后果**是
URL 编码的标签残片（`%E7%89%87%E4%BF%A1%E6%81%AF`）也会被当成正文扫一遍 ——
标出来的绿字用户在自己的界面上根本找不到（那正是"核不到的证据"）。

## 读不到表时**降级但不装作没事**

数据源坏了（库缺失 / 表被改）时：手工清单照旧可用，缺的那一类**记进
`stats()["gaps"]` 并打日志** —— 不静默变成"什么都没命中"（那会让整页
不再有高亮、所有条目都变成可折叠，而没有任何线索）。
"""

from __future__ import annotations

import logging
import re
import string
from dataclasses import dataclass, field
from typing import Any, Final

logger = logging.getLogger(__name__)

# ── kind 取值（**字符串常量而非枚举**：它要进日志与 stats，也要被测试直接比对）
KIND_BOARD: Final = "concept_board"
KIND_STOCK: Final = "a_stock"
KIND_OVERSEAS: Final = "overseas"
KIND_AI: Final = "ai"
#: 全部 kind（顺序 = 建表时的优先级：同一个词出现在多类里时，靠前的赢）
KINDS: Final[tuple[str, ...]] = (KIND_BOARD, KIND_STOCK, KIND_OVERSEAS, KIND_AI)

#: 词的最小长度。单字词（"中"、"大"）在任何中文文本里都能撞上，
#: 没有核对价值，也不该被标绿（与 `tone.MIN_PHRASE_CHARS` 同一条理由）。
MIN_TERM_CHARS: Final = 2

#: 一条情报最多带几个高亮词。一份 300 字的研报可能命中十几个词，
#: 全标绿等于没有重点（界面会变成一片荧光）。
MAX_HIGHLIGHTS: Final = 12

#: 单条文本最多给出几个概念板块 / 个股（与 `tone.MAX_INDUSTRIES`/`MAX_STOCKS` 同量级）
DEFAULT_BOARD_LIMIT: Final = 6
DEFAULT_STOCK_LIMIT: Final = 8

#: 6 位数字（A 股代码的形态；"是不是真在名录里"由词表本身保证）
_CODE_ONLY: Final = re.compile(r"^\d{6}$")

#: ASCII 小写映射。⚠️ 用它而不是 `str.casefold()`：这个映射**不改变长度**
#: （A-Z → a-z 一一对应），所以 `probe[i:i+L]` 的位置与原文严格对齐。
#: `casefold()` 会把 `ß` 变成 `ss`、把 `İ` 变成两个字符 —— 长度一变，
#: 高亮就会标到错的位置上（而且只在极少数输入上复现）。
_ASCII_LOWER: Final = str.maketrans(string.ascii_uppercase, string.ascii_lowercase)


# ======================================================================
# 两张手工维护的清单（用户给的，**逐字**，故意不完整）
# ======================================================================

#: 外部关键标的与关键词（用户清单，逐字）。
#:
#: ⚠️ 名字刻意**不叫** `US_STOCKS`：清单里 `加息` 是**宏观词**（利率），
#: `海力士` 是**韩国** SK 海力士 —— 叫美股会让后来的人照着名字往里加"美股名单"，
#: 而它其实混着宏观词与韩股。要拆分（`overseas_names` + `macro_keywords`）
#: 等用户定，现在**逐字照抄，不擅自分类**。
OVERSEAS_TERMS: Final[tuple[str, ...]] = (
    "英伟达", "NVIDIA", "美光", "海力士", "加息", "特斯拉", "Tesla",
    "苹果", "微软", "谷歌", "Meta", "亚马逊", "AMD", "台积电",
)

#: AI 关键词（用户清单，逐字）。只看词形，不判断语义（那是模型的事）。
AI_TERMS: Final[tuple[str, ...]] = (
    "AI", "人工智能", "大模型", "LLM", "GPT", "算力",
)

_HAND_NOTE: Final[dict[str, str]] = {
    KIND_OVERSEAS: "用户手工清单（外部关键标的与关键词），覆盖故意不完整",
    KIND_AI: "用户手工清单（AI 关键词），覆盖故意不完整",
}


# ======================================================================
# 表结构
# ======================================================================

@dataclass(frozen=True)
class VocabEntry:
    """词表里的一项。`term` 是**要逐字匹配的字符串**（原文里长什么样就写什么样）。"""

    term: str
    #: `concept_board` | `a_stock` | `overseas` | `ai`
    kind: str
    #: A 股代码。**只有 `kind="a_stock"` 有**，且只从名录取 —— 模型给的一律不用
    code: str = ""
    #: 主线挖掘的板块代码（`885xxx.TI`）。**只有 `kind="concept_board"` 有**。
    #:
    #: ⚠️ 这是对"`code` 只给 a_stock"的一点扩展：板块代码是另一个命名空间
    #: （`886042.TI` 不是股票代码），混进同一个字段会让"这个 code 是什么"
    #: 取决于 kind —— 那正是本项目最忌讳的"字段含义看上下文"。分开两个字段，
    #: 各自的名字就说清了它是什么。情报流的 `boards[].code` 用的就是它。
    board_code: str = ""
    #: 备注（手工清单写清"为什么在这里"；数据源来的留空）
    note: str = ""


@dataclass(frozen=True)
class VocabHit:
    """一次命中：**带位置**（高亮要用它，实体计数也要用它）。

    ⚠️ `term` 是**原文里的那一段切片**（`text[start:end]`），不是表里的拼写。
    两者只可能在拉丁词的大小写上不同（原文写 `nvidia`、表里存 `NVIDIA`）——
    高亮必须逐字能在界面上找到，所以给读者看的是**原文那一份**；
    规范拼写与代码在 `entry` 里（实体输出用得到）。
    """

    term: str
    entry: VocabEntry
    #: 在文本里的下标区间 `[start, end)`
    start: int
    end: int

    @property
    def kind(self) -> str:
        return self.entry.kind

    @property
    def code(self) -> str:
        return self.entry.code

    @property
    def board_code(self) -> str:
        return self.entry.board_code


@dataclass(frozen=True)
class VocabTable:
    """建好索引的整张表（进程内缓存这一份）。"""

    entries: tuple[VocabEntry, ...]
    #: 查表键（ASCII 小写）→ 条目
    index: dict[str, VocabEntry]
    #: 最长词的长度（滑窗上界）
    max_len: int
    #: 每次读表失败的原因（**要让调用方看得到**，不静默降级）
    gaps: tuple[str, ...] = ()
    by_kind: dict[str, tuple[VocabEntry, ...]] = field(default_factory=dict)
    #: A 股代码 → 名称（`stock_name` 用）
    code_name: dict[str, str] = field(default_factory=dict)


def build_table(entries: tuple[VocabEntry, ...] | list[VocabEntry],
                gaps: tuple[str, ...] = ()) -> VocabTable:
    """把条目编成索引（**测试也用它造受控词表**）。

    同一个词出现在多类里时按 `KINDS` 的顺序取先出现的那类，并在日志里报数量 ——
    静默覆盖会让"这个词为什么没被算成板块"永远查不出来。
    """
    index: dict[str, VocabEntry] = {}
    by_kind: dict[str, list[VocabEntry]] = {k: [] for k in KINDS}
    code_name: dict[str, str] = {}
    collisions = 0
    for entry in entries:
        term = str(entry.term or "").strip()
        if len(term) < MIN_TERM_CHARS or entry.kind not in by_kind:
            continue
        key = term.translate(_ASCII_LOWER)
        if key in index:
            if index[key] is not entry:
                collisions += 1
            continue
        index[key] = entry
        by_kind[entry.kind].append(entry)
        if entry.kind == KIND_STOCK and entry.code and entry.code not in code_name:
            code_name[entry.code] = term
    if collisions:
        logger.info("实体词表：%d 个词跨类重复（按 %s 的顺序取先出现的）",
                    collisions, "→".join(KINDS))
    return VocabTable(
        entries=tuple(index.values()),
        index=index,
        max_len=max((len(e.term) for e in index.values()), default=0),
        gaps=tuple(gaps),
        by_kind={k: tuple(v) for k, v in by_kind.items()},
        code_name=code_name,
    )


# ======================================================================
# 读表（进程内缓存一次）
# ======================================================================

_TABLE: VocabTable | None = None


def reset_cache() -> None:
    """清缓存（测试 / 换库用）。"""
    global _TABLE
    _TABLE = None


def _load_board_entries(gaps: list[str]) -> list[VocabEntry]:
    """概念板块：`ml_board` 里 `kind='concept'`（**主线挖掘跟踪的那份目录**）。

    ⚠️ 走数据仓自己的 `_read` 而**不是**公开的 `boards()`：

      · `boards()` 会按 `configs/sector_blacklist.yaml` 过滤 —— 那是"打分池"的
        口径，不是"哪些板块存在"。用它会让词表变成打分池的子集，
        表现为"某些板块永远认不出来"，且没有任何报错；
      · 目录为空时 `boards()` 会**自动向远端同步**（在接口路径里发网络请求）。

    本模块要的是"目录里有什么"。
    """
    try:
        from src.mainline.config import load_config
        from src.mainline.datastore import MainlineDataStore

        store = MainlineDataStore(config=load_config())
        rows = store._read(
            "SELECT name, code FROM ml_board WHERE kind = 'concept'")
    except Exception as exc:  # noqa: BLE001 读不到词表不该让页面/抽取失败
        gap = (f"concept_board 词表不可用（{type(exc).__name__}）："
               "概念板块不会参与识别与高亮")
        logger.warning("实体词表：%s", gap)
        gaps.append(gap)
        return []
    out: list[VocabEntry] = []
    for row in rows:
        name = str(row["name"] or "").strip()
        code = str(row["code"] or "").strip()
        if len(name) >= MIN_TERM_CHARS:
            out.append(VocabEntry(term=name, kind=KIND_BOARD, board_code=code))
    if not out:
        gap = "concept_board 词表为空（主线数据仓里 ml_board 没有概念板块）"
        logger.warning("实体词表：%s", gap)
        gaps.append(gap)
    return out


def _load_stock_entries(gaps: list[str]) -> list[VocabEntry]:
    """A 股个股：`quant_stock_directory` 的**股票**名录（代码从这里取）。

    走行情仓唯一入口 `warehouse.open_warehouse`（`WarehouseSource.query` 是它的
    薄封装），不自己开 sqlite。

    过滤条件写死三个交易所，而不只写 `instrument_type='股票'`：
    实测 `instrument_type='股票'` 的 5562 行恰好就是 SZSE 2901 + SSE 2318 +
    BSE 343，写成显式白名单是为了让"这是 A 股名录"在代码里看得见
    （表里另有 KRX 的指数行 `000300 DHAutoNex`）。

    ⚠️ **不用 `ml_member.name`**：那份成分股池以 `700xxx.TI` 这类港股/境外板块
    为主，实测 `003774 亮晴控股`、`000728 中国电信`（= 00728.HK）—— 港股代码
    补零成 6 位后连 `_CODE_RE` 都拦不住，会把港股的名字安到 A 股代码上。
    """
    from src.mainline.sources import WarehouseSource

    src = WarehouseSource()
    if not src.available():
        gap = f"a_stock 词表不可用（行情仓不存在：{src.gap}）：个股不会参与识别与高亮"
        logger.warning("实体词表：%s", gap)
        gaps.append(gap)
        return []
    try:
        rows = src.query(
            "SELECT code, name FROM quant_stock_directory"
            " WHERE instrument_type = '股票'"
            "   AND exchange IN ('SZSE', 'SSE', 'BSE')"
            "   AND name <> ''")
    except Exception as exc:  # noqa: BLE001 同上
        gap = (f"a_stock 词表读不到（{type(exc).__name__}）："
               "个股不会参与识别与高亮")
        logger.warning("实体词表：%s", gap)
        gaps.append(gap)
        return []
    out: list[VocabEntry] = []
    for row in rows:
        code = str(row["code"] or "").strip()
        name = str(row["name"] or "").strip()
        if _CODE_ONLY.match(code) and len(name) >= MIN_TERM_CHARS:
            out.append(VocabEntry(term=name, kind=KIND_STOCK, code=code))
    if not out:
        gap = "a_stock 词表为空（行情仓里没有 A 股名录）"
        logger.warning("实体词表：%s", gap)
        gaps.append(gap)
    return out


def _load_entries() -> tuple[list[VocabEntry], list[str]]:
    """四个来源合成一张表。读不到的那类**记 gap**，其余照常可用。

    ⚠️ 每个数据源外面再包一层 `try`（它们内部已经各自兜过一次）：
    这里失败的是"整类词表消失"，而症状是"整页不再有高亮、所有条目都变成
    可折叠"—— 静默下去没有任何线索，所以宁可多兜一层，也要把 gap 记下来。
    """
    gaps: list[str] = []
    entries: list[VocabEntry] = []
    for kind, loader in ((KIND_BOARD, _load_board_entries),
                         (KIND_STOCK, _load_stock_entries)):
        try:
            entries += loader(gaps)
        except Exception as exc:  # noqa: BLE001 读不到一类不该让整表失败
            gap = f"{kind} 词表读取异常（{type(exc).__name__}）"
            logger.warning("实体词表：%s", gap)
            gaps.append(gap)
    entries += [VocabEntry(term=t, kind=KIND_OVERSEAS,
                           note=_HAND_NOTE[KIND_OVERSEAS])
                for t in OVERSEAS_TERMS]
    entries += [VocabEntry(term=t, kind=KIND_AI, note=_HAND_NOTE[KIND_AI])
                for t in AI_TERMS]
    return entries, gaps


def table() -> VocabTable:
    """整张表（**进程内缓存**，首次调用才读库）。"""
    global _TABLE
    if _TABLE is None:
        entries, gaps = _load_entries()
        _TABLE = build_table(entries, tuple(gaps))
        logger.info("实体词表载入 %d 条（max_len=%d）：%s%s",
                    len(_TABLE.entries), _TABLE.max_len,
                    {k: len(v) for k, v in _TABLE.by_kind.items()},
                    f"，缺口 {len(gaps)} 处" if gaps else "")
    return _TABLE


def entries_by_kind(kind: str) -> tuple[VocabEntry, ...]:
    """某一类的全部条目（顺序 = 建表顺序）。"""
    return table().by_kind.get(kind, ())


# ======================================================================
# 扫描（实体解析与高亮**共用**的唯一匹配入口）
# ======================================================================

def scan(text: str) -> list[VocabHit]:
    """扫出文本里所有命中的词（**带位置**，同一位置最长优先）。

    ⚠️ 入参必须是**清洗后**的文本（剥掉 `<e …>` 富文本标签、去掉展示噪音），
    也就是读者真正看到的那份 —— 在原始文本上扫会把 URL 编码的标签残片
    也当成正文，标出来的绿字用户在自己界面上找不到。
    """
    t = table()
    s = text or ""
    if not s or t.max_len <= 0:
        return []
    # 长度不变的 ASCII 小写副本：位置与原文严格对齐（见 `_ASCII_LOWER`）
    probe = s.translate(_ASCII_LOWER)
    hits: list[VocabHit] = []
    n = len(s)
    for i in range(n):
        # ★ 从最长往下试：同一个起点上先命中的就是最长的那个
        for length in range(min(t.max_len, n - i), MIN_TERM_CHARS - 1, -1):
            entry = t.index.get(probe[i:i + length])
            if entry is not None:
                # `term` 用原文切片（大小写可能与表里不同）——
                # 高亮要逐字能在界面上找到，见 `VocabHit` 的说明
                hits.append(VocabHit(term=s[i:i + length], entry=entry,
                                     start=i, end=i + length))
                break
    return hits


def _hits_of_kind(text: str, kind: str) -> dict[str, dict[str, Any]]:
    """某一类的命中，按词去重（保留首次出现顺序与出现次数）。

    去重键用**原文切片**：`nvidia` 与 `NVIDIA` 在原文里是两段不同的字，
    标绿时要各标各的（前端是逐字查找）。
    """
    out: dict[str, dict[str, Any]] = {}
    for hit in scan(text):
        if hit.kind != kind:
            continue
        got = out.get(hit.term)
        if got is None:
            out[hit.term] = {
                "term": hit.term,
                "code": hit.code,
                "board_code": hit.board_code,
                "count": 1,
                "start": hit.start,
            }
        else:
            got["count"] += 1
    return out


def _top_of_kind(text: str, kind: str, limit: int) -> list[dict[str, Any]]:
    """某一类的命中，按"出现次数降序、首次出现位置升序"取前 N 个。"""
    found = list(_hits_of_kind(text, kind).values())
    found.sort(key=lambda h: (-h["count"], h["start"]))
    return found[:max(1, limit)]


def boards_in_text(text: str, *, limit: int = DEFAULT_BOARD_LIMIT
                   ) -> list[dict[str, Any]]:
    """文本里命中的概念板块：`[{name, code, count}]`（`code` = 主线板块代码）。

    ⚠️ 名字一定是**原文里真有的那串字**（表里存的就是它）：
    界面上要能拿它去原文核对，核不到的等于给了用户一条假证据。
    """
    return [{"name": h["term"], "code": h["board_code"], "count": h["count"]}
            for h in _top_of_kind(text, KIND_BOARD, limit)]


def stocks_in_text(text: str, *, limit: int = DEFAULT_STOCK_LIMIT
                   ) -> list[dict[str, Any]]:
    """文本里命中的个股：`[{name, code, count}]`，**代码来自词表**（不是模型）。

    代码不来自原文也可以：名字逐字在原文里、代码来自 A 股名录，两者都可核对，
    缺的只是"这两个是一对"这个配对关系（`stock_names.resolve` 同口径）。
    """
    return [{"name": h["term"], "code": h["code"], "count": h["count"]}
            for h in _top_of_kind(text, KIND_STOCK, limit)]


def highlights(title: str, summary: str = "", *,
               limit: int = MAX_HIGHLIGHTS) -> list[str]:
    """标题/摘要里**要标绿**的词（四类都要）。

    返回**按长度降序**去重的子串；每个词都逐字出现在 `title` 或 `summary` 里
    （前端直接拿去替换，不做同义映射）。

    长度降序不只是好看：前端按顺序匹配时，`存储芯片` 必须先于 `芯片` 被吃掉，
    否则绿字会被切成两截。
    """
    text = f"{title or ''} {summary or ''}".strip()
    if not text:
        return []
    first: dict[str, int] = {}
    for hit in scan(text):
        first.setdefault(hit.term, hit.start)
    return sorted(first, key=lambda w: (-len(w), first[w]))[:max(1, limit)]


# ======================================================================
# 按词/按码取（抽取侧的闸门用）
# ======================================================================

def board_code(term: str) -> str:
    """板块名 → 主线板块代码。**不是本表里的板块 → 空串（不认，也不猜）。**"""
    entry = table().index.get(str(term or "").strip().translate(_ASCII_LOWER))
    return entry.board_code if entry is not None and (
        entry.kind == KIND_BOARD) else ""


def stock_code(term: str) -> str:
    """个股名 → A 股代码（**只从词表来**，模型给的那个永远不采用）。"""
    entry = table().index.get(str(term or "").strip().translate(_ASCII_LOWER))
    return entry.code if entry is not None and entry.kind == KIND_STOCK else ""


def stock_name(code: str) -> str:
    """A 股代码 → 名称。查不到返回空串（**不编名字**，同 `stock_names` 口径）。"""
    return table().code_name.get(str(code or "").strip(), "")


def boards_available() -> bool:
    """概念板块那一类是否可用（读不到时调用方**退回旧的逐字判据**）。

    ⚠️ 与 `stats()["gaps"]` 配套：缺了要在日志/统计里看得见。
    调用方（`tone._resolve_industry`）在词表为空时退回"逐字即可"，
    而不是"一律丢弃" —— 后者会让界面上的行业**整块消失**且没有报错线索。
    """
    return bool(entries_by_kind(KIND_BOARD))


def stats() -> dict[str, Any]:
    """词表规模与缺口（排障用：某一类突然变 0 说明数据源坏了）。"""
    t = table()
    return {
        "total": len(t.entries),
        "max_len": t.max_len,
        **{f"kind_{k}": len(t.by_kind.get(k, ())) for k in KINDS},
        "gaps": list(t.gaps),
    }


__all__ = [
    "AI_TERMS",
    "DEFAULT_BOARD_LIMIT",
    "DEFAULT_STOCK_LIMIT",
    "KINDS",
    "KIND_AI",
    "KIND_BOARD",
    "KIND_OVERSEAS",
    "KIND_STOCK",
    "MAX_HIGHLIGHTS",
    "MIN_TERM_CHARS",
    "OVERSEAS_TERMS",
    "VocabEntry",
    "VocabHit",
    "VocabTable",
    "board_code",
    "boards_available",
    "boards_in_text",
    "build_table",
    "entries_by_kind",
    "highlights",
    "reset_cache",
    "scan",
    "stats",
    "stock_code",
    "stock_name",
    "stocks_in_text",
    "table",
]
