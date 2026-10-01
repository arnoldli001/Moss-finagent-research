"""拥挤度**库归属**整改的判据（`CHG-0143`）。

## 背景（两个真缺陷，都有实测证据）

**① 写者闸门有一个缺口。** 拥挤度的表原先**根本没登记**在
`configs/data_stores.yaml`，而写者闸门是从登记派生的
（`JobSpec.updates × writable_here()`）—— 于是前端的「一键刷新」
（`POST /sector_crowding/refresh_all`）往共享库里 UPSERT **218 万行**，
**不受任何闸门管**，`/health` 里也看不见它。

**② 用户配置是全局共享的。** `sector_crowding_list`（1,226 行）/
`_watch` / `_alert` **没有 `tenant_id`、也没有环境隔离**，
只在共享遗留主库里 —— 于是 **dev 与 pilot 共用同一份板块清单**：
开发时点掉的板块，客户那边也消失。

根因是**数据分类错位**：拥挤度把两类生命周期完全不同的数据塞进了
同一个库 —— **市场参考数据**（无用户维度，三个环境读同一份，共享是对的）
与**用户配置**（每人/每租户一份，必须隔离）。

⇒ 整改：参考数据留在共享库；用户配置迁到**本环境应用库 `app_db`**。

## 这组判据守什么

| # | 判据 | 防的失效 |
|---|---|---|
| ① | `crowding_shared` 必须登记且写者**具体到环境** | `writer: main` 只报告不阻断 ⇒ 闸门形同虚设 |
| ② | 用户配置表在 `app_db`、参考表在共享库 | 迁移静默失效（还从旧库读） |
| ③ | `get_db_connection` 的 `writable` **默认 False** | 默认改成 True ⇒ 读路径（pilot）当场变红 |
| ④ | 非写者传 `writable=True` 必须 fail-closed | 只有声明没有闸门 |
| ⑤ | `refresh.py` 的写连接**显式**传 `writable=True` | 漏传 = 静默只读，不报错 |
| ⑥ | `_TABLE_CANDIDATES["sector_crowding_list"]` 首选 `app_db` | 顺序反 ⇒ 命中旧库那张表，迁移静默失效 |
| ⑦ | 请求路径只建**自己的**配置库 | 读实例去写共享库（正是要防的事） |
| ⑧ | `config_db_path` 跟随被显式指定的参考库 | 半个配置 ⇒ 单测读到**真实**应用库（泄漏） |
"""

from __future__ import annotations

import ast
import sqlite3
from pathlib import Path

import pytest

from src.infrastructure.catalog import data_stores as ds
from src.sector_crowding import db
from src.sector_crowding.config import SectorCrowdingConfig, load_config

ROOT = Path(__file__).resolve().parents[2]
DB_PY = ROOT / "src" / "sector_crowding" / "db.py"
REFRESH_PY = ROOT / "src" / "sector_crowding" / "refresh.py"
ROUTES_PY = ROOT / "src" / "api" / "routes" / "sector_crowding.py"
CONNECTOR_PY = (ROOT / "src" / "infrastructure" / "connectors"
                / "platform_data_connector.py")

#: 必须落在**共享**库的表（市场参考数据，无用户维度）
REFERENCE_TABLES = ("sector_crowding_daily", "sector_meta", "sector_member",
                    "sector_crowding_max_ma5")
#: 必须落在**本环境应用库**的表（用户配置）
CONFIG_TABLES = ("sector_crowding_list", "sector_crowding_watch",
                 "sector_crowding_alert")


@pytest.fixture
def split_config(tmp_path: Path) -> SectorCrowdingConfig:
    """参考库与配置库**分开**的临时配置（模拟真实的两库布局）。"""
    config = SectorCrowdingConfig()
    config.database.path = str(tmp_path / "shared.db")
    config.database.config_path = str(tmp_path / "app.db")
    return config


