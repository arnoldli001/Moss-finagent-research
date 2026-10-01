"""★ 字段可达性：**库里有的字段，连接器必须能取到**（离线、零 LLM、零花费）。

## 用户要求（2026-09-29 原话）

> 「任务完成后，要加测试用例，连接器确保数据库里的所有字段（至少相关表都要
>   遍历到）**可达数据库、可匹配获取到**（不走云端大模型花，**不花钱**的测试用例）。」

## 这条要防的缺陷形状

本项目最贵的一类缺陷是"**数据在库里，但没人能取到**"，而且**全部不报错**：
`extra_json` 里的个股解禁明细、`ml_member_corr`（选错表时的那次）、
白名单挡掉的中文标签……它们的共同点是"**列在、值在、就是没有一条路径读它**"。
所以本文件把"可达"做成机器判据，**判据从库里现读**（`PRAGMA table_info`），
新增一列会自动进入判据 —— 不需要维护一张会腐烂的清单。

## 三层判据（互相独立，任何一层红了都是真缺陷）

**A 列级可达** —— 声明读的每张表的**每一列**，要么在连接器/仓储源码里被
**按名引用**，要么在 `_UNMAPPED_COLUMNS` 里**带理由**登记。
红了说明：库里加了列/改了名，而取数路径一个字没动（或某列压根没人用）。

**B 表级无死声明、无越界读** —— `_TABLE_CANDIDATES` 声明的表**必须被至少一族
SQL 真的读到**；连接器源码里出现的表名**必须在声明里**。
红了说明：声明与实际不符（写了个没人用的候选 / 读了张没声明的表）。

**C 行为级可取到** —— 用**真库的 DDL** 在临时库建同名表 + 造哨兵值，把连接器
指过去，逐族 `fetch()` → 必须**产点**，且每个关键字段的**哨兵值**能在结果里找到。
红了说明：代码"提到了"该列但**实际取不出来**（JOIN 写错、字段名拼错、被过滤掉）。

## 为什么这套测试**不花钱**

1. 只用 `sqlite3` + 连接器本地路径（`PlatformDataConnector(backend=None)`），
   不装配采集链 ⇒ 不触发 `ValuationProvider` 的联网路径；
2. 有 `_no_network` **autouse** 夹具：把 `socket.socket.connect` 换成抛异常 ——
   任何一次真实联网都会让用例直接失败，而不是"悄悄花了钱还绿着"；
3. 不 import 任何 LLM 相关模块（`tests/unit/test_llm_*` 才有）。

`_UNMAPPED_COLUMNS` 的**过期语义**：一条登记如果后来在源码里被引用了，
`test_unmapped_registry_is_not_stale` 会红 —— 逼着删掉它，**不留永久豁免**。
"""

from __future__ import annotations

import ast
import json
import re
import sqlite3
import textwrap
import traceback
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import pytest

from src.infrastructure.catalog import data_stores
from src.infrastructure.connectors import platform_data_connector as pdc
from src.infrastructure.repositories import unlock_plan_repo

#: **取数面**：判定"这一列有没有人读"时扫的源码范围。
#:
#: 为什么不只看平台连接器自己：同一个人可能由**别的取数模块**读走
#: （`quant_daily_basic.dv_ttm` 由连接器的行情列表读、`sector_crowding_daily`
#: 的 `sector_amount` 由拥挤度模块自己读）。只看一个文件会报一堆**假缺陷**，
#: 而假缺陷和"探针自己错了"是同一类问题的两面（AGENTS.md 反复登记过）。
#: 范围仍然**有界**：连接器层 + 仓储层 + 行情仓 + 各功能板块自己的存储层。
_READER_DIRS: tuple[Path, ...] = (
    Path(pdc.__file__).parent,                        # infrastructure/connectors
    Path(pdc.__file__).parents[2] / "infrastructure" / "repositories",
)
_READER_FILES: tuple[Path, ...] = (
    Path(pdc.__file__).parents[2] / "quant" / "warehouse.py",
    Path(pdc.__file__).parents[2] / "sector_crowding" / "db.py",
    Path(pdc.__file__).parents[2] / "fundflow" / "provider.py",
    Path(pdc.__file__).parents[2] / "sector_rotation" / "store.py",
    Path(unlock_plan_repo.__file__),
)

