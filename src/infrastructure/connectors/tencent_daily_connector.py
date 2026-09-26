"""腾讯财经日K连接器（个股/ETF 前复权、指数点数口径），日线链的**独立在线通道**。

## 为什么需要它（实测，2026-09-17）

同一天实测四个源对 300308 的日线可用性：

```
迅投QMT          可用（但一掉线就整条链见底）
本地行情CSV      DataFetchError: 本地CSV无行情数据: 300308  ← 该票根本没导出过
AkShare          东财行情接口失败（300308），回退新浪源: Connection aborted → 空
Tushare Pro      可用（0.14s）
腾讯             可用 —— 做T面板的实时链路（`intraday/sources.py`）一直走的它
```

关键点：**AkShare 的两个子源（东财 + 新浪）会同时不可用**，
而腾讯是**另一条完全独立的 HTTP 通道**。做T面板的分钟线/快照当时是正常的，
说明腾讯这条通道没被阻断 —— 它值得成为日线链上的一跳。

## 口径

接口：`https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param=sz300308,day,,,640,qfq`

- **前复权**：`qfq` 参数，与 QMT/AkShare东财/Tushare 一致；响应里对应 `qfqday` 节点
  （未请求复权时是 `day` 节点，本连接器两个都认，优先 `qfqday`）。
- **列序坑**：腾讯的行是 `[日期, 开, 收, 高, 低, 成交量(手)]` —— **收在高之前**。
  按"开高低收"的直觉去解会把 high/low 读反，K线形态与量柱判断会整体错位。
- **单位**：成交量单位是**手**（与东财/新浪一致）；腾讯日K不返回成交额，
  `amount` 置 None（下游 `daily_bars_from_points` 会补 0），
  **不编造**成交额。
- **升序输出**：腾讯返回升序，这里仍显式排序，避免依赖上游行为。
- **指数口径不同（实测 2026-09-22）**：指数（sh000300 / sz399006 / sh000001）
  的响应**只有 `day` 节点、没有 `qfqday`**（指数不除权），成交量是**股**量级
  （沪深300 单日 1.5~1.9 亿）。所以指数一律标 `adjust="none"`；
  个股/ETF 标 `adjust="qfq"`，若只拿到 `day` 节点也会如实降级标 `none`
  （**绝不把不复权数据标成前复权** —— 那会让下游以为口径一致而混用）。

## 分页（一次请求最多 641 根）

实测（2026-09-22）：
- `param=sh600036,day,2015-01-01,2020-01-01,640,qfq` → 640 行，末行 2019-12-31；
- `param=sh600036,day,,2020-01-01,640,qfq` → 同样 640 行（**空起点 = 截至锚点的最后 N 根**，
  不是"从最早开始"）；
- `param=sh600036,day,1990-01-01,2020-01-01,640,qfq` → 又是同一批（**count 命中的是从锚点
  往回数的 N 根**，而**不是**区间内的前 N 根）；
- `param=sh600036,day,2004-01-01,2004-12-31,640,qfq` → 241 行（区间比 N 短时给全区间）。

因此**多页必须靠"把 end 锚点往前挪"**：拿到本页最早日期 → 锚点回退一天 → 再请求。
`fetch()` 在 `start_date` 与 `end_date` 都给全时自动翻页（每页间隔 ≥ 640 个交易日才需要下一页，
短区间仍只打一次网络）；只给 `start_date`（end 缺省）时退回单次取数以保证"最新 bar"不丢。
页数上限 `_MAX_PAGES = 40`（≈25 年交易日），到不了请求起点就抛 `DataFetchError`
而**不返回截断的数据**（截断与"该股上市晚"无法区分，是静默错误）。

## 当日形成中bar：**只有"完全不带区间"的请求才给**（实测 2026-09-23 盘中）

这是本连接器最容易踩死的一个坑，单独记一节：

```
2026-09-23 盘中（10:20，腾讯分时 date=20260923 有 40 根，行情确实是活的）
param=sz300308,day,1990-01-01,2026-09-23,640,qfq  → 640 行，末行 **2026-09-22**  ← 漏当天
param=sz300308,day,,2026-09-23,640,qfq           → 640 行，末行 **2026-09-22**  ← 漏当天
param=sz300308,day,,,640,qfq                      → 641 行，末行 **2026-09-23**  ← 有当天形成中bar
```

结论：**只要请求里出现日期区间（哪怕只是 end 锚点、哪怕 count 一样），
腾讯就把"今天这根还没收盘的 bar"排除掉**；完全开放的 `day,,,N,qfq`
（"截至现在的最后 N 根"）才会把它带出来。锚点前推到明天/下周都没用。

后果（2026-09-23 用户报障"日K当日数据未刷新出来"）：日K做T面板按 §11.3 的设计
**显式给区间**取数（`start=500天前, end=今天`）→ 必然走带区间的分页路径 →
盘中永远停在昨天，而面板又提示"QMT 日线订阅未生效"（QMT 早已默认关闭，这句提示是误导）。

修法：`_wants_forming_bar()` 判断"这次请求要不要当天的形成中bar"（end 不早于今天），
要的时候**分页的第一页**改用完全开放形式（`_page(open_range=True)`）拿最新 N 根，
后续页仍按 end 锚点回退 —— 第 1 页的 `earliest` 正好是下一页的锚点，翻页语义不变。
历史区间（end 在过去）不加这个：否则第一页会跳到今天，翻页要跨越到目标区间，
轻则白翻几十页、重则撞 `_MAX_PAGES` 抛错。

支持 `stock_close:` / `etf_close:` / `index_close:` 三类前缀；
北交所（4/8/920）腾讯日K不覆盖，明确拒绝而不是拼一个错符号。
"""