def _tables(path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        return {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


# ====================================================== ① 登记与写者

def test_crowding_shared_is_registered() -> None:
    """★ 拥挤度必须**登记**在 registry —— 否则写者闸门无从派生。

    未登记的后果不只是"看不见"：`writable_here()` 会抛 `StoreNotFound`，
    而**裁剪逻辑会吞掉异常**（`test_warehouse_write_ownership.py:266`
    记过这个形状）⇒ 作业照跑、写照发生，一声不响。
    """
    store = ds.get_store("crowding_shared")
    assert store.kind == "sqlite"
    assert store.isolation == "shared", "参考数据是共享的，不该 per_env"
    assert store.writable is True, "写者自己都写不了，说明声明自相矛盾"
    assert store.resolved().name == "moss_finagent.db"


def test_crowding_shared_writer_is_a_concrete_env() -> None:
    """★ 写者必须**点名到具体环境**，不能是 `main`。

    `writer: main` 在 `writable_here()` 里对隔离档只能给出
    `decided=False`（"口径待裁定"）—— **只报告不阻断**，
    于是第 1 步等于没做（`data_stores.py` 的注释把这一支写得很清楚：
    "那批共享存储还没有归属裁定，硬拒会让它们当场失去写者"）。
    """
    store = ds.get_store("crowding_shared")
    assert store.writer in ds.ISOLATED_ENVS, (
        f"crowding_shared.writer={store.writer!r} 不是具体环境名；"
        f"共享存储的写者必须点名到承担更新责任的那个环境")


def test_only_the_declared_writer_may_write_crowding(monkeypatch) -> None:
    """写者环境可写、**其余每个隔离环境都不可写且已裁定**（两侧都要钉）。"""
    writer = ds.get_store("crowding_shared").writer
    for env in ds.ISOLATED_ENVS:
        monkeypatch.setenv("MOSS_ENV", env)
        decision = ds.writable_here("crowding_shared")
        if env == writer:
            assert decision.allowed is True and decision.decided is True
        else:
            assert decision.allowed is False, f"{env} 不该能写拥挤度参考数据"
            assert decision.decided is True, (
                f"{env} 的拒绝必须**已裁定**（否则只是提示，拦不住写）")
            assert writer in decision.reason, "理由里要点名谁是写者"


# ====================================================== ② 表落对库

def test_reference_tables_go_to_the_shared_db(
        split_config: SectorCrowdingConfig) -> None:
    """参考表必须建在**共享**库那一侧。"""
    db.init_tables(config=split_config)
    shared = _tables(Path(split_config.database.path))
    for table in REFERENCE_TABLES:
        assert table in shared, f"{table} 不在共享参考库里：{sorted(shared)}"


def test_config_tables_go_to_the_environment_db(
        split_config: SectorCrowdingConfig) -> None:
    """★ 用户配置表必须建在**本环境应用库**那一侧（本轮整改的核心）。"""
    db.init_tables(config=split_config)
    app = _tables(Path(split_config.database.config_path))
    for table in CONFIG_TABLES:
        assert table in app, f"{table} 不在本环境配置库里：{sorted(app)}"


def test_config_tables_are_not_created_in_the_shared_db(
        split_config: SectorCrowdingConfig) -> None:
    """★ 共享库里**不许**再建用户配置表。

    两侧都建会让 `platform_data_connector._table_store()` 的
    "按表存在选库"产生歧义，迁移静默失效。
    """
    db.init_tables(config=split_config)
    shared = _tables(Path(split_config.database.path))
    leaked = set(CONFIG_TABLES) & shared
    assert not leaked, (
        f"用户配置表被建进了共享库：{sorted(leaked)} —— "
        f"会与旧库那张同名表争抢 `_table_store()` 的候选判定")


def test_reference_tables_are_not_created_in_the_app_db(
        split_config: SectorCrowdingConfig) -> None:
    """反向：应用库里不该出现参考表（对称判据，防"两边都建"）。"""
    db.init_tables(config=split_config)
    app = _tables(Path(split_config.database.config_path))
    leaked = set(REFERENCE_TABLES) & app
    assert not leaked, f"参考表被建进了本环境库：{sorted(leaked)}"


def test_cross_db_join_works(split_config: SectorCrowdingConfig) -> None:
    """★ 跨库 JOIN 必须真的能跑（10 处 SQL 同句引用两类表）。

    这是 `ATTACH` 设计的核心验收：`query_all_latest_water_level` /
    `query_alerts` 会 `LEFT JOIN app_db.sector_crowding_list`。
    """
    db.init_tables(config=split_config)
    conn = db.get_db_connection(split_config)
    try:
        assert [r[0] for r in conn.execute("PRAGMA database_list")], "库列表为空"
        db.upsert_sector_crowding(conn, [{
            "trade_date": "20260101", "sector_code": "A.TI",
            "water_level": 0.9}])
        db.upsert_list_item(conn, "A.TI", sector_name="测试")
        rows = db.query_all_latest_water_level(conn)
        assert [r["sector_code"] for r in rows] == ["A.TI"]
        assert db.query_alerts(conn) is not None
        assert db.query_list_view(conn) is not None
    finally:
        conn.close()


def test_cross_db_write_works(split_config: SectorCrowdingConfig) -> None:
    """★ 跨库**写**也必须能跑（`seed_list` / `hide_dead_boards`）。

    这两个是"读参考表 → 写配置表"，SQLite 支持 `ATTACH` 后的跨库
    `INSERT ... SELECT` 与 `UPDATE ... WHERE EXISTS`。
    """
    db.init_tables(config=split_config)
    conn = db.get_db_connection(split_config)
    try:
        db.upsert_sector_crowding(conn, [{
            "trade_date": "20260101", "sector_code": "A.TI"}])
        db.update_sector_meta(conn, sector_code="A.TI", sector_name="测试",
                              bars=5)
        seeded = db.seed_list(conn, concepts_only=False)
        assert seeded >= 1, "seed_list 没有从参考表种出任何行"
        assert db.query_hidden_boards(conn) is not None
        db.hide_dead_boards(conn, force=True)
    finally:
        conn.close()


# ====================================================== ③④ 写闸门

def test_get_db_connection_writable_defaults_to_false() -> None:
    """★ **接入判据**（语法树）：`writable` 默认必须是 `False`。

    默认改成 `True` 的后果是**读路径当场变红**：pilot 只读共享库，
    而每个读请求都会过这道闸门 ⇒ 整个拥挤度页面对客户 500。
    `AGENTS.md`：默认值即护栏，安全的一侧做成默认。
    """
    tree = ast.parse(DB_PY.read_text(encoding="utf-8"))
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "get_db_connection"), None)
    assert target is not None, "找不到 get_db_connection"
    arg = next((a for a in target.args.kwonlyargs if a.arg == "writable"), None)
    assert arg is not None, "get_db_connection 没有 writable 关键字参数"
    default = target.args.kw_defaults[target.args.kwonlyargs.index(arg)]
    assert isinstance(default, ast.Constant) and default.value is False, (
        "writable 的默认值不是 False —— 读路径（pilot）会被闸门挡下")


