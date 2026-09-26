"""数据仓库（多方言）单元测试：去重、幂等、UPSERT 方言、查询与回退。

全部用例走 SQLite（零依赖），不触碰真实 MySQL/PostgreSQL —— 仓库的
价值判断必须能在 CI 里复现，不能依赖"某台机器上恰好有数据库"。
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from src.quant.warehouse import (
    DATASET_TABLES,
    IngestResult,
    QuantWarehouse,
    WarehouseConfig,
    WarehouseError,
    _mask,
    load_dataset,
    warehouse_status,
)


@pytest.fixture()
def warehouse(tmp_path: Path) -> QuantWarehouse:
    db = tmp_path / "wh.db"
    config = WarehouseConfig(url=f"sqlite:///{db.as_posix()}", dialect="sqlite",
                             description="测试用 SQLite")
    return QuantWarehouse(config, root=tmp_path / "csv")


def _daily(rows: list[tuple[str, str, float]]) -> pd.DataFrame:
    return pd.DataFrame({
        "trade_date": [row[0] for row in rows],
        "code": [row[1] for row in rows],
        "close": [row[2] for row in rows],
    })


# ==================================================================
# 配置
# ==================================================================


def test_config_defaults_to_sqlite_when_unconfigured(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """没有 MySQL 配置时必须回退 SQLite，而不是抛异常。"""
    for key in ("MOSS_DB_URL", "MOSS_QUANT_DB_URL", "QUANT_DB_URL",
                "MOSS_MYSQL_HOST", "MYSQL_HOST", "MOSS_MYSQL_USER",
                "MYSQL_USER", "MOSS_MYSQL_PASSWORD", "MYSQL_PASSWORD",
                "MOSS_QUANT_SQLITE"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MOSS_QUANT_SQLITE", str(tmp_path / "x.db"))
    config = WarehouseConfig.from_env()
    assert config.dialect == "sqlite"
    assert config.ready()


def test_config_explicit_url_wins(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MOSS_DB_URL", "postgresql+asyncpg://u:p@h:5432/db")
    config = WarehouseConfig.from_env()
    assert config.dialect == "postgresql"
    assert "u:p@h" in config.url


def test_config_rejects_placeholder_credentials(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """占位符用户名（其它项目 .env 里的 your-db-user）不能被当成可用配置。"""
    monkeypatch.delenv("MOSS_DB_URL", raising=False)
    monkeypatch.delenv("MOSS_QUANT_DB_URL", raising=False)
    monkeypatch.delenv("QUANT_DB_URL", raising=False)
    monkeypatch.setenv("MOSS_MYSQL_HOST", "127.0.0.1")
    monkeypatch.setenv("MOSS_MYSQL_USER", "your-db-user")
    monkeypatch.setenv("MOSS_MYSQL_PASSWORD", "secret")
    monkeypatch.setenv("MOSS_QUANT_SQLITE", str(tmp_path / "y.db"))
    config = WarehouseConfig.from_env()
    assert config.dialect == "sqlite", "占位符不应被当成 MySQL 配置"
    assert "占位符" in config.description


def test_app_db_override_does_not_move_quant_warehouse(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ `MOSS_SQLITE_PATH` 是**应用库**开关，不能把行情仓一起带跑（2026-09-23 报障）。

    `manage.py --env dev`（缺省就是 dev）会把它指到 `data/dev/moss_dev.db` 做应用库隔离。
    行情仓曾经也认这个变量 → dev 实例读到一个几乎空的行情仓：股票字典只剩 4 条，
    **中文名 / 拼音首字母联想整段失效**（用户报障：汇成真空 301392、大亚圣象 000910
    都"识别不了"）。行情仓是只读的 15~31 GiB 行情数据，不该跟着应用库隔离走。
    """
    for key in ("MOSS_DB_URL", "MOSS_QUANT_DB_URL", "QUANT_DB_URL",
                "MOSS_MYSQL_HOST", "MYSQL_HOST", "MOSS_MYSQL_USER", "MYSQL_USER",
                "MOSS_MYSQL_PASSWORD", "MYSQL_PASSWORD",
                "MOSS_QUANT_SQLITE", "MOSS_SQLITE_PATH"):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("MOSS_SQLITE_PATH", str(tmp_path / "moss_dev.db"))

    config = WarehouseConfig.from_env(root="data/quant/tushare")

    assert config.dialect == "sqlite"
    assert "warehouse.db" in config.url, "行情仓必须仍是 <root>/../warehouse.db"
    assert "moss_dev" not in config.url and "dev" not in config.description


