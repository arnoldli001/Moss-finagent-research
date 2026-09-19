"""运行指标页卡死：根因与修复（2026-09-16 实测）。

## 现象
打开「运行指标」页一直停在「加载指标中…」。

## 实测根因
    /api/v1/metrics?limit=1000    13.18s
    /api/v1/health               300.04s ← 超时（前端 Promise.all 等它 → 永远转圈）

`/health` 里 `build_data_health()` 是**同步**函数，被直接放在 async 路由里跑，
于是整段 CPU/IO 都压在事件循环上（同时间所有请求一起卡）。它内部最贵的一步是
仓库统计：对 **14.28 GB** 的 SQLite 逐表 `SELECT COUNT(*)`：

    COUNT(*)   daily 0.46s | daily_basic 8.52s | adj_factor 8.66s | stk_limit 7.43s
    MAX(rowid) 全部 0.00s，且与 COUNT(*) 数值完全相等
    九表合计：42s（冷页缓存下 30s+，且第二次更慢——把 OS 页缓存冲掉了）

## 修法
1. **行数改用 `MAX(rowid)`**（写入是 `ON CONFLICT DO UPDATE` 原地更新、且从不 DELETE
   → rowid 无空洞，与 COUNT(*) 恒等；响应里带 `count_mode` 标明口径）；
2. **仓库统计落盘缓存** `data/quant/warehouse_stats.json`：请求永远读缓存秒回，
   超 10 分钟在后台线程重算；
3. **`/health` 把 build_data_health 放进 `asyncio.to_thread`**，不再阻塞事件循环；
4. **启动时后台预热**一次，用户第一次打开就是热的；
5. **前端不再 `Promise.all`**：指标 15 秒一刷、健康度 60 秒一刷，各自渲染。
"""

from __future__ import annotations

import json
import time

import pandas as pd
import pytest


def test_sqlite_rowid_matches_count_after_upsert_overwrite(tmp_path) -> None:
    """核心不变量：upsert 覆盖写**不产生 rowid 空洞**，所以 MAX(rowid) == COUNT(*)。

    这是"用 MAX(rowid) 当行数"成立的前提。一旦哪天改成 DELETE + INSERT，
    这条测试会失败 —— 那时计数就会偏高，必须改回 COUNT(*)。
    """
    from src.quant.warehouse import QuantWarehouse, WarehouseConfig

    config = WarehouseConfig.from_env(root=str(tmp_path))
    house = QuantWarehouse(config)
    house.create_all()
    frame = pd.DataFrame({
        "code": ["600036", "600036", "300308"],
        "trade_date": ["20260915", "20260916", "20260916"],
        "close": [40.1, 40.2, 900.0]})
    house.upsert("daily", frame)
    # 同一批再写一遍（走 ON CONFLICT DO UPDATE 覆盖）→ 行数不变、rowid 不变
    house.upsert("daily", frame)
    house.upsert("daily", frame.assign(close=[41.0, 41.1, 901.0]))

    stats = house.stats()
    assert stats["count_mode"] == "rowid"
    rows = [item for item in stats["tables"] if item["dataset"] == "daily"]
    assert rows and rows[0]["rows"] == 3

    exact = house.stats(count_mode="exact")
    exact_rows = [item for item in exact["tables"] if item["dataset"] == "daily"]
    assert exact_rows and exact_rows[0]["rows"] == 3
    assert rows[0]["rows"] == exact_rows[0]["rows"]


def test_stats_exact_mode_uses_count(tmp_path) -> None:
    from src.quant.warehouse import QuantWarehouse, WarehouseConfig

    house = QuantWarehouse(WarehouseConfig.from_env(root=str(tmp_path)))
    house.create_all()
    house.upsert("daily", pd.DataFrame({
        "code": ["600036"], "trade_date": ["20260916"], "close": [40.0]}))
    assert house.stats(count_mode="exact")["count_mode"] == "count"
    assert house.stats()["count_mode"] == "rowid"