#: **显式登记"这一列不接"**：`列名 → 理由`。
#:
#: ⚠️ 这是**例外**，不是垃圾桶。每条都必须写清"为什么不接"，而且带过期检查：
#: 一旦源码里引用了它，`test_unmapped_registry_is_not_stale` 会红，逼你删掉。
_UNMAPPED_COLUMNS: dict[str, str] = {}

#: 逐族的**关键字段**（行为级判据 C 用）：这些字段必须真的从库里取到并出现在 extra 里。
#: 判据写成"**哨兵值**出现在取数结果里"，而不是"代码里提到了它"。
#:
#: ⚠️ 字段的哨兵值**按本族全部表**去查（不是只看第一张）：一个族的字段常常来自
#: 多张表（`概念拥挤度` 的 `water_level` 在 `sector_crowding_daily`、`raw_name` 在
#: `ml_stock_theme`）。只看第一张会让一半字段**静默跳过**（"没量到"被当成"通过"）。
_FAMILY_REQUIRED_FIELDS: dict[str, tuple[str, ...]] = {
    "解禁计划": ("code", "name", "unlock_date", "shares", "market_cap",
               "pct_of_float", "share_type"),
    "概念拥挤度": ("board_code", "corr", "sector_name", "water_level",
               "raw_crowding", "ma5_crowding", "max_ma5",
               # ★ `ml_stock_theme` 的溯源列（2026-09-29 接上）
               "raw_name", "model", "prompt_sig", "scored_at"),
    "主线告警": ("board_code", "board_name", "level", "score", "kind"),
    "个股告警": ("alert_type", "alert_level", "title"),
    "行业拥挤度": ("sector_name", "water_level", "raw_crowding"),
    # ★ `quant_daily_basic` 的原始字段（2026-09-29 接上，随 `extra.raw_inputs` 下发）
    "估值水位": ("pe_ttm", "pb", "close_basic", "ps", "turnover_rate_f",
               "total_share", "float_share", "free_share"),
}

#: `_TABLE_CANDIDATES` 里"确实由本连接器 SQL 读取"的表 → 对应族（行为级取数用）。
_FAMILY_TABLES: dict[str, tuple[str, ...]] = {
    "解禁计划": ("unlock_plan",),
    "概念拥挤度": ("ml_member_corr", "sector_crowding_daily",
               "sector_crowding_max_ma5", "ml_stock_theme"),
    "主线告警": ("mainline_alert",),
    "个股告警": ("fact_alerts",),
    "行业拥挤度": ("sector_crowding_list", "sector_crowding_daily",
               "sector_crowding_max_ma5"),
    "估值水位": ("quant_daily_basic",),
}


# ============================================================================
# 夹具：离线保证 + 只读连接
# ============================================================================