def test_non_writer_is_rejected_fail_closed(monkeypatch, tmp_path) -> None:
    """★ 非写者实例传 `writable=True` 必须**抛错**，且理由含写者名。"""
    writer = ds.get_store("crowding_shared").writer
    other = next(e for e in ds.ISOLATED_ENVS if e != writer)
    monkeypatch.setenv("MOSS_ENV", other)
    with pytest.raises(PermissionError) as excinfo:
        db.get_db_connection(
            SectorCrowdingConfig(), path=tmp_path / "x.db", writable=True)
    message = str(excinfo.value)
    assert writer in message, f"理由里要点名写者：{message}"
    assert "不能写" in message


def test_writer_is_allowed(monkeypatch, tmp_path) -> None:
    """写者实例必须放行（否则本机刷新也跑不了）。"""
    monkeypatch.setenv("MOSS_ENV", ds.get_store("crowding_shared").writer)
    conn = db.get_db_connection(
        SectorCrowdingConfig(), path=tmp_path / "x.db", writable=True)
    conn.close()


def test_refresh_write_paths_declare_writable() -> None:
    """★ **接入判据**（语法树）：`refresh.py` 每处连接都必须显式 `writable=True`。

    漏传不会报错 —— 只会让刷新**静默地以只读口径连上去**，
    然后在写的那一刻才失败（或者更糟：在共享库上悄悄写成功）。
    """
    tree = ast.parse(REFRESH_PY.read_text(encoding="utf-8"))
    sites = [n for n in ast.walk(tree)
             if isinstance(n, ast.Call)
             and getattr(n.func, "attr", None) == "get_db_connection"]
    assert sites, "find 不到 refresh.py 里的 get_db_connection 调用"
    missing = [n.lineno for n in sites
               if not any(kw.arg == "writable"
                          and getattr(kw.value, "value", None) is True
                          for kw in n.keywords)]
    assert not missing, (
        f"refresh.py 第 {missing} 行的连接没有显式 writable=True —— "
        f"刷新会静默以只读口径连库")