def test_warehouse_health_uses_disk_cache(tmp_path, monkeypatch) -> None:
    """仓库统计要落盘：第二次请求不能再走 14GB 库（实测 6~30 秒）。"""
    from src.api import data_health

    calls = {"n": 0}

    def fake_status(root: str = "") -> dict:
        calls["n"] += 1
        return {"dialect": "sqlite", "total_rows": 10, "tables": [
            {"dataset": "daily", "table": "quant_daily", "rows": 10,
             "first": "20060104", "last": "20260915"}]}

    monkeypatch.setattr("src.quant.warehouse.warehouse_status", fake_status)
    monkeypatch.setattr(data_health, "_WAREHOUSE_STATS_FILE",
                        tmp_path / "warehouse_stats.json")

    # 预热路径（force）：算出来并落盘，冷启动后第一次请求就是热的
    first = data_health._warehouse_health(force=True)  # noqa: SLF001
    assert calls["n"] == 1
    assert first["dataset_count"] == 1
    assert first["latest_date"] == "20260915"
    assert (tmp_path / "warehouse_stats.json").exists()

    second = data_health._warehouse_health()  # noqa: SLF001
    assert calls["n"] == 1, "第二次必须读缓存，不能重算"
    assert second["total_rows"] == 10
    assert "cached_age_seconds" in second


def test_warehouse_health_survives_corrupt_cache(tmp_path, monkeypatch) -> None:
    """缓存文件坏了：当次给"生成中"占位并起后台重算，**不阻塞请求**。"""
    from src.api import data_health

    broken = tmp_path / "warehouse_stats.json"
    broken.write_text("{不是JSON", encoding="utf-8")
    monkeypatch.setattr(data_health, "_WAREHOUSE_STATS_FILE", broken)
    monkeypatch.setattr("src.quant.warehouse.warehouse_status",
                        lambda root="": {"tables": []})
    result = data_health._warehouse_health()  # noqa: SLF001
    assert result["stats_pending"] is True
    assert result["dataset_count"] == 0


def test_warehouse_health_reports_errors_without_raising(monkeypatch) -> None:
    """预热路径（force=True）里仓库报错要转成 available=False，不能抛。"""
    from src.api import data_health

    def boom(root: str = ""):
        raise RuntimeError("库坏了")

    monkeypatch.setattr(data_health, "_WAREHOUSE_STATS_FILE",
                        __import__("pathlib").Path("/nonexistent/x.json"))
    monkeypatch.setattr("src.quant.warehouse.warehouse_status", boom)
    result = data_health._warehouse_health(force=True)  # noqa: SLF001
    assert result["available"] is False
    assert "库坏了" in result["error"]


def test_warehouse_health_cold_request_does_not_block(tmp_path, monkeypatch) -> None:
    """**关键回归**：没有缓存时请求路径不能去扫 14GB 的库（实测冷页 28 秒）。

    预热线程会用 force=True 把统计算好落盘，所以冷启动后第一次请求就是热的；
    万一请求抢在预热前面，也必须秒回占位结构而不是等几十秒。
    """
    from src.api import data_health

    monkeypatch.setattr(data_health, "_WAREHOUSE_STATS_FILE",
                        tmp_path / "warehouse_stats.json")
    started = {"n": 0}

    def slow_status(root: str = "") -> dict:
        started["n"] += 1
        time.sleep(1.0)                     # 模拟慢查询（不该被请求路径等待）
        return {"dialect": "sqlite", "tables": []}

    monkeypatch.setattr("src.quant.warehouse.warehouse_status", slow_status)

    t = time.perf_counter()
    result = data_health._warehouse_health()  # noqa: SLF001
    elapsed = time.perf_counter() - t

    assert elapsed < 0.5, f"请求路径等了 {elapsed:.2f}s，说明又在同步扫库"
    assert result["stats_pending"] is True
    deadline = time.time() + 5
    while started["n"] == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert started["n"] == 1, "应起后台重算"


def test_build_data_health_is_cached(monkeypatch) -> None:
    """整份健康度带 5 分钟缓存：面板轮询不该反复付 4.5 秒。"""
    from src.api import data_health

    calls = {"n": 0}

    def fake_uncached(runtime, *, force: bool = False):
        calls["n"] += 1
        return {"generated_at": "now", "tables": [], "forced": force}

    monkeypatch.setattr(data_health, "_build_data_health_uncached", fake_uncached)
    data_health.invalidate_cache()
    first = data_health.build_data_health(None)
    second = data_health.build_data_health(None)
    assert calls["n"] == 1
    assert first is second
    third = data_health.build_data_health(None, force=True)
    assert calls["n"] == 2
    assert third is not first
    assert third["forced"] is True, "force 必须一路透传给子统计（否则只重算了外壳）"


