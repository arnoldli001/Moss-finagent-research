"""ETF 份额启动自检 —— 每次起服务时确认"最新交易日的份额齐不齐"，缺了就补。

## 为什么需要它（2026-09-22 实测事故）

份额是**次日 8:30 左右**才发布的（交易所口径），而 ETF 日线当晚就有。所以
「2026-09-21 17:55 跑一次全市场快照同步」这件事本身就会制造缺口：
`fund_daily` 落了 2138 行，`fund_share` 当时只回 765 行 —— 9 只被监控的宽基里
**7 只**的份额被写成 `NULL`。而面板以 `MAX(trade_date)` 为锚点只读最后一行，
于是「核心宽基 ETF 份额」整块显示成空。

**这不是一次性的数据事故，而是每天晚上都会重演的时序问题**：任何在份额发布前
跑的同步都会留下同样形状的缺口。所以修法不能只是"手工补一次数"，必须在
**进程启动**这条必经路径上放一道自检：

    本地最新交易日 == 该已发布的交易日？
      ├─ 否（本地落后）        → 整段补同步（行情 + 份额一起）
      ├─ 是，但份额成片为 NULL → 只补份额（`sync_etf_shares`，幂等）
      └─ 是，且份额齐全        → 不联网、不发请求

## 三条设计约束

1. **不拦启动。** 挂在 lifespan 的后台任务里、带超时；任何异常只记日志。
   自检失败 = 退回今天之前的行为，绝不等于服务起不来。
2. **不误报。** "今天还没收盘"和"份额还没发布"都不是缺口：前者用
   `latest_closed_trade_date(lag_days=1)` 的日历口径排掉，后者靠
   `fund_share` 返回的**基金类型分布**判定（接口连 OF 基金都没给 = 全市场都还没发布）。
   把这两种情况当成"缺口"会天天白打几十次接口，久了没人再看这条日志。
3. **只补不造。** 补份额走 `UPDATE ... WHERE shares IS NULL`，永远不插入
   "行情有、份额无"的新行 —— 那正是当初出事的机制。见 `datastore.sync_etf_shares`。
"""

from __future__ import annotations

import logging
import os
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief

logger = logging.getLogger(__name__)

#: 判定"份额成片缺失"的比例下限，**可被 `configs/etf_flow.yaml` 的
#: `thresholds.share_guard_missing_ratio` 覆盖**（阈值都放配置里，见该文件头）。
#:
#: 为什么是 30% 而不是 0：`ml_etf` 里混着 `158008.OF` 这类**场外基金**，
#: 它们的份额长期不发布 —— 实测发布完整的那天仍缺 377/2138 ≈ 18%。
#: 拿"有任意一行 NULL"当缺口会让自检每次启动都白打一遍接口（久了没人再看日志）。
#: 而真正的缺口长这样：`fund_share` 还没发布时缺 1373/2138 ≈ 64%。
#: 30% 落在两者之间，且**判据本身优先取被监控证券**（见 `ensure_etf_shares`
#: 里 `codes` 那一支）—— 正常日起伏不会再触发补拉。
MISSING_RATIO_FLOOR = 0.30


@dataclass
class ShareGuardReport:
    """一次自检的结果（可直接 `to_dict()` 给接口 / 日志展示）。"""

    state: str = "skipped"
    #: fresh（无需动作）/ filled（已补齐）/ stale（补了但仍落后）/ unpublished
    #: （份额还没发布，等也等不到）/ no_calendar / no_data / offline / failed
    action: str = ""
    trade_date: str = ""            # 本地 ml_etf 的最新交易日
    reference: str = ""             # 本该齐全的交易日（日历口径）
    rows: int = 0
    with_share: int = 0
    missing: int = 0
    #: 其中**被监控证券**（面板真正展示的那几只）缺份额的行数。判据用它，
    #: 因为全表里混着份额长期不发布的场外基金，按全表判会天天误报。
    watched_missing: int = 0
    changes: dict[int, int] = field(default_factory=dict)   # 第 N 次等待补齐的行数
    waited: float = 0.0
    seconds: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def ratio(self) -> float:
        return round(self.missing / self.rows, 4) if self.rows else 0.0

    def to_dict(self) -> dict[str, Any]:
        return {"state": self.state, "action": self.action,
                "trade_date": self.trade_date, "reference": self.reference,
                "rows": self.rows, "with_share": self.with_share,
                "missing": self.missing, "missing_ratio": self.ratio,
                "watched_missing": self.watched_missing,
                "changes": {str(k): v for k, v in self.changes.items()},
                "waited": round(self.waited, 1),
                "seconds": round(self.seconds, 2), "notes": list(self.notes)}

    def summary(self) -> str:
        head = f"ETF 份额自检：{self.state}"
        if self.trade_date:
            head += (f"（{self.trade_date} 全表 {self.with_share}/{self.rows} 行有份额"
                     f"，监控清单缺 {self.watched_missing}）")
        if self.action:
            head += f" → {self.action}"
        return head + ("；" + "；".join(self.notes) if self.notes else "")


