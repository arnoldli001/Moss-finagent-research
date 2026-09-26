"""baostock 日线连接器（个股/ETF 前复权、指数不复权），**免 token 的全历史兜底**。

## 为什么需要它

QMT 终端已无行情权限且不在运行（127.0.0.1:58610 不通），日线链改成
AkShare → 腾讯 → Tushare 之后还差一道**独立故障域**的兜底：AkShare（东财/新浪）、
腾讯（HTTP 行情站）、Tushare（Pro 积分接口）三家之中，前两家同属"公开行情站"、
第三家要 token 与积分。baostock 是第四个独立通道，且**不需要 token**。

## 定位（实测，2026-09-22）

一次调用就返回**全历史**（2015-01-01→今，sh.600036）：

```
个股  2850 行 / 6.3s      ← 慢（约 3.8s/只、指数约 8.2s/只）
ETF   sh.510300 可取
指数   sh.000300 可取（adjustflag=3）
```

**慢**是它唯一的缺点，所以它排在在线链的**最后一位**（AkShare→腾讯→Tushare 之后），
只在前面几个源都拿不到时才顶上；批量全市场补数也不该把它放在第一位。

## 单位（**必须核实，这里错了就是 100 倍级静默误差**）

2026-09-22 实测 sh.510300 / 2026-09-16 一行：

```
字段       baostock 原始值      Tushare fund_daily 同日       结论
volume     968759702           vol=9687597.02（手）          baostock 单位 = 股
amount     4384779786.0000     amount=4384779.786（千元）     baostock 单位 = 元
```

对照 sh.600036 2026-09-22 也是一样：baostock `volume=43342069`，腾讯同日的成交量
是 `433421` 手 —— **正好差 100 倍**。所以 `volume` **÷100** 折手。

**指数用同一个 ÷100**，2026-09-22 四源同日对照（唯一能定死口径的办法）：

```
baostock  sh.000300 volume = 17,786,387,600（股）  ÷100 = 177,863,876
Tushare   index_daily vol  =    177,863,876（手）        ← 与上式**逐位相同**
AkShare   新浪指数 volume   = 17,786,387,600（股）  ÷100 = 177,863,876  ← 同样一致
腾讯       fqkline 指数成交量 = 177,863,876,000（**1000 倍**，它自己没折算，由腾讯连接器处理）
成交额     baostock 523,616,704,756.2 元 == Tushare 523,616,704.7562 千元 ×1000 ✓
```

即 **股→手 的换算只有 ÷100 一个系数**（一手 = 100 股），指数不是特例 ——
本项目曾在这里绕过一次弯路（以为指数要 ÷1000），是三源对照把它纠回来的。

因此本连接器：
- `volume`：股 → **÷100** 折成手，与东财/新浪/腾讯/Tushare 一致；
- `amount`：元 → **原样**（同样与其它源一致）；
- 并在 `extra["volume_unit"]`/`extra["volume_raw_unit"]`/`extra["amount_unit"]` 里
  如实标注原始口径，便于事后核对（口径差异必须留痕，不能只在代码里悄悄除）。

## 复权口径

- `adjustflag="2"` = 前复权 → 个股与 ETF；
- `adjustflag="1"` = 后复权、`"3"` = 不复权；
- **指数不除权**：请求 `adjustflag="3"` 并把 `adjust` 标成 `none`（指数是点数序列，
  与腾讯指数节点的处理一致）。实测 ETF 的 adjustflag=2 与 =3 在无分红区间数值相同，
  这属于正常现象（该区间无分红），不代表复权没生效。

## 登录

baostock 的查询必须先 `bs.login()`（返回码 `'0'` 才算成功），进程内只需一次，
卸载时 `bs.logout()`。路由是用 `asyncio.to_thread` 并发调用 `fetch` 的，
所以登录用**模块级锁 + 幂等标志**保护：多个线程同时进来只有一个真正 login，
避免重复登录把别人的会话顶掉。
"""

from __future__ import annotations

import asyncio
import logging
import threading
from datetime import date
from typing import Any

