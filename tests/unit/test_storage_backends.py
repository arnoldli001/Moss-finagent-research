"""存储后端工厂 + PostgreSQL/Redis适配测试（全离线：Fake连接池/内存Redis，不依赖外部服务）。"""

import pytest

from src.core.config import Settings
from src.core.exceptions import ConfigError
from src.core.schemas import DataPoint
from src.infrastructure.repositories.cached_repo import CachedRepository, _cache_key
from src.infrastructure.repositories.macro_repo import MacroRepository
from src.infrastructure.repositories.postgres_repo import PostgresRepository, normalize_dsn
from src.infrastructure.repositories.repository_factory import build_repository


def _settings(**over) -> Settings:
    return Settings(**over)


def _point(indicator: str = "CPI", value: float = 2.1, period: str = "2026-08-01") -> DataPoint:
    return DataPoint(
        indicator=indicator, value=value, period_date=period,
        source_name="AkShare", source_url="https://x",
    )


# ---------- 工厂 ----------

def test_factory_default_sqlite(tmp_dir):
    repo = build_repository(_settings(sqlite_path=f"{tmp_dir}/x.db"))
    assert isinstance(repo, MacroRepository)


def test_factory_postgres_lazy_does_not_connect():
    """postgres后端构造时不得连接（懒连接），否则无PG环境应用无法启动。"""
    repo = build_repository(_settings(data_backend="postgres"))
    assert isinstance(repo, PostgresRepository)
    assert repo._pool is None


def test_factory_unknown_backend_raises():
    with pytest.raises(ConfigError, match="DATA_BACKEND"):
        build_repository(_settings(data_backend="mysql"))


def test_factory_redis_wrapper(tmp_dir):
    repo = build_repository(_settings(
        sqlite_path=f"{tmp_dir}/x.db", redis_cache_enabled=True,
        redis_url="redis://127.0.0.1:6399/0",  # 不存在的端口→降级直连
    ))
    assert isinstance(repo, CachedRepository)
    assert isinstance(repo.inner, MacroRepository)


def test_dsn_normalization():
    assert normalize_dsn(
        "postgresql+asyncpg://u:p@h:5432/db"
    ) == "postgresql://u:p@h:5432/db"
    assert normalize_dsn("postgresql://u:p@h/db") == "postgresql://u:p@h/db"


# ---------- PostgreSQL Fake池（验证SQL装配与幂等计数语义） ----------

class FakePGConn:
    """记录SQL的假asyncpg连接，用内存表模拟ON CONFLICT幂等。"""

    def __init__(self, table: dict) -> None:
        self._table = table  # key=(indicator,period,hash) → row dict

    async def execute(self, sql: str, *args):
        if sql.lstrip().startswith("CREATE"):
            return "CREATE"
        # INSERT路径，参数顺序对齐COLUMNS
        row = dict(zip(
            ("data_id", "indicator", "value", "unit", "period_date", "extra_json",
             "source_name", "source_url", "source_type", "publish_time", "fetch_time",
             "fetch_method", "raw_content_hash", "processed_by", "process_time",
             "confidence", "verified", "task_id", "created_at"),
            args,
            strict=False,
        ))
        key = (row["indicator"], row["period_date"], row["raw_content_hash"])
        if key in self._table:
            return "INSERT 0 0"
        self._table[key] = row
        return "INSERT 0 1"

    async def fetch(self, sql: str, *params):
        if "GROUP BY" in sql:
            counts: dict[str, int] = {}
            for r in self._table.values():
                counts[r["indicator"]] = counts.get(r["indicator"], 0) + 1
            return [{"indicator": k, "n": v} for k, v in counts.items()]
        indicator = params[0]
        rows = [r for r in self._table.values() if r["indicator"] == indicator]
        if len(params) >= 2:
            rows = [r for r in rows if (r["period_date"] or "") >= params[1]]
        if len(params) >= 3:
            rows = [r for r in rows if (r["period_date"] or "") <= params[2]]
        rows.sort(key=lambda r: r["period_date"] or "")
        return [dict(r) for r in rows]


class FakePool:
    def __init__(self) -> None:
        self.table: dict = {}
        self.closed = False

    class _Acquire:
        def __init__(self, pool) -> None:
            self._pool = pool

        async def __aenter__(self):
            return FakePGConn(self._pool.table)

        async def __aexit__(self, *exc):
            return None

    def acquire(self):
        return self._Acquire(self)

    async def execute(self, sql):
        return "CREATE"

    async def close(self):
        self.closed = True


async def test_postgres_save_idempotent_with_fake_pool(monkeypatch):
    repo = PostgresRepository("postgresql://u:p@h/db")
    fake_pool = FakePool()

    async def fake_create_pool(dsn, **kw):
        return fake_pool

    import sys
    import types
    asyncpg_stub = types.ModuleType("asyncpg")
    asyncpg_stub.create_pool = fake_create_pool
    monkeypatch.setitem(sys.modules, "asyncpg", asyncpg_stub)

    pts = [_point("CPI", 2.1, "2026-08-01"), _point("CPI", 2.0, "2026-07-01")]
    stats1 = await repo.save_points(pts, "task_1")
    assert stats1 == {"inserted": 2, "skipped": 0, "total": 2}
    stats2 = await repo.save_points(pts, "task_2")
    assert stats2["inserted"] == 0 and stats2["skipped"] == 2

    loaded = await repo.query_points("CPI")
    assert len(loaded) == 2 and loaded[0].value == 2.0  # 按期间升序
    counts = await repo.count_by_indicator()
    assert counts == {"CPI": 2}
    await repo.close()
    assert fake_pool.closed