@pytest.fixture(autouse=True)
def _no_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """**本文件任何用例都不许连外网**（用户要求"不花钱"）。

    ⚠️ **loopback 必须放行**，而且必须**真的连上去**（转调原 `connect`）：
    Windows 上 `asyncio.run()` 建 ProactorEventLoop 时要用 `socket.socketpair()`
    建**自管道**，其回退实现走 `socket.connect(('127.0.0.1', port))` ——
    一律拒绝会让**每一个** `asyncio.run(conn.fetch(...))` 在"建事件循环"这一步
    就抛错，看上去像"连接器在偷偷联网，判据抓到了"，实际是**夹具自己错了**
    （本轮实测：5 个族全红，栈顶停在 `proactor_events._make_self_pipe`）。
    所以判据写成"**非本地地址**一律拒"，并且对本地地址**透传**（不透传会让
    `socketpair` 的 `accept()` 永远阻塞 —— 假红换成挂起，更难查）。
    """
    import socket

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    local = {"127.0.0.1", "localhost", "::1", "0.0.0.0", ""}

    def _host_of(address: Any) -> str | None:
        """连接目标的 host（非 tuple 地址 → None = 本地文件套接字，放行）。"""
        if isinstance(address, tuple) and address:
            return str(address[0])
        return None

    def _connect(self: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
        host = _host_of(address)
        if host is None or host in local:
            return real_connect(self, address, *args, **kwargs)
        raise AssertionError(
            f"本文件必须离线运行（零 LLM、零花费）—— 这里连了外网 {address!r}")

    def _connect_ex(self: Any, address: Any, *args: Any, **kwargs: Any) -> Any:
        host = _host_of(address)
        if host is None or host in local:
            return real_connect_ex(self, address, *args, **kwargs)
        raise AssertionError(
            f"本文件必须离线运行（零 LLM、零花费）—— 这里连了外网 {address!r}")

    monkeypatch.setattr(socket.socket, "connect", _connect, raising=False)
    monkeypatch.setattr(socket.socket, "connect_ex", _connect_ex, raising=False)


def _open_store(name: str) -> sqlite3.Connection | None:
    """只读打开登记的存储（拿不到 → None，调用方 skip）。"""
    try:
        path = data_stores.resolve_store(name)
    except Exception:  # noqa: BLE001 registry 不可用
        return None
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    conn.row_factory = sqlite3.Row
    return conn


def _find_table(table: str) -> tuple[str, sqlite3.Connection] | None:
    """按候选存储序列找这张表（与连接器 `_table_store` 同一口径）。"""
    for store in pdc._TABLE_CANDIDATES.get(table, ()):  # noqa: SLF001
        conn = _open_store(store)
        if conn is None:
            continue
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if row is not None:
            return store, conn
        conn.close()
    return None


def _source_for(table: str) -> tuple[str, sqlite3.Connection] | None:
    """这张表**在本机真库**里的 `(存储名, 只读连接)`，找不到 → None。

    `unlock_plan` 不在 `pdc._TABLE_CANDIDATES` 里（它归 `unlock_plan_repo` 管，
    `_STORE = app_db`），所以走它自己的 `_path()` —— 否则「解禁计划」这一族
    在**每一条**判据里都会被静默跳过（看着绿，其实没验）。
    """
    if table == unlock_plan_repo.TABLE:
        path = Path(unlock_plan_repo._path())  # noqa: SLF001 仓储自己的路径入口
        if not path.exists():
            return None
        try:
            conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
        except sqlite3.Error:
            return None
        conn.row_factory = sqlite3.Row
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
            (table,)).fetchone()
        if row is None:
            conn.close()
            return None
        return f"{unlock_plan_repo._STORE}(unlock_plan_repo)", conn  # noqa: SLF001
    return _find_table(table)


def _all_tables() -> list[str]:
    """判据要遍历的全部表 = 连接器声明的候选 ∪ 逐股解禁表（**唯一来源**）。"""
    return sorted(set(pdc._TABLE_CANDIDATES)  # noqa: SLF001
                  | {unlock_plan_repo.TABLE})


def _columns(table: str) -> list[str]:
    found = _source_for(table)
    if found is None:
        return []
    _store, conn = found
    try:
        return [str(r["name"]) for r in conn.execute(
            f"PRAGMA table_info({table})").fetchall()]
    finally:
        conn.close()


def _reader_files() -> list[Path]:
    """取数面的全部 `.py`（有界：连接器层 + 仓储层 + 四个功能模块的存储层）。"""
    files: list[Path] = [p for p in _READER_FILES if p.exists()]
    for directory in _READER_DIRS:
        if directory.exists():
            files.extend(sorted(directory.rglob("*.py")))
    return files


def _source_text() -> str:
    return "\n".join(path.read_text(encoding="utf-8")
                     for path in _reader_files())


def _docstring_nodes(tree: ast.AST) -> set[int]:
    """所有**文档字符串**节点的 `id()`（判据 A 要排除它们）。

    文档里写一句"本表有 `ps` 列"**不等于**有人读它 —— 拿它当"已引用"是假绿。
    （注释本来就不进 AST；文档字符串是**字面量**，不排掉就会被算进来。）
    """
    found: set[int] = set()
    holders = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        if not isinstance(node, holders):
            continue
        body = getattr(node, "body", None) or []
        if not body:
            continue
        first = body[0]
        if (isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)):
            found.add(id(first.value))
    return found


