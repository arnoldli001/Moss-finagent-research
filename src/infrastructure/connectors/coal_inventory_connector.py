r"""电厂煤炭库存真实数据连接器（中电联 CECI 周报，官方 JSON 接口）。

## 指标与口径

指标 id `ind:重点电厂煤炭库存(万吨)` 取的是中电联《CECI周报》里的
**「纳入统计的发电企业煤炭库存」(万吨)** —— 这是公开免费源里**唯一**口径正确
（发电企业/电厂库存，而非港口库存）的连续序列。

口径必须如实披露：中电联的"纳入统计的发电企业"是**其《电力行业燃料统计》
的样本口径**，不等于国家层面的"全国统调电厂/重点电厂"全量。序列与全量口径
同向、量级接近，但不是同一个总体，故 `extra.scope_note` 显式登记。

不采用港口库存做备源：CCTD 的秦皇岛港/环渤海港口库存是**港口**口径
（2026-09-24 分别 605 / 2442 万吨），与电厂库存是不同总体、量级差数倍，
混进同一条序列会造出凭空的跳变（与白酒"内参/综合"两个口径不可混用同理）。
故备源只有本地快照。

## 数据源（2026-09-26 实测）

- 列表: GET https://www.cec.org.cn/ms-mcms/mcms/content/list?id=719&pageNumber=N&pageSize=30
  → JSON，`totalNum=503`，字段 articleID / basicTitle / publicTime(epoch ms)
- 正文: GET https://www.cec.org.cn/ms-mcms/mcms/content/detail?id=<articleID>
  → JSON，正文 HTML 在 `data.articleContent`
- 需要浏览器 UA + `Referer: https://www.cec.org.cn/`；免费、无登录、无 Key。

## 两条必须记住的解析陷阱

1. **必须锚定「发电企业煤炭库存」**，不能只匹配"煤炭库存" ——
   正文里还有"海路运输电厂煤炭库存2891万吨"（另一个总体），
   只匹配后半段会静默抓到错的那一列。
2. 绝对量的句式有两种，正则必须同时覆盖：
   - `…发电企业煤炭库存10340万吨`（数紧跟锚点）
   - `…发电企业煤炭库存较9月10日增30万吨至10362万吨`（中间夹一个环比增量）
   只写 `库存([\d.]+)万吨` 会在第二种句式上抓到环比增量（差约 300 倍且不报错）。

## 期别日期怎么来

正文里的统计日有两种出现方式：锚点前直接写（`9月17日纳入统计…`），
或只给出比较基期（`较8月20日减少367万吨` —— 统计日 = 8月20日 + 7 天）。
两种都取不到时退回该篇周报的**发布日期**，并在 `extra.period_basis` 里说明。

## 历史深度

第 2026-07-06 期及更早的周报只公布**环比增减**、不公布绝对库存，因此本连接器
只能给出最近约 11 周（2026-07-09 起）的绝对量序列；再往前没有绝对量可取。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import requests

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector
from src.infrastructure.connectors.real_industry_connector import (
    _latest_snapshot,
    _write_snapshot,
)

logger = logging.getLogger(__name__)

INDICATOR = "ind:重点电厂煤炭库存(万吨)"
_LIST_URL = ("https://www.cec.org.cn/ms-mcms/mcms/content/list"
             "?id=719&pageNumber={page}&pageSize={size}")
_DETAIL_URL = "https://www.cec.org.cn/ms-mcms/mcms/content/detail?id={aid}"
_HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
    "Referer": "https://www.cec.org.cn/",
}
_TIMEOUT_SEC = 25
_SNAPSHOT_SOURCE = "cec_power_plant_coal"

#: 锚点：全口径序列名。绝不能缩短成"煤炭库存"（会撞上"海路运输电厂煤炭库存"）
_ANCHOR = "发电企业煤炭库存"
_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")
#: 句1: 库存[较X增N万吨]至10362万吨 ；句2: 库存10340万吨
_VALUE_RE = re.compile(r"至\s*([\d,]{4,9})\s*万吨")
_VALUE_BARE_RE = re.compile(r"([\d,]{4,9})\s*万吨")
_MD_RE = re.compile(r"(\d{1,2})月(\d{1,2})日")

#: 电厂库存的合理量级（万吨）：低于此值说明抓错列/抓成港口口径
_MIN_VALUE, _MAX_VALUE = 3000.0, 30000.0
_MIN_ROWS = 2
_MAX_STALE_DAYS = 21
#: 连续多少篇没有绝对量就认为已越过"只公布环比"的窗口，停止翻页
_MISS_LIMIT = 10
_MAX_PAGES = 3
_PAGE_SIZE = 30


def _plain(html: str) -> str:
    """HTML/JSON转义 → 单空格纯文本。"""
    text = _TAG_RE.sub(" ", html)
    text = text.replace("\\n", " ").replace("\\t", " ").replace("\\r", " ")
    text = text.replace("\\u002F", "/").replace("&nbsp;", " ")
    return _WS_RE.sub(" ", text)


def _resolve_period(text: str, anchor_at: int, pub_date: date) -> tuple[str, str]:
    """定出该期的统计日；返回 (YYYY-MM-DD, period_basis)。"""
    before = text[max(0, anchor_at - 40):anchor_at]
    found = _MD_RE.findall(before)
    if found:
        month, day = (int(x) for x in found[-1])
        return _to_iso(month, day, pub_date), "stat_date_stated"

    after = text[anchor_at:anchor_at + 80]
    # "较8月20日减少367万吨" → 统计日 = 基期 + 7 天
    rel = _MD_RE.search(after)
    if rel:
        month, day = int(rel.group(1)), int(rel.group(2))
        base = _to_date(month, day, pub_date)
        if base is not None:
            return (base + timedelta(days=7)).isoformat(), "stat_date_derived_+7d"
    return pub_date.isoformat(), "publish_date"


def _to_date(month: int, day: int, pub_date: date) -> date | None:
    """把正文里的 M月D日 补上年份（跨年时回退一年）。"""
    year = pub_date.year
    if month > pub_date.month + 1:  # 正文月明显晚于发布月 → 属上一年
        year -= 1
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _to_iso(month: int, day: int, pub_date: date) -> str:
    d = _to_date(month, day, pub_date)
    return (d or pub_date).isoformat()


def parse_cec_inventory(html: str, pub_date: date) -> tuple[str, float, str] | None:
    """CECI周报正文 → (期别, 发电企业煤炭库存万吨, period_basis)；取不到返回 None。

    只认「发电企业煤炭库存」这一列；同一篇里若还有"海路运输电厂煤炭库存"
    等其它总体，一律不取。
    """
    text = _plain(html)
    at = text.find(_ANCHOR)
    if at < 0:
        return None
    window = text[at + len(_ANCHOR):at + len(_ANCHOR) + 80]

    m = _VALUE_RE.search(window) or _VALUE_BARE_RE.search(window)
    if not m:
        return None
    try:
        value = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    if not (_MIN_VALUE <= value <= _MAX_VALUE):
        # 越界说明抓到了别的列（如环比增量或港口口径），宁缺勿错
        logger.warning("CECI电厂库存越界(%s)，本期望弃用", value)
        return None
    period, basis = _resolve_period(text, at, pub_date)
    return period, value, basis


class CoalInventoryConnector(BaseConnector):
    """电厂煤炭库存（中电联 CECI 周报「发电企业煤炭库存」，周频，约最近11期）。"""

    #: 口径必须落在 source_name 里：行业 Agent 的 `_format_with_fresh` 只把
    #: indicator/期间/值/confidence/**source_name** 拼进 LLM 上下文，**不展开 extra**
    #: —— 把"样本口径"只写进 extra 等于没披露（同 liquor_price_connector 的说明）。
    source_name = "中电联CECI周报(发电企业样本库存)"
    source_url = "https://www.cec.org.cn/"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "simulated": False,
            "indicators": [INDICATOR],
            "notes": (
                "中电联《CECI周报》纳入统计的发电企业煤炭库存，周频、万吨；"
                "为中电联燃料统计样本口径（非全国统调/重点电厂全量）；"
                "2026-07-06 及更早期次只公布环比增减，故绝对量序列仅约最近11周"
            ),
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator == INDICATOR

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        if indicator != INDICATOR:
            raise DataFetchError(f"电厂煤炭库存连接器不支持的指标: {indicator}")

        storage_fallback = False
        try:
            rows = await asyncio.to_thread(self._fetch_rows)
            if len(rows) < _MIN_ROWS:
                raise DataFetchError(
                    f"CECI有效期数不足({len(rows)} < {_MIN_ROWS})，"
                    "疑似接口或正文结构变化")
            newest = date.fromisoformat(rows[-1][0])
            stale = (date.today() - newest).days
            if stale > _MAX_STALE_DAYS:
                raise DataFetchError(
                    f"CECI最新一期过旧({rows[-1][0]}，距今{stale}天)")
        except Exception as exc:  # noqa: BLE001 网络/解析/新鲜度失败退快照
            snap = _latest_snapshot(_SNAPSHOT_SOURCE)
            snap_rows = (snap or {}).get("records") or []
            if not snap_rows:
                raise DataFetchError(
                    f"电厂煤炭库存获取失败且无本地快照: {exc}") from exc
            logger.warning("CECI在线取数失败，使用本地快照: %s", exc)
            rows = [(str(r["period"]), float(r["value"]), "snapshot")
                    for r in snap_rows]
            storage_fallback = True
        else:
            _write_snapshot(_SNAPSHOT_SOURCE, {
                "source_url": self.source_url,
                "scope": "中电联燃料统计样本：纳入统计的发电企业",
                "fetch_time": datetime.now().isoformat(),
                "records": [{"period": p, "value": v} for p, v, _ in rows],
            })

        return self._to_points(rows, indicator, storage_fallback,
                               start_date, end_date)

    # ---------------- 抓取 ----------------

    def _fetch_rows(self) -> list[tuple[str, float, str]]:
        """翻列表→逐篇取正文，抽出绝对量序列（升序、按期去重）。"""
        by_period: dict[str, tuple[float, str]] = {}
        misses = 0
        for page in range(1, _MAX_PAGES + 1):
            articles = self._list_page(page)
            if not articles:
                break
            for aid, pub in articles:
                got = parse_cec_inventory(self._detail(aid), pub)
                if got is None:
                    misses += 1
                    if misses >= _MISS_LIMIT:
                        # 已越过"只公布环比增减"的窗口，再往前翻也没有绝对量
                        return sorted((p, v, b) for p, (v, b) in by_period.items())
                    continue
                misses = 0
                period, value, basis = got
                by_period.setdefault(period, (value, basis))
        return sorted((p, v, b) for p, (v, b) in by_period.items())

    @staticmethod
    def _list_page(page: int) -> list[tuple[int, date]]:
        url = _LIST_URL.format(page=page, size=_PAGE_SIZE)
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT_SEC)
        resp.raise_for_status()
        payload = json.loads(resp.content.decode("utf-8", "ignore"))
        items = ((payload or {}).get("data") or {}).get("list") or []
        out: list[tuple[int, date]] = []
        for it in items:
            aid, ts = it.get("articleID"), it.get("publicTime")
            if aid is None or ts is None:
                continue
            out.append((int(aid),
                        datetime.fromtimestamp(int(ts) / 1000).date()))
        return out

    @staticmethod
    def _detail(article_id: int) -> str:
        url = _DETAIL_URL.format(aid=article_id)
        resp = requests.get(url, headers=_HEADERS, timeout=_TIMEOUT_SEC)
        resp.raise_for_status()
        payload = json.loads(resp.content.decode("utf-8", "ignore"))
        data = (payload or {}).get("data") or {}
        return str(data.get("articleContent") or "")

    # ---------------- 组装 ----------------

    def _to_points(
        self,
        rows: list[tuple[str, float, str]],
        indicator: str,
        storage_fallback: bool,
        start_date: str | None,
        end_date: str | None,
    ) -> list[DataPoint]:
        points: list[DataPoint] = []
        for period, value, basis in rows:
            if start_date and period < start_date:
                continue
            if end_date and period > end_date:
                continue
            extra: dict[str, Any] = {
                "simulated": False,
                "frequency": "weekly",
                "unit": "万吨",
                "scope": "纳入统计的发电企业（中电联燃料统计样本）",
                "scope_note": (
                    "中电联样本口径，非全国统调/重点电厂全量；"
                    "同向可比，总体不同"
                ),
                "period_basis": basis,
                "raw_indicator": indicator[len("ind:"):],
            }
            if storage_fallback:
                extra["storage_fallback"] = True
            points.append(DataPoint(
                indicator=indicator,
                value=value,
                unit="万吨",
                period_date=period,
                extra=extra,
                source_name=self.source_name,
                source_url=self.source_url,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.API_CALL,
                confidence=0.85,
                verified=True,
            ))
        return points
