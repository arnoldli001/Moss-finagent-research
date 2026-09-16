"""做T权重档案仓储单测（dim_intraday_profile 表）。

这一支是"用户资产"的落库路径，重点钉住四件事：
  1. 同 code 重复保存**更新而不是新增**，且保留 created_at（否则"什么时候建的"就丢了）；
  2. 非法代码/未知字段名必须**入库即报错**，坏数据不能进库；
  3. 老库缺列要能自动补（字段会长大的档案必备）；
  4. 从库里读出来的覆盖项能真的作用到配置上（端到端，不是只测 SQL）。
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from src.core.exceptions import ConfigError
from src.domain.intraday.models import (
    SOURCE_AUTO_CHARACTER,
    SOURCE_MANUAL,
    IntradayProfile,
)
from src.infrastructure.repositories.intraday_profile_sqlite_repo import (
    TABLE,
    IntradayProfileSqliteRepository,
)


@pytest.fixture
def db_path(tmp_dir: str) -> str:
    return os.path.join(tmp_dir, "test_profiles.db")


@pytest.fixture
def repo(db_path: str):
    return IntradayProfileSqliteRepository(db_path)


def _profile(code: str = "300308", **kwargs) -> IntradayProfile:
    payload = {
        "code": code, "name": "中际旭创",
        "weights": {"chan": 20.0, "boll": 6.0},
        "thresholds": {"action": 35.0},
        "levels": {"stop_loss_pct": 2.0, "atr_stop_mult": 0.8},
    }
    payload.update(kwargs)
    return IntradayProfile(**payload)


@pytest.mark.asyncio
async def test_schema_is_created_and_idempotent(repo, db_path: str) -> None:
    await repo.ensure_schema()
    await repo.ensure_schema()  # 幂等
    with sqlite3.connect(db_path) as conn:
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    assert TABLE in names


@pytest.mark.asyncio
async def test_upsert_and_get_round_trip(repo) -> None:
    saved = await repo.upsert(_profile())
    assert saved.code == "300308"
    assert saved.created_at and saved.updated_at
    loaded = await repo.get("300308")
    assert loaded is not None
    assert loaded.weights == {"chan": 20.0, "boll": 6.0}
    assert loaded.thresholds == {"action": 35.0}
    assert loaded.levels["atr_stop_mult"] == pytest.approx(0.8)
    assert loaded.name == "中际旭创"
    assert loaded.source == SOURCE_MANUAL


@pytest.mark.asyncio
async def test_repeated_upsert_updates_instead_of_duplicating(repo) -> None:
    first = await repo.upsert(_profile())
    second = await repo.upsert(_profile(name="改名了", note="第二次",
                                        weights={"chan": 25.0}))
    items = await repo.list()
    assert len(items) == 1, "同 code 必须原地更新"
    assert second.weights == {"chan": 25.0}
    assert second.name == "改名了"
    assert second.note == "第二次"
    # created_at 保留，updated_at 推进
    assert second.created_at == first.created_at
    assert second.updated_at >= first.updated_at


@pytest.mark.asyncio
async def test_get_missing_returns_none(repo) -> None:
    assert await repo.get("600036") is None


@pytest.mark.asyncio
async def test_list_orders_by_updated_at_desc(repo) -> None:
    await repo.upsert(_profile("300308"))
    await repo.upsert(_profile("600036", name="招商银行"))
    await repo.upsert(_profile("000001", name="平安银行"))
    items = await repo.list()
    assert len(items) == 3
    stamps = [item.updated_at for item in items]
    assert stamps == sorted(stamps, reverse=True)


@pytest.mark.asyncio
async def test_list_respects_limit(repo) -> None:
    for index in range(5):
        await repo.upsert(_profile(f"60000{index}", name=f"票{index}"))
    assert len(await repo.list(limit=2)) == 2


@pytest.mark.asyncio
async def test_delete_is_idempotent(repo) -> None:
    await repo.upsert(_profile())
    assert await repo.delete("300308") is True
    assert await repo.delete("300308") is False
    assert await repo.get("300308") is None


@pytest.mark.asyncio
async def test_daily_weights_and_character_snapshot_persist(repo) -> None:
    await repo.upsert(_profile(
        daily_weights={"trend": 26.0, "chan_daily": 18.0},
        daily_thresholds={"action": 30.0, "hint": 18.0},
        template="trend", source=SOURCE_AUTO_CHARACTER,
        character_profile={"grade": "活跃", "regime": "swing", "t_friendly": 68}))
    loaded = await repo.get("300308")
    assert loaded is not None
    assert loaded.daily_weights == {"trend": 26.0, "chan_daily": 18.0}
    assert loaded.daily_thresholds["action"] == pytest.approx(30.0)
    assert loaded.template == "trend"
    assert loaded.source == SOURCE_AUTO_CHARACTER
    assert loaded.character_profile["t_friendly"] == 68


@pytest.mark.parametrize("bad_code", ["30030", "3003088", "abcdef", "", " 30030"])
def test_invalid_code_is_rejected(bad_code: str) -> None:
    with pytest.raises(ConfigError, match="6位数字"):
        _profile(bad_code)


def test_unknown_source_is_rejected() -> None:
    with pytest.raises(ConfigError, match="未知的档案来源"):
        _profile(source="whatever")


@pytest.mark.asyncio
async def test_missing_columns_are_added_automatically(db_path: str) -> None:
    """模拟"老库"：先建一张缺列的旧表，仓储必须能自动补列后正常读写。"""
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"CREATE TABLE {TABLE} (code TEXT PRIMARY KEY, name TEXT,"
            " weights_json TEXT, thresholds_json TEXT, levels_json TEXT,"
            " source TEXT, created_at TEXT, updated_at TEXT)")
        conn.execute(
            f"INSERT INTO {TABLE} (code, name, weights_json, source,"
            " created_at, updated_at) VALUES ('600036','老档案','{}','manual',"
            " '2026-01-01T00:00:00','2026-01-01T00:00:00')")
    repo = IntradayProfileSqliteRepository(db_path)
    await repo.ensure_schema()
    old = await repo.get("600036")
    assert old is not None and old.name == "老档案"
    # 补列后新字段可写可读
    await repo.upsert(_profile("300308", daily_weights={"trend": 20.0}))
    loaded = await repo.get("300308")
    assert loaded is not None and loaded.daily_weights == {"trend": 20.0}


@pytest.mark.asyncio
async def test_corrupt_json_column_does_not_break_list(repo, db_path: str) -> None:
    await repo.upsert(_profile())
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            f"UPDATE {TABLE} SET weights_json = '{{not json' WHERE code = '300308'")
    items = await repo.list()
    assert len(items) == 1
    assert items[0].weights == {}


@pytest.mark.asyncio
async def test_repository_without_schema_sync_still_works(repo) -> None:
    """直接 upsert（不先 ensure_schema）也必须自动建表 —— 装配顺序不该是坑。"""
    saved = await repo.upsert(_profile())
    assert saved.code == "300308"


# ==================== 端到端：档案 → 配置 → 打分口径 ====================

@pytest.mark.asyncio
async def test_profile_override_reaches_config(repo) -> None:
    from src.intraday.config import IntradayConfig

    await repo.upsert(_profile())
    loaded = await repo.get("300308")
    assert loaded is not None
    config = IntradayConfig()
    scoped = config.with_override("300308", loaded.as_override())
    assert scoped.weights.chan == pytest.approx(20.0)
    assert scoped.weights.box == config.weights.box  # 未列出的沿用全局
    assert scoped.thresholds.action == pytest.approx(35.0)
    assert scoped.levels.stop_loss_pct == pytest.approx(2.0)
    # 全局配置不能被污染（深拷贝）
    assert config.weights.chan != pytest.approx(20.0)
    assert config.levels.stop_loss_pct == pytest.approx(1.0)


@pytest.mark.asyncio
async def test_unknown_weight_key_is_rejected_before_config_use(repo) -> None:
    """库里若混进未知因子名，必须在使用时被配置层拦住（不能静默生效）。"""
    from src.intraday.config import IntradayConfig

    await repo.upsert(_profile(weights={"boll_band": 8.0}))
    loaded = await repo.get("300308")
    assert loaded is not None
    with pytest.raises(ConfigError, match="boll_band"):
        IntradayConfig.validate_override(loaded.as_override().model_dump())


@pytest.mark.asyncio
async def test_describe_is_human_readable(repo) -> None:
    await repo.upsert(_profile())
    loaded = await repo.get("300308")
    assert loaded is not None
    text = loaded.describe()
    assert "分时权重" in text and "档位" in text and "阈值" in text


@pytest.mark.asyncio
async def test_default_db_path_matches_project_sqlite(tmp_path_unused=None) -> None:
    """默认库路径必须与 fact_* 三张表同库（新增一张表就是往这个库加）。"""
    from src.core.config import get_settings

    repo = IntradayProfileSqliteRepository(get_settings().sqlite_path)
    assert repo._db_path == get_settings().sqlite_path  # noqa: SLF001