from src.core import symbols
from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)
from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

_SOURCE_URL = "http://baostock.com"
# baostock 日线字段（顺序即返回行顺序，本连接器按 fields 名字取值，不靠位置）
_FIELDS = "date,open,high,low,close,preclose,volume,amount,adjustflag,turn,pctChg"
# 复权标志：2=前复权、3=不复权（指数用 3）
_ADJUST_FLAG_QFQ = "2"
_ADJUST_FLAG_NONE = "3"

# 股 → 手：一手 = 100 股。**个股/ETF/指数都用这一个系数**，不做指数特例。
# 实测（2026-09-22，000300.SH 同日四源对照，这是唯一能定死口径的办法）：
#   baostock volume = 17,786,387,600（股）  ÷100  = 177,863,876
#   Tushare  index_daily vol = 177,863,876（手）        ← 与上式**逐位相同**
#   腾讯     fqkline 指数成交量 = 177,863,876,000       ← 是上式的 1000 倍，即"股"口径
#   AkShare  新浪指数 volume   = 17,786,387,600（股，同 baostock）
#   成交额：baostock 523,616,704,756.2 元 == Tushare 523,616,704.7562 千元 ×1000 ✓
# 结论：股/手 的换算三源一致是 ÷100；**腾讯的指数量是股**（它没换算），由腾讯连接器自己处理。
_SHARES_PER_LOT = 100.0

_SUPPORTED_PREFIXES: tuple[str, ...] = (
    "stock_close:", "index_close:", "etf_close:")

# 指数字段：000xxx/880xxx → 沪，399xxx → 深（与 quote_qmt_code / index_symbol 一致）
_INDEX_SH_PREFIXES: tuple[str, ...] = ("000", "880")
_INDEX_SZ_PREFIXES: tuple[str, ...] = ("399",)
# ETF 字段：51/56/58 → 沪，15/16 → 深
_ETF_SH_PREFIXES: tuple[str, ...] = ("51", "56", "58")
_ETF_SZ_PREFIXES: tuple[str, ...] = ("15", "16")

# 无 start_date 时的默认起点：baostock 全历史一次取回，"不限"就等于"从上市那天起"，
# 但 1990 之前的空区间实测返回空表（不是错误），所以给一个早于所有 A 股上市日的常量。
_DEFAULT_START = "1990-01-01"

# 登录状态（进程级）：baostock 的会话是模块全局的，必须串行化
_login_lock = threading.Lock()
_logged_in = False


def to_baostock_symbol(indicator: str) -> str:
    """行情指标 → baostock 符号（`sh.600036` / `sz.399006` / `sh.510300`）。

    市场归属**复用 `src.core.symbols`**（唯一的权威实现）：该项目曾因 sh/sz 规则
    散在 5 处、其中 3 处漏了沪市 `5` 段，把 588170 这类科创 ETF 判成深市，
    取数全空且报错指不到原因。

    ⚠️ **指数是例外**：`symbols.market_of` 的默认规则是"首位 0 → 深市"，
    但 `000300`（沪深300）/`000001`（上证指数）都是**沪市**指数 ——
    直接套 `market_of` 会拼出 `sz.000300`，baostock 换回一个空结果，
    而空结果与"指数不存在"无法区分。所以指数按 000/880→sh、399→sz 单独判归属，
    个股/ETF 仍走 `market_of`（它们的代码段规则恰好一致，不需要第二套）。
    """
    prefix, _, raw_code = str(indicator or "").partition(":")
    code = raw_code.strip()
    if prefix not in ("stock_close", "index_close", "etf_close") or not code:
        raise DataFetchError(f"baostock连接器不支持的指标: {indicator}")
    try:
        text = symbols.normalize(code)  # 6位数字 + 北交所拒绝
        if prefix == "index_close":
            if text.startswith(_INDEX_SH_PREFIXES):
                return f"sh.{text}"
            if text.startswith(_INDEX_SZ_PREFIXES):
                return f"sz.{text}"
            raise symbols.SymbolError(
                f"暂支持000xxx(沪)/880xxx(沪)/399xxx(深)指数: {text}")
        if prefix == "etf_close":
            if not text.startswith(_ETF_SH_PREFIXES + _ETF_SZ_PREFIXES):
                raise symbols.SymbolError(
                    f"暂支持51/56/58(沪)/15/16(深)ETF: {text}")
        elif symbols.is_etf_code(text):
            raise symbols.SymbolError(
                f"个股路径不支持ETF代码（请用 etf_close: 前缀）: {text}")
        return f"{symbols.market_of(text)}.{text}"
    except symbols.SymbolError as exc:
        raise DataFetchError(str(exc)) from exc