def _floor_from_config() -> float:
    """从 `configs/etf_flow.yaml` 读补拉阈值；读不到就用模块默认值。

    阈值放配置而不是只写死在代码里，与 `etf_flow.py` 的既有取舍一致
    （"阈值都是起点而非结论，应当用回测结果反过来调"）。读取失败**必须**
    回退默认：自检是启动路径，配置坏掉不该变成"启动报错"。
    """
    try:
        from src.mainline.etf_flow import load_config

        value = load_config().threshold("share_guard_missing_ratio")
        if value is None:
            return MISSING_RATIO_FLOOR
        floor = float(value)
        return floor if 0.0 <= floor <= 1.0 else MISSING_RATIO_FLOOR
    except Exception:  # noqa: BLE001 配置不可用时用默认值，不拦启动
        return MISSING_RATIO_FLOOR


def monitored_codes() -> list[str]:
    """ETF 份额监控观测清单里的代码（`configs/etf_flow.yaml` 的 watchlist）。

    自检优先按这份清单判定缺口：`ml_etf` 全表里混着份额长期不发布的场外基金，
    而**面板只展示这些代码** —— 判据与展示口径对齐，才不会"自检说缺、面板好好的"。
    读不到配置时返回空列表（退回全表判据，不影响功能）。
    """
    try:
        from src.mainline.etf_flow import load_config

        return [spec.code for spec in load_config().all_etfs]
    except Exception:  # noqa: BLE001 同上
        return []


def recent_closed_days(store: Any, *, limit: int = 3) -> list[str]:
    """最近的**已收盘**交易日列表（新的在前，最多 `limit` 个）。

    这是整个自检的口径基准，两条边界必须同时成立：
    * `trade_date <= 今天`，且当天 **15:00 之前不算已收盘**（日线都没落定，
      更别说次日才发布的份额）—— 少了这条，每天开盘后都会误报一次缺口；
    * 只认 `ml_calendar` 里 `is_open = 1` 的日子，不拿工作日近似。

    取列表而不是单个日期，是因为"最新已收盘日"的份额本来就**还没发布**：
    它只能用来判断"本地是不是太旧了"，判断"份额该齐了没"要用它的**前一个**交易日。
    调用方按 `days[0]` / `days[1]` 各取所需，口径就不会在两处各写一遍。
    """
    today = datetime.now().strftime("%Y%m%d")
    rows = store._read(  # noqa: SLF001 只读探测，与 etf_flow.build_snapshot 同一手法
        "SELECT trade_date FROM ml_calendar WHERE is_open = 1"
        " AND trade_date <= ? ORDER BY trade_date DESC LIMIT ?",
        (today, max(int(limit), 1) + 1))
    days = [str(row["trade_date"]) for row in rows]
    if days and days[0] == today and datetime.now().hour < 15:
        days = days[1:]
    return days[:max(int(limit), 1)]


def _env_flag(name: str, default: float) -> float:
    """读一个秒数环境变量；非法值回退默认（配置错不该让启动流程崩）。"""
    text = os.environ.get(name, "").strip()
    if not text:
        return default
    try:
        return max(float(text), 0.0)
    except ValueError:
        return default


