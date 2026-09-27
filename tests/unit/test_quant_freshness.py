"""行情仓库新鲜度 + 同步作业的单元测试。

背景（真实事故）：2026-09-18 09:10 手动跑「量化选股」，选出来的日期是
**20260915** —— 本地 Tushare 仓库里最新就到 0915。选股逻辑没错，
它忠实地用了"手上最新的一天"，但界面上完全看不出用的是两天前的行情。

这里锁住三件事：
  1. 「最近一个已收盘交易日」的算法（**盘中不算今天**，收盘后算今天）；
  2. 滞后时要能给出机器可判的 `data_stale` 与人话说明；
  3. 同步作业在"没有缺口 / 日历不可用 / 仓库不可用"时**不猜、不误报成功**。
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.quant.freshness import (
    EOD_RELEASE,
    freshness,
    latest_complete_trade_date,
)
from src.scheduler import jobs as jobs_mod

#: 2026-09-14(一) ~ 09-18(五) 是交易日；09-19/20 是周末
WEEK = ["20260914", "20260915", "20260916", "20260917", "20260918"]


# ======================================================================
# 最近一个「已收盘」交易日
# ======================================================================


def test_intraday_does_not_count_today() -> None:
    """盘中的"最新可用"是**昨天** —— 今天的数据还没产生，不能当成应有数据。"""
    at_0925 = datetime(2026, 9, 18, 9, 25)
    assert latest_complete_trade_date(now=at_0925, days=WEEK) == "20260917"
    at_1445 = datetime(2026, 9, 18, 14, 45)
    assert latest_complete_trade_date(now=at_1445, days=WEEK) == "20260917"


def test_after_eod_release_today_counts() -> None:
    """过了 EOD 发布线（16:30）之后，今天的行情就算"应该有"了。"""
    assert EOD_RELEASE.strftime("%H:%M") == "16:30"
    after = datetime(2026, 9, 18, 17, 0)
    assert latest_complete_trade_date(now=after, days=WEEK) == "20260918"
    # 刚过线 1 分钟也算（留的余量已在 EOD_RELEASE 里）
    just = datetime(2026, 9, 18, 16, 31)
    assert latest_complete_trade_date(now=just, days=WEEK) == "20260918"


def test_before_release_line_still_yesterday() -> None:
    edge = datetime(2026, 9, 18, 16, 29)
    assert latest_complete_trade_date(now=edge, days=WEEK) == "20260917"


def test_weekend_resolves_to_friday() -> None:
    """周末没有新数据，应该拿周五。"""
    saturday = datetime(2026, 9, 19, 10, 0)
    assert latest_complete_trade_date(now=saturday, days=WEEK) == "20260918"
    sunday = datetime(2026, 9, 20, 23, 0)
    assert latest_complete_trade_date(now=sunday, days=WEEK) == "20260918"


def test_empty_calendar_is_unknown_not_today() -> None:
    """日历不可用时返回空串（判不了），**绝不退化成"就当成今天"**。"""
    assert latest_complete_trade_date(now=datetime(2026, 9, 18, 10, 0),
                                      days=[]) == ""


# ======================================================================
# freshness：滞后判定
# ======================================================================


def test_stale_run_is_flagged_with_a_readable_note() -> None:
    info = freshness("20260915", now=datetime(2026, 9, 18, 9, 10), days=WEEK)
    assert info["data_stale"] is True
    assert info["expected_trade_date"] == "20260917"
    assert info["checked"] is True
    # 说明里必须同时出现"实际用的"和"应该用的"，否则用户没法判断差几天
    assert "20260915" in info["note"] and "20260917" in info["note"]


def test_fresh_run_is_not_flagged() -> None:
    info = freshness("20260917", now=datetime(2026, 9, 18, 9, 10), days=WEEK)
    assert info["data_stale"] is False
    assert info["note"] == ""


def test_newer_than_expected_is_not_stale() -> None:
    """数据比"应有日期"还新（比如收盘后同步了当天）→ 不算滞后。"""
    info = freshness("20260918", now=datetime(2026, 9, 18, 17, 0), days=WEEK)
    assert info["data_stale"] is False


def test_unknown_calendar_reports_checked_false() -> None:
    """`checked=False` 表示**没判**，调用方不能读成"确认新鲜"。"""
    info = freshness("20260915", now=datetime(2026, 9, 18, 9, 10), days=[])
    assert info["checked"] is False
    assert info["data_stale"] is False
    assert info["note"] == ""


def test_missing_data_date_is_not_stale() -> None:
    info = freshness("", now=datetime(2026, 9, 18, 9, 10), days=WEEK)
    assert info["data_stale"] is False
    assert info["checked"] is False


# ======================================================================
# 同步作业：接线 + 不猜
# ======================================================================


def test_registry_declares_the_sync_job() -> None:
    """作业必须在 registry 里（cron 只允许在这一处声明）。"""
    from src.scheduler.registry import JOB_REGISTRY

    spec = JOB_REGISTRY["quant_data_sync"]
    assert spec.kind == "quant_data_sync"
    # 2026-09-23 起改成「工作日 16:00~23:30 每 30 分钟」。原来是一天一次（16:40）：
    # 那次若撞上 Tushare 还没发布（`moneyflow` 实测晚约 2 个交易日才全），
    # 就只能等**下一个工作日**，中间用户的页面一直显示「数据滞后」而不自愈。
    assert spec.cron == "*/30 16-23 * * 1-5"
    assert "daily" in spec.params["datasets"]
    # ⚠️ 清单必须覆盖 `panels.py` 真正会读的日频数据集，漏一个就是静默降级：
    # `adj_factor` 缺了 → 价格不复权 → 除权日出现假跳空，而面板照算不误。
    from src.quant.sync_gap import DAILY_DATASETS

    assert set(spec.params["datasets"]) == set(DAILY_DATASETS)


def test_sync_uses_a_dataset_that_actually_exists() -> None:
    """`datasets` 里写的名字必须在仓库注册表里 —— 写错只会在凌晨静默失败。"""
    from src.quant.warehouse import DATASET_TABLES
    from src.scheduler.registry import JOB_REGISTRY

    for name in JOB_REGISTRY["quant_data_sync"].params["datasets"]:
        assert name in DATASET_TABLES, f"{name} 不是仓库数据集"


class _Spec:
    def __init__(self, **params):
        self.params = params
        self.name = "quant_data_sync"


def test_sync_refuses_to_guess_without_calendar(monkeypatch) -> None:
    """日历不可用 → 明确失败，而不是"随便补到今天"。"""
    monkeypatch.setattr(jobs_mod, "_latest_complete_trade_date", lambda: "")
    processed, detail = _run(jobs_mod._quant_data_sync(_Spec()))
    assert processed == 0
    assert detail.startswith("失败")
    assert "交易日历" in detail


class _Warehouse:
    def __init__(self, *, available: bool = True, latest: str = "20260915",
                 by_dataset: dict[str, str] | None = None):
        self._available = available
        self._latest = latest
        #: 分数据集水位（缺省时所有数据集共用 `latest`）
        self._by_dataset = dict(by_dataset or {})
        self.ingested: list[tuple[str, list[str]]] = []

    def available(self) -> bool:
        return self._available

    def load(self, dataset, **kwargs):  # noqa: ANN001, ANN003
        import pandas as pd

        stamp = self._by_dataset.get(dataset, self._latest)
        if not stamp:
            return pd.DataFrame()
        return pd.DataFrame({"trade_date": [stamp]})

    def ingest_dataset(self, dataset, *, keys=None):  # noqa: ANN001
        self.ingested.append((dataset, list(keys or [])))
        return len(keys or [])


def _run(coro):
    import asyncio

    return asyncio.run(coro)


def test_sync_is_a_noop_when_already_current(monkeypatch) -> None:
    """仓库已经到目标日 → 直接返回，**不下载、不写库**（幂等，可反复跑）。"""
    warehouse = _Warehouse(latest="20260917")
    monkeypatch.setattr(jobs_mod, "_latest_complete_trade_date",
                        lambda: "20260917")
    # 分区与仓库同水位：此时不该有任何"补灌"动作（否则每日路径要多扫 5000+ 分区）。
    # ⚠️ 2026-09-22 起补灌是问 `DatasetStore` 要**每档自己的分区键**，所以这里必须
    # 连分区目录一起换掉 —— 只 patch `_partition_latest`（水位标量）已经管不住它，
    # 真实的 5000+ 分区会漏进来，让"无操作"变成"补灌 12 行"。
    monkeypatch.setattr(jobs_mod, "_partition_latest", lambda _dataset: "20260917")
    _patch_warehouse(monkeypatch, warehouse)
    _patch_dataset_store(monkeypatch, {name: ["20260915", "20260916", "20260917"]
                                       for name in ("daily", "daily_basic",
                                                    "stk_limit", "moneyflow")})

    processed, detail = _run(jobs_mod._quant_data_sync(_Spec()))

    assert processed == 0
    assert "已是最新" in detail
    assert warehouse.ingested == []


def test_sync_keeps_downloading_when_warehouse_is_unavailable(monkeypatch) -> None:
    """**回归（2026-09-21）**：仓库不可用**不该**让整轮退出——分区必须照下。

    原来这里是 `return 0, "失败：本地量化仓库不可用"`，于是仓库一坏，
    **分区也停了**。而真正被读的是分区（`load_dataset` 会回退到它）：
    分区停在 0917 → 竞价选股的 18 天窗口差一天 → 首板否决规则静默不生效，
    作业记录里却只写着"仓库不可用"，两件事看不出关联。

    正确语义：下载照做（水位取仓库/分区更新的那个），灌库才是尽力而为。
    """
    monkeypatch.setattr(jobs_mod, "_latest_complete_trade_date",
                        lambda: "20260917")
    monkeypatch.setattr(jobs_mod, "_partition_latest", lambda _dataset: "")
    _patch_warehouse(monkeypatch, _Warehouse(available=False))
    downloaded = _patch_download(monkeypatch, days=["20260916", "20260917"])

    processed, detail = _run(jobs_mod._quant_data_sync(_Spec()))

    assert downloaded["calendar"] is not None, "仓库不可用时仍要发起下载"
    assert downloaded["days"] == ["20260916", "20260917"]
    assert not detail.startswith("失败"), f"仓库不可用不该判作业失败：{detail}"
    assert "未灌库" in detail
    assert processed == 0


def test_sync_ingests_when_partition_is_ahead_of_warehouse(monkeypatch) -> None:
    """分区已到目标、仓库还落后 → **不下载**，但要尽力补灌（否则仓库永远追不上）。"""
    monkeypatch.setattr(jobs_mod, "_latest_complete_trade_date",
                        lambda: "20260917")
    monkeypatch.setattr(jobs_mod, "_partition_latest", lambda _dataset: "20260917")
    warehouse = _Warehouse(latest="20260915")
    _patch_warehouse(monkeypatch, warehouse)
    # ⚠️ 2026-09-22 起补灌是问 `DatasetStore` 要**每档自己的分区键**（与
    # test_sync_is_a_noop_when_already_current 同因）：不换掉分区目录的话，
    # 无数据环境（CI/干净克隆）读到的是真实空分区 → 补灌恒为空，用例失败。
    _patch_dataset_store(monkeypatch, {name: ["20260915", "20260916", "20260917"]
                                       for name in ("daily", "daily_basic",
                                                    "stk_limit", "moneyflow")})
    # 日历缓存窗口给一个远期末日：`_refresh_calendar_horizon` 判"缓存已覆盖
    # 今天"直接跳过 —— 用例不联网、不碰真实缓存（真实缓存末日落后时会发起
    # 刷新，污染"没下载"断言）。不用 `date.today()`：本函数已有固定日期
    # fixture，同函数再锚时钟会被 scan_date_bombs 判成定时炸弹。
    monkeypatch.setattr(jobs_mod, "_calendar_cache_window",
                        lambda: ("19901219", "29991231"))
    downloaded = _patch_download(monkeypatch, days=["20260916"])

    processed, detail = _run(jobs_mod._quant_data_sync(_Spec()))

    assert downloaded["calendar"] is None, "分区已最新时不该重复下载"
    assert "已是最新" in detail
    assert warehouse.ingested, "仓库落后时要补灌"
    assert processed > 0


def test_sync_backfills_each_dataset_from_its_own_watermark(monkeypatch) -> None:
    """**回归（2026-09-22）**：补灌必须按**每档自己的水位**算缺口。

    原来只有一句 `if warehouse_latest < partition_latest:`，然后用
    `DatasetStore("daily").keys()` 算出 `pending` 再喂给每一个数据集 ——
    默认了"所有数据集水位一致"。而 `moneyflow` 比 `daily` 晚发布约 2 个交易日，
    于是实测成了：daily 仓库 0918 / 分区 0922 → pending = {0921, 0922}，
    moneyflow 仓库还停在 **0915**，0916~0918 永远进不来。

    后果不只是"少三天数据"：`ml_board_flow` 由个股口径聚合而来，
    仓库 moneyflow 停在哪天、板块资金流就停在哪天，而评分照跑不误 ——
    0916~0922 的分数全部把 0915 的资金流当最新值，界面上看不出异常。
    """
    monkeypatch.setattr(jobs_mod, "_latest_complete_trade_date",
                        lambda: "20260922")
    monkeypatch.setattr(jobs_mod, "_partition_latest", lambda _dataset: "20260922")
    warehouse = _Warehouse(by_dataset={"daily": "20260918", "moneyflow": "20260915"})
    _patch_warehouse(monkeypatch, warehouse)
    partitions = ["20260915", "20260916", "20260917", "20260918",
                  "20260921", "20260922"]
    _patch_dataset_store(monkeypatch, {name: partitions
                                       for name in ("daily", "daily_basic",
                                                    "stk_limit", "moneyflow")})
    downloaded = _patch_download(monkeypatch, days=[])

    processed, detail = _run(jobs_mod._quant_data_sync(_Spec()))

    assert downloaded["calendar"] is None, "分区已到目标，不该发起下载"
    assert "已是最新" in detail
    got = dict(warehouse.ingested)
    assert got["daily"] == ["20260921", "20260922"]
    assert got["moneyflow"] == ["20260916", "20260917", "20260918",
                                "20260921", "20260922"], \
        "moneyflow 比 daily 落后 3 天，0916~0918 不能被 daily 的缺口带偏而漏掉"
    assert processed > 0


def test_sync_backfill_is_idempotent_when_warehouse_is_current(monkeypatch) -> None:
    """每档都到最新 → 一次 ingest 都不发（幂等，可反复跑）。"""
    monkeypatch.setattr(jobs_mod, "_latest_complete_trade_date",
                        lambda: "20260922")
    monkeypatch.setattr(jobs_mod, "_partition_latest", lambda _dataset: "20260922")
    warehouse = _Warehouse(by_dataset={"daily": "20260922", "moneyflow": "20260922"})
    _patch_warehouse(monkeypatch, warehouse)
    _patch_dataset_store(monkeypatch, {"daily": ["20260922"], "moneyflow": ["20260922"]})
    _patch_download(monkeypatch, days=[])

    processed, _detail = _run(jobs_mod._quant_data_sync(_Spec()))

    assert warehouse.ingested == []
    assert processed == 0


def test_sync_headline_reports_partition_watermark(monkeypatch) -> None:
    """水位取"仓库/分区里更新的那个" —— 仓库不可用时由分区说了算。"""
    monkeypatch.setattr(jobs_mod, "_latest_complete_trade_date",
                        lambda: "20260917")
    monkeypatch.setattr(jobs_mod, "_partition_latest", lambda _dataset: "20260917")
    _patch_warehouse(monkeypatch, _Warehouse(available=False))
    downloaded = _patch_download(monkeypatch, days=[])

    _processed, detail = _run(jobs_mod._quant_data_sync(_Spec()))

    assert "20260917" in detail
    assert "仓库不可用" in detail
    assert downloaded["calendar"] is None, "分区已到目标时不该下载"


def _patch_download(monkeypatch, *, days: list[str]) -> dict:
    """把 `_quant_data_sync` 里延迟 import 的下载器换成假的。

    `seen["calendar"]` 记录"到底有没有发起下载"（`None` = 一次都没发起），
    比 `sync_daily` 的入参更适合当"是否下载"的判据 —— 交易日历为空时
    也不会调 `sync_daily`。
    """
    import src.quant.download as download_mod
    import src.quant.tushare_source as tushare_mod

    seen: dict = {"calendar": None, "days": []}

    class _Result:
        rows = 3

    class _Downloader:
        def __init__(self, *_a, **_k):
            pass

        async def calendar(self, start, end):  # noqa: ANN001
            seen["calendar"] = (start, end)
            return list(days)

        async def sync_daily(self, _dataset, wanted):  # noqa: ANN001
            seen["days"] = list(wanted)
            return _Result()

    monkeypatch.setattr(download_mod, "TushareDownloader", _Downloader)
    # 签名要跟真实 `resolve_token` 一致（调用方会传参数进来）
    monkeypatch.setattr(tushare_mod, "resolve_token", lambda *a, **k: "t")
    return seen


# ======================================================================
# 2026-09-23：「收盘了为什么图上还没有今天」的两处根因
# ======================================================================


class _FullDownloader:
    """按**真实** `TushareDownloader` 的行为模拟：`index_daily` 不在日频表里。"""

    def __init__(self, *_a, **_k):
        self.daily_calls: list[tuple[str, list[str]]] = []
        self.index_calls: list[list[str]] = []
        self.calendar_calls: list[tuple[str, str]] = []
        self.fail: set[str] = set()

    async def calendar(self, start, end):  # noqa: ANN001
        self.calendar_calls.append((start, end))
        return ["20260923"]

    async def sync_daily(self, dataset, days):  # noqa: ANN001
        if dataset == "index_daily":
            raise KeyError("index_daily")   # 真实代码就是在这里抛的
        if dataset in self.fail:
            raise RuntimeError(f"{dataset} 炸了")
        self.daily_calls.append((dataset, list(days)))

        class _Result:
            rows = 5
        return _Result()

    async def sync_index(self, days):  # noqa: ANN001
        self.index_calls.append(list(days))

        class _Result:
            rows = 5
        return _Result()


def _patch_full_downloader(monkeypatch) -> _FullDownloader:
    """把下载器换成 `_FullDownloader` 的**同一个实例**（调用不调用都拿得到它）。"""
    import src.quant.download as download_mod
    import src.quant.tushare_source as tushare_mod

    downloader = _FullDownloader()
    monkeypatch.setattr(download_mod, "TushareDownloader",
                        lambda *_a, **_k: downloader)
    monkeypatch.setattr(tushare_mod, "resolve_token", lambda *a, **k: "t")
    return downloader


def test_sync_routes_index_daily_to_the_index_entry_point(monkeypatch) -> None:
    """**回归（2026-09-23）**：`index_daily` 必须走 `sync_index`。

    它列在作业清单里（`sync_gap.DAILY_DATASETS`），却不在
    `download.py::DAILY_DATASETS` 里 —— 指数是 5 个代码逐个拉的。
    走 `sync_daily` 会 `KeyError`，而且抛在灌库之前：**5 档分区已下好、
    仓库一行没进**，作业只留一句"下载失败"（实测 09-23 18:52 触发的那次）。
    """
    from src.quant.sync_gap import DAILY_DATASETS

    monkeypatch.setattr(jobs_mod, "_latest_complete_trade_date", lambda: "20260923")
    monkeypatch.setattr(jobs_mod, "_partition_latest", lambda _dataset: "20260922")
    # 日历缓存已经含 0923 → 不该再联网刷日历
    monkeypatch.setattr(jobs_mod, "_calendar_cache_window",
                        lambda: ("19901219", "20260923"))
    warehouse = _Warehouse(latest="20260922")
    _patch_warehouse(monkeypatch, warehouse)
    partitions = ["20260922", "20260923"]
    _patch_dataset_store(monkeypatch,
                         {name: partitions for name in DAILY_DATASETS})
    downloader = _patch_full_downloader(monkeypatch)

    processed, detail = _run(
        jobs_mod._quant_data_sync(_Spec(datasets=list(DAILY_DATASETS))))

    assert not detail.startswith("失败"), detail
    assert downloader.index_calls == [["20260923"]], "指数必须走 sync_index"
    assert ("index_daily" not in [name for name, _ in downloader.daily_calls])
    assert sorted(name for name, _ in downloader.daily_calls) == sorted(
        set(DAILY_DATASETS) - {"index_daily"})
    assert processed > 0
    assert {name for name, _ in warehouse.ingested} >= {"daily", "index_daily"}


def test_sync_one_broken_dataset_does_not_block_the_rest(monkeypatch) -> None:
    """逐档隔离：一档炸了，其余档仍要下载 + 灌库，并在说明里点名。"""
    monkeypatch.setattr(jobs_mod, "_latest_complete_trade_date", lambda: "20260923")
    monkeypatch.setattr(jobs_mod, "_partition_latest", lambda _dataset: "20260922")
    monkeypatch.setattr(jobs_mod, "_calendar_cache_window",
                        lambda: ("19901219", "20260923"))
    warehouse = _Warehouse(latest="20260922")
    _patch_warehouse(monkeypatch, warehouse)
    _patch_dataset_store(monkeypatch,
                         {"daily": ["20260922", "20260923"],
                          "suspend_d": ["20260922", "20260923"]})
    downloader = _patch_full_downloader(monkeypatch)
    downloader.fail.add("suspend_d")

    processed, detail = _run(
        jobs_mod._quant_data_sync(_Spec(datasets=["daily", "suspend_d"])))

    assert not detail.startswith("失败"), detail
    assert "suspend_d(RuntimeError) 下载失败" in detail
    assert [name for name, _ in downloader.daily_calls] == ["daily"]
    assert ("daily", ["20260923"]) in warehouse.ingested
    assert processed > 0


def test_sync_refreshes_a_stale_calendar_cache_before_deciding(monkeypatch) -> None:
    """**回归（2026-09-23 真根因）**：日历缓存停在昨天 → 先把它顶到今天。

    不顶的话 `target` 永远等于缓存末日，作业天天报「已是最新」，
    今天的行情永远下不来（实测：09-23 收盘后三次作业 success + 0 行，
    而 Tushare 当天 18:49 已经能取到 5556 行日线）。

    关于写死的 `20260922`：
    date-bomb-exempt: 这里要的是"**已经过期**的缓存末日"，不是"新鲜的 fixture"。
    写死的日期只会**越来越旧**，`20260922 < today` 恒成立，用例语义永不漂移 ——
    与那些"假设 fixture 是新鲜的"定时炸弹正好相反。
    """
    from datetime import date

    today = date.today().strftime("%Y%m%d")
    downloader = _patch_full_downloader(monkeypatch)
    monkeypatch.setattr(jobs_mod, "_calendar_cache_window",
                        lambda: ("19901219", "20260922"))

    note = _run(jobs_mod._refresh_calendar_horizon())

    assert note == ""
    assert downloader.calendar_calls == [("19901219", today)], \
        "必须用墙上时钟（今天）顶日历，而不是用缓存里的末日"


def test_sync_skips_calendar_refresh_when_cache_is_current(monkeypatch) -> None:
    """缓存已含今天 → 一次网络都不发（作业每 30 分钟跑一次，不能白刷）。"""
    from datetime import date

    today = date.today().strftime("%Y%m%d")
    downloader = _patch_full_downloader(monkeypatch)
    monkeypatch.setattr(jobs_mod, "_calendar_cache_window",
                        lambda: ("19901219", today))

    assert _run(jobs_mod._refresh_calendar_horizon()) == ""
    assert downloader.calendar_calls == []


def test_sync_calendar_refresh_failure_keeps_old_calendar(monkeypatch) -> None:
    """刷新失败**只记一句说明**：作业不因此失败，仍按旧日历走（不猜）。"""
    import src.quant.download as download_mod
    import src.quant.tushare_source as tushare_mod

    class _Boom:
        def __init__(self, *_a, **_k):
            pass

        async def calendar(self, _start, _end):  # noqa: ANN001
            raise RuntimeError("网络不通")

    monkeypatch.setattr(download_mod, "TushareDownloader", _Boom)
    monkeypatch.setattr(tushare_mod, "resolve_token", lambda *a, **k: "t")
    monkeypatch.setattr(jobs_mod, "_calendar_cache_window",
                        lambda: ("19901219", "20260922"))

    note = _run(jobs_mod._refresh_calendar_horizon())

    assert "交易日历刷新失败" in note and "20260922" in note
    assert not note.startswith("失败：")   # 说明不会把作业判成 failed



def _patch_warehouse(monkeypatch, warehouse) -> None:
    """把 `_quant_data_sync` 内部 import 的 QuantWarehouse 换成假的。

    函数体里是**延迟 import**（避免调度器启动就拖起 15GiB 的库连接），
    所以补丁要打在 `src.quant.warehouse` 模块属性上，而不是 jobs 模块上。
    """
    import src.quant.warehouse as warehouse_mod

    monkeypatch.setattr(warehouse_mod, "QuantWarehouse", lambda *a, **k: warehouse)


def _patch_dataset_store(monkeypatch, keys_by_dataset: dict[str, list[str]]) -> None:
    """把分区目录换成假的（按数据集给各自的键）。

    同样要打在 `src.quant.dataset_store` 上：`_quant_data_sync` 里是延迟 import。
    """
    import src.quant.dataset_store as store_mod

    class _Store:
        def __init__(self, dataset, *_a, **_k):
            self._dataset = dataset

        def keys(self):
            return list(keys_by_dataset.get(self._dataset, []))

    monkeypatch.setattr(store_mod, "DatasetStore", _Store)


# ======================================================================
# 小工具
# ======================================================================


@pytest.mark.parametrize(("stamp", "expect"), [
    ("20260915", "20260916"),
    ("20260930", "20261001"),
    ("20261231", "20270101"),
    ("bad", "bad"),
])
def test_next_day(stamp, expect) -> None:
    assert jobs_mod._next_day(stamp) == expect


def test_warehouse_latest_reads_max_trade_date() -> None:
    assert jobs_mod._warehouse_latest(_Warehouse(latest="20260917"),
                                      "daily") == "20260917"
    assert jobs_mod._warehouse_latest(_Warehouse(latest=""), "daily") == ""