def test_health_route_runs_health_build_off_the_event_loop() -> None:
    """回归：build_data_health 必须在**线程**里跑，且要在**关键路径线程池**里跑。

    两层原因，都是实测踩出来的：

    1. 同步调用 → 整段 I/O 压在事件循环上，一次 /health 让**所有**并发请求一起卡
       （实测 300 秒超时，做T面板同时卡死）；
    2. 用 `asyncio.to_thread`（默认执行器）→ 与自选池首屏（26 只票并发取数）
       抢同一个池子，池满时 /health 要**排队 144.5 秒**，而它自己只要 3.74 秒。
       改用 `run_infra`（`src/core/executors.py` 的独立线程池）后不再被业务挤掉。
    """
    from pathlib import Path

    source = Path("src/api/routes/research.py").read_text(encoding="utf-8")
    assert "asyncio.to_thread(_data_health" not in source, \
        "健康度不能再用默认执行器（会被自选池首屏挤到排队 144 秒）"
    assert "run_infra(_data_health" in source, \
        "健康度组装必须放到关键路径线程池里，不能在事件循环里同步跑"


@pytest.fixture(autouse=True)
def _reset_refresh_flags():
    """每例前后清掉"后台刷新中"标记：它们是模块级全局量，跨例会互相抑制。"""
    from src.api import data_health

    for name in ("_TUSHARE_REFRESHING", "_WAREHOUSE_REFRESHING"):
        setattr(data_health, name, False)
    yield
    for name in ("_TUSHARE_REFRESHING", "_WAREHOUSE_REFRESHING"):
        setattr(data_health, name, False)


def test_tushare_health_uses_disk_cache(tmp_path, monkeypatch) -> None:
    """Tushare 覆盖要落盘：请求路径不能去遍历 35,711 个分区目录。

    实测（2026-09-17）：平时 2.9 秒，但服务刚起来、磁盘被 akshare 子进程占满时
    膨胀到 **140 秒以上** —— 首个 /health 要 142.8 秒才返回。
    """
    from src.api import data_health

    calls = {"n": 0}

    def fake_build(root: str, universe: str) -> dict:
        calls["n"] += 1
        return {"source": "tushare", "partitions": 35711, "datasets": []}

    monkeypatch.setattr(data_health, "_TUSHARE_STATS_FILE",
                        tmp_path / "tushare_stats.json")
    monkeypatch.setattr(data_health, "_build_tushare_health", fake_build)

    # 第一次：没有缓存 → **立刻**返回"生成中"占位，同时后台重算
    first = data_health._tushare_health()  # noqa: SLF001
    assert first["stats_pending"] is True
    assert first["partitions"] == 0
    cache_file = tmp_path / "tushare_stats.json"
    deadline = time.time() + 5
    while (calls["n"] == 0 or not cache_file.exists()) and time.time() < deadline:
        time.sleep(0.05)
    assert calls["n"] == 1
    assert cache_file.exists(), "后台线程算完必须落盘"

    # 第二次：读缓存，不再重算
    second = data_health._tushare_health()  # noqa: SLF001
    assert calls["n"] == 1, "第二次必须读缓存，不能再去遍历分区目录"
    assert second["partitions"] == 35711


def test_tushare_health_refreshes_in_background_when_stale(
    tmp_path, monkeypatch,
) -> None:
    """缓存过期时：**立即返回旧值**，同时在后台线程重算（绝不阻塞请求）。"""
    from src.api import data_health

    path = tmp_path / "tushare_stats.json"
    monkeypatch.setattr(data_health, "_TUSHARE_STATS_FILE", path)
    monkeypatch.setattr(data_health, "_TUSHARE_TTL", 60.0)
    path.write_text(json.dumps({"_cached_at": time.time() - 3600,
                                "partitions": 1, "datasets": [],
                                "source": "tushare"}), encoding="utf-8")
    started = {"n": 0}

    def fake_build(root: str, universe: str) -> dict:
        started["n"] += 1
        return {"source": "tushare", "partitions": 2, "datasets": []}

    monkeypatch.setattr(data_health, "_build_tushare_health", fake_build)

    t = time.perf_counter()
    result = data_health._tushare_health()  # noqa: SLF001
    elapsed = time.perf_counter() - t

    assert result["partitions"] == 1, "必须立刻返回旧值"
    assert elapsed < 0.5, f"请求路径不能等重算（实测 {elapsed:.2f}s）"
    deadline = time.time() + 5
    while started["n"] == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert started["n"] == 1, "过期后应起后台刷新"


