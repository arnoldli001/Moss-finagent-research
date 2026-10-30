#!/usr/bin/env python
"""数据新鲜度 + 作业暂停 的**一体化自检**（2026-10-30 事故的护栏）。

## 为什么需要它（事故本身）

2026-10-30 用户问：

> 「只要开机，定期自动刷新的脚本是否按预期执行了？」

实测答案：**调度器确实在跑**（`data/pilot/scheduler/runs.jsonl` 到当年累计
9417 条记录，当天还在跑 `us_macro_daily` / `intel_hot_rank` / `quant_data_sync`），
但三条关键链路**各自静默停更**：

| 链路 | 真相 | 停更原因 |
|---|---|---|
| 主线挖掘 | `mainline_score` 停在 **20261009** | `mainline_daily` 连续三次 failed ⇒ 调度器**自动暂停**，18 天没人知道 |
| ETF 份额 | `ml_etf` 停在 **20261023** | 同上（ETF 同步在 `mainline_daily` 里） |
| 板块拥挤度 | `sector_crowding_daily` 停在 **20261023** | 刷新它的是**Windows 计划任务** `MossCrowdingRefresh`，该任务已被删除（`CHG-0244` 记过 360 会清理计划任务） |

三个模块**都没有告警**：暂停只写一行 `INFO 已被自动暂停（连续失败），跳过`，
数据停更则完全没有痕迹 —— 用户只能靠"看页面上的日期"发现。

## 这个脚本回答两个问题

1. **数据新不新**：每个数据集的最新交易日 vs **交易日历算出的应有交易日**；
2. **作业有没有被暂停**：扫所有实例的 `runs.jsonl`，按"最后 3 次全是 failed"
   （= `RunLog.is_paused()` 的同一判据）算出来。

退出码：`0` 全部正常 · `1` 有陈旧/暂停（**可直接当定时任务/CI 的判据**）·
`2` 自检本身跑不起来（路径/库读不到）—— 三种分开，避免"查不到"被当成"没问题"。

## 判据必须认机器可读的东西

- 数据：**日期比较**，不是"有没有报错"；
- 暂停：**状态枚举**（`failed` 计数），不是日志文案；
- 输出：每行一个 `CODE`（`STALE_*` / `PAUSED_*`），供脚本 grep。

## 已知边界（诚实登记）

- "应有交易日"取 `ml_calendar.is_open=1` 的最近一天。**日历自己也会过期**
  （它由 `mainline_daily` 同步）—— 所以日历滞后时会单独报 `STALE_calendar`，
  而不是拿一个过期日历去判别人。
- 只读，绝不写任何库。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]

#: 暂停冷却天数（与 `src/scheduler/registry.PAUSE_COOLDOWN_DAYS` 同一个口径）。
#: 这里**不 import** `src.*`：本脚本要在"配置坏了/库打不开"时也能跑，
#: 依赖 `src` 会把自检本身变成又一个会坏的东西。两边的一致性由
#: `tests/unit/test_data_freshness_check.py` 里一条"常量必须相等"的判据钉住。
PAUSE_COOLDOWN_DAYS = 7

#: 共享库（多个实例共写，见 `configs/data_stores.yaml`）
SHARED_DB = "data/moss_finagent.db"
#: 主线行情缓存（板块指数/资金流/ETF 份额/两融…）
MAINLINE_CACHE_DB = "data/mainline_cache.db"
#: 行情仓库（量化选股/周频指标的数据底座）
WAREHOUSE_DB = "data/quant/warehouse.db"

#: 每个数据集最多允许落后几天（按交易日算）。`quant_daily` 是参照物，不判。
DEFAULT_TOLERANCE = 1


@dataclass
class Finding:
    code: str            # 机器可读：STALE_crowding / PAUSED_mainline_daily …
    subject: str         # 人读的名字
    detail: str
    latest: str = ""     # 实际最新交易日
    expected: str = ""   # 应有交易日（能算出来时）

    def render(self) -> str:
        tail = ""
        if self.latest or self.expected:
            tail = f"  (最新={self.latest or '—'} 应有={self.expected or '—'})"
        return f"[{self.code}] {self.subject}：{self.detail}{tail}"


@dataclass
class Report:
    findings: list[Finding] = field(default_factory=list)
    checked: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.findings

    def render(self) -> str:
        lines = [f"检查了 {len(self.checked)} 项：" + "、".join(self.checked)]
        if self.ok:
            lines.append("✅ 全部新鲜，没有被暂停的作业")
        else:
            lines.append(f"❌ {len(self.findings)} 处异常：")
            lines.extend("  " + item.render() for item in self.findings)
        return "\n".join(lines)


# --------------------------------------------------------------- 纯函数（可测）

def _norm(day: object) -> str:
    """把各种日期形态归一成 `YYYYMMDD`；认不出返回空串。"""
    text = str(day or "").strip()
    if not text or text in ("None", "nan"):
        return ""
    digits = "".join(ch for ch in text[:10] if ch.isdigit())
    return digits[:8] if len(digits) >= 8 else ""


def trading_days_behind(latest: str, expected: str) -> int | None:
    """`latest` 比 `expected` 落后 **几个自然日**；算不出来返回 `None`。

    用自然日而不是交易日：这里只判"是不是明显不新鲜"，
    自然日更保守（不会因为跨周末而漏判），漏报比误报可怕。
    """
    a, b = _norm(latest), _norm(expected)
    if not a or not b:
        return None
    try:
        da = datetime.strptime(a, "%Y%m%d")
        db = datetime.strptime(b, "%Y%m%d")
    except ValueError:
        return None
    return (db - da).days


def judge(max_date: str, expected: str, *, tolerance: int = DEFAULT_TOLERANCE
          ) -> tuple[bool, str]:
    """（是否陈旧, 说明）。`expected` 为空时**不敢判**，返回"未知"。"""
    behind = trading_days_behind(max_date, expected)
    if behind is None:
        return False, "日期无法比较（缺数据或缺日历）"
    if behind < 0:
        return False, f"比应有交易日还新 {-behind} 天（日历可能过期）"
    if behind > tolerance:
        return True, f"落后 {behind} 天（容忍 {tolerance} 天）"
    return False, f"落后 {behind} 天，在容忍范围内"


def paused_jobs(records: list[dict], *, threshold: int = 3,
                cooldown_days: int = PAUSE_COOLDOWN_DAYS,
                now: datetime | None = None) -> list[str]:
    """**当前**处于暂停状态的作业（与 `RunLog.is_paused()` 逐条对齐）。

    判据两条同时成立：
      ① 最后 `threshold` 条可判记录全是 `failed`；
      ② 最后一次失败距今 **不足** `cooldown_days` 天。

    ⚠️ ② 不能省：2026-10-30 之前 `is_paused` 没有时间维度，`mainline_daily`
    在 10-12 连续失败三次后被**永久**暂停（18 天没再被尝试）。加了冷却期之后，
    超过 7 天会自动放行一次 —— 那时它**不该**再被报成"暂停"，
    否则脚本说暂停、调度器却在正常跑，两边互相打脸。
    """
    now = now or datetime.now().astimezone()
    by_job: dict[str, list[dict]] = {}
    for rec in records:
        status = str(rec.get("status") or "")
        if status not in ("success", "partial", "failed"):
            continue          # running / skipped 不计入（与 RunLog 一致）
        by_job.setdefault(str(rec.get("job_name") or ""), []).append(rec)
    out = []
    for name, history in sorted(by_job.items()):
        tail = history[-threshold:]
        if len(tail) != threshold or not all(r.get("status") == "failed" for r in tail):
            continue
        raw = str(tail[-1].get("end_time") or tail[-1].get("start_time") or "")
        try:
            since = datetime.fromisoformat(raw)
        except ValueError:
            since = None
        if since is not None:
            if since.tzinfo is None:
                since = since.astimezone()
            if (now - since).days >= cooldown_days:
                continue          # 冷却期已过 ⇒ 调度器会再试一次，不算暂停
        out.append(name)
    return out


# --------------------------------------------------------------- 读库（只读）

def _max_date(db: str, table: str, column: str) -> str:
    path = REPO / db
    if not path.is_file():
        return ""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            row = conn.execute(f"SELECT MAX({column}) FROM {table}").fetchone()
            return _norm(row[0]) if row else ""
        finally:
            conn.close()
    except sqlite3.Error:
        return ""


def expected_trading_day() -> tuple[str, str]:
    """（应有交易日, 日历自身最新日）。日历不可用时返回两个空串。"""
    path = REPO / MAINLINE_CACHE_DB
    if not path.is_file():
        return "", ""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        try:
            today = datetime.now().strftime("%Y%m%d")
            row = conn.execute(
                "SELECT MAX(trade_date) FROM ml_calendar"
                " WHERE is_open = 1 AND trade_date <= ?", (today,)).fetchone()
            calendar_max = conn.execute(
                "SELECT MAX(trade_date) FROM ml_calendar").fetchone()
            return (_norm(row[0]) if row else "",
                    _norm(calendar_max[0]) if calendar_max else "")
        finally:
            conn.close()
    except sqlite3.Error:
        return "", ""


def scan_pause_logs(*, everywhere: bool = False) -> list[tuple[str, str, list[str]]]:
    """扫运行记录 → [(日志路径, 最后写入日期, 被暂停的作业名)]。

    ⚠️ 默认**只扫当前环境**那本账（`settings.scheduler_dir/runs.jsonl`）：
    `data/scheduler/` 与 `data/dev/scheduler/` 是**历史遗留的旧账**（前者最后一条
    在 2026-10-09，后者 10-27），把它们一起报出来会出现一堆"早就被暂停、
    而且那个实例已经不在跑"的噪音 —— 20 条告警里 13 条是这种，
    真正要紧的两条（pilot 的 `mainline_daily`）反而被淹没。
    要查历史用 `--all`。
    """
    if everywhere:
        paths = sorted((REPO / "data").glob("**/scheduler/runs.jsonl"))
    else:
        current = _scheduler_dir() / "runs.jsonl"
        paths = [current] if current.is_file() else sorted(
            (REPO / "data").glob("**/scheduler/runs.jsonl"))
    out = []
    for path in paths:
        records = []
        last_seen = ""
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue          # 半截行（并发写）不算证据
                    records.append(rec)
                    last_seen = str(rec.get("start_time") or last_seen)
        except OSError:
            continue
        names = paused_jobs(records)
        if names:
            out.append((_rel(path), last_seen[:19], names))
    return out


def _rel(path: Path) -> str:
    """仓内相对路径；不在仓内/是相对路径时原样返回（**绝不因为展示路径而崩**）。"""
    try:
        return str(path.resolve().relative_to(REPO)).replace("\\", "/")
    except (ValueError, OSError):
        return str(path).replace("\\", "/")


# --------------------------------------------------------------- 汇总

def check(*, tolerance: int = DEFAULT_TOLERANCE,
          everywhere: bool = False) -> Report:
    report = Report()
    expected, calendar_max = expected_trading_day()

    if not expected:
        report.findings.append(Finding(
            code="STALE_calendar", subject="交易日历（ml_calendar）",
            detail="读不到可用日历 ⇒ 下面所有日期判据都失去参照物"))
    else:
        report.checked.append(f"日历={expected}")
        # 日历自己也会过期：落后今天超过 7 个自然日就单独报一条
        behind_today = trading_days_behind(calendar_max, datetime.now().strftime("%Y%m%d"))
        if behind_today is not None and behind_today > 7:
            report.findings.append(Finding(
                code="STALE_calendar", subject="交易日历（ml_calendar）",
                detail=f"日历本身落后今天 {behind_today} 天（同步它的作业可能也停了）",
                latest=calendar_max, expected=""))

    datasets = [
        # (code 后缀, 人读名, 库, 表, 日期列)
        ("crowding", "板块拥挤度日线", SHARED_DB, "sector_crowding_daily", "trade_date"),
        ("etf_share", "ETF 份额（ml_etf）", MAINLINE_CACHE_DB, "ml_etf", "trade_date"),
        ("mainline_score", "主线评分", None, "mainline_score", "trade_date"),
        ("board_bar", "主线板块日线（ml_board_bar）", MAINLINE_CACHE_DB,
         "ml_board_bar", "trade_date"),
    ]
    for code, label, db, table, column in datasets:
        if db is None:                      # 各实例自己的库
            db = _env_db()
        latest = _max_date(db, table, column)
        nametag = f"{code}"
        if not latest:
            report.findings.append(Finding(
                code=f"STALE_{nametag}", subject=label,
                detail=f"{db}:{table} 读不到数据（库不存在/表为空）"))
            continue
        stale, why = judge(latest, expected, tolerance=tolerance)
        report.checked.append(f"{code}={latest}")
        if stale:
            report.findings.append(Finding(
                code=f"STALE_{nametag}", subject=label, detail=f"{why}（{db}）",
                latest=latest, expected=expected))

    for log_path, last_seen, names in scan_pause_logs(everywhere=everywhere):
        for name in names:
            report.findings.append(Finding(
                code="PAUSED_job", subject=f"作业 {name}",
                detail=f"最近 3 次全部 failed ⇒ 调度器已自动暂停它"
                       f"（记录：{log_path}，最后一条 {last_seen or '—'}）"))
    return report


def _scheduler_dir() -> Path:
    """当前环境的调度目录（取不到就回默认 `data/scheduler`）。"""
    try:
        sys.path.insert(0, str(REPO))
        from src.core.config import get_settings

        return Path(get_settings().scheduler_dir)
    except Exception:  # noqa: BLE001
        return REPO / "data" / "scheduler"


def _env_db() -> str:
    """当前环境的主库路径（pilot/dev/test 各自一份）。取不到就用共享库。"""
    try:
        sys.path.insert(0, str(REPO))
        from src.core.config import get_settings

        raw = str(get_settings().sqlite_path or "")
        if raw:
            path = Path(raw)
            try:
                return str(path.resolve().relative_to(REPO)).replace("\\", "/")
            except ValueError:
                return str(path)
    except Exception:  # noqa: BLE001 自检不该因为"配置读不到"就崩
        pass
    return SHARED_DB


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="数据新鲜度 + 作业暂停自检（只读；退出码 0=正常 1=有异常 2=自检失败）")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    parser.add_argument("--all", action="store_true",
                        help="扫所有实例的运行记录（默认只扫当前环境那本账）")
    parser.add_argument("--env", choices=("dev", "pilot"), default="",
                        help="按该实例的隔离环境检查（dev / pilot）")
    parser.add_argument("--tolerance", type=int, default=DEFAULT_TOLERANCE,
                        help=f"允许落后几个自然日（默认 {DEFAULT_TOLERANCE}）")
    args = parser.parse_args(argv)
    if args.env:
        # 必须在**第一次** get_settings() 之前落环境：Settings 是 lru_cache 单例，
        # 晚一步就拿到默认（dev）的库路径 —— 这个坑在 manage.py 里也记过。
        try:
            sys.path.insert(0, str(REPO))      # 让 `import manage` 能找到仓库根
            import manage

            overrides = (manage.pilot_isolation_env() if args.env == "pilot"
                         else manage.dev_isolation_env())
            for key, value in overrides.items():
                os.environ[key] = str(value)
        except Exception as exc:  # noqa: BLE001
            print(f"SELFCHECK_FAILED: 无法应用 --env {args.env}：{exc}", file=sys.stderr)
            return 2
    try:
        report = check(tolerance=args.tolerance, everywhere=args.all)
    except Exception as exc:  # noqa: BLE001 自检自己坏了要能被区分出来
        print(f"SELFCHECK_FAILED: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps({
            "ok": report.ok,
            "checked": report.checked,
            "findings": [finding.__dict__ for finding in report.findings],
        }, ensure_ascii=False, indent=2))
    else:
        print(report.render())
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    raise SystemExit(main())