async def test_postgres_connect_failure_raises_config_error(monkeypatch):
    repo = PostgresRepository("postgresql://u:p@127.0.0.1:1/db")

    async def boom(dsn, **kw):
        raise OSError("connection refused")

    import sys
    import types
    asyncpg_stub = types.ModuleType("asyncpg")
    asyncpg_stub.create_pool = boom
    monkeypatch.setitem(sys.modules, "asyncpg", asyncpg_stub)

    with pytest.raises(ConfigError, match="DATA_BACKEND=sqlite"):
        await repo.save_points([_point()], "t")


# ---------- Redis缓存装饰器（内存Fake） ----------

class FakeRedis:
    """实现CachedRepository所用命令的内存Redis。"""

    def __init__(self, *, fail: bool = False) -> None:
        self._store: dict[str, str] = {}
        self._fail = fail
        self.ttl_set: dict[str, int] = {}

    async def ping(self):
        if self._fail:
            raise ConnectionError("boom")

    async def get(self, key):
        if self._fail:
            raise ConnectionError("boom")
        return self._store.get(key)

    async def set(self, key, value, ex=None):
        if self._fail:
            raise ConnectionError("boom")
        self._store[key] = value
        self.ttl_set[key] = ex

    async def scan(self, cursor=0, match=None, count=100):
        keys = [k for k in self._store if _match(k, match)]
        return 0, keys

    async def delete(self, *keys):
        n = 0
        for k in keys:
            n += self._store.pop(k, None) is not None
        return n

    async def aclose(self):
        pass


def _match(key: str, pattern: str) -> bool:
    """极简glob：仅支持尾部*。"""
    if pattern.endswith("*"):
        return key.startswith(pattern[:-1])
    return key == pattern


class FakeInnerRepo:
    """记录穿透次数的假底层仓储。"""

    def __init__(self) -> None:
        self.points = [_point()]
        self.query_calls = 0
        self.saved: list[str] = []

    async def ensure_schema(self):
        pass

    async def save_points(self, points, task_id):
        self.saved.append(task_id)
        return {"inserted": len(points), "skipped": 0, "total": len(points)}

    async def query_points(self, indicator, start_date=None, end_date=None):
        self.query_calls += 1
        return list(self.points)

    async def count_by_indicator(self):
        return {"CPI": len(self.points)}

    async def close(self):
        pass


async def test_cache_hit_avoids_inner_query():
    inner = FakeInnerRepo()
    cached = CachedRepository(inner, "redis://x", ttl_seconds=60)
    cached._client = FakeRedis()  # 直接注入，跳过ping

    first = await cached.query_points("CPI")
    second = await cached.query_points("CPI")
    assert len(first) == 1 and len(second) == 1
    assert inner.query_calls == 1  # 第二次命中缓存，未穿透
    key = _cache_key("CPI", None, None)
    assert cached._client.ttl_set[key] == 60


async def test_cache_invalidated_on_save():
    inner = FakeInnerRepo()
    fake_redis = FakeRedis()
    cached = CachedRepository(inner, "redis://x")
    cached._client = fake_redis

    await cached.query_points("CPI")
    assert inner.query_calls == 1
    await cached.save_points([_point()], "t1")
    # 失效后再查应穿透
    await cached.query_points("CPI")
    assert inner.query_calls == 2


async def test_cache_redis_unreachable_degrades_open(tmp_dir):
    """Redis ping失败 → fail-open直连，查询不报错。"""
    inner = MacroRepository(f"{tmp_dir}/c.db")
    cached = CachedRepository(inner, "redis://127.0.0.1:1/0", socket_timeout=0.2)
    pts = await cached.query_points("CPI")  # 无数据也不抛
    assert pts == []
    await cached.save_points([_point()], "t")
    again = await cached.query_points("CPI")
    assert len(again) == 1
    await cached.close()


async def test_cache_runtime_error_still_returns_data():
    inner = FakeInnerRepo()
    cached = CachedRepository(inner, "redis://x")
    cached._client = FakeRedis(fail=True)  # 已连接但命令持续失败

    result = await cached.query_points("CPI")
    assert len(result) == 1  # 降级直连，数据正常返回


async def test_count_bypasses_cache():
    inner = FakeInnerRepo()
    cached = CachedRepository(inner, "redis://x")
    cached._client = FakeRedis()
    assert (await cached.count_by_indicator()) == {"CPI": 1}
    assert inner.query_calls == 0  # 统计不走查询缓存


def test_cache_key_distinguishes_ranges():
    assert _cache_key("CPI", None, None) != _cache_key("CPI", "2026-01-01", None)
    assert _cache_key("CPI", "2026-01-01", "2026-06-01") != _cache_key(
        "CPI", "2026-01-01", "2026-07-01"
    )