def _referenced_names(text: str) -> set[str]:
    """源码里出现的**标识符**（AST 取 `Name`/`Attribute`/字符串字面量里的词）。

    用 AST 而不是正则：正则会把注释也算进来（本项目实测过这类假绿 ——
    见 `tests/unit/test_shipped_deps.py` 的教训）；文档字符串另按
    `_docstring_nodes()` 显式排除。

    ⚠️ **这是必要条件，不是充分条件**：它只证明"这个列名在源码里出现过"。
    实测漏网形状（本轮踩过）：把列名同时写进"口径说明字典"（`_VALUATION_RAW_BASIS`
    的键）而**忘了真的 SELECT 它** → 判据 A 照样绿（字典键也是字符串字面量），
    只有判据 C（真取一次、在结果里找哨兵**值**）会红。
    所以 A 与 C **缺一不可**，A 独绿不代表字段可达。
    """
    names: set[str] = set()
    for path in _reader_files():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        docstrings = _docstring_nodes(tree)
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                names.add(node.id)
            elif isinstance(node, ast.Attribute):
                names.add(node.attr)
            elif (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and id(node) not in docstrings):
                # 字符串里既可能是 SQL 片段也可能是 payload / 口径字典的键名
                for token in node.value.replace("(", " ").replace(")", " ") \
                        .replace(",", " ").replace("\n", " ").split():
                    names.add(token.strip("\"'`;"))
    return names


# ============================================================================
# A 列级可达
# ============================================================================


def test_every_declared_table_column_is_reachable() -> None:
    """★★ 判据 A：**声明读的每张表的每一列，都必须有一条读取路径**。

    判据从库里现读（`PRAGMA table_info`）：**新增一列会自动进入判据** ——
    这正是"手写清单不会自己长大"的反面（AGENTS.md 里那张退役的豁免表）。

    ⚠️ 本条只是**必要条件**（列名在源码里出现过），**充分条件是判据 C**
    （真取一次、在结果里找到哨兵值）。两者都绿才算"可达"——
    只写进口径字典/文档而没真的 SELECT 的列，本条会漏（见 `_referenced_names`）。
    """
    referenced = _referenced_names(_source_text())
    missing: dict[str, list[str]] = {}
    checked = 0
    for table in _all_tables():
        columns = _columns(table)
        if not columns:
            continue                       # 表不在本机 → 由"表存在性"那条负责
        checked += len(columns)
        for column in columns:
            if column in referenced or column in _UNMAPPED_COLUMNS:
                continue
            missing.setdefault(table, []).append(column)
    if not checked:
        pytest.skip("本机没有任何一张被声明的表（没数据的环境按知名降级处理）")
    assert not missing, (
        "以下**库里的列**在取数路径里一条引用都没有（数据在库里但没人读得到）：\n"
        + "\n".join(f"  · {t}: {cols}" for t, cols in missing.items())
        + "\n→ 两条合法归宿：① 在连接器/仓储里真的读它并随 extra 下发；"
        "② 在 `_UNMAPPED_COLUMNS` 里登记并写清『为什么不接』"
        "（登记带过期检查，源码一引用它就会红，逼你删）。"
    )


def test_unmapped_registry_is_not_stale() -> None:
    """★★ 豁免**自带过期语义**：源码一旦引用该列，就必须把它从登记表里删掉。

    没有这条，"显式登记"会退化成**永久豁免** —— 正是红灯纪律要防的失败模式。
    """
    referenced = _referenced_names(_source_text())
    stale = [column for column in _UNMAPPED_COLUMNS if column in referenced]
    assert not stale, (
        f"`_UNMAPPED_COLUMNS` 里这些列**已经被源码引用了**，登记过期：{stale}\n"
        "→ 删掉条目（它现在是真的接上了）")


# ============================================================================
# B 表级：无死声明 / 无越界读
# ============================================================================


def test_declared_tables_are_actually_read() -> None:
    """★ 判据 B-1：`_TABLE_CANDIDATES` 里声明的表，**必须被至少一族的 SQL 真的读到**。

    防的是"声明了一张没人用的候选表"——它会让"这张表在哪"的排查白跑一趟。
    """
    referenced = _referenced_names(_source_text())
    dead = [table for table in pdc._TABLE_CANDIDATES  # noqa: SLF001
            if table not in referenced]
    assert not dead, (
        f"这些表在 `_TABLE_CANDIDATES` 里声明了，但源码里没有任何读取路径：{dead}\n"
        "→ 要么真的用它，要么从声明里删掉")


