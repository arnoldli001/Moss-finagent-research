"""Tushare Pro 日线连接器（个股前复权 / 指数 / ETF），日线链的**全历史在线源**。

## 为什么需要它（实测，2026-09-17）

日线链原本是 QMT → 本地QMT导出CSV → AkShare。当天实测三个源的状态：

```
迅投QMT         可用，最新 2026-09-16
本地行情CSV      DataFetchError: 本地CSV无行情数据: 300308   ← 该票根本没导出过
AkShare         东财行情接口失败（300308），回退新浪源: Connection aborted → 返回空
```

也就是说 **QMT 一掉线，这条链对 300308 就是空的**：本地CSV没有这只票的文件，
AkShare 的东财主源被阻断、新浪回退也断连。而 Tushare 同一时刻
**0.14 秒**返回 12 根、最新 2026-09-16、close=907.80（与 QMT 完全一致）。

## 三类指标与各自的原生接口（实测 2026-09-22）

```
指标                 接口                        复权          一次性行数（实测）
stock_close:{code}   pro_bar(adj=qfq)→daily备    前复权        2845（2015→今，0.33s）
index_close:{code}   index_daily                 **无复权**    5998（000300.SH 全历史）
etf_close:{code}     fund_daily                  **无复权**    3483（510300.SH 全历史）
```

指数不除权、是点数口径；ETF 走场内基金日线接口。
个股口径与 QMT / AkShare 东财源一致（同为前复权）。

## 口径（必须与其他源一致，否则图上会出现"换源跳空"）

- **个股前复权**：走 `ts.pro_bar(adj='qfq')`，与 QMT / AkShare 东财源一致；
  若账号没有 `adj_factor` 权限或 `pro_bar` 不可用，**降级为不复权**并在
  `extra["adjust"]="none"` 里如实标注（不静默换口径）。
- **单位**（实测对照：510300.SH 2026-09-18 的 Tushare `vol=6297211.23 手`、
  `amount=2877462.543 千元`，与 baostock 的 `629721123 股 / 2877462543 元` 正好差 100 与 1000，
  与腾讯 `qt` 快照的 `6445874 手`量级一致）：
  - `daily` / `fund_daily`：`vol` 单位=**手** → 直接作为 `volume`；
    `amount` 单位=**千元** → ×1000 转成元（与 AkShare 东财/新浪口径对齐）。
  - `index_daily`：`vol` 是**股**量级（沪深300 单日 1.5~1.9 亿股），**不是手** ——
    指数成交量没有"手"的官方口径，所以 **÷100 折成手**并在 `extra["volume_unit"]`
    如实标注，指数成交额 `amount` 同为千元 → ×1000 转元。
- **排序**：Tushare 返回**按日期倒序**，这里统一翻成升序 ——
  下游的 K 线形态/量柱/缠论全部假定时间升序，倒序会算出完全错误的结构。
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

import pandas as pd

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

# 三类日频行情指标（个股 / 指数 / ETF）
_SUPPORTED_PREFIXES: tuple[str, ...] = (
    "stock_close:", "index_close:", "etf_close:")

# ---- 市场归属（与 quote_qmt_code、intraday/sources.index_symbol 同一套规则）----
# A股个股：6/9 沪市（含 688 科创板、900 B股），0/2/3 深市
_SH_SUFFIXES = ("6", "9")
_SZ_SUFFIXES = ("0", "2", "3")
# 指数：000xxx/880xxx 沪（中证/上证系列），399xxx 深（国证/创业板系列）
_INDEX_SH_PREFIXES: tuple[str, ...] = ("000", "880")
_INDEX_SZ_PREFIXES: tuple[str, ...] = ("399",)
# ETF：51/56/58 沪，15/16 深
_ETF_SH_PREFIXES: tuple[str, ...] = ("51", "56", "58")
_ETF_SZ_PREFIXES: tuple[str, ...] = ("15", "16")
# 北交所：4/8 开头；920xxx 是北交所新号段，必须单列判断（否则会被当成深市 9 段沪市）
_BJ_PREFIXES: tuple[str, ...] = ("920", "4", "8")

# 指标前缀 → 语义类别（决定走哪个原生接口、怎么标 adjust）
_KIND_STOCK = "stock"
_KIND_INDEX = "index"
_KIND_ETF = "etf"
_KIND_BY_PREFIX: dict[str, str] = {
    "stock_close": _KIND_STOCK,
    "index_close": _KIND_INDEX,
    "etf_close": _KIND_ETF,
}
# 类别 → 原生接口（个股是 pro_bar 优先，见 _daily_frame）
_API_BY_KIND: dict[str, str] = {
    _KIND_INDEX: "index_daily",
    _KIND_ETF: "fund_daily",
}
# 类别 → 复权口径：指数/ETF 不除权，个股走 pro_bar(qfq) 并在降级时改标
_ADJUST_BY_KIND: dict[str, str] = {
    _KIND_STOCK: "qfq",
    _KIND_INDEX: "none",
    _KIND_ETF: "none",
}


def to_ts_code(code: str, *, kind: str = _KIND_STOCK) -> str:
    """6位代码 + 指标类别 → Tushare ts_code（如 300308 → 300308.SZ）。

    市场归属只在**本函数**判定一次（项目曾因 5 处重复的 sh/sz 规则、其中 3 处
    漏了沪市 `5` 段而把 588170 判成深市，取数全空且报错指不到原因）：

    - `kind="stock"`：6/9 → `.SH`，0/2/3 → `.SZ`；
    - `kind="index"`：000xxx/880xxx → `.SH`，399xxx → `.SZ`（**不能**用个股规则，
      否则 000300.SH 会被拼成 000300.SZ 换回一个空结果）；
    - `kind="etf"`：51/56/58 → `.SH`，15/16 → `.SZ`。

    代码段与类别不符时**明确拒绝**（如 stock 路径收到 5xxxxx/1xxxxx ETF），
    拼一个错误的 ts_code 去换回空结果，与"该股停牌/退市"无法区分。
    北交所（4/8/920）映射 `.BJ` 的行为保持在个股路径上，不回退。
    """
    target = str(code or "").strip()
    if not (target.isdigit() and len(target) == 6):
        raise DataFetchError(f"Tushare连接器需要6位数字代码：{code!r}")
    if kind == _KIND_STOCK:
        if target.startswith(_BJ_PREFIXES):
            return f"{target}.BJ"
        if target.startswith(_SH_SUFFIXES):
            return f"{target}.SH"
        if target.startswith(_SZ_SUFFIXES):
            return f"{target}.SZ"
        # 5xxxxx/1xxxxx 是 ETF/基金，走 etf_close 而非 stock_close
        raise DataFetchError(
            f"Tushare连接器的个股路径不支持该代码（疑似ETF/基金）：{target}")
    if kind == _KIND_INDEX:
        if target.startswith(_INDEX_SH_PREFIXES):
            return f"{target}.SH"
        if target.startswith(_INDEX_SZ_PREFIXES):
            return f"{target}.SZ"
        raise DataFetchError(
            f"Tushare 指数接口暂支持000xxx(沪)/880xxx(沪)/399xxx(深)：{target}")
    if kind == _KIND_ETF:
        if target.startswith(_ETF_SH_PREFIXES):
            return f"{target}.SH"
        if target.startswith(_ETF_SZ_PREFIXES):
            return f"{target}.SZ"
        raise DataFetchError(
            f"Tushare 基金接口暂支持51/56/58(沪)/15/16(深)ETF：{target}")
    raise DataFetchError(f"未知的 Tushare 指标类别: {kind!r}")


def kind_of(indicator: str) -> str:
    """行情指标 → 类别（不支持的指标抛 `DataFetchError`）。"""
    prefix, _, code = str(indicator or "").partition(":")
    kind = _KIND_BY_PREFIX.get(prefix)
    if kind is None or not code.strip():
        raise DataFetchError(f"Tushare连接器不支持的指标: {indicator}")
    return kind


@dataclass(frozen=True)
class _Units:
    """一条原生接口的成交量/成交额原始单位。

    这些数字**不能靠猜**：`daily.vol` 与 `index_daily.vol` 同为 `vol` 却一个
    是手、一个是股量级，混用就是 1000 倍级静默误差（指数成交量口径混乱，
    实测靠**三源同日对照**才钉死 —— 见下面的常量注释）。
    """

    volume_scale: float       # 原始 vol → 手 的换算系数
    amount_scale: float       # 原始 amount → 元 的换算系数
    volume_unit: str = "手"   # extra 里如实标注的最终口径
    volume_raw_unit: str = "手"  # 原始口径（留痕，便于事后核对）


# 个股/ETF：vol=手、amount=千元
# 实测 510300.SH 2026-09-18：Tushare vol=6,297,211.23 手 ↔ 腾讯 6,445,874 手（同日量级一致）、
# baostock 629,721,123 股 ÷100 = 6,297,211.23 手 —— 三源完全对齐。
_UNITS_STOCK_LIKE = _Units(volume_scale=1.0, amount_scale=1000.0,
                           volume_unit="手", volume_raw_unit="手")
# 指数：**同样不换算** —— Tushare 的指数 vol 本来就是**手**，与腾讯完全一致。
# 实测 000300.SH 2026-09-22 四源同日对照（唯一能定死口径的办法）：
#   Tushare index_daily vol =     177,863,876    ← 手
#   腾讯 fqkline 指数     成交量 = 177,863,876    ← 手（与 Tushare **完全相等**）
#   baostock             volume = 17,786,387,600  ← 股（÷1000 = 177,863,876 手）
#   AkShare 新浪指数      volume = 17,786,387,600  ← 股
#   成交额：Tushare 523,616,704.7562 千元 ×1000 == baostock 523,616,704,756.2 元
#
# ⚠️ 这里踩过一次坑，记下来免得再犯：曾按"指数 vol 是股、要 ÷1000"改过一次，
# 结果指数成交量凭空小 1000 倍。真值就是 **1.7786 亿手**（手），
# 因为它与腾讯同日的值**逐位相同** —— 两个独立源给出同一个数，才是硬证据。
_UNITS_INDEX = _Units(volume_scale=1.0, amount_scale=1000.0,
                      volume_unit="手", volume_raw_unit="手")


def units_for(kind: str) -> _Units:
    """类别 → 原始单位口径。"""
    return _UNITS_INDEX if kind == _KIND_INDEX else _UNITS_STOCK_LIKE


def _compact(value: str | None) -> str:
    return (value or "").replace("-", "").replace("/", "")


def frame_to_points(frame: pd.DataFrame | None, indicator: str, *,
                    ts_code: str, adjust: str,
                    units: _Units | None = None,
                    start_date: str | None = None,
                    end_date: str | None = None) -> list[DataPoint]:
    """Tushare 日线 DataFrame → DataPoint（纯函数，便于离线单测）。

    **升序输出**：Tushare 按 trade_date 倒序返回，下游全部假定升序。
    `units` 决定 vol/amount 的换算（指数与个股不同，见 `_Units`）；
    缺省按个股/ETF 口径（手 / 千元→元），与既有调用行为一致。
    """
    if frame is None or len(frame) == 0:
        return []
    scales = units or _UNITS_STOCK_LIKE
    lower = _compact(start_date)
    upper = _compact(end_date)
    points: list[DataPoint] = []
    for _, row in frame.iterrows():
        raw_date = str(row.get("trade_date", "")).strip()
        if len(raw_date) != 8 or not raw_date.isdigit():
            continue
        if lower and raw_date < lower:
            continue
        if upper and raw_date > upper:
            continue
        close = _float(row.get("close"))
        if close is None:
            continue
        amount = _float(row.get("amount"))
        volume = _float(row.get("vol"))
        points.append(DataPoint(
            indicator=indicator,
            value=close,
            period_date=f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
            extra={
                "open": _float(row.get("open")),
                "high": _float(row.get("high")),
                "low": _float(row.get("low")),
                "close": close,
                "pre_close": _float(row.get("pre_close")),
                "pct_chg": _float(row.get("pct_chg")),
                # vol 原始单位见 _Units（个股/ETF=手；指数=约股×10 → ÷1000 折手）
                "volume": None if volume is None else volume * scales.volume_scale,
                "volume_unit": scales.volume_unit,
                "volume_raw_unit": scales.volume_raw_unit,
                # amount 原始单位=千元 → 转元（指数/ETF/个股三张表都是千元）
                "amount": None if amount is None else amount * scales.amount_scale,
                "adjust": adjust,
                "ts_code": ts_code,
            },
            source_name="Tushare Pro",
            source_url="https://tushare.pro",
            source_type=DataSourceType.API,
            fetch_method=FetchMethod.API_CALL,
            confidence=0.85,
            verified=False,
        ))
    points.sort(key=lambda item: item.period_date or "")
    return points


def _float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None  # 过滤 NaN


class TushareConnector(BaseConnector):
    """Tushare Pro 日线（个股前复权优先并如实标注降级；指数/ETF 不复权全历史）。"""

    source_name = "Tushare Pro"
    source_url = "https://tushare.pro"

    def __init__(self, client: Any = None) -> None:
        # 客户端惰性构造：没有 token 时**构造连接器本身不能失败**，
        # 否则整个采集链装配就断了（Tushare 只是兜底，不该拖垮主链路）。
        self._client = client
        self._client_error: str | None = None

    # ---- 能力声明 ----

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith(_SUPPORTED_PREFIXES)

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": ["stock_close:{code}", "index_close:{code}",
                           "etf_close:{code}"],
            "priority": "P1",
            "notes": ("个股日线前复权（ts.pro_bar adj=qfq，无 adj_factor 权限时降级不复权"
                      "并在 extra.adjust 标注）；指数走 index_daily（点数），"
                      "ETF 走 fund_daily（不复权，vol 手 / amount 千元→元）；"
                      "三者都是全历史一次取回；需 TUSHARE_TOKEN（环境变量/.env/Windows注册表）"),
        }

    # ---- 取数 ----

    def _ensure_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._client_error is not None:
            raise DataFetchError(self._client_error)
        try:
            from src.quant.tushare_source import TushareClient, resolve_token

            self._client = TushareClient(resolve_token())
        except Exception as exc:  # noqa: BLE001 token 缺失/权限问题都按"该源不可用"
            self._client_error = f"Tushare 不可用：{brief(exc, BRIEF_DEFAULT)}"
            raise DataFetchError(self._client_error) from exc
        return self._client

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        if not self.supports(indicator):
            raise DataFetchError(f"Tushare连接器不支持的指标: {indicator}")
        kind = kind_of(indicator)
        code = indicator.split(":", 1)[1].strip()
        ts_code = to_ts_code(code, kind=kind)
        # Tushare SDK 是同步阻塞的 → 丢线程池，别把事件循环堵住
        return await asyncio.to_thread(
            self._fetch_sync, indicator, kind, ts_code, start_date, end_date)

    def _fetch_sync(
        self, indicator: str, kind: str, ts_code: str,
        start_date: str | None, end_date: str | None,
    ) -> list[DataPoint]:
        client = self._ensure_client()
        start = _compact(start_date) or "19900101"
        end = _compact(end_date) or "20991231"

        if kind == _KIND_STOCK:
            frame, adjust = self._daily_frame(client, ts_code, start, end)
        else:
            frame, adjust = self._native_frame(client, kind, ts_code, start, end)

        points = frame_to_points(
            frame, indicator, ts_code=ts_code, adjust=adjust,
            units=units_for(kind),
            start_date=start_date, end_date=end_date)
        if not points:
            raise DataFetchError(
                f"Tushare 在 {start}~{end} 无 {ts_code} 日线数据"
                "（区间外/已退市/接口无权限）")
        return points

    @staticmethod
    def _daily_frame(client: Any, ts_code: str, start: str,
                     end: str) -> tuple[pd.DataFrame | None, str]:
        """个股：优先前复权；权限不足或 pro_bar 不可用时降级为不复权。"""
        try:
            import tushare as ts

            frame = ts.pro_bar(ts_code=ts_code, adj="qfq", start_date=start,
                               end_date=end, api=client.pro)
            if frame is not None and len(frame):
                return frame, "qfq"
            logger.info("Tushare pro_bar(qfq) 返回空(%s)，尝试不复权 daily", ts_code)
        except Exception as exc:  # noqa: BLE001 降级不复权，但要留下痕迹
            logger.warning("Tushare 前复权失败(%s)，降级不复权：%s",
                           ts_code, brief(exc, BRIEF_TIGHT))
        frame = client.call(api="daily", ts_code=ts_code,
                            start_date=start, end_date=end)
        return frame, "none"

    @staticmethod
    def _native_frame(client: Any, kind: str, ts_code: str, start: str,
                      end: str) -> tuple[pd.DataFrame | None, str]:
        """指数/ETF：走各自原生接口，**不复权**（如实标 `none`，不冒充 qfq）。

        实测 2026-09-22：`index_daily` 000300.SH 全历史 5998 行、`fund_daily`
        510300.SH 全历史 3483 行，都是单次调用全量返回，无需分页。
        """
        api = _API_BY_KIND[kind]
        frame = client.call(api=api, ts_code=ts_code,
                            start_date=start, end_date=end)
        return frame, _ADJUST_BY_KIND[kind]
