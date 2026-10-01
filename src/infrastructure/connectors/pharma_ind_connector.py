r"""创新药 IND 申报数量真实数据连接器（CDE 药审中心「受理品种信息」公开接口）。

## 指标定义（先定清楚，因为它不是 CDE 直接发布的字段）

指标 id `ind:创新药IND申报数量(个)` 的值 = **1类创新药 IND 的受理号件数**，
按受理日期归月。三个判定条件缺一不可：

1. `acceptid[:4]` ∈ {CXHL, CXSL, CXZL} —— 境内**临床试验**申请（IND）。
   CXHS/CXSS/CXZS 是上市申请(NDA)，JXHL/JXSL 是进口 IND，均**不计入**本口径。
2. `registerkind` 首个 token 是 `1` / `1类` / `1.x`，且**不以「原」开头** ——
   CDE 注册分类里的 1 类创新药（`1.1`~`1.4` 是生物制品 1 类亚型，必须算进来：
   用 `in ('1','1类')` 精确匹配会每年漏掉约 160 件）。
3. 归月用 `createdate[:7]`（受理日期的年月）。

口径是**受理号件数**，不是品种数 —— 同一药品同日可能递 2~7 个受理号
（2026-09：受理号 240 件 / 去重品种 151 个）。指标名说的是"申报数量"，
故取件数；`extra.count_basis` 已登记，改口径时不要只改数字。

## 数据源（2026-09-26 实测，1,034/1,034 页全量扫描 0 报错）

- 页面: https://www.cde.org.cn/main/xxgk/listpage/9f9c74c73e0f8f56a8bfbc646055026d
- 接口: **POST** https://www.cde.org.cn/main/xxgk/getMenuListHc  → application/json
- **认证要点**：必须在**同一个 `requests.Session`** 里先 GET 一次
  `https://www.cde.org.cn/`。冷启动直接 POST 会拿到 **HTTP 202 + JS 反爬挑战页**
  （不是 JSON）；预热后再 POST 稳定 200。
- 表单参数：`year`(单年，只能一个) / `drugtype` / `applytype` / `pageSize`(服务端白名单
  仅 10/20/30/50，传 100 直接 500) / `pageNum`。
- 服务端过滤 `(drugtype,applytype) ∈ {(hy,xy),(swzp,xy),(zy,xy)}` 与"不筛全量再本地过滤"
  **结果完全一致**（1,942 = 1,942），但按年耗时从 ~104s 降到 ~29s，故按三组分别拉取。

## 延迟与"当月不满月"

CDE 受理数据有 T-2 天延迟（2026-09-26 抓到的最新受理日是 2026-09-24），且月末会回填，
因此**当月计数必然偏小**。本连接器照常产出当月点，但打 `extra.partial = True`，
并把已覆盖到的最后日期放进 `extra.covered_through`，避免把不满月当满月用。

## 历史深度

接口可回溯到 2000 年，但 2024 年前的记录 `createdate` 常为 None，故只取
`_MONTHS` 个月（默认 13）内的数据；更早的历史靠**快照累积**（每次成功采集都会把
月度计数并入快照，逐日把序列做长）。
"""
from __future__ import annotations

import asyncio
import json
import logging
from datetime import date, datetime
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

INDICATOR = "ind:创新药IND申报数量(个)"
_HOME_URL = "https://www.cde.org.cn/"
_API_URL = "https://www.cde.org.cn/main/xxgk/getMenuListHc"
_PAGE_URL = ("https://www.cde.org.cn/main/xxgk/listpage/"
             "9f9c74c73e0f8f56a8bfbc646055026d")
_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
_HEADERS = {
    "User-Agent": _UA,
    "X-Requested-With": "XMLHttpRequest",
    "Referer": _PAGE_URL,
    "Origin": "https://www.cde.org.cn",
}
_TIMEOUT_SEC = 30
_SNAPSHOT_SOURCE = "cde_class1_ind_monthly"

#: IND 受理号前缀（境内临床试验申请）
_IND_PREFIXES = ("CXHL", "CXSL", "CXZL")
#: 服务端过滤组合：等价于全量（见模块 docstring），三组分别拉取以省时间
_DRUG_APPLY = (("hy", "xy"), ("swzp", "xy"), ("zy", "xy"))
#: pageSize 服务端白名单只认 10/20/30/50
_PAGE_SIZE = 50
#: 取最近多少个月（更早靠快照累积）
_MONTHS = 13
#: 每组(drugtype,applytype)的翻页上限，给最坏情况兜底。
#: 正常止步靠服务端 total 算出的末页（见 _sweep：CDE 超页会绕回，不能靠翻空判断）。
_MAX_PAGES_PER_COMBO = 40


def is_class1_ind(record: dict[str, Any]) -> bool:
    """是否"1类创新药 IND"受理号（见模块 docstring 的三条判定）。"""
    acceptid = str(record.get("acceptid") or "").upper()
    if acceptid[:4] not in _IND_PREFIXES:
        return False
    kind = str(record.get("registerkind") or "").strip()
    if kind.startswith("原"):
        return False
    token = kind.split(";")[0].strip().split(".")[0]
    return token in ("1", "1类")