def test_no_undeclared_table_is_read() -> None:
    """★ 判据 B-2：**平台连接器自己**读的表**必须在声明里**（否则"表在哪"无从解析）。

    ⚠️ 只扫**本连接器**（+ 它自己的仓储）：判据 B 问的是"这个连接器的声明与实际
    是否一致"。把范围放大到整个取数面会匹配到别的模块自己的 SQL
    （`fact_events` / `news_cache` / `user_alert_read` …）—— 那些表不归它声明，
    拿它们报错就是**判据写宽了**（本轮实测踩过一次）。
    """
    declared = set(pdc._TABLE_CANDIDATES)  # noqa: SLF001
    text = "\n".join(path.read_text(encoding="utf-8") for path in (
        Path(pdc.__file__), Path(unlock_plan_repo.__file__)))
    used = set(re.findall(r"(?:FROM|JOIN)\s+([a-z_][a-z0-9_]*)", text))
    unknown = sorted(t for t in used
                     if t not in declared and t not in {"sqlite_master"})
    assert not unknown, (
        f"连接器源码里读了没声明的表：{unknown}\n"
        "→ 加进 `_TABLE_CANDIDATES`（并说明它可能在哪个存储里）")


# ============================================================================
# C 行为级：用**真库 DDL** 造临时库，逐族真取一次
# ============================================================================


def _build_fixture_db(tmp_path: Path) -> tuple[Path, list[str]]:
    """把真库的 DDL 抄进临时库（**schema 保真**），并插入哨兵数据。

    返回 `(临时库路径, 已建表清单)`；表在本机不存在时跳过它（不猜 schema）。
    """
    target = tmp_path / "field_reachability.db"
    conn = sqlite3.connect(target)
    built: list[str] = []
    try:
        for table in sorted(set().union(*_FAMILY_TABLES.values(),
                                       set(_all_tables()))):
            found = _source_for(table)
            if found is None:
                continue
            store, src = found
            ddl = src.execute(
                "SELECT sql FROM sqlite_master WHERE type='table' AND name=?",
                (table,)).fetchone()
            src.close()
            if ddl is None or not ddl["sql"]:
                continue
            conn.execute(ddl["sql"])
            built.append(f"{store}.{table}")
            _insert_sentinel(conn, table)
        conn.commit()
    finally:
        conn.close()
    return target, built


#: 多行哨兵里标记"**这一行必须被取到**"（其余行只用于把窗口/覆盖范围撑开）。
#: 为什么必须有：`shares` 这类字段在"撑窗口"的行里是 `1.0`，而 `1.0` 在结果 JSON 里
#: **随便都能撞上** —— 拿它当判据等于自造假绿（阈值/计数最容易犯这个错）。
_EXPECTED_KEY = "_expected"

#: 哨兵日期**相对今天算**：写死 `'20260929'` 会在几天后过期 ——
#: 窗口过滤（解禁 30 天、告警 90 天回看）当场把哨兵行滤掉，症状是
#: "库里有数据却取不到"，而真相是**夹具过期了**（判据自己老化，比缺陷更难查）。
_TODAY = date.today()


def _iso(days: int) -> str:
    return (_TODAY + timedelta(days=days)).isoformat()


def _compact(days: int) -> str:
    return (_TODAY + timedelta(days=days)).strftime("%Y%m%d")


def _stamp(days: int, clock: str = "10:00:00") -> str:
    return f"{_iso(days)}T{clock}"