from __future__ import annotations

import logging
from datetime import date, timedelta
from typing import Any

import httpx

from src.core import symbols
from src.core.errors import (
    BRIEF_DEFAULT,
    brief,
)
from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

_FQKLINE_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
_HEADERS = {
    "Referer": "https://gu.qq.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
}
# 一次取多少根日K：500 自然日 ≈ 340 根，做T的日线上下文最多回看 250(规则)+120(画图)
_DEFAULT_COUNT = 640
_TIMEOUT = 12.0
# 分页轮数上限（每页约 640 个交易日 ≈ 2.6 年，40 页 ≈ 100 年，现实中够不到）
_MAX_PAGES = 40
# 区间起点：写成一个"早于任何 A 股上市日"的常量。不能用空起点 ——
# 实测空起点 = "截至 end 锚点的最后 N 根"，翻页语义会整段错位。
_EARLIEST_START = "1990-01-01"
# end 锚点默认值相对今天的前推天数（自然日）：覆盖周末+节假日，
# 且实测 `end=明天` 会正常把这根最新的 bar 带出来（2026-09-22 实测 rows 末行 = 2026-09-22）。
_END_ANCHOR_FORWARD_DAYS = 8

# 指数代码段（与 `intraday/sources.index_symbol`、`xtquant_connector.quote_qmt_code` 一致）
_INDEX_SH_PREFIXES: tuple[str, ...] = ("000", "880")
_INDEX_SZ_PREFIXES: tuple[str, ...] = ("399",)

_SUPPORTED_PREFIXES: tuple[str, ...] = (
    "stock_close:", "index_close:", "etf_close:")


def _index_market(code: str) -> str:
    """指数字段归属：`000xxx`/`880xxx` → sh，`399xxx` → sz。

    指数必须显式区分（`000001` 在"上证指数"与"平安银行"之间歧义），
    所以不走 `market_of` 的"首位 0 → 深市"默认规则。
    """
    text = symbols.normalize(code)
    if text.startswith(_INDEX_SH_PREFIXES):
        return "sh"
    if text.startswith(_INDEX_SZ_PREFIXES):
        return "sz"
    raise symbols.SymbolError(
        f"暂支持000xxx(沪)/880xxx(沪)/399xxx(深)指数: {text}")


def _etf_market(code: str) -> str:
    """ETF 归属：`51`/`56`/`58` → sh，`15`/`16` → sz（沪 5 段里的非 51/56/58 仍拒绝）。"""
    text = symbols.normalize(code)
    if text.startswith(("51", "56", "58")):
        return "sh"
    if text.startswith(("15", "16")):
        return "sz"
    raise symbols.SymbolError(
        f"暂支持51/56/58(沪)/15/16(深)ETF: {text}")