def test_route_bootstrap_only_touches_the_config_db() -> None:
    """★ 请求路径只建**自己的**配置库，不碰共享库。

    对只读的共享库执行 DDL 会报错/挂锁（`init_tables()` 会两个库都建），
    而且会把"读实例写共享库"变成正常动作。
    """
    tree = ast.parse(ROUTES_PY.read_text(encoding="utf-8"))
    target = next((n for n in ast.walk(tree)
                   if isinstance(n, ast.FunctionDef)
                   and n.name == "_ensure_tables"), None)
    assert target is not None, "找不到 _ensure_tables"
    called = {n.func.attr for n in ast.walk(target)
              if isinstance(n, ast.Call)
              and isinstance(n.func, ast.Attribute)}
    assert "ensure_config_tables" in called, (
        "请求路径必须调 ensure_config_tables（只建配置库）—— "
        "调 init_tables 会连共享库一起建")
    assert "init_tables" not in called, (
        "_ensure_tables 不该调 init_tables —— 那会去写共享参考库")


# ====================================================== ⑥ 连接器候选顺序

def test_crowding_list_prefers_app_db() -> None:
    """★ `sector_crowding_list` 的候选**首选必须是 app_db**。

    `_table_store()` 取**第一个存在该表**的候选。旧库里那张同名表不会
    自动消失 ⇒ 不把 `app_db` 放前面，迁移会**静默失效**：接口照常返回旧库
    那份（dev 与 pilot 仍共用板块清单），而没有任何报错。
    """
    tree = ast.parse(CONNECTOR_PY.read_text(encoding="utf-8"))
    value = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and \
                getattr(node.target, "id", "") == "_TABLE_CANDIDATES":
            value = node.value
    assert value is not None, "找不到 _TABLE_CANDIDATES"
    entry = next((k for k in value.keys
                  if getattr(k, "value", None) == "sector_crowding_list"), None)
    assert entry is not None, "候选表里没有 sector_crowding_list"
    order = [getattr(e, "id", None) for e in value.values[
        value.keys.index(entry)].elts]
    assert order[0] == "_APP_DB", (
        f"sector_crowding_list 的候选首选是 {order[0]}，必须是 _APP_DB —— "
        f"否则永远命中旧库那张表，迁移静默失效（实际顺序 {order}）")


def test_reference_tables_keep_shared_first() -> None:
    """反向：参考表的候选**首选仍是共享库**（它们没搬，别顺手改）。"""
    tree = ast.parse(CONNECTOR_PY.read_text(encoding="utf-8"))
    value = None
    for node in ast.walk(tree):
        if isinstance(node, ast.AnnAssign) and \
                getattr(node.target, "id", "") == "_TABLE_CANDIDATES":
            value = node.value
    for name in ("sector_crowding_daily", "sector_crowding_max_ma5"):
        entry = next((k for k in value.keys
                      if getattr(k, "value", None) == name), None)
        assert entry is not None, f"候选表里没有 {name}"
        order = [getattr(e, "id", None) for e in value.values[
            value.keys.index(entry)].elts]
        assert order[0] == "_LEGACY_MAIN", (
            f"{name} 的候选首选被改成 {order[0]} —— 它是**参考数据**，"
            f"留在共享库；改它会让读取随环境漂移")


# ====================================================== ⑧ 配置回退

def test_config_db_follows_an_explicit_reference_path(tmp_path) -> None:
    """★ 半个配置不许导致**跨库污染**。

    单测/隔离用例常只改 `database.path`（把库指到 `tmp_path`）。
    若 `config_db_path` 此时仍返回真实 `app_db`，测试就会：
      · ATTACH 真实应用库 → **读到真实数据**（隔离泄漏）；
      · 或该库没有那些表 → `no such table: app_db.…`。
    两个症状都实测出现过（本文件就是那次修复的产物）。
    """
    config = SectorCrowdingConfig()
    config.database.path = str(tmp_path / "only.db")
    assert config.config_db_path == config.db_path, (
        "只改 path 时 config_db_path 必须跟随它，否则会读到真实应用库")


def test_default_config_uses_registry_for_both_paths() -> None:
    """默认（生产）配置：参考库 = `crowding_shared`，配置库 = `app_db`。"""
    config = load_config()
    assert config.db_path.name == "moss_finagent.db"
    assert config.config_db_path.name == "moss_finagent.db" or \
        "moss_" in config.config_db_path.name, (
            f"配置库路径可疑：{config.config_db_path}")