#: 每张表的哨兵行（**只填真库里存在的列**，其余按列类型兜住）。
#: 值可以是 `dict`（一行）或 `list[dict]`（多行 —— 例如 `unlock_plan`
#: 要靠**跨窗口的两行**才能让"已落库覆盖范围"判定为覆盖，见判据 C）。
_SENTINELS: dict[str, Any] = {
    # ⚠️ 三行：`-5` 与 `+40` 只为把覆盖范围撑到查询窗口两侧（解禁表的三态判据
    #    要求 `win.start <= window[0] and window[1] <= win.end`，否则会走
    #    `out_of_covered_window` ⇒ 不产点，看起来像"表里没数据"）；
    #    `+15` 才是真正要被取到的那一行。
    "unlock_plan": [
        {
            "code": "600036", "name": "哨兵银行", "unlock_date": _iso(-5),
            "shares": 1.0, "market_cap": 1.0, "pct_of_float": 0.001,
            "share_type": "哨兵早期", "certainty": "rule",
            "source": "sentinel", "fetched_at": _stamp(-5), "raw_hash": "s0",
        },
        {
            "code": "600036", "name": "哨兵银行", "unlock_date": _iso(15),
            "shares": 111111.0, "actual_shares": 111111.0,
            "market_cap": 222222.0, "pct_of_float": 0.333, "close_before": 44.4,
            "chg_before_20d": -5.5, "chg_after_20d": 6.6,
            "share_type": "哨兵类型", "certainty": "rule",
            "source": "sentinel", "fetched_at": _stamp(0), "raw_hash": "sentinel",
            _EXPECTED_KEY: True,
        },
        {
            "code": "600036", "name": "哨兵银行", "unlock_date": _iso(40),
            "shares": 1.0, "market_cap": 1.0, "pct_of_float": 0.001,
            "share_type": "哨兵远期", "certainty": "rule",
            "source": "sentinel", "fetched_at": _stamp(0), "raw_hash": "s1",
        },
    ],
    "ml_member_corr": {
        "board_code": "881155.TI", "code": "600036", "corr": 0.987654,
        "samples": 172, "start_date": _iso(-250), "end_date": _iso(-1),
        "computed_at": _iso(0),
    },
    "ml_stock_theme": {
        "code": "600036", "rank": 1, "theme": "哨兵概念",
        "business_score": 99.5, "corr": 0.876543, "final_score": 88.8,
        "reason": "哨兵理由",
        # 溯源列（2026-09-29 接上）：原名 / LLM 层名 / prompt 指纹 / 打分时间
        "raw_name": "哨兵原名", "model": "sentinel_layer",
        "prompt_sig": "sentinel_v1", "scored_at": _stamp(0),
    },
    "quant_daily_basic": {
        "trade_date": _compact(0), "code": "600036",
        "pe_ttm": 17.71, "pb": 3.67, "pe": 15.36,
        # ★ 2026-09-29 接上的原始字段（随 extra.raw_inputs 下发）
        "close_basic": 44.41, "ps": 1.2692, "ps_ttm": 1.1302,
        "turnover_rate": 0.9989, "turnover_rate_f": 1.0074,
        "total_share": 735848500.0, "float_share": 562231390.0,
        "free_share": 557495173.0, "total_mv": 27005639950.0,
        "circ_mv": 20633892013.0, "dv_ratio": 4.0768, "dv_ttm": 4.445,
    },
    "sector_crowding_daily": {
        "trade_date": _compact(0), "sector_code": "881155.TI",
        "sector_name": "银行", "sector_amount": 333333.0,
        "market_amount": 444444.0, "raw_crowding": 0.123456,
        "ma5_crowding": 0.234567, "water_level": 0.345678,
    },
    "sector_crowding_max_ma5": {"sector_code": "881155.TI", "max_ma5": 0.999},
    "sector_crowding_list": {
        "sector_code": "881155.TI", "sector_name": "银行", "visible": 1,
    },
    "mainline_alert": {
        "trade_date": _compact(0), "board_code": "881155.TI",
        "board_name": "哨兵板块", "kind": "concept", "level": "strong",
        "score": 77.7, "resonance": 3, "entry_close": 55.5,
        "max_gain_pct": 8.8, "payload": "{}",
    },
    "fact_alerts": {
        "alert_type": "sentinel_type", "alert_level": "high",
        "title": "哨兵告警标题",
        "affected_stocks_json": json.dumps(
            [{"code": "600036", "name": "哨兵银行", "impact": "positive",
              "reason": "哨兵理由"}], ensure_ascii=False),
        "affected_industries_json": "[]", "status": "active",
        "tenant_id": "tenant_001",
        # ⚠️ 时间列必须给：过滤窗口读的是 `trigger_time[:10] or created_at[:10]`，
        #    留空 → 日期解析成 None → 窗口内一条都不剩（**不是**没数据，是夹具没给）。
        "trigger_time": _stamp(0, "09:00:00"), "created_at": _stamp(0, "09:00:00"),
        "expire_time": _stamp(7, "09:00:00"),
    },
}


def _generic_value(declared_type: str, not_null: bool) -> Any:
    """没给哨兵的列 → 按**声明的类型**给一个能插进去的通用值。

    ⚠️ 不能一律填 `"sentinel"`：SQLite 的列有类型亲和性，往 `INTEGER`/`REAL` 列里
    写字符串会 `IntegrityError: datatype mismatch`（本轮实测踩过 ——
    `sector_crowding_daily` 就有一列不是 TEXT）。
    """
    kind = (declared_type or "").upper()
    if not kind:
        return 1                      # 无类型声明 = BLOB 亲和，数值最通用
    if "INT" in kind:
        return 1
    if any(token in kind for token in ("REAL", "FLOA", "DOUB", "NUM", "DEC")):
        return 1.0
    if any(token in kind for token in ("CHAR", "CLOB", "TEXT")):
        return "sentinel"
    return 1 if not_null else None