def to_tencent_symbol(code: str) -> str:
    """6位**个股**代码 → 腾讯符号（sz300308 / sh600036）。

    保留原有语义：只接受个股代码段，ETF/基金与北交所一律明确拒绝。
    行情指标请走 `to_tencent_market_symbol`（它按前缀分派个股/指数/ETF 三套规则）。
    """
    target = str(code or "").strip()
    if not (target.isdigit() and len(target) == 6):
        raise DataFetchError(f"腾讯日K连接器需要6位数字代码：{code!r}")
    # 北交所归属由 core.symbols 统一判定（4/8/920），不在这里再写一份
    try:
        if symbols.is_etf_code(target):
            raise DataFetchError(
                f"腾讯日K连接器的个股路径不支持ETF代码（请用 etf_close: 前缀）：{target}")
        return symbols.exchange_symbol(target)
    except symbols.SymbolError as exc:
        raise DataFetchError(
            f"腾讯日K不覆盖该代码（北交所/非法代码段）：{target} —— {exc}") from exc


def to_tencent_market_symbol(indicator: str) -> str:
    """行情指标（`stock_close:` / `index_close:` / `etf_close:`）→ 腾讯符号。

    实测 2026-09-22：腾讯对个股/ETF/指数**都**返回数据，符号规则分别与
    `quote_qmt_code`、`intraday/sources.index_symbol` 一致（同一套归属，避免再现
    "5 处 sh/sz 规则里 3 处是错的"那类 bug）。
    """
    prefix, _, raw_code = str(indicator or "").partition(":")
    code = raw_code.strip()
    try:
        if prefix == "stock_close":
            return to_tencent_symbol(code)
        if prefix == "index_close":
            return f"{_index_market(code)}{code}"
        if prefix == "etf_close":
            return f"{_etf_market(code)}{code}"
    except symbols.SymbolError as exc:
        raise DataFetchError(str(exc)) from exc
    raise DataFetchError(f"腾讯日K连接器不支持的指标: {indicator}")


def _number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


#: 指数成交量按量级折手。腾讯**指数**行的单位本身不稳定（实测见 `rows_to_points`），
#: 所以不写死系数，而是按量级判断：
#: 真值（Tushare/baostock/AkShare 三源一致）约 1~2 亿，超过阈值即认为是"放大了 1000 倍"的量级。
_INDEX_VOLUME_SANITY_MAX = 1.0e11


