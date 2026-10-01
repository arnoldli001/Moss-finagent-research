"""本地股票字典：代码 / 中文名 / 拼音首字母 / 全拼，供前端联动联想与名称补全。

## 为什么必须有它（而不是每次去问上游）

这件事在项目里已经出过一次事故：做T自选股里出现了 `603083` 这种**名称就是代码**
的条目 —— 前端加自选时把"当前已加载快照里的名称"发过去，用户没先加载这只票时
名称就是空的，后端只能退化成写代码本身。界面上一排 `603083 / 600150`，
根本认不出是哪只票。

一次上游调用能解决单次查询，但解决不了三件事：

1. **联想**：用户输入 `jqkj` 或 `PAYH` 或 `剑桥`，要在毫秒级给候选 —— 每次都打
   外部接口既慢又会被限流；
2. **离线可用**：回测/做T在盘中跑，不能因为某个搜索接口抖动就输入不了代码；
3. **一致性**：同一个代码在不同页面必须显示同一个名称，不能一处 `剑桥科技`、
   另一处空着。

## 数据来源与分工

| 来源 | 覆盖 | 是否离线 | 用途 |
|------|------|:--------:|------|
| Tushare `stock_basic` | 5562 只 A 股（含行业/市场/上市日） | ✅ | **批量建库的主来源** |
| `pypinyin` | 任意中文 | ✅ | 生成首字母与全拼 |
| 东财 suggest | A股/ETF/指数/港股… | ❌ | **按需补录**（ETF、指数等不在 stock_basic 里的标的） |

实测（2026-09-16）：东财 suggest 接口在本机**可达**（返回 JSON，含 `Name` 与
`PinYin`、`Classify`）；被阻断的是 `push2*` 行情域名，不是搜索域名。

## 拼音为什么用库而不是外部接口

`pypinyin` 是纯本地库：5562 个名称生成拼音只要毫秒级，且结果确定；换成
"逐个去问搜索接口"要 5562 次请求，既慢又不可复现。实测首字母：
平安银行→PAYH、贵州茅台→GZMT、剑桥科技→JQKJ、中国船舶→ZGCB。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.errors import (
    BRIEF_TIGHT,
    brief,
)

logger = logging.getLogger(__name__)

DIRECTORY_TABLE = "quant_stock_directory"
_DIRECTORY_COLUMNS: tuple[tuple[str, str], ...] = (
    ("code", "VARCHAR(16)"),
    ("name", "VARCHAR(64)"),
    ("pinyin_initials", "VARCHAR(64)"),
    ("pinyin_full", "VARCHAR(128)"),
    ("instrument_type", "VARCHAR(16)"),
    ("exchange", "VARCHAR(16)"),
    ("market", "VARCHAR(32)"),
    ("industry", "VARCHAR(64)"),
    ("area", "VARCHAR(32)"),
    ("list_date", "VARCHAR(16)"),
    ("source", "VARCHAR(32)"),
    ("updated_at", "VARCHAR(32)"),
)

# 东财 suggest 的 Classify → 我们的类型
_CLASSIFY_MAP = {
    "astock": "股票", "bstock": "股票", "kcb": "股票", "cyb": "股票",
    "etf": "ETF", "fund": "基金", "index": "指数", "hkstock": "港股",
    "usstock": "美股", "bond": "债券", "zq": "债券",
}


class DirectoryError(RuntimeError):
    """字典不可用或操作失败。"""


@dataclass
class StockEntry:
    """一条字典记录。"""

    code: str
    name: str
    pinyin_initials: str = ""
    pinyin_full: str = ""
    instrument_type: str = "股票"
    exchange: str = ""
    market: str = ""
    industry: str = ""
    area: str = ""
    list_date: str = ""
    source: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "code": self.code, "name": self.name,
            "pinyin_initials": self.pinyin_initials,
            "pinyin_full": self.pinyin_full,
            "instrument_type": self.instrument_type,
            "exchange": self.exchange, "market": self.market,
            "industry": self.industry, "area": self.area,
            "list_date": self.list_date, "source": self.source,
            "label": f"{self.code} {self.name}".strip(),
        }


def to_initials(name: str) -> str:
    """中文名 → 拼音首字母大写（离线，pypinyin）。

    非中文字符（字母/数字）原样保留并转大写：`万科A` → `WKA`、
    `科创半导体ETF华夏` → `KCBDTHX`。这样用户按自己习惯敲
    `wka` 也能命中。
    """
    if not name:
        return ""
    try:
        from pypinyin import Style, lazy_pinyin

        letters = lazy_pinyin(name, style=Style.FIRST_LETTER,
                              errors=lambda item: list(item))
        return "".join(letters).upper()
    except ImportError:  # pragma: no cover 缺库时降级为空，不影响名称补全
        logger.warning("未安装 pypinyin，拼音首字母将为空（uv add pypinyin）")
        return ""


def to_full_pinyin(name: str) -> str:
    """中文名 → 全拼小写（离线）。"""
    if not name:
        return ""
    try:
        from pypinyin import lazy_pinyin

        return "".join(lazy_pinyin(name, errors=lambda item: list(item))).lower()
    except ImportError:  # pragma: no cover
        return ""


# ==================================================================
# 存储
# ==================================================================


@dataclass
class StockDirectory:
    """本地股票字典（存数据库，方言无关）。"""

    warehouse: Any = None
    cache: dict[str, StockEntry] = field(default_factory=dict)
    loaded: bool = False

    # ---------- 建表与写入 ----------

    def ensure_table(self) -> None:
        from sqlalchemy import (
            Column,
            MetaData,
            PrimaryKeyConstraint,
            String,
            Table,
        )

        from src.quant.warehouse import _add_missing_columns

        engine = self.warehouse.engine()
        metadata = MetaData()
        columns = [
            Column(name, String(int(ddl.split("(")[1].rstrip(")"))))
            for name, ddl in _DIRECTORY_COLUMNS
        ]
        Table(DIRECTORY_TABLE, metadata, *columns,
              PrimaryKeyConstraint("code")).create(engine, checkfirst=True)
        _add_missing_columns(engine, self.warehouse.config.dialect,
                             DIRECTORY_TABLE, _DIRECTORY_COLUMNS)

    def upsert(self, entries: list[StockEntry]) -> int:
        """批量写入（重复代码覆盖）。"""
        if not entries:
            return 0
        from sqlalchemy import text

        self.ensure_table()
        columns = [name for name, _ddl in _DIRECTORY_COLUMNS]
        quote = (lambda name: f"`{name}`") if self.warehouse.config.dialect == "mysql" \
            else (lambda name: f'"{name}"')
        now = time.strftime("%Y-%m-%d %H:%M:%S")
        sql = (f"INSERT INTO {quote(DIRECTORY_TABLE)} "
               f"({', '.join(quote(name) for name in columns)}) VALUES "
               f"({', '.join(':' + name for name in columns)})")
        updates = [name for name in columns if name != "code"]
        if self.warehouse.config.dialect == "mysql":
            sql += " ON DUPLICATE KEY UPDATE " + ", ".join(
                f"{quote(name)}=VALUES({quote(name)})" for name in updates)
        else:
            prefix = ("EXCLUDED" if self.warehouse.config.dialect == "postgresql"
                      else "excluded")
            sql += (f" ON CONFLICT ({quote('code')}) DO UPDATE SET "
                    + ", ".join(f"{quote(name)}={prefix}.{quote(name)}"
                                for name in updates))
        with self.warehouse.engine().begin() as connection:
            for entry in entries:
                row = entry.as_dict()
                row["updated_at"] = now
                connection.execute(text(sql), {
                    name: row.get(name) for name in columns})
        self.loaded = False       # 内存缓存失效
        return len(entries)

    # ---------- 建库 ----------

    def build_from_stock_basic(self, *, root: str | Path | None = None,
                               universe: str = "a_share") -> dict[str, Any]:
        """用 Tushare `stock_basic` 批量建库（离线，含拼音）。

        这是主来源：5562 只 A 股的中文名/行业/市场/上市日一次到位。
        ETF、指数不在其中，由 `enrich` 按需补录。
        """
        from src.quant.dataset_store import DatasetStore

        # 默认从 registry 取（`CHG-0069`）。**必须在函数体里补回落**：
        # 只把签名默认值改成 `None` 会把 `None` 一路传进 `DatasetStore(root=None)`
        # 与 `QuantWarehouse(root=None)`，实测 11 个用例当场 TypeError。
        if root is None:
            from src.infrastructure.catalog.data_stores import store_rel

            root = store_rel("tushare_partitions")
        store = DatasetStore("stock_basic", root=root, universe=universe)
        keys = store.keys()
        if not keys:
            raise DirectoryError(
                "没有 stock_basic 缓存：先执行 "
                "`python scripts/quant_sync.py doctor` 或下载股票列表")
        frame = store.load(keys)
        if len(frame) == 0:
            raise DirectoryError("stock_basic 分区为空")
        entries: list[StockEntry] = []
        for record in frame.to_dict("records"):
            name = str(record.get("name") or "").strip()
            code = str(record.get("code") or "").zfill(6)
            if not name or not code:
                continue
            entries.append(StockEntry(
                code=code, name=name,
                pinyin_initials=to_initials(name),
                pinyin_full=to_full_pinyin(name),
                instrument_type="股票",
                exchange=str(record.get("exchange") or ""),
                market=str(record.get("market") or ""),
                industry=str(record.get("industry") or ""),
                area=str(record.get("area") or ""),
                list_date=str(record.get("list_date") or ""),
                source="stock_basic"))
        written = self.upsert(entries)
        logger.info("股票字典建库完成：%d 条", written)
        return {"written": written, "total": self.count()}

    # ---------- 按需补录（ETF / 指数等） ----------

    def enrich(self, codes: list[str], *, timeout: int = 12) -> list[StockEntry]:
        """用东财 suggest 补录字典里没有的代码（ETF、指数…）。

        只对**缺失**的代码发请求，且结果落库 —— 同一个代码只查一次。
        """
        missing = [code for code in dict.fromkeys(codes) if code]
        if not missing:
            return []
        found: list[StockEntry] = []
        for code in missing:
            entry = _fetch_eastmoney(code, timeout=timeout)
            if entry is not None:
                found.append(entry)
        if found:
            self._cache_entries(found)
        return found

    def _cache_entries(self, entries: list[StockEntry]) -> None:
        """把补录结果写回字典库 —— **尽力而为**（`CHG-0087`，共享仓单写者）。

        ## 为什么这里不能抛

        `enrich()` 跑在**读路径**上（`GET /quant/stocks/{code}` 的
        `auto_enrich`、做T自选渲染时的名称补全）。共享行情仓的写权限归
        `warehouse.writer`（现行 = pilot），其余实例**只读** —— 所以这里的写入
        在 dev / 主实例上**必然被拒**。

        但被拒的是"顺手把结果缓存到共享库"这一步，**不是这次查询**：
        名字已经从东财取到了，如实返回它；写不进去只记一条 warning。
        否则一次读操作会因为"缓存写不进去"而 500 —— 故障方向完全错了。

        对照：`build_from_stock_basic()` 是**显式**重建设字典（管理端点触发），
        那是"我就是要写"，**必须**把异常抛出去告诉调用方为什么写不进去。
        """
        try:
            self.upsert(entries)
        except Exception as exc:  # noqa: BLE001
            # 两种异常都要接：显式闸门抛 `WarehouseError`，
            # 而连接级 `PRAGMA query_only=1` 兜底抛的是 SQLAlchemy `OperationalError`
            # （`attempt to write a readonly database`）。
            logger.warning("股票字典补录结果未落库（本实例对共享仓只读）：%s",
                           brief(exc, BRIEF_TIGHT))

    # ---------- 查询 ----------

    def _ensure_loaded(self) -> None:
        if self.loaded:
            return
        self.cache = {}
        if not self.warehouse.available():
            self.loaded = True
            return
        from sqlalchemy import inspect, text

        try:
            if DIRECTORY_TABLE not in inspect(
                    self.warehouse.engine()).get_table_names():
                self.loaded = True
                return
            quote = (lambda name: f"`{name}`") if \
                self.warehouse.config.dialect == "mysql" \
                else (lambda name: f'"{name}"')
            columns = [name for name, _ddl in _DIRECTORY_COLUMNS]
            with self.warehouse.engine().connect() as connection:
                rows = connection.execute(text(
                    f"SELECT {', '.join(quote(name) for name in columns)} "
                    f"FROM {quote(DIRECTORY_TABLE)}")).mappings().all()
            for row in rows:
                # 只取 dataclass 声明过的字段：表里还有 `updated_at` 这类
                # 纯存储列，直接 **row 会 TypeError（实测踩过，
                # 表现是"建库 5562 条但 count() 返回 0"）。
                entry = StockEntry(**{
                    name: (row.get(name) or "")
                    for name in StockEntry.__dataclass_fields__})
                self.cache[entry.code] = entry
        except Exception as exc:  # noqa: BLE001 字典坏了不该让业务崩
            logger.warning("股票字典加载失败：%s", brief(exc, BRIEF_TIGHT))
        self.loaded = True

    def count(self) -> int:
        self._ensure_loaded()
        return len(self.cache)

    def get(self, code: str, *, auto_enrich: bool = True) -> StockEntry | None:
        """按代码取条目；字典里没有且 `auto_enrich` 时按需补录一次。"""
        normalized = _normalize_code(code)
        self._ensure_loaded()
        entry = self.cache.get(normalized)
        if entry is not None and entry.name and entry.name != normalized:
            return entry
        if not auto_enrich:
            return entry
        found = self.enrich([normalized])
        return found[0] if found else entry

    def name_of(self, code: str, *, auto_enrich: bool = True) -> str:
        """代码 → 中文名（找不到时返回空串，**不要退回代码本身**）。

        返回代码本身会让"名称就是代码"这种脏数据看起来像正常值 ——
        正是做T自选股里那批 `603083` 条目的成因。

        `auto_enrich=False` 时**不发任何网络请求**：请求路径上（例如渲染自选列表）
        不该有隐式的外部调用，需要确定性时就关掉它。
        """
        entry = self.get(code, auto_enrich=auto_enrich)
        if entry is None:
            return ""
        return "" if entry.name == entry.code else entry.name

    def search(self, query: str, *, limit: int = 20,
               types: tuple[str, ...] = ()) -> list[StockEntry]:
        """联动联想：支持 代码 / 中文名 / 拼音首字母 / 全拼。

        排序规则（越靠前越"像用户想找的"）：
            0 代码完全相等
            1 代码前缀
            2 首字母完全相等（用户敲 `payh`）
            3 首字母前缀（敲 `pay`）
            4 全拼前缀
            5 中文名包含（敲 `平安`）
            6 中文名首字匹配（敲 `平`）
        """
        self._ensure_loaded()
        text = (query or "").strip()
        if not text:
            return []
        upper = text.upper()
        lowered = text.lower()
        digits = text.isdigit()
        scored: list[tuple[int, str, StockEntry]] = []
        for entry in self.cache.values():
            if types and entry.instrument_type not in types:
                continue
            name = entry.name or ""
            initials = entry.pinyin_initials or ""
            full = entry.pinyin_full or ""
            rank: int | None = None
            if digits:
                if entry.code == text:
                    rank = 0
                elif entry.code.startswith(text):
                    rank = 1
            if rank is None and initials:
                if initials == upper:
                    rank = 2
                elif initials.startswith(upper):
                    rank = 3
            if rank is None and full and full.startswith(lowered):
                rank = 4
            if rank is None and name and text in name:
                rank = 5
            if rank is None and name and name.startswith(text):
                rank = 6
            if rank is not None:
                scored.append((rank, entry.code, entry))
        scored.sort(key=lambda item: (item[0], item[1]))
        return [entry for _rank, _code, entry in scored[:limit]]


def _normalize_code(code: str) -> str:
    return str(code or "").strip().split(".")[0].zfill(6)


def _fetch_eastmoney(code: str, *, timeout: int = 12) -> StockEntry | None:
    """东财 suggest：按代码查名称与拼音（A股/ETF/指数都覆盖）。"""
    url = ("https://searchapi.eastmoney.com/api/suggest/get?"
           + urllib.parse.urlencode({"input": code, "type": "14",
                                     "count": "8", "token": "D43BF722C8E33BDC906FB84D85E326E8"}))
    request = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 补录失败不影响已有条目
        logger.debug("东财补录 %s 失败：%s", code, brief(exc, BRIEF_TIGHT))
        return None
    rows = ((payload.get("QuotationCodeTable") or {}).get("Data")) or []
    for row in rows:
        if _normalize_code(row.get("Code", "")) != code:
            continue
        name = str(row.get("Name") or "").strip()
        if not name:
            continue
        classify = str(row.get("Classify") or "").lower()
        return StockEntry(
            code=code, name=name,
            # 东财直接给了拼音；缺失时用本地库补
            pinyin_initials=(str(row.get("PinYin") or "").upper()
                             or to_initials(name)),
            pinyin_full=to_full_pinyin(name),
            instrument_type=_CLASSIFY_MAP.get(classify, "其它"),
            exchange=str(row.get("JYS") or ""),
            market=str(row.get("SecurityTypeName") or ""),
            source="eastmoney")
    return None


def stock_directory(root: str | Path | None = None) -> StockDirectory:
    from src.quant.warehouse import QuantWarehouse

    # 同 `build_from_stock_basic`：默认从 registry 取，且**必须补回落**。
    if root is None:
        from src.infrastructure.catalog.data_stores import store_rel

        root = store_rel("tushare_partitions")
    return StockDirectory(warehouse=QuantWarehouse(root=root))


__all__ = [
    "DIRECTORY_TABLE",
    "DirectoryError",
    "StockDirectory",
    "StockEntry",
    "stock_directory",
    "to_full_pinyin",
    "to_initials",
]
