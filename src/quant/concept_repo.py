"""个股 ↔ 概念板块库（Tushare 同花顺指数口径）：反查某只票属于哪些概念，并按相关性排序。

## 为什么需要它

做T面板的「关联板块」原来是**手打**的（`PCB概念,PET铜箔`）—— 用户得先自己知道
这只票属于什么概念。而板块情绪、板块涨幅排行这两个维度都靠它绑定，
不填就等于白丢两维分。

## 数据源（实测 2026-09-17）

| 用途 | 接口 | 实测结果 |
|---|---|---|
| 反查某票的概念 | `ths_member(con_code="688825.SH")` | ✅ **一次返回 50 个概念** |
| 概念名 / 成员数 | `ths_index(exchange="A", type=…)` | ✅ 合并 8 个 type 得 **1471** 条名录 |
| 板块净额 / 涨幅 | 复用 `FundFlowProvider.sector_snapshot_sync()` | ✅ 1030 个板块 |

关键点：`ths_member` 支持 **`con_code` 反查**（而不是只能"按概念查成分股"），
所以不需要"遍历 1471 个概念各拉一次成分"这种上千次调用的做法。

## 相关性怎么算（可解释，不拍脑袋）

同一只票会挂几十个概念，其中很多是**无信息量的宽泛概念**
（实测 `700001.TI 同花顺全A` 5524 个成员、`885338.TI 融资融券` 3844 个）——
把它们当"关联板块"等于没关联。因此：

    relevance = 0.65 * 1/sqrt(成员数)      # 越"窄"的概念越能刻画这只票
              + 0.20 * 归一化(板块净额)     # 当日资金关注度（机构视角）
              + 0.15 * 归一化(板块涨幅)     # 当日强弱

并**硬性剔除**两类噪声：成员数 > `MAX_MEMBERS`（默认 1500）的宽泛概念，
以及名字含"指数 / 成份股 / 样本股 / 全A"这类市场级条目。

## 缓存

名录（`ths_index`，1471 条）缓存 24 小时；反查结果（按代码）缓存 24 小时 ——
概念归属是**低频变化**数据，没必要每次进页面都打 Tushare。反查结果同时落 SQLite，
Tushare 不可用时仍能返回上次的结果（并标注 `stale=True`，不假装是最新的）。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)

logger = logging.getLogger(__name__)


def _safe_int(value: Any, default: int = 0) -> int:
    """任意值 → int（NaN/Inf/None/非数字都给 default，不抛错）。

    pandas 的 `count` 列常带 NaN；`int(nan)` 抛 ValueError、`int(inf)` 抛
    OverflowError —— 而概念库是**增强功能**，不该因为一个空字段让接口 500。
    """
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if number != number or number in (float("inf"), float("-inf")):   # NaN / ±Inf
        return default
    return int(number)


def _age_seconds(stamp: str) -> float:
    """`updated_at` 字符串 → 距今多少秒（解析失败按"很旧"处理）。"""
    try:
        moment = datetime.fromisoformat(str(stamp))
    except (TypeError, ValueError):
        return float("inf")
    return max(0.0, (datetime.now() - moment).total_seconds())


def _is_usable_concept(name: str, members: int) -> bool:
    """这个概念能不能当"关联板块"用（三道闸门，全部可解释）。

    1. 名录里没名字 → 跳过（不猜）；
    2. 成员数越界 → 跳过：> `MAX_MEMBERS` 是市场级大盘（"同花顺全A" 5524 只），
       < `MIN_MEMBERS` 是临时标签（"上市首五日" 3 只）；
    3. 名字命中指数/量化标签词 → 跳过（"百元股""昨日资金前十""制造业指数"）。

    为什么必须有第 3 条：实测 `上市首五日`、`百元股`、`高市净率` 的"窄度"很高，
    只按成员数排序会把它们顶到第一位 —— 那是**量化标签**，不是这只票的主题概念。
    """
    if not name:
        return False
    if not (MIN_MEMBERS <= members <= MAX_MEMBERS):
        return False
    if any(hint in name for hint in _BROAD_NAME_HINTS):
        return False
    if any(hint in name for hint in _NOISE_NAME_HINTS) and \
            not any(keep in name for keep in _NOISE_EXCEPTIONS):
        return False
    return True

#: 超过这个成员数的概念视为"市场级宽泛概念"，不作为个股的关联板块
MAX_MEMBERS = 1500
#: 低于这个成员数的概念是**临时标签**（"上市首五日"只有 3 只票），不是主题板块
MIN_MEMBERS = 8
#: 名录与反查的缓存时长（秒）：概念归属低频变化，24 小时足够
INDEX_TTL = 24 * 3600
MEMBERSHIP_TTL = 24 * 3600

#: 名字里带这些词的属于市场级/指数级条目，不是"这只票的概念"
_BROAD_NAME_HINTS = ("指数", "成份股", "成分股", "样本股", "全A", "全Ａ", "全收益")

#: 名字里带这些词的属于**量化标签 / 临时事件**，不是主题概念。
#: 实测噪声来源：`上市首五日`(3 只)、`百元股`(213 只)、`昨日资金前十`(10 只)、
#: `高市净率`、`创历史新高`、`龙虎榜指数` —— 它们排进"关联板块"毫无意义。
_NOISE_NAME_HINTS = (
    "首五日", "百元股", "低价股", "高价股", "高市净率", "低市净率",
    "高市盈率", "低市盈率", "高ROE", "高贝塔", "贝塔值", "昨日", "今日",
    "近期", "新高", "新低", "涨停", "跌停", "连板", "解禁", "并购", "重组",
    "调研", "龙虎榜", "资金前十", "成交前十", "涨幅前十", "振幅", "换手",
    "融资融券", "沪股通", "深股通", "标普", "MSCI", "富时", "证金", "汇金",
    "重仓", "持股", "基金", "社保", "QFII", "机构", "大盘", "中盘", "小盘",
    "股", "行业", "制造业", "指数",
    # 业绩/财务类横切概念：实测"2026中报预增"(507 只) 会因为**净额极大**
    # 冲到第一位。它描述的是"业绩好不好"，不是"这票属于哪个题材板块"。
    "预增", "预减", "预盈", "预亏", "业绩", "中报", "年报", "季报",
    "高送转", "分红", "摘帽", "ST", "扭亏", "增长", "超预期",
)
#: 这些"含股字/含行业字"的例外要保留（真实主题概念里带这些字）
_NOISE_EXCEPTIONS = ("船舶", "军工", "航空", "股票", "次新", "大军工")

#: `ths_index` 的查询变体：实测**不带 exchange 与带 exchange 返回的条目不同**，
#: 必须都查一遍才能覆盖（只用 `exchange="A"` 会漏掉 CPO / 液冷服务器 这类主题）。
#: 刻意**不做 18 种组合**：实测 {A,N}/{A,I}/{A}/{空} 四种即可覆盖 1471 条，
#: 变体越多首建越慢（18 种时首建要 137 秒，前端联想会超时）。
_INDEX_QUERIES: tuple[dict[str, str], ...] = (
    {"exchange": "A", "type": "N"},
    {"exchange": "A", "type": "I"},
    {"exchange": "A"},
    {},
)


@dataclass
class ConceptBoard:
    """一个概念板块 + 它与某只个股的相关性。"""

    code: str
    name: str
    members: int = 0
    relevance: float = 0.0
    net_amount: float | None = None
    change_pct: float | None = None
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        """转成接口返回用的普通字典。"""
        return {
            "code": self.code,
            "name": self.name,
            "members": self.members,
            "relevance": round(self.relevance, 4),
            "net_yi": (None if self.net_amount is None
                       else round(self.net_amount / 1e8, 2)),
            "change_pct": self.change_pct,
            "reasons": list(self.reasons),
        }


class ConceptRepository:
    """个股↔概念库（进程内单例，`concept_repository()` 获取）。

    Args:
        db_path: 落库路径（None 则只用内存缓存）。
        client: 可注入的 Tushare 客户端（测试用；None 时按需创建）。
        fundflow: 可注入的资金流 provider（用于给概念补"当日资金关注度"）。
    """

    def __init__(self, db_path: str | Path | None = None,
                 client: Any = None, fundflow: Any = None) -> None:
        self._db_path = Path(db_path) if db_path else None
        self._client = client
        self._fundflow = fundflow
        self._lock = threading.Lock()
        self._index_cache: tuple[float, dict[str, tuple[str, int]]] | None = None
        self._index_refreshing = False
        self._member_cache: dict[str, tuple[float, list[str]]] = {}
        self._fundflow_failed = False
        self._ensure_schema()

    # ---------------- 基础设施 ----------------

    def _connect(self) -> sqlite3.Connection | None:
        """打开 SQLite（WAL + busy_timeout，与项目其它仓储一致）。"""
        if self._db_path is None:
            return None
        try:
            self._db_path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self._db_path, timeout=15)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA busy_timeout=15000")
            return conn
        except sqlite3.Error as exc:
            logger.info("概念库落库不可用（只用内存缓存）：%s", brief(exc, BRIEF_TIGHT))
            return None

    def _ensure_schema(self) -> None:
        """建表（幂等）。两张表：概念名录、个股→概念的归属。"""
        conn = self._connect()
        if conn is None:
            return
        try:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS dim_concept (
                    ts_code   TEXT PRIMARY KEY,
                    name      TEXT NOT NULL,
                    members   INTEGER DEFAULT 0,
                    updated_at TEXT
                );
                CREATE TABLE IF NOT EXISTS map_stock_concept (
                    code      TEXT NOT NULL,
                    ts_code   TEXT NOT NULL,
                    name      TEXT DEFAULT '',
                    members   INTEGER DEFAULT 0,
                    updated_at TEXT,
                    PRIMARY KEY (code, ts_code)
                );
                CREATE INDEX IF NOT EXISTS idx_stock_concept_code
                    ON map_stock_concept(code);
                """)
            conn.commit()
        except sqlite3.Error as exc:
            logger.warning("概念库建表失败：%s", brief(exc, BRIEF_DEFAULT))
        finally:
            conn.close()

    def _tushare(self) -> Any:
        """Tushare 客户端（不可用时返回 None 并只记一次日志）。"""
        if self._client is None:
            try:
                from src.quant.tushare_source import TushareClient

                self._client = TushareClient()
            except Exception as exc:  # noqa: BLE001 未配 token 是正常状态
                logger.info("概念库：Tushare 客户端不可用（%s）", brief(exc, BRIEF_TIGHT))
                self._client = False
        return self._client or None

    def _fundflow_provider(self) -> Any:
        """资金流 provider（用于补概念的净额/涨幅；失败后不再重试）。"""
        if self._fundflow is None and not self._fundflow_failed:
            try:
                from src.fundflow.provider import FundFlowProvider

                self._fundflow = FundFlowProvider()
            except Exception as exc:  # noqa: BLE001
                logger.info("概念库：资金流 provider 不可用（%s）", brief(exc, BRIEF_TIGHT))
                self._fundflow_failed = True
        return self._fundflow

    # ---------------- 名录 ----------------

    def index(self) -> dict[str, tuple[str, int]]:
        """`{ts_code: (name, members)}`（多查询变体合并；内存缓存 + SQLite 落库）。

        ## 为什么要"先给旧的、再后台刷新"

        `ths_index` 首建需要 4 次 Tushare 调用（实测 137 秒里大部分是网络等待），
        而联想输入框**每次敲字**都会问这个概念库 —— 首建阻塞请求会让前端直接超时
        （实测 `concepts/suggest` 报 `TimeoutError`）。
        所以：SQLite 里有名录就**立刻**返回它，同时起一个后台线程去刷新（下次请求生效）。
        概念归属是低频变化数据，用"半天前的名录"完全够用。
        """
        with self._lock:
            cached = self._index_cache
        if cached is not None:
            age = time.monotonic() - cached[0]
            if age < INDEX_TTL:
                return cached[1]
            if cached[1]:
                self._refresh_index_async()     # 过期但可用：先用旧的
                return cached[1]
        stored, age = self._load_index_with_age()
        if stored:
            with self._lock:
                self._index_cache = (time.monotonic(), stored)
            if age < INDEX_TTL:
                # 落库的名录还新 → **完全跳过在线拉取**（进程重启后首请求也不慢）
                logger.info("概念名录：直接用落库的 %d 条（%.1f 小时前更新）",
                            len(stored), age / 3600)
            else:
                self._refresh_index_async()
                logger.info("概念名录：落库的 %d 条已过期（%.1f 小时），后台刷新中",
                            len(stored), age / 3600)
            return stored
        merged = self._fetch_index()            # 首次（无任何缓存）只能同步等
        with self._lock:
            self._index_cache = (time.monotonic(), merged)
        logger.info("概念名录：首次拉取 %d 条", len(merged))
        return merged

    def _refresh_index_async(self) -> None:
        """后台刷新名录（去重：同一时间只跑一个刷新线程）。"""
        with self._lock:
            if self._index_refreshing:
                return
            self._index_refreshing = True

        def worker() -> None:
            try:
                merged = self._fetch_index()
                if merged:
                    with self._lock:
                        self._index_cache = (time.monotonic(), merged)
            finally:
                with self._lock:
                    self._index_refreshing = False

        threading.Thread(target=worker, name="concept-index-refresh",
                         daemon=True).start()

    def _fetch_index(self) -> dict[str, tuple[str, int]]:
        """真正去打 Tushare 拉名录（4 个查询变体合并）。"""
        merged: dict[str, tuple[str, int]] = {}
        client = self._tushare()
        if client is not None:
            for query in _INDEX_QUERIES:
                try:
                    frame = client.call("ths_index", **query)
                except Exception as exc:  # noqa: BLE001 单个变体失败不影响其余
                    logger.debug("ths_index %s 不可用：%s", query, brief(exc, BRIEF_TIGHT))
                    continue
                if frame is None or len(frame) == 0:
                    continue
                for _, row in frame.iterrows():
                    code = str(row.get("ts_code") or "").strip()
                    name = str(row.get("name") or "").strip()
                    if not (code and name):
                        continue
                    # `count` 可能是 NaN（实测 ths_index 部分行为空），
                    # 直接 int() 会抛 "cannot convert float NaN to integer"
                    members = _safe_int(row.get("count"))
                    # 同一 code 出现在多个变体里：保留**成员数更大**的那条
                    # （空/0 值不该覆盖真实值）
                    current = merged.get(code)
                    if current is None or members > current[1]:
                        merged[code] = (name, members)
        if merged:
            self._save_index(merged)
        else:
            merged = self._load_index()      # 在线失败 → 用上次落库的名录
            if merged:
                logger.info("概念名录：在线不可用，改用落库的 %d 条", len(merged))
        return merged

    def _save_index(self, merged: dict[str, tuple[str, int]]) -> None:
        """把名录写进 SQLite（失败只记日志）。"""
        conn = self._connect()
        if conn is None:
            return
        stamp = datetime.now().isoformat(timespec="seconds")
        try:
            conn.executemany(
                "INSERT INTO dim_concept(ts_code, name, members, updated_at) "
                "VALUES(?,?,?,?) ON CONFLICT(ts_code) DO UPDATE SET "
                "name=excluded.name, members=excluded.members, "
                "updated_at=excluded.updated_at",
                [(code, name, members, stamp)
                 for code, (name, members) in merged.items()])
            conn.commit()
        except sqlite3.Error as exc:
            logger.debug("概念名录落库失败：%s", brief(exc, BRIEF_TIGHT))
        finally:
            conn.close()

    def _load_index(self) -> dict[str, tuple[str, int]]:
        """从 SQLite 读名录（缺失返回空 dict）。"""
        stored, _age = self._load_index_with_age()
        return stored

    def _load_index_with_age(self) -> tuple[dict[str, tuple[str, int]], float]:
        """读名录 + 判断它有多旧（秒）；用于决定"直接用"还是"后台刷新"。

        **进程重启后的首个请求**（用户"强制刷新"往往就是这种场景）必须立刻拿到名录：
        实测冷启动要跑 4 次 Tushare 调用、约 90 秒，前端联想会直接超时。
        因此把落库时间也读出来，够新就完全跳过在线拉取（`age < INDEX_TTL`）。
        """
        conn = self._connect()
        if conn is None:
            return {}, float("inf")
        try:
            rows = conn.execute(
                "SELECT ts_code, name, members, updated_at FROM dim_concept").fetchall()
        except sqlite3.Error:
            return {}, float("inf")
        finally:
            conn.close()
        if not rows:
            return {}, float("inf")
        stored = {str(code): (str(name), int(members or 0))
                  for code, name, members, _stamp in rows}
        newest = max((str(stamp or "") for *_rest, stamp in rows), default="")
        return stored, _age_seconds(newest)

    # ---------------- 反查 ----------------

    def boards_of(self, code: str, *, name: str = "",
                  limit: int = 12) -> tuple[list[ConceptBoard], bool]:
        """某只票的概念板块（按相关性降序）。

        Args:
            code: 6 位代码（会自动补交易所后缀查 Tushare）。
            name: 股票名（仅用于日志）。
            limit: 最多返回几个。

        Returns:
            `(概念列表, stale)`；`stale=True` 表示在线取数失败、用的是落库结果。
        """
        digits = "".join(char for char in str(code) if char.isdigit())[:6]
        if len(digits) != 6:
            return [], False
        with self._lock:
            cached = self._member_cache.get(digits)
        if cached is not None and (time.monotonic() - cached[0]) < MEMBERSHIP_TTL:
            codes, stale = cached[1], False
        else:
            codes, stale = self._fetch_membership(digits)
        boards = self._score(codes, self.index())
        logger.info("概念关联：%s%s → %d 个概念（stale=%s）",
                    digits, f"({name})" if name else "", len(boards), stale)
        return boards[:limit], stale

    def _fetch_membership(self, digits: str) -> tuple[list[str], bool]:
        """拉某票的概念清单；在线失败时回落落库结果。"""
        suffix = "SH" if digits.startswith(("6", "9")) else "SZ"
        client = self._tushare()
        codes: list[str] = []
        if client is not None:
            try:
                frame = client.call("ths_member", con_code=f"{digits}.{suffix}")
                if frame is not None and len(frame):
                    codes = [str(value).strip()
                             for value in frame["ts_code"].tolist()
                             if str(value).strip()]
            except Exception as exc:  # noqa: BLE001 权限/网络问题都按"不可用"处理
                logger.info("ths_member 反查失败(%s)：%s", digits, brief(exc, BRIEF_TIGHT))
        if codes:
            self._save_membership(digits, codes)
            with self._lock:
                self._member_cache[digits] = (time.monotonic(), codes)
            return codes, False
        stored = self._load_membership(digits)
        with self._lock:
            self._member_cache[digits] = (time.monotonic(), stored)
        return stored, bool(stored)      # 有落库数据 → stale；没有 → 空且不算 stale

    def _save_membership(self, digits: str, codes: list[str]) -> None:
        """落库个股→概念归属（先删后插，保证概念被摘掉时不会残留）。

        ⚠️ 这里**只读已缓存/已落库的名录**，绝不调用 `self.index()`：
        `index()` 在冷启动时可能触发 4 次 Tushare 调用（实测约 90 秒），
        而本方法在**每次新查一只票**时都会被调用 —— 早期版本正是这里把
        首查拖到 90 秒（现象是"第一只票 6 秒、第二只票 93 秒"）。
        名单落库只存 code/name/members，名字缺失不影响后续读取（读的时候会再查名录）。
        """
        conn = self._connect()
        if conn is None:
            return
        with self._lock:
            cached = self._index_cache[1] if self._index_cache else {}
        index = cached or self._load_index()
        stamp = datetime.now().isoformat(timespec="seconds")
        try:
            conn.execute("DELETE FROM map_stock_concept WHERE code = ?", (digits,))
            conn.executemany(
                "INSERT OR REPLACE INTO map_stock_concept"
                "(code, ts_code, name, members, updated_at) VALUES(?,?,?,?,?)",
                [(digits, code, index.get(code, ("", 0))[0],
                  index.get(code, ("", 0))[1], stamp) for code in codes])
            conn.commit()
        except sqlite3.Error as exc:
            logger.debug("概念归属落库失败：%s", brief(exc, BRIEF_TIGHT))
        finally:
            conn.close()

    def _load_membership(self, digits: str) -> list[str]:
        """读落库的归属（缺失返回空列表）。"""
        conn = self._connect()
        if conn is None:
            return []
        try:
            rows = conn.execute(
                "SELECT ts_code FROM map_stock_concept WHERE code = ?",
                (digits,)).fetchall()
            return [str(row[0]) for row in rows]
        except sqlite3.Error:
            return []
        finally:
            conn.close()

    # ---------------- 相关性打分 ----------------

    def _snapshot(self) -> dict[str, dict[str, Any]]:
        """板块截面（净额/涨幅），失败返回空 dict。"""
        provider = self._fundflow_provider()
        if provider is None:
            return {}
        try:
            return provider.sector_snapshot_sync() or {}
        except Exception as exc:  # noqa: BLE001 打分为可选增强
            logger.info("板块截面不可用（概念打分退化为仅按宽度）：%s", brief(exc, BRIEF_TIGHT))
            return {}

    def _score(self, codes: list[str],
               index: dict[str, tuple[str, int]]) -> list[ConceptBoard]:
        """给概念列表打分排序（并剔除市场级宽泛概念）。

        打分公式见模块 docstring；净额/涨幅按本次候选集合做 min-max 归一化，
        因此"资金关注度"是**相对这只票的所有概念**而言的（同一天同一只票内部可比）。
        """
        snapshot = self._snapshot()
        by_name = {str(key).strip(): value for key, value in snapshot.items()}
        candidates: list[ConceptBoard] = []
        for code in codes:
            name, members = index.get(code, ("", 0))
            if not _is_usable_concept(name, members):
                continue
            board = ConceptBoard(code=code, name=name, members=members)
            info = by_name.get(name)
            if info:
                board.net_amount = info.get("net")
                board.change_pct = info.get("pct_change")
                if board.net_amount is not None:
                    board.reasons.append(
                        f"当日主力净额 {board.net_amount / 1e8:+.2f} 亿")
                if board.change_pct is not None:
                    board.reasons.append(f"板块涨幅 {board.change_pct:+.2f}%")
            candidates.append(board)
        if not candidates:
            return []

        # 资金项按**人均**口径：板块绝对净额主要由"有多少只票"决定
        # （实测"商业航天"502 只净额 +29 亿、"航海装备"只有 10 只，
        # 用绝对额时大板块天然占优，细分题材永远排不上来）。
        intensities = [b.net_amount / max(b.members, 1)
                       for b in candidates if b.net_amount is not None]
        net_max = max((abs(value) for value in intensities), default=0.0)
        pcts = [abs(b.change_pct) for b in candidates if b.change_pct is not None]
        pct_max = max(pcts) if pcts else 0.0
        for board in candidates:
            # 窄度 = (1/成员数)^0.35。为什么用幂次而不是 `1/members` 或 `1/sqrt`：
            #   - 纯 `1/members` 时所有值都趋近 0（0.002~0.125），资金项（min-max 到
            #     0~1）会**反客为主** → 实测 "商业航天"(502 只) 压过 "航海装备"(10 只)；
            #   - 纯 `1/sqrt` 又把宽度差压平（0.04~0.32），同样翻车；
            #   - 0.35 次幂后细分题材明显领先、大题材仍保留区分度（10 只→0.45，
            #     500 只→0.11），此时资金/涨幅作为加分项才合理。
            narrow = (1.0 / max(board.members, 1)) ** 0.35
            intensity = (board.net_amount / max(board.members, 1)
                         if board.net_amount is not None else None)
            net_norm = (abs(intensity) / net_max
                        if net_max and intensity is not None else 0.0)
            pct_norm = (abs(board.change_pct) / pct_max
                        if pct_max and board.change_pct is not None else 0.0)
            # 权重刻意**让窄度能压过资金面**：资金/涨幅合计最多贡献 0.28，
            # 因此"今天资金很猛的大板块"顶多把名次往上挪一点，赢不了"细分题材"。
            # 为什么必须这样：min-max 归一化会把当日候选里的资金差异**拉满到 0~1**，
            # 若资金项权重够大，宽泛概念（绝对净额天然大）就会翻上来 ——
            # 实测 `商业航天`(502 只, 人均净额小但总额大) 曾压过 `航海装备`(10 只)。
            # 语义上也是这么回事：**一只票属不属于某个题材，由宽窄决定；
            # 当日资金只是加分项。**
            board.relevance = 0.72 * narrow + 0.17 * net_norm + 0.11 * pct_norm
            board.reasons.insert(0, f"概念内 {board.members} 只票（越窄越相关）")
            if intensity is not None:
                board.reasons.append(f"人均主力净额 {intensity / 1e8:+.3f} 亿/只")
        candidates.sort(key=lambda item: -item.relevance)
        return candidates

    # ---------------- 名称联想 ----------------

    def suggest(self, keyword: str, *, limit: int = 12) -> list[dict[str, Any]]:
        """按关键字联想概念名（给「关联板块」输入框用）。

        匹配规则：命中即返回，按"越窄越靠前"排序 —— 用户打「存储」时，
        `存储芯片`（200 只）应该排在泛泛的大类前面。
        """
        text = str(keyword or "").strip()
        if not text:
            return []
        # 名录里同一个概念名可能有多个 ts_code（实测"激光雷达"出现两次），
        # 按名字去重后再返回，否则联想列表里会出现重复项。
        best: dict[str, tuple[int, str]] = {}
        for code, (name, members) in self.index().items():
            if not _is_usable_concept(name, members):
                continue
            if text not in name:
                continue
            current = best.get(name)
            if current is None or members < current[0]:
                best[name] = (members, code)
        ordered = sorted(((members, name, code)
                          for name, (members, code) in best.items()),
                         key=lambda item: item[0])
        return [{"code": code, "name": name, "members": members}
                for members, name, code in ordered[:limit]]


# ============================================================================
# 进程内单例
# ============================================================================

_INSTANCE: ConceptRepository | None = None
_INSTANCE_LOCK = threading.Lock()


def concept_repository(db_path: str | Path | None = None) -> ConceptRepository:
    """取进程内单例（与项目其它 provider 一致的单例获取方式）。"""
    global _INSTANCE
    if _INSTANCE is None:
        with _INSTANCE_LOCK:
            if _INSTANCE is None:
                _INSTANCE = ConceptRepository(db_path=db_path or _default_db_path())
    return _INSTANCE


def _default_db_path() -> Path | None:
    """默认落库位置：与主库同目录（取不到就只用内存缓存）。"""
    try:
        import os

        configured = os.environ.get("MOSS_DB_PATH", "").strip()
        if configured:
            return Path(configured)
        return Path("data/moss_finagent.db")
    except Exception:  # noqa: BLE001
        return None


__all__ = [
    "MAX_MEMBERS",
    "ConceptBoard",
    "ConceptRepository",
    "concept_repository",
]