def adjustflag_for(indicator: str) -> str:
    """按指标类别选 adjustflag：个股/ETF → `2`(前复权)，指数 → `3`(不复权)。"""
    prefix, _, code = str(indicator or "").partition(":")
    if prefix == "index_close":
        return _ADJUST_FLAG_NONE
    if prefix in ("stock_close", "etf_close") and code.strip():
        return _ADJUST_FLAG_QFQ
    raise DataFetchError(f"baostock连接器不支持的指标: {indicator}")


def adjust_label_for(indicator: str) -> str:
    """与 adjustflag 对应的口径标签（写进 `extra["adjust"]`，不静默换口径）。"""
    return "none" if adjustflag_for(indicator) == _ADJUST_FLAG_NONE else "qfq"


def _to_float(value: Any) -> float | None:
    """baostock 的数值都是**字符串**（如 `'40.9200000000'`），统一转 float。"""
    if value is None or value == "":
        return None
    try:
        number = float(str(value).strip())
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def rows_to_points(rows: list[list[str]], fields: list[str], indicator: str, *,
                   adjust: str, start_date: str | None = None,
                   end_date: str | None = None) -> list[DataPoint]:
    """baostock 结果行 → DataPoint（纯函数，便于离线单测）。

    - `volume`：原始单位是**股** → ÷100 折成手（个股/ETF/指数同一系数，
      实测对照见模块常量注释）；
    - `amount`：原始单位是**元** → 原样；
    - 日期形如 `2026-09-18`，与项目统一的 `period_date` 口径一致，无需转换；
    - 停牌日的空字段行（无 close）跳过，不编造。
    """
    lower = (start_date or "").replace("-", "")
    upper = (end_date or "").replace("-", "")
    points: list[DataPoint] = []
    for row in rows:
        record = dict(zip(fields, row, strict=False))
        raw_date = str(record.get("date", "")).strip().replace("-", "")
        if len(raw_date) != 8 or not raw_date.isdigit():
            continue
        if lower and raw_date < lower:
            continue
        if upper and raw_date > upper:
            continue
        close = _to_float(record.get("close"))
        if close is None:
            continue
        volume_shares = _to_float(record.get("volume"))
        points.append(DataPoint(
            indicator=indicator,
            value=close,
            period_date=f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
            extra={
                "open": _to_float(record.get("open")),
                "high": _to_float(record.get("high")),
                "low": _to_float(record.get("low")),
                "close": close,
                "pre_close": _to_float(record.get("preclose")),
                "pct_chg": _to_float(record.get("pctChg")),
                # 单位换算：股 → 手（÷100，个股/ETF/指数同一系数）；原始口径留痕
                "volume": (None if volume_shares is None
                           else volume_shares / _SHARES_PER_LOT),
                "volume_unit": "手",
                "volume_raw_unit": "股",
                "amount": _to_float(record.get("amount")),  # 元，原样
                "amount_unit": "元",
                "adjust": adjust,
            },
            source_name="baostock",
            source_url=_SOURCE_URL,
            source_type=DataSourceType.API,
            fetch_method=FetchMethod.API_CALL,
            confidence=0.75,
            verified=False,
        ))
    points.sort(key=lambda item: item.period_date or "")
    return points


def _import_baostock() -> Any:
    """惰性导入 baostock（未安装时抛 `DataFetchError`，不让装配期就炸）。

    单独抽成函数是为了单测能替换成假模块 —— 测试**绝不打网络**。
    """
    try:
        import baostock as bs  # noqa: PLC0415 惰性导入：未安装时连接器仍可装配
    except ImportError as exc:
        raise DataFetchError("baostock 未安装（pip install baostock）") from exc
    return bs


