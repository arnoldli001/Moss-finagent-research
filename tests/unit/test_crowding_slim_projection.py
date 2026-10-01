"""拥挤度「展示瘦身投影」的单元守卫（`CHG-0137`）。

## 为什么需要这一组

用户报障「打开单个概念的历史拥挤度数据的图，**十几秒才出数据**」，
根因**不是数据库**：后端全链路 **13 ms**，而明文 424 KB / gzip 66 KB，
在 ~4.6 KB/s 的劣化隧道上要 **14 秒**（口径见 `docs/PRD.md` §22）。

修复是"展示路径只发前端真正消费的列 + 浮点按显示精度取整"。这组判据守三件事：

1. **`slim=True` 的列必须与前端类型定义一一对应** —— 少了前端读到 `undefined`，
   多了就是白传字节（本轮的病根）；
2. **精度必须被真正执行** —— 这是收益最大的一步：取整让 gzip 重新有效
   （66,507 B → 33,491 B，砍半）。精度一旦退回完整 double，压比立刻恶化；
3. **默认路径不许被瘦身** —— `refresh.py` 有两处调用**依赖完整列**
   （重算历史水位、拉完整历史）。这条是**最危险的**：改错了不会报错，
   只会让水位悄悄算错。

第 3 条同时用行为判据（`slim=False` 仍含 `id`/`created_at`/`updated_at`）
和**语法树判据**（路由必须显式传 `slim=True`）两头钉住。
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from src.sector_crowding import db

ROUTE_FILE = (Path(__file__).resolve().parents[2]
              / "src" / "api" / "routes" / "sector_crowding.py")


@pytest.fixture
def conn(tmp_path: Path) -> sqlite3.Connection:
    connection = db.get_db_connection(path=tmp_path / "crowding.db")
    db.init_tables(connection)
    yield connection
    connection.close()


def _seed(conn: sqlite3.Connection, code: str = "885927.TI",
          days: int = 30) -> None:
    """写入带**完整精度**的行（模拟入库时的 19~20 位尾数）。"""
    rows = [{
        "trade_date": f"2026{1 + i // 28:02d}{1 + i % 28:02d}",
        "sector_code": code,
        "sector_name": "PCB概念",
        # 刻意给"脏"精度：取整判据必须能看出来
        "sector_amount": 402367923.33051234567,
        "market_amount": 1653132781025.09012345,
        "raw_crowding": 0.00024339722008355307,
        "ma5_crowding": 0.00023728136044507660,
        "water_level": 0.86338812345678,
    } for i in range(days)]
    db.upsert_sector_crowding(conn, rows)


# ------------------------------------------------------- ① 列契约

def test_slim_columns_match_frontend_type(conn: sqlite3.Connection) -> None:
    """`SLIM_COLUMNS` 必须与前端 `CrowdingRow` 逐字一致。

    前端类型定义在 `web/src/sectorCrowdingApi.ts` 的 `CrowdingRow`。
    两边漂移的两个方向都坏：**少了** → 图表画不出来（读 `undefined`）；
    **多了** → 白传字节（本轮病根：28.9% 的明文是前端从不读的列）。
    """
    expected = {
        "trade_date", "sector_code", "sector_name", "sector_amount",
        "market_amount", "raw_crowding", "ma5_crowding", "water_level",
    }
    assert set(db.SLIM_COLUMNS) == expected, (
        "SLIM_COLUMNS 与前端 CrowdingRow 漂移了 —— "
        "改这里必须同步改 web/src/sectorCrowdingApi.ts")


def test_slim_drops_fields_the_frontend_never_reads(
        conn: sqlite3.Connection) -> None:
    """三个白传列必须被摘掉（实测合计 28.9% 的明文）。"""
    _seed(conn)
    row = db.query_sector_crowding(conn, "885927.TI", slim=True)[0]
    for banned in ("id", "created_at", "updated_at"):
        assert banned not in row, (
            f"`{banned}` 又被发出去了 —— 前端 CrowdingRow 里没有它，"
            f"属于白传字节（见 docs/PRD.md §22.3）")


def test_slim_row_keys_are_exactly_slim_columns(
        conn: sqlite3.Connection) -> None:
    """行的键集合必须**恰好**等于 `SLIM_COLUMNS`（不是"包含"）。"""
    _seed(conn)
    row = db.query_sector_crowding(conn, "885927.TI", slim=True)[0]
    assert set(row) == set(db.SLIM_COLUMNS)


# ------------------------------------------------------- ② 精度

def test_slim_rounds_floats_to_display_precision(
        conn: sqlite3.Connection) -> None:
    """浮点必须按 `SLIM_PRECISION` 取整 —— 这是 gzip 砍半的来源。"""
    _seed(conn)
    row = db.query_sector_crowding(conn, "885927.TI", slim=True)[0]
    for column, digits in db.SLIM_PRECISION.items():
        value = row[column]
        assert isinstance(value, float), f"{column} 应为 float"
        assert value == round(value, digits), (
            f"`{column}` = {value!r} 未按 {digits} 位取整 —— "
            f"完整 double 尾数是高熵的，gzip 压不动（见 docs/PRD.md §22.4）")


def test_slim_actually_shortens_float_repr(conn: sqlite3.Connection) -> None:
    """取整必须**真的**缩短字符串形态（不是只在数值上相等）。

    判据取 `repr()` 长度而不是数值 —— 因为进 JSON 的是字符串形态，
    体积取决于它。原值 19 位小数，取整后必须显著更短。
    """
    _seed(conn)
    row = db.query_sector_crowding(conn, "885927.TI", slim=True)[0]
    digits = len(repr(row["raw_crowding"]).split(".")[-1])
    assert digits <= db.SLIM_PRECISION["raw_crowding"], (
        f"raw_crowding 仍有 {digits} 位小数")


def test_slim_keeps_none_water_level(conn: sqlite3.Connection) -> None:
    """`water_level = None` 是**有语义的**（数据不足），取整不许把它变成 0。

    把"不知道"画成"不拥挤"是本项目登记过的口径错误
    （`docs/PRD.md`：NULL 不参与告警 ——「不知道」≠「不拥挤」）。
    """
    rows = [{
        "trade_date": "20260101", "sector_code": "885927.TI",
        "sector_name": "PCB概念", "sector_amount": 1.0,
        "market_amount": 100.0, "raw_crowding": 0.01,
        "ma5_crowding": 0.01, "water_level": None,
    }]
    db.upsert_sector_crowding(conn, rows)
    got = db.query_sector_crowding(conn, "885927.TI", slim=True)[0]
    assert got["water_level"] is None, "None 被改成了别的值"


# ------------------------------------------------------- ③ 默认路径不许被瘦身

def test_default_is_full_row_and_keeps_water_level_columns(
        conn: sqlite3.Connection) -> None:
    """★ `slim=False`（默认）必须是**完整行** —— 水位重算依赖这些列。

    `refresh.py:317`（重算历史水位）与 `:449`（拉完整历史）都调这个函数。
    它们如果拿到瘦身行，水位会**静默算错**（最难发现的一类故障）。
    """
    _seed(conn, days=3)
    row = db.query_sector_crowding(conn, "885927.TI")[0]
    for column in ("id", "created_at", "updated_at"):
        assert column in row, (
            f"默认路径丢了 `{column}` —— refresh.py 的水位重算依赖完整行")
    assert row["id"] is not None


def test_default_does_not_round(conn: sqlite3.Connection) -> None:
    """默认路径**不许**取整 —— 入库原值要原样给回水位重算。"""
    _seed(conn, days=3)
    row = db.query_sector_crowding(conn, "885927.TI")[0]
    assert row["raw_crowding"] == 0.00024339722008355307, (
        "默认路径被取整了 —— 水位重算需要原始精度")


def test_refresh_callers_do_not_opt_into_slim() -> None:
    """★ `refresh.py` 的两处调用**不许**传 `slim=True`（语法树判据）。

    行为判据（上面两条）只能证明"默认是完整的"，证明不了
    "没人把 refresh 改成 `slim=True`" —— 那正是会静默算错水位的改法。
    """
    source = (Path(__file__).resolve().parents[2]
              / "src" / "sector_crowding" / "refresh.py").read_text(
                  encoding="utf-8")
    tree = ast.parse(source)
    found = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else "")
        if name != "query_sector_crowding":
            continue
        found += 1
        bad = [kw for kw in node.keywords
               if kw.arg == "slim" and getattr(kw.value, "value", None) is True]
        assert not bad, (
            f"refresh.py:{node.lineno} 对 query_sector_crowding 传了 slim=True —— "
            f"水位重算需要完整精度，这会**静默**算错水位")
    assert found >= 2, (
        f"只找到 {found} 处 refresh 调用（预期 2 处）—— "
        f"判据可能已经取错节点（恒绿）")


def test_detail_route_opts_into_slim() -> None:
    """★ 详情接口**必须**显式传 `slim=True`（否则瘦身静默失效）。

    这条防的是"改回去"：把 `slim=True` 删掉不会有任何报错，
    只是体积悄悄涨回 66 KB / 14 秒 —— 正是本次报障的现场。
    """
    tree = ast.parse(ROUTE_FILE.read_text(encoding="utf-8"))
    target = None
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "_sector_detail_sync":
            target = node
            break
    assert target is not None, "找不到 _sector_detail_sync"

    calls = [n for n in ast.walk(target)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "query_sector_crowding"]
    assert calls, "_sector_detail_sync 没有调用 query_sector_crowding"
    ok = any(kw.arg == "slim" and getattr(kw.value, "value", None) is True
             for call in calls for kw in call.keywords)
    assert ok, (
        "_sector_detail_sync 必须显式传 slim=True —— 去掉它不会报错，"
        "只会让响应体积涨回 424 KB / 劣化隧道上 14 秒（docs/PRD.md §22）")


def test_detail_route_reads_meta_by_primary_key() -> None:
    """元数据必须**按主键**取一行，不许读整表再线性查找。

    原先 `next((i for i in db.query_sector_meta(conn) ...))` 为了取一行
    读出 2517 行 —— 白付 5.9 ms，且这个形状会随板块数继续变差。
    """
    tree = ast.parse(ROUTE_FILE.read_text(encoding="utf-8"))
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "_sector_detail_sync"), None)
    assert target is not None

    calls = [n for n in ast.walk(target)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "query_sector_meta"]
    assert calls, "_sector_detail_sync 没有调用 query_sector_meta"
    ok = any(kw.arg == "sector_code" and
             getattr(kw.value, "id", None) == "sector_code"
             for call in calls for kw in call.keywords)
    assert ok, (
        "query_sector_meta 必须传 sector_code= 走主键直查 —— "
        "读整表 2517 行只为取一行是白付（CHG-0137）")