#: `_EXPECTED_KEY` 定义在 `_SENTINELS` 之前（导入期就要用），这里只用不定义。
def _sentinel_rows(table: str, *, expected_only: bool = False) -> list[dict[str, Any]]:
    """某张表的哨兵行（**统一口径**：`dict` 与 `list[dict]` 都归一成列表）。

    `_insert_sentinel()` 与行为级判据的"哨兵池"都走这里 ——
    两处各写一遍必然漂移，症状是"插进去了但判据找不到"（或反过来）。
    `expected_only=True` 时只返回被标记为"必须取到"的行（没标记则返回全部）。
    `_` 开头的键是**判据元数据**，不会进库、也不会当字段判。
    """
    payload = _SENTINELS.get(table)
    if payload is None:
        return []
    raw = payload if isinstance(payload, list) else [payload]
    marked = [row for row in raw if row.get(_EXPECTED_KEY)]
    if expected_only and marked:
        raw = marked
    return [{key: value for key, value in row.items()
             if not key.startswith("_")} for row in raw]


def _insert_sentinel(conn: sqlite3.Connection, table: str) -> None:
    """按真库的列清单插入哨兵行（缺的列按**声明类型**兜住，见 `_generic_value`）。"""
    rows = _sentinel_rows(table)
    if not rows:
        return
    info = conn.execute(f"PRAGMA table_info({table})").fetchall()
    for payload in rows:
        values: dict[str, Any] = {}
        for row in info:
            column, declared, not_null = str(row[1]), str(row[2] or ""), bool(row[3])
            values[column] = (payload[column] if column in payload
                              else _generic_value(declared, not_null))
        placeholders = ", ".join(f":{c}" for c in values)
        names = ", ".join(values)
        conn.execute(f"INSERT INTO {table} ({names}) VALUES ({placeholders})",
                     values)


#: 从结果 JSON 里抠数字（用于数值哨兵的**容差**匹配）。
_NUMBER_RE = re.compile(r"-?\d+(?:\.\d+)?(?:[eE][-+]?\d+)?")


def _rendered_numbers(blob: str) -> list[float]:
    numbers: list[float] = []
    for token in _NUMBER_RE.findall(blob):
        try:
            numbers.append(float(token))
        except ValueError:  # pragma: no cover - 正则保证可转
            continue
    return numbers


def _sentinel_reached(sentinel: Any, blob: str, numbers: list[float]) -> bool:
    """哨兵值**真的出现在取数结果里**了吗。

    · 数值 → 按容差比（连接器可能 `round()`；写死 `str()` 相等会变成假红），
      容差 = `max(1e-6, |哨兵|×1e-4)`；
    · 其它 → 子串匹配。

    ⚠️ **绝对不许**把"字段名出现在结果里"写成通过条件 —— 那是**假绿生成器**：
    `"samples"` 这个词里就含 `ps`，于是 `ps` 会永远"可达"（本轮实测踩过）。
    判据只认**值**，不认名字。
    """
    if isinstance(sentinel, bool) or sentinel is None:
        return str(sentinel).lower() in blob.lower()
    if isinstance(sentinel, (int, float)):
        target = float(sentinel)
        tolerance = max(1e-6, abs(target) * 1e-4)
        return any(abs(number - target) <= tolerance for number in numbers)
    return str(sentinel) in blob