def parse_ind_records(records: list[dict[str, Any]]) -> dict[str, int]:
    """受理记录 → {YYYY-MM: 件数}（只统计 1 类创新药 IND）。"""
    counts: dict[str, int] = {}
    for r in records:
        if not is_class1_ind(r):
            continue
        created = str(r.get("createdate") or "")
        if len(created) < 7:
            continue
        month = created[:7]
        counts[month] = counts.get(month, 0) + 1
    return counts


def _month_floor(months_back: int) -> str:
    """往前 months_back 个月的月初 YYYY-MM-01。"""
    today = date.today()
    idx = today.year * 12 + (today.month - 1) - months_back
    return f"{idx // 12:04d}-{idx % 12 + 1:02d}-01"


class PharmaIndConnector(BaseConnector):
    """1类创新药 IND 申报件数（CDE 受理品种信息，月度，默认近13个月）。"""

    #: 口径落在 source_name 里（原因同 coal/liquor 连接器）：行业 Agent 的
    #: `_format_with_fresh` 只拼 source_name、不展开 extra，所以"受理号件数"与
    #: "当月不满月"这两个关键局限必须出现在名字上，否则会被当成完整月度值用。
    source_name = "CDE药审中心(1类IND受理号件数·当月不满月)"
    source_url = _PAGE_URL

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "simulated": False,
            "indicators": [INDICATOR],
            "notes": (
                "1类创新药 IND 受理号件数，按受理日期归月；"
                "CDE 有 T-2 延迟且月末回填，当月计数偏小（extra.partial=True）；"
                "口径为受理号件数而非品种数"
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
            raise DataFetchError(f"创新药IND连接器不支持的指标: {indicator}")

        cutoff = _month_floor(_MONTHS - 1)
        storage_fallback = False
        covered_through = ""
        try:
            counts, covered_through = await asyncio.to_thread(
                self._fetch_counts, cutoff)
            if not counts:
                raise DataFetchError(
                    "CDE受理接口未返回任何1类创新药IND记录（疑似反爬或过滤失配）")
        except Exception as exc:  # noqa: BLE001 网络/反爬/解析失败退快照
            snap = _latest_snapshot(_SNAPSHOT_SOURCE)
            snap_counts = (snap or {}).get("counts") or {}
            if not snap_counts:
                raise DataFetchError(
                    f"创新药IND获取失败且无本地快照: {exc}") from exc
            logger.warning("CDE在线取数失败，使用本地快照: %s", exc)
            counts = {k: int(v) for k, v in snap_counts.items()}
            storage_fallback = True
        else:
            # 与历史快照合并：每次只拉近13个月，合并后序列逐日做长
            merged = dict((_latest_snapshot(_SNAPSHOT_SOURCE) or {}).get("counts") or {})
            merged.update(counts)
            _write_snapshot(_SNAPSHOT_SOURCE, {
                "source_url": _API_URL,
                "count_basis": "acceptance_number",
                "fetch_time": datetime.now().isoformat(),
                "counts": merged,
            })
            counts = {k: int(v) for k, v in merged.items()}

        return self._to_points(counts, indicator, storage_fallback,
                               covered_through, start_date, end_date)

    # ---------------- 抓取 ----------------

    def _fetch_counts(self, cutoff: str) -> tuple[dict[str, int], str]:
        """按三组服务端过滤拉取，返回 ({YYYY-MM: 件数}, 已覆盖到的最后受理日)。

        只保留 `cutoff` 当月及以后的月份：翻页为了判断"已越过窗口"必然会多读
        一页，那一页里早于窗口的月份只被**部分**统计（例如 2025-07 只有 1 件，
        真实为 224 件）。这种"半个月"混进序列比缺月更危险，故直接丢弃 ——
        窗口之前的历史由历次快照累积而来，本来就是完整的。
        """
        counts: dict[str, int] = {}
        newest_created = ""
        cutoff_month = cutoff[:7]
        with requests.Session() as sess:
            sess.headers.update(_HEADERS)
            # 必须先预热：冷启动直接 POST 只会拿到 202 JS 挑战页
            self._warm(sess)
            for year in range(int(cutoff[:4]), date.today().year + 1):
                for drugtype, applytype in _DRUG_APPLY:
                    newest_created = self._sweep(
                        sess, year, drugtype, applytype, cutoff, counts,
                        newest_created)
        return ({m: n for m, n in counts.items() if m >= cutoff_month},
                newest_created)

    def _sweep(
        self,
        sess: requests.Session,
        year: int,
        drugtype: str,
        applytype: str,
        cutoff: str,
        counts: dict[str, int],
        newest_created: str,
    ) -> str:
        """拉取某年某组，返回更新后的 newest_created。

        ⚠️ 翻页必须用服务端 `total` 算出末页并**严格止步**：实测（2026-09-26）
        CDE 对超过末页的 pageNum **不是返回空，而是绕回前面的页**——
        `year=2026&drugtype=hy&applytype=xy` 共 1351 条(28页)，
        请求第 29/30/31 页返回的仍是第 1 页（CXHL2601201 / 2026-09-24）。
        若靠"翻到空页为止"或固定页数上限，多出来的页会把最新月份重复计数
        （实测会把 2026-09 从 240 抬到 795 —— 数字看着合理，却全是重复）。
        """
        last_page: int | None = None
        seen: set[str] = set()
        for page in range(1, _MAX_PAGES_PER_COMBO + 1):
            if last_page is not None and page > last_page:
                return newest_created
            records, total = self._page(sess, year, drugtype, applytype, page)
            if last_page is None and total:
                last_page = max(1, -(-total // _PAGE_SIZE))  # 向上取整
            if not records:
                return newest_created
            # 防御：绕回时整页都是见过的受理号 → 立即停，别重复计数
            ids = [str(r.get("acceptid") or "") for r in records]
            if ids and all(i and i in seen for i in ids):
                logger.info("CDE %s/%s/%s 第%d页与已见记录完全重合（疑似绕回），停止翻页",
                            year, drugtype, applytype, page)
                return newest_created
            seen.update(i for i in ids if i)

            oldest = ""
            for r in records:
                created = str(r.get("createdate") or "")[:10]
                if not created:
                    continue
                oldest = created if not oldest else min(oldest, created)
                newest_created = max(newest_created, created)
            for month, n in parse_ind_records(records).items():
                counts[month] = counts.get(month, 0) + n
            if oldest and oldest < cutoff:
                return newest_created
        return newest_created

    @staticmethod
    def _warm(sess: requests.Session) -> None:
        """预热拿反爬 Cookie：不做这一步 POST 会返回 202 挑战页。"""
        try:
            sess.get(_HOME_URL, timeout=_TIMEOUT_SEC)
        except requests.RequestException as exc:  # 预热失败仍试一次 POST
            logger.warning("CDE预热GET失败（仍尝试POST）: %s", exc)

    def _page(
        self,
        sess: requests.Session,
        year: int,
        drugtype: str,
        applytype: str,
        page: int,
    ) -> tuple[list[dict[str, Any]], int]:
        """取一页，返回 (records, total)。total 用于算末页（见 _sweep 的绕回说明）。"""
        payload = {
            "statenow": "", "year": str(year), "drugtype": drugtype,
            "applytype": applytype, "acceptid": "", "drugname": "",
            "company": "", "pageSize": str(_PAGE_SIZE), "pageNum": str(page),
        }
        resp = sess.post(_API_URL, data=payload, timeout=_TIMEOUT_SEC)
        if resp.status_code != 200 or "json" not in (
                resp.headers.get("content-type") or "").lower():
            # 202/HTML = 反爬挑战。重预热一次，仍失败就交给上层退快照
            logger.warning("CDE接口返回非JSON(HTTP %s)，重新预热后重试",
                           resp.status_code)
            self._warm(sess)
            resp = sess.post(_API_URL, data=payload, timeout=_TIMEOUT_SEC)
            if resp.status_code != 200:
                raise DataFetchError(
                    f"CDE受理接口不可用(HTTP {resp.status_code})，疑似反爬收紧")
        try:
            body = json.loads(resp.content.decode("utf-8", "ignore"))
        except json.JSONDecodeError as exc:
            raise DataFetchError(f"CDE受理接口返回非JSON: {exc}") from exc
        if int(body.get("code") or 0) != 200:
            raise DataFetchError(f"CDE受理接口报错: {body.get('msg')!r}")
        data = body.get("data") or {}
        return list(data.get("records") or []), int(data.get("total") or 0)

    # ---------------- 组装 ----------------

    def _to_points(
        self,
        counts: dict[str, int],
        indicator: str,
        storage_fallback: bool,
        covered_through: str,
        start_date: str | None,
        end_date: str | None,
    ) -> list[DataPoint]:
        current_month = date.today().strftime("%Y-%m")
        points: list[DataPoint] = []
        for month in sorted(counts):
            period = f"{month}-01"
            if start_date and period < start_date:
                continue
            if end_date and period > end_date:
                continue
            extra: dict[str, Any] = {
                "simulated": False,
                "frequency": "monthly",
                "unit": "个",
                "count_basis": "acceptance_number",
                "definition": "1类创新药IND受理号件数（CXHL/CXSL/CXZL，注册分类1类）",
                "count_note": "受理号件数，非品种数（同药同日可递多个受理号）",
                "raw_indicator": indicator[len("ind:"):],
            }
            if month == current_month:
                # T-2 延迟 + 月末回填：当月必然不满月，显式标注避免当满月用
                extra["partial"] = True
                extra["partial_note"] = (
                    f"当月数据截断，已覆盖至 {covered_through[:10] or '未知'}")
            if storage_fallback:
                extra["storage_fallback"] = True
            points.append(DataPoint(
                indicator=indicator,
                value=float(counts[month]),
                unit="个",
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