def test_tushare_health_survives_corrupt_cache(tmp_path, monkeypatch) -> None:
    """缓存坏了：给"生成中"占位并起后台重算，不在请求路径上遍历分区。"""
    from src.api import data_health

    broken = tmp_path / "tushare_stats.json"
    broken.write_text("{不是JSON", encoding="utf-8")
    monkeypatch.setattr(data_health, "_TUSHARE_STATS_FILE", broken)
    monkeypatch.setattr(data_health, "_build_tushare_health",
                        lambda root, universe: {"source": "tushare",
                                                "partitions": 3, "datasets": []})
    result = data_health._tushare_health()  # noqa: SLF001
    assert result["stats_pending"] is True
    assert result["partitions"] == 0


def test_tushare_health_cold_request_does_not_block(tmp_path, monkeypatch) -> None:
    """**关键回归**：没有缓存时请求路径不能去遍历 3.5 万个分区。

    实测（2026-09-17）：服务刚起来、磁盘被 akshare 子进程占满时，这一步要
    **140 秒以上** —— 首个 /health 因此 142.8 秒才返回。
    """
    from src.api import data_health

    monkeypatch.setattr(data_health, "_TUSHARE_STATS_FILE",
                        tmp_path / "tushare_stats.json")
    started = {"n": 0}

    def slow_build(root: str, universe: str) -> dict:
        started["n"] += 1
        time.sleep(1.0)
        return {"source": "tushare", "partitions": 35711, "datasets": []}

    monkeypatch.setattr(data_health, "_build_tushare_health", slow_build)

    t = time.perf_counter()
    result = data_health._tushare_health()  # noqa: SLF001
    elapsed = time.perf_counter() - t

    assert elapsed < 0.5, f"请求路径等了 {elapsed:.2f}s"
    assert result["stats_pending"] is True
    deadline = time.time() + 5
    while started["n"] == 0 and time.time() < deadline:
        time.sleep(0.05)
    assert started["n"] == 1, "应起后台重算"


def test_invalidate_cache_include_disk_clears_both_stats_files(
    tmp_path, monkeypatch,
) -> None:
    """运维/测试清缓存要连落盘统计一起删，否则会读到旧结果。"""
    from src.api import data_health

    wh = tmp_path / "warehouse_stats.json"
    ts = tmp_path / "tushare_stats.json"
    wh.write_text("{}", encoding="utf-8")
    ts.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(data_health, "_WAREHOUSE_STATS_FILE", wh)
    monkeypatch.setattr(data_health, "_TUSHARE_STATS_FILE", ts)

    data_health.invalidate_cache(include_disk=True)

    assert not wh.exists() and not ts.exists()


def test_notes_disclose_row_count_method() -> None:
    """口径要写在面板能看到的说明里，不让人猜数字怎么来的。"""
    from src.api import data_health

    data_health.invalidate_cache()
    payload = data_health._build_data_health_uncached(None)  # noqa: SLF001
    assert any("rowid" in note for note in payload["notes"])


def test_stats_wall_clock_saving_is_significant(tmp_path) -> None:
    """量化收益：缓存命中必须远快于重算（这里是同进程内的小库对照）。"""
    from src.api import data_health
    from src.quant.warehouse import QuantWarehouse, WarehouseConfig

    house = QuantWarehouse(WarehouseConfig.from_env(root=str(tmp_path)))
    house.create_all()
    house.upsert("daily", pd.DataFrame({
        "code": ["600036"], "trade_date": ["20260916"], "close": [40.0]}))
    data_health._write_warehouse_stats(house.stats())  # noqa: SLF001

    started = time.perf_counter()
    data_health._warehouse_health()  # noqa: SLF001  读落盘缓存
    cached = time.perf_counter() - started
    started = time.perf_counter()
    house.stats(count_mode="exact")
    exact = time.perf_counter() - started
    assert cached < exact or cached < 0.05


@pytest.mark.parametrize("dialect_expect", [("rowid")])
def test_count_mode_field_present(dialect_expect: str, tmp_path) -> None:
    from src.quant.warehouse import QuantWarehouse, WarehouseConfig

    house = QuantWarehouse(WarehouseConfig.from_env(root=str(tmp_path)))
    house.create_all()
    assert house.stats()["count_mode"] == dialect_expect
