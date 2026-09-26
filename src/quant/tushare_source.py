"""Tushare Pro 数据源（5000 积分档）：客户端、频次控制、权限自检、字段规范化。

**token 解析三级兜底**（实测踩坑）：用户把 token 写进 Windows 环境变量后，
正在运行的服务进程**不一定**能看到 —— 子进程继承的是父进程启动时的环境快照。
实测：注册表 User/Machine 两个作用域都写好了 `tushare_token`，
但 `os.environ` 里是空的（DSH 进程早于设置环境变量启动）。
因此解析顺序为：

    1. 环境变量（大小写都试，Windows 上 `tushare_token` / `TUSHARE_TOKEN` 等价）
    2. 项目根目录 `.env`（key=value 文本）
    3. **Windows 注册表**（当前用户 → 本机），仅 win32 生效

第三级让"设置完环境变量但没重启进程"也能直接跑通，不至于卡在配置上。
token 全程不打印、不落日志（只报长度与首尾 4 位）。

**5000 积分档的能力**（doc 290 表一）：500 次/分、常规数据无上限，可调 2000～5000 分档接口，
其中 `fina_indicator_vip`（全市场财务横截面）是 5000 分档的关键接口。

单位规范（**这里不统一就会出 10000 倍级错误**）：
    金额 → 元（Tushare 原口径：daily.amount 千元、daily_basic.total_mv 万元、
                moneyflow 万元、fina_indicator.fcff 元）
    股本 → 股（原口径：total_share/float_share/free_share 万股）
    比率/百分比 → 保持百分数（如 roe=18.5 表示 18.5%），字段名带 pct 的即是
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pandas as pd

from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)

logger = logging.getLogger(__name__)

_ENV_KEYS = ("TUSHARE_TOKEN", "tushare_token", "TS_TOKEN")
_WAN = 1.0e4          # 万元 → 元
_QIAN = 1.0e3         # 千元 → 元


# ==================================================================
# token 解析
# ==================================================================


def _from_env() -> str:
    for key in _ENV_KEYS:
        value = os.environ.get(key)
        if value and value.strip():
            return value.strip()
    return ""


def _from_dotenv(root: Path | None = None) -> str:
    candidates = [Path(root or ".") / ".env", Path(root or ".") / ".env.local"]
    for path in candidates:
        if not path.exists():
            continue
        try:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                stripped = line.strip()
                if not stripped or stripped.startswith("#") or "=" not in stripped:
                    continue
                key, _, raw = stripped.partition("=")
                if key.strip().upper() in ("TUSHARE_TOKEN", "TS_TOKEN"):
                    value = raw.strip().strip("'\"")
                    if value:
                        return value
        except OSError:  # pragma: no cover - 文件不可读时继续兜底
            continue
    return ""


def _from_windows_registry() -> str:
    if os.name != "nt":
        return ""
    try:
        import winreg  # type: ignore[import-not-found]
    except ImportError:  # pragma: no cover
        return ""
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        for name in ("tushare_token", "TUSHARE_TOKEN"):
            try:
                with winreg.OpenKey(hive, "Environment") as handle:
                    value, _ = winreg.QueryValueEx(handle, name)
            except OSError:
                continue
            if value and str(value).strip():
                return str(value).strip()
    return ""


def resolve_token(token: str | None = None) -> str:
    """解析 token（显式传入 → 环境变量 → .env → Windows 注册表）。"""
    if token and token.strip():
        return token.strip()
    for source, getter in (("环境变量", _from_env),
                           (".env", _from_dotenv),
                           ("Windows 注册表", _from_windows_registry)):
        value = getter()
        if value:
            logger.debug("Tushare token 来源：%s", source)
            return value
    raise TushareConfigError(
        "未找到 Tushare token。请任选一种方式设置后重试：\n"
        "  1) 环境变量 TUSHARE_TOKEN=xxx（设置后需重启进程/终端）\n"
        "  2) 项目根目录 .env 写 TUSHARE_TOKEN=xxx\n"
        "  3) Windows 系统环境变量（用户或系统级，本模块会直接读注册表，无需重启）")


def token_hint(token: str) -> str:
    """安全提示串（永不返回完整 token）。"""
    if not token:
        return "（空）"
    return f"{token[:4]}…{token[-4:]}（长度 {len(token)}）"


# ==================================================================
# 异常
# ==================================================================


class TushareError(RuntimeError):
    """Tushare 调用失败的基类。"""


class TushareConfigError(TushareError):
    """配置问题（token 缺失等）。"""


class TusharePermissionError(TushareError):
    """积分/权限不足 —— 这类错误必须显式暴露，不能被当成"没数据"糊过去。"""


class TushareRateLimitError(TushareError):
    """频次超限（重试若干次仍失败）。"""


# ==================================================================
# 客户端
# ==================================================================


@dataclass
class _RateLimiter:
    """滑动窗口限流（5000 积分档 = 500 次/分）。

    下载是并发跑 `asyncio.to_thread` 的，多个线程会同时进这里 —— 必须加锁，
    否则窗口计数被竞争破坏，一分钟内可能放行远超上限的请求（然后被服务端限流）。
    """

    max_calls: int = 500
    window_seconds: float = 60.0
    _stamps: list[float] = field(default_factory=list)
    _lock: Any = field(default_factory=lambda: __import__("threading").Lock())

    def acquire(self) -> float:
        """返回需要等待的秒数（并记录本次调用）。"""
        with self._lock:
            now = time.monotonic()
            cutoff = now - self.window_seconds
            self._stamps = [stamp for stamp in self._stamps if stamp > cutoff]
            wait = 0.0
            if len(self._stamps) >= self.max_calls:
                wait = self.window_seconds - (now - self._stamps[0]) + 0.05
            self._stamps.append(now + max(0.0, wait))
            return max(0.0, wait)


@dataclass
class CallStats:
    """调用统计（进度展示 + 频次体检）。"""

    calls: int = 0
    retries: int = 0
    waited_seconds: float = 0.0
    failures: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {"calls": self.calls, "retries": self.retries,
                "waited_seconds": round(self.waited_seconds, 1),
                "failures": dict(list(self.failures.items())[:5])}


class TushareClient:
    """带频次控制与错误分类的 Tushare Pro 客户端（同步 + 异步两套入口）。"""

    def __init__(self, token: str | None = None, *,
                 max_calls_per_minute: int = 500,
                 retries: int = 3,
                 retry_wait: float = 3.0) -> None:
        self._token = resolve_token(token)
        self._pro: Any = None
        self._limiter = _RateLimiter(max_calls=max_calls_per_minute)
        self._retries = max(1, int(retries))
        self._retry_wait = retry_wait
        self.stats = CallStats()

    # ---------- 底层 ----------

    @property
    def pro(self) -> Any:
        if self._pro is None:
            import tushare as ts
            self._pro = ts.pro_api(self._token)
        return self._pro

    # ---------- 同步调用 ----------

    def call(self, api: str, **params: Any) -> pd.DataFrame:
        """调用一个接口，返回 DataFrame（空结果返回空表，不抛错）。

        错误分三类：权限/积分不足（立即抛）、频次超限（等待重试）、其它（退避重试）。
        """
        last_error: Exception | None = None
        for attempt in range(self._retries):
            wait = self._limiter.acquire()
            if wait > 0:
                self.stats.waited_seconds += wait
                time.sleep(wait)
            try:
                result = getattr(self.pro, api)(**params)
                self.stats.calls += 1
                if result is None:
                    return pd.DataFrame()
                return result if isinstance(result, pd.DataFrame) else pd.DataFrame(result)
            except Exception as exc:  # noqa: BLE001 SDK 抛的都是普通 Exception
                message = str(exc)
                self.stats.calls += 1
                self.stats.failures[api] = message[:160]
                if _is_permission_error(message):
                    raise TusharePermissionError(
                        f"{api} 权限/积分不足：{message[:200]}"
                        f"\n（5000 积分档应可调用 daily_basic/moneyflow/stk_limit/"
                        f"suspend_d/adj_factor/fina_indicator_vip/bak_daily；"
                        f"若报错请到权限中心确认积分是否到账）") from exc
                if _is_rate_limit_error(message):
                    self.stats.retries += 1
                    time.sleep(self._retry_wait * (attempt + 1))
                    last_error = TushareRateLimitError(f"{api} 频次超限：{message[:160]}")
                    continue
                last_error = TushareError(f"{api} 调用失败：{message[:200]}")
                if attempt < self._retries - 1:
                    self.stats.retries += 1
                    time.sleep(self._retry_wait * (attempt + 1))
                    continue
                raise last_error from exc
        raise last_error or TushareError(f"{api} 调用失败（重试 {self._retries} 次）")

    async def acall(self, api: str, **params: Any) -> pd.DataFrame:
        """异步入口（SDK 是阻塞的，丢线程池跑）。"""
        return await asyncio.to_thread(self.call, api, **params)

    # ---------- 权限自检 ----------

    def probe(self) -> dict[str, dict[str, Any]]:
        """逐接口小样本探测，返回每个接口的可用性（用户排障用）。"""
        checks: list[tuple[str, dict[str, Any]]] = [
            ("stock_basic", {"exchange": "", "list_status": "L"}),
            ("trade_cal", {"exchange": "SSE", "start_date": "20260101",
                           "end_date": "20260110"}),
            ("daily", {"trade_date": _recent_trade_date()}),
            ("daily_basic", {"trade_date": _recent_trade_date()}),
            ("adj_factor", {"trade_date": _recent_trade_date()}),
            ("stk_limit", {"trade_date": _recent_trade_date()}),
            ("suspend_d", {"trade_date": _recent_trade_date(), "suspend_type": "S"}),
            ("moneyflow", {"trade_date": _recent_trade_date()}),
            ("index_dailybasic", {"trade_date": _recent_trade_date()}),
            ("bak_daily", {"trade_date": _recent_trade_date()}),
            ("fina_indicator_vip", {"period": _recent_period()}),
        ]
        report: dict[str, dict[str, Any]] = {}
        for api, params in checks:
            try:
                frame = self.call(api, **params)
                report[api] = {"ok": True, "rows": int(len(frame)),
                               "columns": list(frame.columns)[:12],
                               "params": params}
            except TusharePermissionError as exc:
                report[api] = {"ok": False, "kind": "permission",
                               "detail": brief(exc, BRIEF_DEFAULT), "params": params}
            except Exception as exc:  # noqa: BLE001
                report[api] = {"ok": False, "kind": type(exc).__name__,
                               "detail": brief(exc, BRIEF_DEFAULT), "params": params}
        return report


def _recent_trade_date() -> str:
    """最近一个工作日（够探活用，不追求精确交易日历）。"""
    stamp = pd.Timestamp.today().normalize()
    while stamp.weekday() >= 5:
        stamp -= pd.Timedelta(days=1)
    return stamp.strftime("%Y%m%d")


def _recent_period() -> str:
    """最近一个已过披露期的报告期。"""
    today = pd.Timestamp.today()
    for month_day in ("1231", "0930", "0630", "0331"):
        candidate = pd.Timestamp(f"{today.year}{month_day[:2]}{month_day[2:]}")
        if candidate + pd.Timedelta(days=120) <= today:
            return f"{today.year}{month_day}"
    return f"{today.year - 1}1231"


def _is_permission_error(message: str) -> bool:
    keywords = ("权限", "积分", "没有访问", "not permit", "permission",
                "无权限", "need more", "VIP")
    return any(word.lower() in message.lower() for word in keywords)


def _is_rate_limit_error(message: str) -> bool:
    keywords = ("每分钟", "频次", "频率", "超出", "too many", "rate limit",
                "访问过快")
    return any(word.lower() in message.lower() for word in keywords)


# ==================================================================
# 字段规范化（Tushare 原始列 → 规范英文列 + 规范单位）
# ==================================================================

# daily：金额千元 → 元；成交量「手」保留（因子用不到手/股换算时不动它）
DAILY_MAP: dict[str, str] = {
    "ts_code": "ts_code", "trade_date": "trade_date", "open": "open",
    "high": "high", "low": "low", "close": "close", "pre_close": "pre_close",
    "pct_chg": "pct_chg", "vol": "volume_lot", "amount": "amount",
}

# daily_basic：市值万元 → 元；股本万股 → 股
DAILY_BASIC_MAP: dict[str, str] = {
    "ts_code": "ts_code", "trade_date": "trade_date", "close": "close_basic",
    "turnover_rate": "turnover_rate", "turnover_rate_f": "turnover_rate_f",
    "volume_ratio": "volume_ratio", "pe": "pe", "pe_ttm": "pe_ttm", "pb": "pb",
    "ps": "ps", "ps_ttm": "ps_ttm", "dv_ratio": "dv_ratio", "dv_ttm": "dv_ttm",
    "total_share": "total_share", "float_share": "float_share",
    "free_share": "free_share", "total_mv": "total_mv", "circ_mv": "circ_mv",
    "limit_status": "limit_status",
}

MONEYFLOW_MAP: dict[str, str] = {
    "ts_code": "ts_code", "trade_date": "trade_date",
    "buy_lg_amount": "buy_lg_amount", "sell_lg_amount": "sell_lg_amount",
    "buy_elg_amount": "buy_elg_amount", "sell_elg_amount": "sell_elg_amount",
    "net_mf_amount": "net_mf_amount",
}

LIMIT_MAP: dict[str, str] = {
    "ts_code": "ts_code", "trade_date": "trade_date",
    "up_limit": "up_limit", "down_limit": "down_limit",
    "pre_close": "limit_pre_close",
}

# namechange：**历史名称**（ST/*ST 判定的唯一数据源）。
# 每行是一段名称生效区间 `[start_date, end_date]`（闭区间，end_date 空 = 仍在生效）。
# ⚠️ 不要拿 `stock_basic.name` 判 ST：那是"今天"的名录，会把
# "当年 ST、后来摘帽"的票当成正常股，回测就不是当时的现实了。
# 详见 `src/quant/st_status.py`。
NAME_CHANGE_MAP: dict[str, str] = {
    "ts_code": "ts_code", "name": "name", "start_date": "start_date",
    "end_date": "end_date", "ann_date": "ann_date",
    "change_reason": "change_reason",
}

# fina_indicator：只取因子需要的字段（其余字段按需再加）
FINA_MAP: dict[str, str] = {
    "ts_code": "ts_code", "ann_date": "ann_date", "end_date": "end_date",
    "eps": "eps", "bps": "bps", "ocfps": "ocfps", "revenue_ps": "revenue_ps",
    "roe": "roe", "roe_waa": "roe_waa", "roa": "roa", "npta": "npta",
    "grossprofit_margin": "grossprofit_margin",
    "netprofit_margin": "netprofit_margin",
    "debt_to_assets": "debt_to_assets",
    "current_ratio": "current_ratio", "quick_ratio": "quick_ratio",
    "ocf_to_profit": "ocf_to_profit", "ocf_to_or": "ocf_to_or",
    "tr_yoy": "tr_yoy", "or_yoy": "or_yoy", "netprofit_yoy": "netprofit_yoy",
    "dt_netprofit_yoy": "dt_netprofit_yoy", "ocf_yoy": "ocf_yoy",
    "roe_yoy": "roe_yoy", "assets_yoy": "assets_yoy", "eqt_yoy": "eqt_yoy",
    "fcff": "fcff", "fcfe": "fcfe", "ebit": "ebit", "ebitda": "ebitda",
    "profit_dedt": "profit_dedt", "assets_turn": "assets_turn",
    "inv_turn": "inv_turn", "ar_turn": "ar_turn",
}

_UNIT_SCALE: dict[str, float] = {
    # 万元 → 元
    "total_mv": _WAN, "circ_mv": _WAN, "net_mf_amount": _WAN,
    "buy_lg_amount": _WAN, "sell_lg_amount": _WAN,
    "buy_elg_amount": _WAN, "sell_elg_amount": _WAN,
    # 千元 → 元
    "amount": _QIAN,
    # 万股 → 股
    "total_share": _WAN, "float_share": _WAN, "free_share": _WAN,
}


#: **文本列**：`normalize` 不能对它们做数字强转。
#: 事故记录（2026-09-25）：这套转换原先只放行 4 个日期/代码列，其余一律
#: `pd.to_numeric(errors="coerce")` —— 于是 `namechange.name` 变成全 NaN，
#: 表现为"历史上曾被 ST 的票数 = 0"；`suspend_d.suspend_type`（S=停牌/R=复牌）
#: 与 `suspend_timing`、`bak_daily.industry` 同样被清空。
#: 加新数据集时，**文本字段一定要登记到这里**（`normalize` 现在也会
#: 在"有值→全 NaN"时打 warning 兜住这类错误）。
_TEXT_COLUMNS: tuple[str, ...] = (
    # 代码与日期
    "ts_code", "code", "trade_date", "ann_date", "end_date", "start_date",
    "list_date", "suspend_timing",
    # 名称/分类/原因
    "name", "change_reason", "industry", "area", "suspend_type",
)


def normalize(frame: pd.DataFrame, mapping: dict[str, str], *,
              extra: dict[str, pd.Series] | None = None) -> pd.DataFrame:
    """按映射重塑列名并统一单位（纯函数，便于单测）。

    ⚠️ **文本列必须显式登记在 `_TEXT_COLUMNS`**，否则会被下面的
    "逐列数字强转"吃掉。这不是理论风险：实测 `namechange.name`、
    `suspend_d.suspend_type`/`suspend_timing`、`bak_daily.industry`
    全被转成了 NaN —— 而且**不报错**。其中 `namechange.name` 直接导致
    "历史上曾被 ST 的票数 = 0"（一个安静的错误答案）。
    """
    if frame is None or len(frame) == 0:
        return pd.DataFrame(columns=list(mapping.values()))
    out = pd.DataFrame()
    for source, target in mapping.items():
        if source in frame.columns:
            out[target] = frame[source]
    if extra:
        for target, series in extra.items():
            out[target] = series
    for column, scale in _UNIT_SCALE.items():
        if column in out.columns:
            out[column] = pd.to_numeric(out[column], errors="coerce") * scale
    for column in out.columns:
        if column in _TEXT_COLUMNS:
            continue
        source_values = int(out[column].notna().sum())
        out[column] = pd.to_numeric(out[column], errors="coerce")
        if source_values and not out[column].notna().any():
            # 有值 → 全 NaN = 把文本列当数字强转了。宁可吵一句也不静默。
            logger.warning(
                "normalize：列 %r 的 %d 个非空值被数字强转全部丢弃 ——"
                "如果它是文本列，请加进 tushare_source._TEXT_COLUMNS",
                column, source_values)
    return out.reset_index(drop=True)


def to_code(ts_code: pd.Series) -> pd.Series:
    """`600519.SH` → `600519`（与项目其它模块的 6 位代码口径统一）。"""
    return ts_code.astype(str).str.split(".").str[0].str.zfill(6)