def rows_to_points(rows: list[Any], indicator: str, *, symbol: str,
                   adjust: str = "qfq",
                   start_date: str | None = None,
                   end_date: str | None = None) -> list[DataPoint]:
    """腾讯日K行 → DataPoint（纯函数，便于离线单测）。

    行序：`[日期, 开, 收, 高, 低, 成交量, ...]` —— 收在**高之前**。
    `adjust`：实际命中的节点口径（`qfqday` → `"qfq"`，`day` → `"none"`），
    **由调用方按实际节点传入**，不在这里猜 —— 猜错就是静默换口径。

    ⚠️ 成交量单位**按指标类别不同**，且腾讯的**指数**行本身不稳定
    （实测 2026-09-22，同日四源对照：腾讯 / Tushare / baostock / AkShare 新浪）：

    ```
    600036  腾讯 433,421           手    ↔ Tushare 433,420.69 手 ↔ baostock 43,342,069 股÷100
    510300  腾讯 6,445,874         手    ↔ Tushare 6,445,873.51 手 ↔ 新浪 644,587,351 股÷100
    000300  真值 177,863,876 手（Tushare index_daily / baostock÷100 / 新浪÷100 三源一致）
            但腾讯同一分钟内的两次请求分别给出 177,863,876,000 与 177,863,876,000,000
            —— 单位在**同一标的、同一天**都不自洽（曾分别观察到 1000 倍与 10^6 倍偏差）。
    ```

    所以：个股/ETF 行是**手**（原样使用）；**指数行只按量级兜底** ——
    超过 `_INDEX_VOLUME_SANITY_MAX` 就认为是放大了 1000 倍的量级并 ÷1000，
    同时在 `extra["volume_raw"]` 保留原值。**刻意不做"精确换算"**：
    腾讯指数的单位契约不成立，任何写死系数都会在某个时刻静默放大 1000 倍；
    按量级兜底 + 留痕，最坏情况是量柱不精确，而不是把 1000 倍误差灌进下游因子。
    """
    lower = (start_date or "").replace("-", "")
    upper = (end_date or "").replace("-", "")
    is_index = indicator.startswith("index_close:")
    points: list[DataPoint] = []
    for row in rows:
        if not isinstance(row, (list, tuple)) or len(row) < 6:
            continue
        raw_date = str(row[0]).strip().replace("-", "")
        if len(raw_date) != 8 or not raw_date.isdigit():
            continue
        if lower and raw_date < lower:
            continue
        if upper and raw_date > upper:
            continue
        close = _number(row[2])
        if close is None:
            continue
        volume = _number(row[5])
        volume_raw_unit = "手"
        if is_index and volume is not None and volume > _INDEX_VOLUME_SANITY_MAX:
            # 指数行单位不自洽：按量级兜底（真值约 1~2 亿），原值留在 extra 里可追溯
            volume = volume / 1000.0
            volume_raw_unit = "指数原量级(已÷1000)"
        points.append(DataPoint(
            indicator=indicator,
            value=close,
            period_date=f"{raw_date[:4]}-{raw_date[4:6]}-{raw_date[6:]}",
            extra={
                "open": _number(row[1]),
                "close": close,
                "high": _number(row[3]),
                "low": _number(row[4]),
                "volume": volume,               # 手
                "volume_unit": "手",
                "volume_raw_unit": volume_raw_unit,
                # 腾讯日K不返回成交额：置 None，绝不编造
                "amount": None,
                "adjust": adjust,
                "symbol": symbol,
            },
            source_name="腾讯财经日K",
            source_url=_FQKLINE_URL,
            source_type=DataSourceType.API,
            fetch_method=FetchMethod.API_CALL,
            confidence=0.8,
            verified=False,
        ))
    points.sort(key=lambda item: item.period_date or "")
    return points


def _shift_days(day: str, delta: int) -> str:
    """`YYYY-MM-DD` 加减自然日（翻页锚点回退用）。非法日期原样返回。"""
    try:
        year, month, date_ = (int(part) for part in day.split("-"))
        return (date(year, month, date_) + timedelta(days=delta)).isoformat()
    except (TypeError, ValueError):
        return day