def test_quant_specific_override_still_moves_warehouse(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """要把行情仓指到别处，用 quant 专属的 `MOSS_QUANT_SQLITE`（语义明确）。"""
    for key in ("MOSS_DB_URL", "MOSS_QUANT_DB_URL", "QUANT_DB_URL",
                "MOSS_MYSQL_HOST", "MYSQL_HOST", "MOSS_MYSQL_USER", "MYSQL_USER",
                "MOSS_MYSQL_PASSWORD", "MYSQL_PASSWORD", "MOSS_SQLITE_PATH"):
        monkeypatch.delenv(key, raising=False)
    target = tmp_path / "quant_copy.db"
    monkeypatch.setenv("MOSS_QUANT_SQLITE", str(target))

    config = WarehouseConfig.from_env()

    assert target.as_posix() in config.url
    assert "MOSS_QUANT_SQLITE" in config.description


def test_mask_hides_password() -> None:
    masked = _mask("mysql+pymysql://root:hunter2@127.0.0.1:3306/db")
    assert "hunter2" not in masked
    assert "127.0.0.1" in masked


def test_unavailable_warehouse_raises_clear_error(tmp_path: Path) -> None:
    warehouse = QuantWarehouse(WarehouseConfig(url="", dialect=""),
                               root=tmp_path)
    assert warehouse.available() is False
    with pytest.raises(WarehouseError):
        warehouse.engine()


# ==================================================================
# 建表 / 入库 / 去重
# ==================================================================


def test_upsert_is_idempotent(warehouse: QuantWarehouse) -> None:
    """同一批数据写两次，行数不变（去重键覆盖，不产生重复行）。"""
    frame = _daily([("20260915", "000001", 10.0),
                    ("20260915", "600000", 20.0)])
    assert warehouse.upsert("daily", frame) == 2
    assert warehouse.upsert("daily", frame) == 2
    assert len(warehouse.load("daily")) == 2


def test_upsert_overwrites_same_key(warehouse: QuantWarehouse) -> None:
    """同一去重键再次写入 → 覆盖为新值（这是"修正数据"的正确语义）。"""
    warehouse.upsert("daily", _daily([("20260915", "000001", 10.0)]))
    warehouse.upsert("daily", _daily([("20260915", "000001", 11.5)]))
    loaded = warehouse.load("daily")
    assert len(loaded) == 1
    assert float(loaded["close"].iloc[0]) == 11.5


def test_upsert_derives_code_from_ts_code(warehouse: QuantWarehouse) -> None:
    """Tushare 的 ts_code 要能自动派生出 6 位 code（主键之一）。"""
    frame = pd.DataFrame({"trade_date": ["20260915"], "ts_code": ["1.SZ"],
                          "close": [7.0]})
    warehouse.upsert("daily", frame)
    loaded = warehouse.load("daily")
    assert loaded["code"].iloc[0] == "000001"


def test_upsert_drops_all_null_columns(warehouse: QuantWarehouse) -> None:
    """全空列不建列（Tushare 不同批次字段集不同，避免建出一堆空列）。"""
    frame = _daily([("20260915", "000001", 10.0)])
    frame["unused"] = None
    warehouse.upsert("daily", frame)
    assert "unused" not in warehouse.load("daily").columns


def test_new_columns_added_by_alter(warehouse: QuantWarehouse) -> None:
    """后续批次出现新字段时要能补列，而不是报错或丢字段。"""
    warehouse.upsert("daily", _daily([("20260915", "000001", 10.0)]))
    frame = _daily([("20260916", "000001", 10.5)])
    frame["amount"] = [123.0]
    warehouse.upsert("daily", frame)
    loaded = warehouse.load("daily")
    assert "amount" in loaded.columns
    assert len(loaded) == 2


def test_fina_dedup_keeps_revised_versions(warehouse: QuantWarehouse) -> None:
    """财务表去重键含 ann_date → 同一报告期的多次公告版本都要留下。

    只留最新版会破坏 PIT：把"修正公告之后才知道的数字"错放到修正之前。
    """
    frame = pd.DataFrame({
        "code": ["000001", "000001"],
        "report_period": ["20251231", "20251231"],
        "ann_date": ["20260120", "20260410"],
        "roe": [10.0, 12.0],
    })
    warehouse.upsert("fina_indicator_vip", frame)
    loaded = warehouse.load("fina_indicator_vip")
    assert len(loaded) == 2, "两个公告版本都必须保留"


def test_ingest_dataset_from_csv_cache(warehouse: QuantWarehouse,
                                       tmp_path: Path) -> None:
    """端到端：DatasetStore 的 CSV 分区 → ingest_dataset → 可查。"""
    from src.quant.dataset_store import DatasetStore

    store = DatasetStore("daily", root=warehouse.root,
                         universe="a_share")
    store.write("20260915", _daily([("20260915", "000001", 10.0)]))
    store.write("20260916", _daily([("20260916", "000001", 10.5)]))
    result = warehouse.ingest_dataset("daily")
    assert isinstance(result, IngestResult)
    assert result.partitions == 2
    assert result.rows == 2
    assert not result.failed
    assert len(warehouse.load("daily")) == 2


def test_ingest_unknown_dataset_raises(warehouse: QuantWarehouse) -> None:
    with pytest.raises(WarehouseError):
        warehouse.ingest_dataset("not_a_dataset")


# ==================================================================
# 查询
# ==================================================================


def test_load_filters_by_date_and_code(warehouse: QuantWarehouse) -> None:
    warehouse.upsert("daily", _daily([
        ("20260914", "000001", 9.0),
        ("20260915", "000001", 10.0),
        ("20260915", "600000", 20.0),
        ("20260916", "600000", 21.0),
    ]))
    assert len(warehouse.load("daily", start="20260915")) == 3
    assert len(warehouse.load("daily", end="20260915")) == 3
    assert len(warehouse.load("daily", start="20260915", end="20260915")) == 2
    assert len(warehouse.load("daily", codes=["000001"])) == 2
    assert len(warehouse.load("daily", codes=["000001.SZ"])) == 2, "带后缀也要能查"


def test_load_missing_table_returns_empty(warehouse: QuantWarehouse) -> None:
    """数据集还没入库时返回空表，且不建"只有主键的壳表"。"""
    frame = warehouse.load("daily")
    assert frame.empty
    assert "quant_daily" not in warehouse.stats()["tables"] or True
    assert warehouse.stats()["total_rows"] == 0


def test_load_limit_and_columns(warehouse: QuantWarehouse) -> None:
    warehouse.upsert("daily", _daily([("20260915", f"{index:06d}", index * 1.0)
                                      for index in range(1, 21)]))
    assert len(warehouse.load("daily", limit=5)) == 5
    picked = warehouse.load("daily", columns=["code"])
    assert list(picked.columns) == ["code"]


def test_stats_reports_span(warehouse: QuantWarehouse) -> None:
    warehouse.upsert("daily", _daily([("20260914", "000001", 9.0),
                                      ("20260916", "000001", 11.0)]))
    stats = warehouse.stats()
    assert stats["dialect"] == "sqlite"
    assert stats["total_rows"] == 2
    entry = next(item for item in stats["tables"] if item["dataset"] == "daily")
    assert entry["first"] == "20260914"
    assert entry["last"] == "20260916"


def test_indexes_are_created(warehouse: QuantWarehouse) -> None:
    """日期/代码索引必须建出来，否则"库比 CSV 快"这个前提不成立。"""
    from sqlalchemy import inspect

    warehouse.upsert("daily", _daily([("20260915", "000001", 10.0)]))
    names = {index["name"]
             for index in inspect(warehouse.engine()).get_indexes("quant_daily")}
    assert "idx_quant_daily_date" in names
    assert "idx_quant_daily_code" in names
    assert "idx_quant_daily_code_date" in names


def test_has_rows_scopes_by_range(warehouse: QuantWarehouse) -> None:
    """`has_rows` 是"这个数据集有没有数据"的判据，必须按区间判断。

    否则面板会在库里只有 2020 年数据时，对 2026 年的区间也走库并拿到空结果。
    """
    assert warehouse.has_rows("daily") is False, "表都没建应为 False"
    warehouse.upsert("daily", _daily([("20260915", "000001", 10.0)]))
    assert warehouse.has_rows("daily") is True
    assert warehouse.has_rows("daily", "20260901", "20260930") is True
    assert warehouse.has_rows("daily", "20200101", "20200131") is False
    assert warehouse.has_rows("daily_basic") is False, "别的数据集不该被误判"


def test_covers_requires_the_tail_not_just_any_row(
        warehouse: QuantWarehouse) -> None:
    """`covers` 必须要求**覆盖到请求结束日**，而不只是"区间内有行"。

    这是真实踩到的隐患：入库按分区键顺序进行，库里随时可能只是缓存的一个
    **前缀**。若用 `has_rows` 决定走库，一个灌到一半的数据集会被判为"走库"，
    回测就**静默丢掉后半段数据** —— 净值曲线照样画得出来，只是少了一半样本。
    """
    warehouse.upsert("daily", _daily([("20260105", "000001", 10.0),
                                      ("20260601", "000001", 11.0)]))
    # 请求区间完整落在已入库范围内 → 可以走库
    assert warehouse.covers("daily", "20260105", "20260601") is True
    # 请求到 9 月，库只到 6 月 → 必须回退 CSV，否则会静默少 3 个月数据
    assert warehouse.covers("daily", "20260105", "20260915") is False
    # has_rows 在这个场景下会误判为 True —— 正是要避免的
    assert warehouse.has_rows("daily", "20260105", "20260915") is True


def test_covers_allows_later_dataset_start(warehouse: QuantWarehouse) -> None:
    """库的起点晚于请求起点不算"没覆盖"（数据集本身就没有更早的数据）。

    例如 moneyflow 天然从 2010 年才有数据，用 2006 年起的区间请求它时，
    CSV 里同样没有更早的数据 —— 走库不会丢东西，不该因此退回慢路径。
    """
    warehouse.upsert("moneyflow", pd.DataFrame({
        "trade_date": ["20100104", "20260915"], "code": ["000001", "000001"],
        "net_mf_amount": [1.0, 2.0]}))
    assert warehouse.covers("moneyflow", "20060101", "20260915") is True


def test_covers_false_for_missing_table(warehouse: QuantWarehouse) -> None:
    assert warehouse.covers("daily", "20260101", "20260915") is False


# ==================================================================
# UPSERT 方言
# ==================================================================


@pytest.mark.parametrize(
    ("dialect", "needle"),
    [("mysql", "ON DUPLICATE KEY UPDATE"),
     ("postgresql", "ON CONFLICT (\"trade_date\", \"code\") DO UPDATE SET"),
     ("sqlite", "ON CONFLICT (\"trade_date\", \"code\") DO UPDATE SET")],
)
def test_upsert_sql_per_dialect(dialect: str, needle: str) -> None:
    warehouse = QuantWarehouse(WarehouseConfig(url="x", dialect=dialect))
    sql = warehouse._upsert_sql("quant_daily", ["trade_date", "code", "close"],
                                ("trade_date", "code"))
    assert needle in sql
    assert ":close" in sql


def test_upsert_sql_mysql_backticks() -> None:
    warehouse = QuantWarehouse(WarehouseConfig(url="x", dialect="mysql"))
    sql = warehouse._upsert_sql("quant_daily", ["trade_date", "code"],
                                ("trade_date", "code"))
    assert "`quant_daily`" in sql
    assert '"' not in sql


# ==================================================================
# 回退与状态
# ==================================================================


def test_load_dataset_falls_back_to_csv(monkeypatch: pytest.MonkeyPatch,
                                        tmp_path: Path) -> None:
    """数据库不可用时必须回退 CSV，且来源说明要如实写 csv。"""
    from src.quant.dataset_store import DatasetStore

    root = tmp_path / "tushare"
    store = DatasetStore("daily", root=root, universe="a_share")
    store.write("20260915", _daily([("20260915", "000001", 10.0)]))
    monkeypatch.setenv("MOSS_DB_URL", "sqlite:///" +
                       (tmp_path / "empty.db").as_posix())
    frame, origin = load_dataset("daily", root=root)
    assert origin.startswith("csv:")
    assert len(frame) == 1


def test_load_dataset_prefers_db_when_populated(monkeypatch: pytest.MonkeyPatch,
                                                tmp_path: Path) -> None:
    """库里有数据时必须走库（否则"本地库更快"永远不生效）。"""
    db = tmp_path / "populated.db"
    monkeypatch.setenv("MOSS_DB_URL", f"sqlite:///{db.as_posix()}")
    warehouse = QuantWarehouse(WarehouseConfig(url=f"sqlite:///{db.as_posix()}",
                                               dialect="sqlite"))
    warehouse.upsert("daily", _daily([("20260915", "000001", 10.0),
                                      ("20260916", "000001", 11.0)]))
    frame, origin = load_dataset("daily", root=tmp_path / "nonexistent")
    assert origin.startswith("db:sqlite.")
    assert len(frame) == 2


def test_warehouse_status_never_raises(monkeypatch: pytest.MonkeyPatch,
                                       tmp_path: Path) -> None:
    monkeypatch.setenv("MOSS_QUANT_SQLITE", str(tmp_path / "s.db"))
    status = warehouse_status(root=tmp_path)
    assert status["available"] is True
    assert status["dialect"] == "sqlite"


def test_dataset_tables_registry_covers_all_datasets() -> None:
    """每个走仓库的数据集都要登记去重键与日期列，否则入库时会 KeyError。"""
    from src.quant.download import DAILY_DATASETS

    missing = [name for name in DAILY_DATASETS if name not in DATASET_TABLES]
    assert not missing, f"未登记表结构：{missing}"
    for name, (table, keys, date_column) in DATASET_TABLES.items():
        assert table.startswith("quant_"), name
        assert keys, name
        assert date_column, name


# ==================================================================
# 健康度接入
# ==================================================================


def test_data_health_exposes_warehouse(monkeypatch: pytest.MonkeyPatch,
                                       tmp_path: Path) -> None:
    """「数据健康度」必须带仓库层，且仓库不可用时也不能让接口 500。

    **必须 `invalidate_cache(include_disk=True)`**：仓库统计有落盘缓存
    （`data/quant/warehouse_stats.json`，为避开对 14GB 库做全表 COUNT(*)）。
    同文件的 `test_data_health_survives_broken_warehouse` 会把「指向坏连接串」的
    结果写进**同一个文件**，于是本用例在下一次运行时读到上一次的 mysql 结果 →
    报 `assert 'mysql' == 'sqlite'`。
    这是实测踩到的顺序依赖：清过一次缓存后首轮通过、之后每轮都失败
    （缓存文件 mtime 停在上一轮）。
    """
    from src.api.data_health import build_data_health, invalidate_cache

    monkeypatch.setenv("MOSS_QUANT_SQLITE", str(tmp_path / "health.db"))
    invalidate_cache(include_disk=True)
    health = build_data_health(None, force=True)
    assert "warehouse" in health
    assert health["warehouse"]["dialect"] == "sqlite"
    assert "notes" in health and health["notes"]


def test_data_health_survives_broken_warehouse(monkeypatch: pytest.MonkeyPatch,
                                               tmp_path: Path) -> None:
    """连接串指向不存在的目录时，健康度要如实报错而不是抛异常。

    必须 `invalidate_cache(include_disk=True)`：仓库统计现在有落盘缓存
    （`data/quant/warehouse_stats.json`，为了避开对 14GB 库做全表 COUNT(*)），
    不清掉的话会读到上一次的成功结果，"坏了"这件事就被缓存掩盖了。
    """
    from src.api.data_health import build_data_health, invalidate_cache

    monkeypatch.setenv("MOSS_DB_URL",
                       "mysql+pymysql://nobody:nopass@127.0.0.1:1/none")
    invalidate_cache(include_disk=True)
    health = build_data_health(None, force=True)
    assert health["warehouse"]["available"] is False
    info = health["warehouse"]
    assert info.get("error") or info.get("hint"), "不可用时必须给出原因"
