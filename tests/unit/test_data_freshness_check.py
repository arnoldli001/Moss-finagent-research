"""`scripts/data_freshness_check.py` 的判据自证（2026-10-30 事故的护栏）。

## 这个脚本为什么值得一组测试

它存在的唯一理由，是**让"停更"这件事自己浮出来** —— 而 2026-10-30 那次
真实事故里，三条链路分别停了 7 / 18 / 21 天，系统**一声没响**：

- `mainline_daily` 连续三次 `failed` ⇒ 调度器自动暂停它，只写一行 INFO；
- 拥挤度日线靠 Windows 计划任务刷新，任务被删掉 ⇒ 完全没有痕迹；
- 数据停更本身从来不是"错误"，所以任何"看报错"的判据都抓不到。

⇒ 判据必须只剩两条硬的：**日期比日期**、**状态数枚举**。
下面每条测试都在钉这两个东西的**边界**（容忍天数、跳跃记录、半截行）。
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_SCRIPT = REPO / "scripts" / "data_freshness_check.py"


def _load():
    spec = importlib.util.spec_from_file_location("data_freshness_check", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    # ⚠️ 必须先登记到 `sys.modules` 再 exec：这个脚本里用了 `@dataclass`，
    #    而 `dataclasses` 在处理字段类型时会去 `sys.modules[cls.__module__]`
    #    查名字 —— 没登记就抛 `'NoneType' object has no attribute '__dict__'`，
    #    报错位置在标准库里，完全看不出是"加载器少了一行"。
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


freshness = _load()


# --------------------------------------------------------------- 日期归一

@pytest.mark.parametrize("raw,expected", [
    ("20261029", "20261029"),
    ("2026-10-29", "20261029"),
    ("2026-10-29T15:00:00+08:00", "20261029"),
    ("", ""),
    (None, ""),
    ("None", ""),
    ("nan", ""),
    ("2026-10", ""),        # 不完整，不许凑成 202610xx
])
def test_norm_accepts_real_date_shapes(raw, expected) -> None:
    """日期归一要认 `YYYYMMDD` / `YYYY-MM-DD` / ISO 三种形态，垃圾返回空串。

    ⚠️ 空串与"很旧"**必须分开**：认不出时返回空 ⇒ 上层报"读不到数据"，
    而不是拿 `0` 去比较、把"没查"显示成"落后一万天"。
    """
    assert freshness._norm(raw) == expected


def test_trading_days_behind_returns_none_when_unparsable() -> None:
    assert freshness.trading_days_behind("", "20261030") is None
    assert freshness.trading_days_behind("20261029", "") is None


# --------------------------------------------------------------- 陈旧判定

def test_judge_fresh_data_within_tolerance() -> None:
    stale, why = freshness.judge("20261029", "20261030")
    assert not stale and "落后 1 天" in why


def test_judge_flags_data_beyond_tolerance() -> None:
    """事故里 ETF 份额落后 7 天、主线评分落后 21 天 —— 都必须判陈旧。"""
    stale, why = freshness.judge("20261023", "20261030")
    assert stale and "7 天" in why
    stale, _ = freshness.judge("20260924", "20261030", tolerance=1)
    assert stale


def test_judge_does_not_flag_when_calendar_is_stale() -> None:
    """数据比"应有交易日"还新 ⇒ 是**日历**过期，不该把数据判成陈旧。"""
    stale, why = freshness.judge("20261030", "20261023")
    assert not stale and "日历可能过期" in why


def test_judge_unknown_dates_are_not_silently_ok() -> None:
    """算不出来时返回 `False`（不阻断），但说明里必须写明"无法比较"。

    脚本的**调用方**（下面的 `check()`）会把"读不到数据"单独报一条
    `STALE_*`，所以这里返回 `False` 不会造成"静默通过"。
    """
    stale, why = freshness.judge("", "20261030")
    assert not stale and "无法比较" in why


def test_tolerance_is_configurable() -> None:
    assert not freshness.judge("20261027", "20261030", tolerance=3)[0]
    assert freshness.judge("20261027", "20261030", tolerance=2)[0]


# --------------------------------------------------------------- 暂停判定

def _rec(name: str, status: str) -> dict:
    return {"job_name": name, "status": status, "start_time": "2026-10-30T08:00:00"}


def test_paused_after_three_consecutive_failures() -> None:
    """与 `RunLog.is_paused()` **同一判据**：最后 3 次全是 failed。

    这条对齐很重要：脚本与调度器用不同判据时，会出现"脚本说暂停、
    调度器还在跑"（或反过来），两边都没错，但结论互相打脸。
    """
    records = [_rec("mainline_daily", "failed") for _ in range(3)]
    assert freshness.paused_jobs(records) == ["mainline_daily"]


def test_two_failures_are_not_paused() -> None:
    records = [_rec("mainline_daily", "failed") for _ in range(2)]
    assert freshness.paused_jobs(records) == []


def test_success_breaks_the_streak() -> None:
    """中间成功过一次 ⇒ 连续失败计数被打断（这是"人工介入后恢复"的机制）。"""
    records = ([_rec("mainline_daily", "failed") for _ in range(2)]
               + [_rec("mainline_daily", "success")]
               + [_rec("mainline_daily", "failed") for _ in range(2)])
    assert freshness.paused_jobs(records) == []


def test_running_and_skipped_records_are_ignored() -> None:
    """`running` / `skipped` 不参与（与 `RunLog.is_paused` 的过滤条件一致）。

    否则"三次 running"（进程刚重启）会被误判成"暂停"。
    """
    records = ([_rec("x", "failed") for _ in range(2)]
               + [_rec("x", "running"), _rec("x", "skipped")])
    assert freshness.paused_jobs(records) == []


def test_paused_jobs_are_sorted_per_job() -> None:
    """多作业各自独立计数，输出按名字排序（便于 diff / grep）。"""
    records = ([_rec("b_job", "failed") for _ in range(3)]
               + [_rec("a_job", "failed") for _ in range(3)])
    assert freshness.paused_jobs(records) == ["a_job", "b_job"]


# --------------------------------------------------------------- 端到端汇总

def test_check_reports_stale_dataset_and_paused_job(monkeypatch) -> None:
    """汇总层：陈旧数据 + 被暂停作业都要进 findings，且 `ok` 为 False。"""
    monkeypatch.setattr(freshness, "expected_trading_day",
                        lambda: ("20261030", "20261030"))
    monkeypatch.setattr(freshness, "_max_date",
                        lambda db, table, column: {
                            "ml_etf": "20261023",           # 落后 7 天 ⇒ 陈旧
                            "sector_crowding_daily": "20261030",
                            "mainline_score": "20261030",
                            "ml_board_bar": "20261030",
                        }.get(table, ""))
    monkeypatch.setattr(freshness, "scan_pause_logs",
                        lambda everywhere=False: [("data/pilot/scheduler/runs.jsonl",
                                                  "2026-10-30T08:50:02",
                                                  ["mainline_daily"])])
    report = freshness.check()
    assert not report.ok
    codes = [finding.code for finding in report.findings]
    assert "STALE_etf_share" in codes
    assert "PAUSED_job" in codes
    # 其余三项新鲜 ⇒ 不该出现它们的 STALE
    assert "STALE_crowding" not in codes
    assert "STALE_mainline_score" not in codes


def test_check_without_calendar_reports_calendar_not_ok(monkeypatch) -> None:
    """日历读不到时**不许**安静地判"全部新鲜"。"""
    monkeypatch.setattr(freshness, "expected_trading_day", lambda: ("", ""))
    monkeypatch.setattr(freshness, "_max_date", lambda *a: "20261030")
    monkeypatch.setattr(freshness, "scan_pause_logs", lambda everywhere=False: [])
    report = freshness.check()
    assert not report.ok
    assert [f.code for f in report.findings] == ["STALE_calendar"]


def test_main_exit_codes(monkeypatch, capsys) -> None:
    """退出码三态：0 正常 / 1 有异常 / 2 自检自己坏了。

    ★ "查不到"必须落在 2 或 1，**不能**落 0 —— 否则定时任务把
    "自检没跑起来"当成"数据都好"，正是这次事故的形状。
    """
    class _Report:
        ok = True
        checked: list[str] = []
        findings: list = []

        def render(self) -> str:
            return "✅ 全部新鲜"

    monkeypatch.setattr(freshness, "check", lambda **kwargs: _Report())
    assert freshness.main([]) == 0

    class _Bad(_Report):
        ok = False

    monkeypatch.setattr(freshness, "check", lambda **kwargs: _Bad())
    assert freshness.main([]) == 1

    def _boom(**kwargs):
        raise RuntimeError("库读不到")

    monkeypatch.setattr(freshness, "check", _boom)
    assert freshness.main([]) == 2


def test_json_output_is_machine_readable(monkeypatch, capsys) -> None:
    """`--json` 的字段名就是**对外契约**（定时任务/CI 按 `code` grep）。

    这里刻意用**真的** `Finding` 数据类而不是假对象：假对象若只放了类属性，
    `__dict__` 里是空的，`--json` 输出会静默丢掉字段 —— 那一版就是这么漏的。
    """
    finding = freshness.Finding(code="STALE_etf_share", subject="ETF 份额",
                                detail="落后 7 天", latest="20261023",
                                expected="20261030")

    class _Report:
        ok = False
        checked = ["crowding=20261030"]
        findings = [finding]

        def render(self) -> str:
            return "unused"

    monkeypatch.setattr(freshness, "check", lambda **kwargs: _Report())
    assert freshness.main(["--json"]) == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["findings"][0]["code"] == "STALE_etf_share"
    assert payload["findings"][0]["latest"] == "20261023"