def test_families_can_actually_fetch_every_required_field(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★★ 判据 C（行为级）：**真 schema + 哨兵值**，逐族真取一次，字段必须出来。

    为什么必须有这一层：判据 A 只证明"源码里提到了这个列名"，
    而**写错 JOIN / 拼错字段 / 过滤条件把它滤掉**这三种情况都能通过 A。
    只有真的取一次、在结果里找到哨兵**值**，才算"可匹配获取到"。
    """
    target, built = _build_fixture_db(tmp_path)
    if not built:
        pytest.skip("本机没有被声明表的 DDL（没数据的环境按知名降级处理）")
    monkeypatch.setattr(pdc, "_store_path", lambda _name: target)
    monkeypatch.setattr(pdc, "_TABLE_CANDIDATES",
                        {t: ("fixture",) for t in pdc._TABLE_CANDIDATES})  # noqa: SLF001
    monkeypatch.setattr(unlock_plan_repo, "_path", lambda: str(target))
    pdc.reset_cache()

    conn = pdc.PlatformDataConnector()
    import asyncio

    problems: list[str] = []
    checked: list[str] = []
    for family, tables in _FAMILY_TABLES.items():
        if not any(t in " ".join(built) for t in tables):
            continue
        # 本族**全部表**的哨兵合成一个池子：字段可能来自任意一张表，
        # 只看 `tables[0]` 会让另一半字段静默跳过。
        sentinel_pool: dict[str, Any] = {}
        for table in tables:
            for payload in _sentinel_rows(table, expected_only=True):
                for field, value in payload.items():
                    sentinel_pool.setdefault(field, value)
        # 占位符**从连接器现读**（`_INDUSTRY_NAME_FAMILIES` 是唯一真值源）：
        # 写死名单的话，以后新增一个按行业名占位的族，这里会拿 6 位代码去取它 ——
        # 症状是 `DataFetchError: 需要一个 6 位 A 股代码，收到 '银行'`（本轮实测踩过）。
        industry_family = family in pdc._INDUSTRY_NAME_FAMILIES  # noqa: SLF001
        indicator = f"{family}:{'银行' if industry_family else '600036'}"
        try:
            points = asyncio.run(conn.fetch(indicator))
        except Exception as exc:  # noqa: BLE001 取数异常要报出来（不是 skip）
            # 带上栈：只写 `type: msg` 时，"哪一跳抛的"要靠猜（本轮实测踩过）。
            problems.append(
                f"{family}: fetch 抛错 {type(exc).__name__}: {exc}\n"
                + textwrap.indent(traceback.format_exc(), "      "))
            continue
        if not points:
            problems.append(f"{family}: 哨兵库里有数据却**没产点**（取不到）")
            continue
        blob = json.dumps([p.extra for p in points], ensure_ascii=False,
                          default=str)
        numbers = _rendered_numbers(blob)
        checked.append(family)
        for field in _FAMILY_REQUIRED_FIELDS.get(family, ()):
            if field not in sentinel_pool:
                problems.append(
                    f"{family}: 夹具没给字段 {field} 哨兵值（这条判据验不了它）")
                continue
            if not _sentinel_reached(sentinel_pool[field], blob, numbers):
                problems.append(
                    f"{family}: 字段 {field} 的哨兵值 "
                    f"{sentinel_pool[field]!r} 没出现在取数结果里（列在、取不出来）")
    # 顺序很重要：**先报问题再断言"验过至少一族"** —— 否则"全部族都抛错"
    # 会以"一个族都没被验证到"的形式出现，把真正的报错信息盖掉。
    assert not problems, (
        "以下族/字段**取不到**（库里有、代码提到了，但实际取不出来）：\n"
        + "\n".join(f"  · {p}" for p in problems)
        + f"\n已成功验证的族：{checked}"
    )
    assert checked, (
        "一个族都没被验证到（夹具或声明有问题）："
        f"已建表 {built}，已声明族 {list(_FAMILY_TABLES)}")


def test_raw_inputs_degrades_without_breaking_the_family(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★★ 反向判据：共享行情仓**读不到**时，原始字段要**如实缺**，不许把整族打挂。

    为什么必须有这条：新增的 `raw_inputs` 是**附加信息**，而
    `quant_daily_basic` 只在共享行情仓里（实测主/试点实例的 `app_db` 没有它，
    见 `catalog/column_index.py` 的登记）。如果它取不到就抛异常，
    平台估值路径（本来不需要仓也能给分）会**整族失败** ——
    那正是"修一个坏一个"，而且只在**别人的环境**上出现。
    """
    from src.core.exceptions import DataFetchError

    def _boom(_table: str) -> Any:
        raise DataFetchError("模拟：本环境没有共享行情仓")

    monkeypatch.setattr(pdc, "_table_store", _boom)
    pdc.reset_cache()
    raw = pdc.PlatformDataConnector()._raw_inputs("600036", "2026-09-29")  # noqa: SLF001
    assert raw["values"] == {}, "取不到就不许编值（更不许填 0）"
    assert raw["unavailable_reason"], "缺了必须给原因（'没量到'要看得见）"
    extra = pdc.PlatformDataConnector()._raw_inputs_extra(raw)  # noqa: SLF001
    assert extra["values"] == {} and extra["unavailable_reason"]
    assert "quant_daily_basic" in extra["unavailable_reason"]