def _ensure_login(bs: Any) -> None:
    """幂等且线程安全地登录（路由用 `asyncio.to_thread` 并发调用 fetch）。

    实测 `bs.login()` 成功返回 `error_code='0'`、`error_msg='success'`；
    网络不通时返回非 `'0'` —— 这种情况必须**带原始 message 抛错**，
    否则上层只能看到一句"没数据"，与"该股停牌"无法区分。
    """
    global _logged_in
    if _logged_in:
        return
    with _login_lock:
        if _logged_in:  # 双检：等锁期间别的线程可能已经登录过
            return
        result = bs.login()
        code = str(getattr(result, "error_code", ""))
        if code != "0":
            raise DataFetchError(
                f"baostock 登录失败(code={code})："
                f"{brief(getattr(result, 'error_msg', ''), BRIEF_DEFAULT)}")
        _logged_in = True
        logger.info("baostock 登录成功（进程内单次登录，后续调用复用会话）")


def fetch_history_raw(bs: Any, code: str, adjustflag: str, start_date: str,
                      end_date: str) -> tuple[list[list[str]], list[str]]:
    """同步取数（供 `asyncio.to_thread` 调用），返回 `(行, 字段名)`。"""
    _ensure_login(bs)
    result = bs.query_history_k_data_plus(
        code, _FIELDS,
        start_date=start_date, end_date=end_date,
        frequency="d", adjustflag=adjustflag)
    error_code = str(getattr(result, "error_code", ""))
    if error_code != "0":
        raise DataFetchError(
            f"baostock 查询失败({code} {start_date}~{end_date}, code={error_code})："
            f"{brief(getattr(result, 'error_msg', ''), BRIEF_DEFAULT)}")
    fields = list(getattr(result, "fields", []) or [])
    rows: list[list[str]] = []
    while result.next():
        rows.append(list(result.get_row_data()))
    return rows, fields


def reset_login_state() -> None:
    """重置进程内登录标志（仅供测试与显式重连使用）。"""
    global _logged_in
    with _login_lock:
        _logged_in = False


class BaostockConnector(BaseConnector):
    """baostock 日线（免 token 全历史兜底；慢，排在在线链最后一位）。"""

    source_name = "baostock"
    source_url = _SOURCE_URL

    def __init__(self, bs: Any = None) -> None:
        # 允许注入假模块/假客户端（单测用）；缺省惰性导入真 baostock
        self._bs = bs

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith(_SUPPORTED_PREFIXES)

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": ["stock_close:{code}", "index_close:{code}",
                           "etf_close:{code}"],
            "priority": "P2",
            "notes": ("免 token 的全历史兜底（一次调用返回全部历史）；"
                      "个股/ETF 前复权(adjustflag=2)、指数不复权(3)；"
                      "volume 原始单位=股 → 折手（个股/ETF ÷100、指数 ÷1000），"
                      "amount=元；实测约 3.8s/只（指数约 8.2s），故排在在线链最后"),
        }

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        if not self.supports(indicator):
            raise DataFetchError(f"baostock连接器不支持的指标: {indicator}")
        code = to_baostock_symbol(indicator)
        adjustflag = adjustflag_for(indicator)
        adjust = adjust_label_for(indicator)
        start = (start_date or _DEFAULT_START)
        end = (end_date or date.today().isoformat())
        bs = self._bs or _import_baostock()
        # baostock 是同步阻塞 + 模块级会话 → 丢线程池
        rows, fields = await asyncio.to_thread(
            fetch_history_raw, bs, code, adjustflag, start, end)
        points = rows_to_points(
            rows, fields, indicator, adjust=adjust,
            start_date=start_date, end_date=end_date)
        if not points:
            raise DataFetchError(
                f"baostock 在 {start}~{end} 无 {code} 日线数据"
                "（区间外/已退市/代码不存在）")
        return points