def ensure_etf_shares(store: Any, *, wait_seconds: float = 0.0,
                      poll_seconds: float = 60.0,
                      timeout_seconds: float = 90.0,
                      codes: list[str] | None = None) -> ShareGuardReport:
    """自检并（必要时）补齐本地 ETF 份额。**只读 + 幂等补份额，不删不改已有值。**

    `wait_seconds`：允许等多久再判"未发布"。默认 0 —— 启动路径上不该凭空多等，
    要等就由调用方（或 `MOSS_ETF_SHARE_WAIT`）显式给，并受 `timeout_seconds` 兜底。
    """
    started = time.monotonic()
    report = ShareGuardReport()
    if store is None:
        report.state, report.notes = "skipped", ["数据仓不可用"]
        return report

    # 判据优先取**被监控证券**（面板真正展示的那几只）；观测清单为空时退回全表。
    # 两者差别很大：全表里混着份额长期不发布的场外基金（实测 377 行），
    # 按全表判会天天误报缺口。`report.rows/with_share/missing` 保持**全表**口径
    # 以便与库直接对账，判据另走 `watched_missing`。
    watch = list(codes) if codes else monitored_codes()
    floor = _floor_from_config()

    def coverage(day: str = "") -> dict[str, Any]:
        raw = store.etf_share_coverage(day)
        gate = store.etf_share_coverage(day, codes=watch) if watch else raw
        raw["gate_rows"] = int(gate.get("rows") or 0)
        raw["gate_missing"] = int(gate.get("missing") or 0)
        return raw

    def load(data: dict[str, Any]) -> float:
        """把一次 coverage 灌进 report，返回**缺口比例**（无样本时 0）。

        ⚠️ 这里返回的必须是 `missing / rows`（缺口），不是
        `etf_share_coverage()["share_ratio"]`（已覆盖）—— 两者分母相同、分子相反。
        早先就是拿后者当判据，于是"缺口 100%"被读成"覆盖 0%"以下，自检永远不补数。
        """
        report.trade_date = str(data.get("trade_date") or "")
        report.rows = int(data.get("rows") or 0)
        report.with_share = int(data.get("with_share") or 0)
        report.missing = int(data.get("missing") or 0)
        report.watched_missing = int(data.get("gate_missing") or 0)
        return report.ratio

    try:
        days = recent_closed_days(store)
        current = coverage()
    except Exception as exc:  # noqa: BLE001 自检失败只降级
        report.state, report.notes = "failed", [brief(exc, BRIEF_TIGHT)]
        report.seconds = time.monotonic() - started
        return report

    report.state = "fresh"
    # ⚠️ `load()` 必须在这里就调用：`report.rows` 全靠它灌进来，下一个判断
    # 就是看它。早先把它放在 `if not report.rows` **之后**，于是每次自检都
    # 直接返回 `no_data`（"本地 ml_etf 表为空"），而库其实好好的 ——
    # 一个纯顺序错误，表现却是"自检永远说没数据"，且不报任何错。
    ratio = load(current)
    if not report.rows:
        report.state = "no_data"
        report.action = "本地 ml_etf 表为空，需先做一次 ETF 同步"
        report.seconds = time.monotonic() - started
        return report
    # ⚠️ 日历为空的判断必须**排在所有 `days[...]` 之前**：那会让 `days[0]` 抛
    # IndexError，自检就从"跳过"变成"异常降级"（虽也不拦启动，但日志里会多一条
    # 无意义的 traceback，把真实原因盖住）。
    if not days:
        report.state = "no_calendar"
        report.action = "本地日历没有已收盘的交易日，跳过新鲜度比对"
        report.seconds = time.monotonic() - started
        return report
    # 口径：`days[0]` = 最新已收盘交易日（份额**通常**还没发布）；
    #       `days[1]` = 份额**必然已发布**的交易日，所以本地一旦落后到它之前，
    #       就不是"等发布"而是真的缺数据了。
    report.reference = days[1] if len(days) > 1 else ""

    # ---- ① 落后太多（连必然已发布的那个交易日都没跟上）→ 整段补同步 ----
    if report.reference and report.trade_date < report.reference:
        report.action = f"本地最新 {report.trade_date} 落后于 {report.reference}，整段补同步"
        result = _resync(store, days[0])
        if result is None:
            report.state = "offline"
            report.notes.append("无 Tushare 源，无法补同步")
            report.seconds = time.monotonic() - started
            return report
        report.notes.append(result.note)
        ratio = load(coverage())
        # 补完仍停在基准日之前 = 这次没补上，如实报 stale（不谎报 filled）；
        # 补上了就继续往下判"那一天的份额齐不齐"。
        if report.trade_date < report.reference:
            report.state = "stale"
            report.seconds = time.monotonic() - started
            return report

    # ---- ② 判"本地最新这一天的份额齐不齐" ----
    #
    # 判据是**全表缺口比例**（`report.missing / report.rows`）而不是"有没有 NULL"：
    # `ml_etf` 里混着 `158008.OF` 这类份额长期不发布的场外基金，实测份额齐全的
    # 那天全表仍缺 18%，而真缺口是 64% —— 阈值 30% 落在两者之间。
    #
    # 为什么不把 `days[0]`（份额本来就还没发布的那天）单独豁免：那要靠猜。
    # 这里选择**实测一次再判**——`sync_etf_shares` 是幂等的 `UPDATE ... WHERE
    # shares IS NULL`，[1] 份额在 → 一次调用补齐；[2] 没发布 → 一次调用拿回 0 行，
    # 如实报 `unpublished`。两种情况都只花**一次** `fund_share`，
    # 换来的是"永远不误报、也永远不漏补"。
    if ratio <= floor:
        report.seconds = time.monotonic() - started
        return report                       # 正常：不发任何网络请求

    result = _backfill(store, report.trade_date, watch or None)
    if result is None:
        report.state = "offline"
        report.action = "无 Tushare 源，无法补份额"
        report.seconds = time.monotonic() - started
        return report
    report.action = f"补份额：{result.note}"
    ratio = load(coverage(report.trade_date))
    if ratio <= floor:
        report.state = "filled"
        report.seconds = time.monotonic() - started
        return report

    # ---- ③ 补完还是缺：这一天的份额这一轮确实还没发布 ----
    deadline = started + max(timeout_seconds, 0.0)
    waited = 0.0
    while (wait_seconds > 0 and waited < wait_seconds
           and time.monotonic() < deadline):
        step = min(poll_seconds, wait_seconds - waited)
        time.sleep(max(step, 1.0))
        waited += step
        again = _backfill(store, report.trade_date, watch or None)
        ratio = load(coverage(report.trade_date))
        if again is not None and again.rows:
            report.changes[int(waited)] = again.rows
        if ratio <= floor:
            report.state = "filled"
            report.waited = waited
            report.seconds = time.monotonic() - started
            return report
    report.state = "unpublished"
    report.waited = waited
    report.notes.append(
        "该交易日份额尚未发布（交易所次日 8:30 左右更新），"
        "面板今日会显示上一交易日的口径；本次不阻塞启动")
    report.seconds = time.monotonic() - started
    return report