class TencentDailyConnector(BaseConnector):
    """腾讯财经日K（个股/ETF 前复权，指数点数不复权）。"""

    source_name = "腾讯财经日K"
    source_url = _FQKLINE_URL

    def __init__(self, client: httpx.AsyncClient | None = None,
                 *, count: int = _DEFAULT_COUNT,
                 max_pages: int = _MAX_PAGES) -> None:
        self._client = client
        self._count = max(2, int(count))
        self._max_pages = max(1, int(max_pages))

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
            "notes": ("个股/ETF 前复权、指数点数口径（腾讯 fqkline，独立于东财/新浪通道）；"
                      "**带日期区间时不返回当天形成中bar**，要最新数据必须用不带区间的形式"
                      "（本连接器已按此处理，见模块 docstring）；"
                      "单次上限约 641 根，多页靠 end 锚点回退（fetch 自动翻页）；"
                      "只返回成交量（手），不返回成交额；北交所不覆盖"),
        }

    # ---- 取数 ----

    async def _page(self, client: httpx.AsyncClient, symbol: str,
                    end_anchor: str, *, open_range: bool = False
                    ) -> tuple[list[Any], str | None, str]:
        """取一页：返回 `(行, 本页最早日期, 实际节点名)`。

        `open_range=True` 走**完全不带日期区间**的形式（`day,,,N,qfq`）：
        这是唯一能拿到**当天形成中bar**的形式（实测见模块 docstring），
        代价是它不认锚点 —— 只能用于"要最新 N 根"的那一页（分页第 1 页）。

        节点口径：个股/ETF 请求 `qfq` → 响应给 `qfqday`；**指数只有 `day`**
        （实测 2026-09-22：sh000300/sz399006/sh000001 三个指数均无 `qfqday` 节点）。
        """
        param = (f"{symbol},day,,,{self._count},qfq" if open_range
                 else f"{symbol},day,{_EARLIEST_START},{end_anchor},{self._count},qfq")
        url = f"{_FQKLINE_URL}?param={param}"
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # noqa: BLE001 网络/解析异常统一转 DataFetchError
            raise DataFetchError(
                f"腾讯日K请求失败({symbol}): {brief(exc, BRIEF_DEFAULT)}") from exc

        node = (payload.get("data") or {}).get(symbol) or {}
        node_name = "qfqday" if node.get("qfqday") else "day"
        rows = node.get(node_name) or []
        if not rows and node:
            # 有些标的（上市不足/停牌/区间外）只在 `data` 下给个空 dict；如实报缺口
            logger.info("腾讯日K返回空节点(%s)：%s", symbol, list(node)[:6])
        # 本页最早日期取 **min** 而不是 `rows[0]`：翻页锚点必须严格回退，
        # 一旦行序不是升序（上游行为无契约保证），用首行当锚点会让锚点**前进**，
        # 翻页就变成原地打转：翻满页数后报错，或更糟 —— 静默只拿到尾部一段。
        normalized = [str(row[0]).strip().replace("-", "") for row in rows
                      if isinstance(row, (list, tuple)) and row]
        valid = [item for item in normalized
                 if len(item) == 8 and item.isdigit()]
        earliest = min(valid) if valid else None
        return rows, earliest, node_name

    @staticmethod
    def _adjust_for(indicator: str, node_name: str) -> str:
        """按实际节点标复权口径：指数恒 `none`，其余按节点（`day` 节点 ≠ 前复权）。"""
        if indicator.startswith("index_close:"):
            return "none"
        return "qfq" if node_name == "qfqday" else "none"

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        """取日K。`start_date`+`end_date` 都给全时**自动翻页**覆盖整个区间。

        只给 `start_date`（end 缺省）时退回单次取数：此时"最新 bar"比"补全区间"重要，
        且无锚点时无法保证分页语义（见模块 docstring 的实测）。

        **要最新数据时（end 缺省或不早于今天）会额外保住"当天形成中bar"**：
        不带区间的形式才给这根 bar，见模块 docstring。
        """
        if not self.supports(indicator):
            raise DataFetchError(f"腾讯日K连接器不支持的指标: {indicator}")
        symbol = to_tencent_market_symbol(indicator)

        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=_TIMEOUT, headers=_HEADERS)
        try:
            if not (start_date and end_date):
                # 单次取数 = "给我最新的 N 根"：直接用不带区间的形式，
                # 否则盘中拿不到当天形成中bar（模块 docstring 的实测）。
                rows, _, node_name = await self._page(
                    client, symbol, self._default_anchor(end_date),
                    open_range=True)
                points = self._rows_to_points(
                    rows, indicator, symbol, node_name, start_date, end_date)
                if not points:
                    raise DataFetchError(
                        f"腾讯日K在 {start_date or '不限'}~{end_date or '不限'} "
                        f"无 {symbol} 数据")
                return points
            return await self._fetch_paged(
                client, indicator, symbol, start_date, end_date,
                forming_bar=self._wants_forming_bar(end_date))
        finally:
            if own_client:
                await client.aclose()

    @staticmethod
    def _wants_forming_bar(end_date: str | None) -> bool:
        """这次请求要不要"当天的形成中bar"。

        判据：请求的 end **不早于今天**（None = "要到最新"）。
        end 在过去（历史区间/回测）时为 False —— 那种请求的第一页不能跳到今天，
        否则翻页要从今天一路退回目标区间（白翻几十页，甚至撞 `_MAX_PAGES` 抛错）。

        ⚠️ 这也意味着：**历史区间里不会有今天这根 bar**，这是对的 ——
        回测拿"未收盘的 bar"当已实现数据是未来函数。
        """
        if not end_date:
            return True
        return end_date >= date.today().isoformat()

    @staticmethod
    def _default_anchor(end_date: str | None) -> str:
        """end 锚点：显式 end_date 优先，否则取"今天之后几天"（实测能带出最新 bar）。"""
        if end_date:
            return end_date
        return (date.today() + timedelta(days=_END_ANCHOR_FORWARD_DAYS)).isoformat()

    @staticmethod
    def _rows_to_points(rows: list[Any], indicator: str, symbol: str,
                        node_name: str, start_date: str | None,
                        end_date: str | None) -> list[DataPoint]:
        return rows_to_points(
            rows, indicator, symbol=symbol,
            adjust=TencentDailyConnector._adjust_for(indicator, node_name),
            start_date=start_date, end_date=end_date)

    async def fetch_history(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        """多页历史取数（批量全市场补数的显式入口）。

        与 `fetch` 的区别只是**强制翻页语义**：两个日期都缺省时也走分页路径，
        默认从 `1990-01-01` 拉到今天 —— 调用方明确要"多页历史"时才付这个代价。
        """
        if not self.supports(indicator):
            raise DataFetchError(f"腾讯日K连接器不支持的指标: {indicator}")
        symbol = to_tencent_market_symbol(indicator)
        lower = start_date or _EARLIEST_START
        upper = end_date or (
            date.today() + timedelta(days=_END_ANCHOR_FORWARD_DAYS)).isoformat()
        own_client = self._client is None
        client = self._client or httpx.AsyncClient(
            timeout=_TIMEOUT, headers=_HEADERS)
        try:
            return await self._fetch_paged(
                client, indicator, symbol, lower, upper)
        finally:
            if own_client:
                await client.aclose()

    async def _fetch_paged(
        self, client: httpx.AsyncClient, indicator: str, symbol: str,
        start_date: str, end_date: str, *, forming_bar: bool = False,
    ) -> list[DataPoint]:
        """按 end 锚点回退翻页，直到覆盖 `start_date`（或翻满 `_max_pages`）。

        `forming_bar=True`（由 `_wants_forming_bar` 给出）时，**只有第 1 页**
        走不带区间的形式去拿当天形成中bar；第 1 页返回的 `earliest` 正好当下一页的锚点，
        所以翻页语义与原来完全一致。历史区间请求（forming_bar=False）行为不变。
        """
        lower = start_date.replace("-", "")
        rows_by_date: dict[str, Any] = {}
        node_name = ""
        anchor = end_date
        reached = False
        pages = 0
        for pages in range(1, self._max_pages + 1):  # noqa: B007 循环后要用 pages 报错
            rows, earliest, page_node_name = await self._page(
                client, symbol, anchor, open_range=forming_bar and pages == 1)
            node_name = node_name or page_node_name
            for row in rows:
                if not isinstance(row, (list, tuple)) or not row:
                    continue
                key = str(row[0]).strip().replace("-", "")
                if len(key) != 8 or not key.isdigit():
                    continue
                # 后取的页只覆盖更早的日期，先到的行保留即可（同一交易日数据一致）
                rows_by_date.setdefault(key, row)
            if earliest is None:
                break
            if earliest <= lower:          # earliest 已是 YYYYMMDD，与 lower 同口径
                reached = True
                break
            # 锚点回退一天，避免重复取同一根（_shift_days 吃 YYYY-MM-DD）
            anchor = _shift_days(
                f"{earliest[:4]}-{earliest[4:6]}-{earliest[6:]}", -1)

        if not rows_by_date:
            raise DataFetchError(
                f"腾讯日K在 {start_date}~{end_date} 无 {symbol} 数据")
        if not reached:
            # 必须响亮失败：截断的数据与"该股上市晚"无法区分，静默返回就是错数据
            earliest_seen = min(rows_by_date)
            raise DataFetchError(
                f"腾讯日K翻页 {pages}/{self._max_pages} 页仍未覆盖请求起点 {start_date}"
                f"（{symbol} 目前最早到 {earliest_seen[:4]}-{earliest_seen[4:6]}-"
                f"{earliest_seen[6:]}）；单次上限约 {self._count + 1} 根，"
                "请缩小区间或改用 baostock/Tushare 全历史源")

        ordered = [rows_by_date[key] for key in sorted(rows_by_date)]
        points = rows_to_points(
            ordered, indicator, symbol=symbol,
            adjust=self._adjust_for(indicator, node_name),
            start_date=start_date, end_date=end_date)
        if not points:
            raise DataFetchError(
                f"腾讯日K在 {start_date}~{end_date} 无 {symbol} 数据")
        return points