def _backfill(store: Any, trade_date: str,
              codes: list[str] | None) -> Any:
    """补份额；无 Tushare 源、或接口整轮失败时返回 None（调用方报 offline）。

    `result.status == "failed"` 要当成 None 而不是"补了 0 行"：那两者在报告里
    必须分开 —— 前者是"数据源不可用"（要人去查），后者是"份额还没发布"（正常）。
    混成一种，运维会为一个正常的时序每天收到一条看起来像故障的记录。
    """
    if getattr(store, "tushare", None) is None:
        return None
    try:
        result = store.sync_etf_shares(trade_date=trade_date, codes=codes)
    except Exception as exc:  # noqa: BLE001 补数失败只记日志，不拦启动
        logger.warning("ETF 份额补拉失败（%s）：%s", trade_date,
                       brief(exc, BRIEF_TIGHT))
        return None
    return None if getattr(result, "status", "") == "failed" else result


def _resync(store: Any, end: str) -> Any:
    """整段补同步（行情 + 份额，回看 10 天）；无 Tushare 源返回 None。"""
    if getattr(store, "tushare", None) is None:
        return None
    try:
        return store.sync_catchup(days=10)
    except Exception as exc:  # noqa: BLE001 同步失败只降级，不拦启动
        logger.warning("ETF 整段补同步失败（目标 %s）：%s", end,
                       brief(exc, BRIEF_TIGHT))
        return None


def startup_wait_seconds() -> float:
    """启动时允许等待份额发布的秒数（`MOSS_ETF_SHARE_WAIT`，默认 0 = 不等）。

    默认 0 的理由：份额在**次日 8:30 左右**发布，而服务通常在发布之后启动 ——
    绝大多数启动"数据本来就在"，等待只会白白拖慢首屏。真正需要等的场景
    （夜间自动重启、盘中反复重启）由运维显式给一个值。
    """
    return _env_flag("MOSS_ETF_SHARE_WAIT", 0.0)


def log_report(report: ShareGuardReport) -> None:
    """按结果选级别打日志：正常情况一行 info，真补了 / 真缺了才 warning。"""
    text = report.summary()
    if report.state in ("failed", "unpublished", "stale"):
        logger.warning("%s", text)
    elif report.action or report.missing:
        logger.info("%s", text)
    else:
        logger.debug("%s", text)
