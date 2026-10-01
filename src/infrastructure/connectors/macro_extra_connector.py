r"""中国宏观补充连接器：官方 PMI 与 GDP（东财口径为主，国家统计局为备源）。

## 为什么需要它（用户报障原话）

「要确保数据都能找到，不存在数据缺少问题」「连接器找不到、指标等级里无id、
INDICATOR_CATALOG 目录里没有，都要自动去联网获取数据，做最差的兜底，
一定要找到数据」。

实测（2026-09-28）：`PMI` / `GDP` 在**采集侧没有任何连接器实现** ——

    supports("PMI")        -> 无人支持
    supports("PMI:制造业")  -> 无人支持
    supports("GDP")        -> 无人支持
    supports("GDP:同比")    -> 无人支持

而下游**三处独立契约**早就把它们当真实指标对待：
A08 宏观 Agent 的白名单字面量 `"PMI"` / `"GDP"` / `"GDP同比"`
（`supervisor.py::_AGENT_DATA_WHITELIST`）、取数深度契约
（`catalog/fetch_depth.py`：`^(CPI|PPI|M2|社融|PMI)` → 月频 12 条、
`^(GDP|gdp)` → 季频 8 条）、别名表（`catalog/synonym_dict.py`：
`"pmi"→("PMI",)`、`"gdp"→("GDP",)`）。
**只有采集侧是空的** —— 于是"当前宏观环境如何"这个问题缺了 PMI/GDP
这两个最基本的维度，且不报错。

## 口径（2026-09-28 逐条真实调用，不是推断）

| 指标 id | 口径 | 单位 | 频率 | 实测最新一期 |
|---|---|---|---|---|
| `PMI` | 国家统计局/中采联 **官方制造业PMI**（扩散指数，50=荣枯线） | 指数 | 月 | 2026-08 = 49.8 |
| `PMI:制造业` | 同上（同一序列的显式别名，便于按口径点名） | 指数 | 月度 | 2026-08 = 49.8 |
| `PMI:非制造业` | 官方**非制造业商务活动指数** | 指数 | 月度 | 2026-08 = 49.0 |
| `GDP` | 国内生产总值 **累计值**（现价，年内累计） | 亿元 | 季度 | 2026 上半年 = 695704.0 |
| `GDP:同比` | 国内生产总值 **累计同比**（不变价） | % | 季度 | 2026 上半年 = 4.7 |

**`PMI` 为什么取「制造业」口径**（而不是综合 PMI 产出指数）：
  1. 它是"中国 PMI"的默认语义 —— `configs/calendar_official.yaml` 的日历事件名
     就是「制造业PMI」，`docs/PRD.md` 也把 PMI 与 CPI/PPI 并列；
  2. **双源可取**：东财 `macro_china_pmi` 与国家统计局 NBS 都能取到，且逐月对拍
     一致（2026-08 两边都是 49.8）→ 有真备源；
  3. 综合 PMI 产出指数实测**只有 NBS 一个源**（`综合PMI产出指数(%)`，
     2026-08 = 49.5），东财 `macro_china_pmi` 不含它 —— 单源不宜做默认口径。
     若要登记 `PMI:综合`，在 `_SPECS` 加一行即可（`PMI:制造业` 那行就是模板）。

**`GDP` 与 `GDP:同比` 为什么是两个 id**：东财 `macro_china_gdp` 的两列
（`国内生产总值-绝对值` / `国内生产总值-同比增长`）就是这两者，一一对应、
各自单值。合成一个 id 会造出"同一个 indicator 多种语义"
（AGENTS.md 的 `fed:policy_range` 教训：数字看着有据、语义是错的）。

## 三个实测陷阱（都会**静默**出错，所以写在这里）

1. **季度标签不能用 `akshare_connector.period_to_iso`**。它的正则
   `(\d{4})\D{0,2}(\d{1,2})` 把 `2026年第1-2季度` 与 `2026年第1季度`
   **都解析成 `2026-01`**（实测复现 4 例）—— 四个季度塌成一个期别、互相覆盖，
   而下游"取最新一条"会拿到错的那一条。本模块自己解析季度标签，
   累计口径取**窗口最后一个月**：`2026年第1-2季度` → `2026-06`、
   `2025年第1-4季度` → `2025-12`。实测 82 行 → **82 个互不冲突**的期别。
2. **金十的 GDP 是另一个口径，不能当备源**：`macro_china_gdp_yearly`
   （金十数据中心「中国GDP年率报告」）实测是**单季同比** ——
   发布 2024-12（全年）金十 5.4 而东财累计同比 5.0、2024-06 金十 4.7 而累计 5.0；
   且只更新到 2025-07。**数字看着有据、语义是错的，比"缺数据"更危险**，
   故 `GDP:同比` 当前**没有同口径备源**，东财失败即如实报错。
   （PMI 不同：金十 `macro_china_pmi_yearly` 与东财**是同一序列** ——
   实测 208 个重叠月逐月相等，只是更新停在 2025-08，故仍以 NBS 为首选备源。）
3. **东财依赖**：两个主源接口都打在 `datacenter-web.eastmoney.com`。
   本机对东财有过 TLS SNI 阻断（见 `src/core/eastmoney_direct.py`）；应用启动时
   若开了 `MOSS_EM_DIRECT`，它会**全局**给 `requests` 打补丁，akshare 一并受益。
   阻断漂移时由 NBS 备源接管。

## 备源（同口径，逐值对拍过）

国家统计局 NBS（`data.stats.gov.cn`，akshare `macro_china_nbs_nation`）：

    · 月度 `采购经理指数 > 制造业采购经理指数`    行 `制造业采购经理指数(%)`
    · 月度 `采购经理指数 > 非制造业采购经理指数`  行 `非制造业商务活动指数(%)`
    · 季度 `国民经济核算 > 国内生产总值 (现价)`   行 `国内生产总值_累计值(亿元)`

实测对拍（两边**逐值相等**）：2026-08 制造业 49.8 = 49.8、非制造业 49.0 = 49.0；
2026 二季度累计值 695704.0 = 695704.0。

⚠️ NBS 新站有**偶发 WAF 挑战**（`JSONDecodeError: unexpected character...`，
实测连续压测几轮后出现、数十秒不恢复），与
`akshare_connector._nbs_price_yoy_points` 注释里记录的现象一致
→ 本模块照它的做法重试 3 次、退避 2/4/6 秒。

## 期别格式：**期末日**（`YYYY-MM-DD`）

`period_date` 标的是"这条数据所描述的那段时间的**最后一天**"，不是月首：

    PMI 2026年8月份     → 2026-08-31（官方制造业PMI 正是**当月最后一天**发布的）
    GDP 2026年第1-2季度 → 2026-06-30（累计窗口 = 到 6 月为止）

为什么不用"月首"（`YYYY-MM` / `YYYY-MM-01`，M2/社融那条路是这么标的）——
两笔实测账（`DataFreshnessEvaluator.evaluate`，today = 2026-09-29）：

    月首标注  PMI 2026-08-01 → 距今天数 59 → conf 0.374 → **stale（权重 0.3）**
    期末标注  PMI 2026-08-31 → 距今天数 29 → conf 0.617 → lagging（权重 0.7）
    月首标注  GDP 2026-06-01 → 距今天数 120 → conf 0.135 → **stale**，
              且 `router._is_db_fresh`（conf≥0.4）判**不新鲜 → 每次查询都穿透联网**
    期末标注  GDP 2026-06-30 → 距今天数 91 → conf 0.513 → lagging，库够新即短路

月首标注会凭空多算**最多 30 天**的"数据年龄"，把一个月前刚发布的官方数据打成
"陈旧，仅供参考"、还让路由每次白跑一趟网络；对 PMI 更严重 —— 它**早于**官方
发布日（月末披露），与本仓库 CPI/PPI 那条"数据月→次月对齐、防止月末信号偷看"
的纪律相冲突。

GDP 的残余提前量如实披露在 `extra["period_basis"]`（季度累计值官方在**次月中旬**
发布，比期末日晚约两周）—— 接口帧里没有发布时刻字段，不臆造一个。

## 不伪造数据

接口异常 / 返回结构变化 / 数值越界 → `DataFetchError`，**绝不**返回 0 或占位值。
「没量到」≠「量到 0」。

## 期别格式

`period_date` 是 **期末日**（`YYYY-MM-DD`）：该数据描述的那段期间的**最后一天**
（PMI 数据月月末 = 官方发布日；GDP 是累计窗口末月月末）。为什么不标月首、
以及它多算 30 天"数据年龄"的实测账，见上面的《期别格式：期末日》一节。

`core/data_freshness._parse_period_date` 对 `YYYY-MM` 与 `YYYY-MM-DD` 都认，
本仓库没有任何消费方假设月首格式（已 grep 过 `period_date` 的全部解析点）。
"""

from __future__ import annotations

import asyncio
import calendar
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from src.core.errors import BRIEF_TIGHT, brief
from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

#: 主源（东财数据中心）页面 —— akshare 就是抓这两个页面背后的 JSON 接口
_EM_PMI_PAGE = "https://data.eastmoney.com/cjsj/pmi.html"
_EM_GDP_PAGE = "https://data.eastmoney.com/cjsj/gdp.html"
#: 备源（国家统计局）站点
_NBS_PAGE = "https://data.stats.gov.cn/"

#: NBS 请求的期别窗口。**只收录实测可用的形态**（`LAST10` 实测通；
#: 更长的 `LAST40` / `2016-` 在 2026-09-28 的 WAF 窗口内未能验证，故不写进来当默认）。
_NBS_PERIOD = "LAST10"
#: 备源重试次数与退避基数（秒）：照 `akshare_connector._nbs_price_yoy_points` 的既有做法
_NBS_RETRIES = 3
_NBS_BACKOFF_SEC = 2.0

#: 中文数字 → 季序号（NBS 季度标签用「第二季度」；东财用阿拉伯数字「第1-2季度」）
_CN_QUARTER = {"一": 1, "二": 2, "三": 3, "四": 4, "1": 1, "2": 2, "3": 3, "4": 4}

#: 「2026年第1-2季度」/「2026年第二季度」→ 年内累计到第 N 个季度
_QUARTER_RE = re.compile(
    r"(\d{4})\s*年\s*第?\s*([一二三四1-4])\s*(?:[-~—－至]\s*([一二三四1-4])\s*)?季度")
#: 「2026年08月份」/「2026年8月」
_MONTH_RE = re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月")


def _quarter_to_period(label: Any) -> str | None:
    """季度累计标签 → 期别**期末日**：`2026年第1-2季度` → `'2026-06-30'`。

    取"累计窗口最后一个月"是因为 `GDP` 口径是**年内累计值** —— "1-2 季度"这一行
    代表的是**到 6 月为止**的累计量；标成 4 月会让"取最新一期"与新鲜度判定
    都错位一个月。日用月末（见模块 docstring：月末标注少算 30 天年龄）。

    ⚠️ 不要换用 `akshare_connector.period_to_iso`：它把四个季度标签塌成
    `YYYY-01`（见模块 docstring 陷阱 1）。
    """
    match = _QUARTER_RE.search(str(label))
    if match is None:
        return None
    last = _CN_QUARTER.get(match.group(3) or match.group(2))
    if last is None:
        return None
    return _month_end(int(match.group(1)), last * 3)


def _month_to_period(label: Any) -> str | None:
    """「2026年08月份」/「2026年8月」 → `'2026-08-31'`；解析不出返回 None。

    官方制造业PMI 在**当月最后一天**发布，所以月末日就是它的发布日 ——
    既不早于发布（无偷看），也不凭空多算 30 天年龄（见模块 docstring）。
    """
    match = _MONTH_RE.search(str(label))
    if match is None:
        return None
    month = int(match.group(2))
    if not 1 <= month <= 12:
        return None
    return _month_end(int(match.group(1)), month)


def _month_end(year: int, month: int) -> str:
    """该年月的最后一天（`YYYY-MM-DD`）。用标准库算闰年，不硬编码 28/30/31。"""
    return f"{year:04d}-{month:02d}-{calendar.monthrange(year, month)[1]:02d}"


def _to_float(raw: Any) -> float | None:
    """安全转 float（NaN / 空 / 文本一律 None，不把缺失当 0）。"""
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    return None if value != value else value  # NaN != NaN


def _in_range(period: str, start_date: str | None, end_date: str | None) -> bool:
    """期末日 `YYYY-MM-DD` 是否落在 [start, end] 内（**月粒度、按整月包含**）。

    为什么按月而不是按日：调用方（SmartFetcher / 面板）给的区间是"月"的语义
    （"最近12期PMI"），而期别标的是期末日；按日比较会让"查到 2026-08-15 为止"
    这种请求把 2026-08-31 那期漏掉 —— 用户看到的是"最新一期没了"。

    与 `akshare_connector._in_range` 同语义（那边还要处理日粒度，这边只有月粒度，
    故不 import 那个**私有**函数：它所在文件正被并发协作者修改，少一条耦合）。
    """
    key = period.replace("-", "")
    if len(key) < 6:
        return False
    low, high = key[:6] + "01", key[:6] + "31"
    start = (start_date or "").replace("-", "")
    end = (end_date or "").replace("-", "")
    start = (start + "01")[:8] if start else ""
    end = (end + "31")[:8] if len(end) == 6 else end
    return (not start or high >= start) and (not end or low <= end)


def _pick_column(columns: list[str], candidates: tuple[str, ...], where: str) -> str:
    """按**精确**列名依次取首个命中；都不命中 → `DataFetchError`（带实际列名）。

    ⚠️ 这里**故意不做子串匹配**：本项目实测过"锚点缩短即静默抓错列"
    （`coal_inventory_connector`：只匹配"煤炭库存"会抓到
    "海路运输电厂煤炭库存"，是另一个总体）。本连接器同样敏感 ——
    `制造业-指数` 是 `非制造业-指数` 的子串，一旦列名漂移就会静默串到
    另一个口径上。宁可**响亮失败**，也不返回错序列。
    """
    for name in candidates:
        if name in columns:
            return name
    raise DataFetchError(
        f"{where} 返回结构异常：缺列 {list(candidates)}，实际列={columns}")


@dataclass(frozen=True)
class _Spec:
    """一个指标 id 的完整取数口径（东财主源列 + NBS 备源行）。"""

    indicator: str
    #: 口径人话。**会进 `source_name`** —— Agent 的 `_format_with_fresh` 只把
    #: source_name 拼进 LLM 上下文、不展开 extra，口径只写 extra 等于没披露
    #: （同 `coal_inventory_connector` 的说明）。
    label: str
    unit: str
    frequency: str
    basis: str
    period_of: Callable[[Any], str | None]
    em_func: str
    em_page: str
    em_date_col: str
    em_columns: tuple[str, ...]
    #: 值域兜底（含端点）：越界 = 列名漂移抓错列（如抓到"同比"列）→ 该行弃用。
    #: 实测：`制造业-同比` 的量级是 ±4，`制造业-指数` 是 49 上下。
    lo: float
    hi: float
    cumulative: bool = False
    #: NBS 备源（`nbs_path` 为空 = 该指标**没有同口径备源**，如实报错）
    nbs_kind: str = ""
    nbs_path: str = ""
    nbs_rows: tuple[str, ...] = field(default_factory=tuple)


#: 指标 id → 口径。id 是**精确串**（`supports()` 就是这张表的键集合）
_SPECS: dict[str, _Spec] = {
    spec.indicator: spec
    for spec in (
        _Spec(
            indicator="PMI",
            label="国家统计局·官方制造业PMI",
            unit="指数",
            frequency="monthly",
            basis="国家统计局/中采联 官方制造业采购经理指数（扩散指数，50=荣枯线）",
            period_of=_month_to_period,
            em_func="macro_china_pmi",
            em_page=_EM_PMI_PAGE,
            em_date_col="月份",
            em_columns=("制造业-指数",),
            lo=20.0, hi=80.0,
            nbs_kind="月度数据",
            nbs_path="采购经理指数 > 制造业采购经理指数",
            nbs_rows=("制造业采购经理指数(%)",),
        ),
        _Spec(
            indicator="PMI:制造业",
            label="国家统计局·官方制造业PMI",
            unit="指数",
            frequency="monthly",
            basis="同 `PMI`（同一序列的显式口径别名）",
            period_of=_month_to_period,
            em_func="macro_china_pmi",
            em_page=_EM_PMI_PAGE,
            em_date_col="月份",
            em_columns=("制造业-指数",),
            lo=20.0, hi=80.0,
            nbs_kind="月度数据",
            nbs_path="采购经理指数 > 制造业采购经理指数",
            nbs_rows=("制造业采购经理指数(%)",),
        ),
        _Spec(
            indicator="PMI:非制造业",
            label="国家统计局·官方非制造业商务活动指数",
            unit="指数",
            frequency="monthly",
            basis="国家统计局/中采联 官方非制造业商务活动指数（扩散指数，50=荣枯线）",
            period_of=_month_to_period,
            em_func="macro_china_pmi",
            em_page=_EM_PMI_PAGE,
            em_date_col="月份",
            em_columns=("非制造业-指数",),
            lo=20.0, hi=80.0,
            nbs_kind="月度数据",
            nbs_path="采购经理指数 > 非制造业采购经理指数",
            nbs_rows=("非制造业商务活动指数(%)",),
        ),
        # 注：`综合PMI产出指数` 实测**只有 NBS 有**（2026-08 = 49.5）。
        # 要登记 `PMI:综合` 的话，照下面 GDP 那条的形状写：
        #   em_func 无 → 让 `_rows_from_eastmoney` 直接抛错会污染日志，
        #   所以真要做需要给 _Spec 加一个"仅备源"标记，别直接复制。
        _Spec(
            indicator="GDP",
            label="国家统计局·GDP累计值(现价)",
            unit="亿元",
            frequency="quarterly",
            basis="国内生产总值**累计值**（现价，年内累计：1季度/1-2季度/1-3季度/1-4季度）",
            period_of=_quarter_to_period,
            em_func="macro_china_gdp",
            em_page=_EM_GDP_PAGE,
            em_date_col="季度",
            em_columns=("国内生产总值-绝对值",),
            lo=1_000.0, hi=5_000_000.0,
            cumulative=True,
            nbs_kind="季度数据",
            nbs_path="国民经济核算 > 国内生产总值 (现价)",
            nbs_rows=("国内生产总值_累计值(亿元)",),
        ),
        _Spec(
            indicator="GDP:同比",
            label="国家统计局·GDP累计同比(不变价)",
            unit="%",
            frequency="quarterly",
            basis="国内生产总值**累计同比**（不变价，与累计值同期别）",
            period_of=_quarter_to_period,
            em_func="macro_china_gdp",
            em_page=_EM_GDP_PAGE,
            em_date_col="季度",
            em_columns=("国内生产总值-同比增长",),
            lo=-30.0, hi=30.0,
            cumulative=True,
            # ★ 无同口径备源：金十 `macro_china_gdp_yearly` 是**单季同比**
            #   （见模块 docstring 陷阱 2），混进来就是"数字看着有据、语义是错的"。
            #   所以这里**故意**留空 —— 东财失败时如实抛 DataFetchError。
        ),
    )
}


def _import_akshare() -> Any:
    try:
        import akshare as ak
    except ImportError as exc:  # pragma: no cover - 依赖缺失路径
        raise DataFetchError("akshare未安装，请执行: uv sync --extra data") from exc
    return ak


class MacroExtraConnector(BaseConnector):
    """中国宏观补充：官方 PMI（制造业/非制造业）与 GDP（累计值/累计同比）。

    主源 AkShare 的东财口径（`macro_china_pmi` / `macro_china_gdp`），
    备源国家统计局 NBS（`macro_china_nbs_nation`，同口径、逐值对拍过）。
    """

    source_name = "中国宏观PMI/GDP(AkShare-东财/统计局)"
    source_url = _EM_PMI_PAGE

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "simulated": False,
            # ⚠️ 这里放的是**精确 id**（不带 `{...}` 占位符）：模板前缀会触发
            # `test_contract_consistency` 的"①→② 已实现却未登记"断言，
            # 而这几个 id 的登记由登记侧负责（本轮不在本文件职责内）。
            "indicators": list(_SPECS),
            "notes": (
                "官方PMI(制造业/非制造业，扩散指数，月频) + GDP(累计值亿元/累计同比%，"
                "季频)。主源东财(AkShare)，备源国家统计局NBS(同口径对拍一致)；"
                "GDP:同比 无同口径备源(金十该序列是单季同比，故意不接)"
            ),
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        """精确匹配（不做前缀/正则）：认哪些 id 完全由 `_SPECS` 决定。"""
        return indicator in _SPECS

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        spec = _SPECS.get(indicator)
        if spec is None:
            raise DataFetchError(
                f"宏观补充连接器不支持的指标: {indicator}"
                f"（支持: {', '.join(_SPECS)}）")

        rows, origin, primary_error = await asyncio.to_thread(
            self._fetch_rows, spec)

        points: list[DataPoint] = []
        for period, value in rows:
            if not _in_range(period, start_date, end_date):
                continue
            extra: dict[str, Any] = {
                "simulated": False,
                "unit": spec.unit,
                "frequency": spec.frequency,
                "basis": spec.basis,
                "cumulative": spec.cumulative,
                # 期别 = 期末日（见模块 docstring《期别格式》）；GDP 的发布提前量
                # 如实标注，不让下游把"期末"当成"可获取时点"。
                "period_basis": (
                    "期末日（YYYY-MM-DD）：PMI 为数据月月末=官方发布日；"
                    "GDP 为累计窗口末月月末，官方发布在次月中旬（比期别晚约两周）"),
                "origin": origin,
                "source_interface": (
                    spec.em_func if origin == "eastmoney" else "macro_china_nbs_nation"),
            }
            if primary_error:
                extra["fallback_source"] = "国家统计局NBS(AkShare)"
                extra["primary_error"] = primary_error
            source = ("(东财/AkShare)" if origin == "eastmoney"
                      else "(国家统计局NBS/AkShare)")
            points.append(DataPoint(
                indicator=indicator,
                value=value,
                unit=spec.unit,
                period_date=period,
                extra=extra,
                source_name=f"{spec.label}{source}",
                source_url=spec.em_page if origin == "eastmoney" else _NBS_PAGE,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.API_CALL,
                # publish_time 留空：这两个接口的帧里**没有**发布时刻字段，
                # 不臆造一个（与 akshare_connector 的宏观序列同一处理）。
                confidence=0.85 if origin == "eastmoney" else 0.8,
                verified=False,
            ))
        return points

    # ---------------- 取数（同步，在线程池里跑）----------------

    def _fetch_rows(
        self, spec: _Spec,
    ) -> tuple[list[tuple[str, float]], str, str]:
        """主源 →（失败才走）备源；返回 (行, origin, 主源失败原因)。

        行是 `(期末日 YYYY-MM-DD, 值)` 的**未过滤**全序列；区间过滤由 `fetch` 统一做
        （这样"区间内没有数据"返回空列表、"源本身没数据"抛错，两者不会混淆）。
        """
        try:
            rows = self._rows_from_eastmoney(spec)
        except Exception as exc:  # noqa: BLE001 源故障 → 试备源
            reason = brief(exc, BRIEF_TIGHT)
            if not spec.nbs_path:
                raise DataFetchError(
                    f"{spec.indicator} 主源(东财 {spec.em_func})取数失败，"
                    f"且该指标没有同口径备源: {reason}") from exc
            logger.warning(
                "%s 主源(东财 %s)失败：%s → 改走国家统计局备源 %s",
                spec.indicator, spec.em_func, reason, spec.nbs_path)
            rows = self._rows_from_nbs(spec, reason)
            origin = "nbs"
        else:
            reason = ""
            origin = "eastmoney"

        if not rows:
            # 兜底：正常路径不会到这里（`_rows_from_*` 空结果已抛错）
            raise DataFetchError(f"{spec.indicator} 未取到任何数据点（{origin}）")
        return rows, origin, reason

    def _rows_from_eastmoney(self, spec: _Spec) -> list[tuple[str, float]]:
        """东财口径（akshare `macro_china_pmi` / `macro_china_gdp`，长表）。"""
        ak = _import_akshare()
        frame = getattr(ak, spec.em_func)()
        if frame is None or len(frame) == 0:
            raise DataFetchError(f"{spec.em_func} 返回空帧")
        columns = [str(c) for c in frame.columns]
        value_col = _pick_column(columns, spec.em_columns, spec.em_func)
        if spec.em_date_col not in columns:
            raise DataFetchError(
                f"{spec.em_func} 返回结构异常：缺日期列 {spec.em_date_col!r}，"
                f"实际列={columns}")

        rows: list[tuple[str, float]] = []
        skipped = 0
        for _, row in frame.iterrows():
            period = spec.period_of(row[spec.em_date_col])
            value = _to_float(row[value_col])
            if period is None or value is None:
                skipped += 1
                continue
            if not spec.lo <= value <= spec.hi:
                # 越界说明列名漂移后抓错了列（如抓到"同比"列）—— 宁缺勿错
                logger.warning("%s(%s) 数值越界(%s)已弃用该行",
                               spec.indicator, spec.em_func, value)
                skipped += 1
                continue
            rows.append((period, value))
        if not rows:
            raise DataFetchError(
                f"{spec.em_func} 未产出可用行（列={columns}，跳过 {skipped} 行）")
        # ★ 必须升序：东财帧是**新→旧**（实测 `macro_china_pmi` 首=2026-08、
        #   尾=2008-01）。原样透出会得到一个"降序连接器" —— 走网络是降序、
        #   命中本地 DB 短路却是升序（`DataPointRepository.query_points`
        #   按 period_date 升序），任何依赖顺序的消费方都会时好时坏。
        #   同一坑 `series_to_points` 的 docstring 已记录过一次（2026-09-26）。
        rows.sort()
        logger.info("%s 由东财口径取到 %d 期（%s → %s）",
                    spec.indicator, len(rows), rows[0][0], rows[-1][0])
        return rows

    def _rows_from_nbs(self, spec: _Spec, primary_error: str) -> list[tuple[str, float]]:
        """NBS 备源（`macro_china_nbs_nation`，**宽表**：index=指标行名、columns=期别）。"""
        ak = _import_akshare()
        frame: Any = None
        last_exc: BaseException | None = None
        for attempt in range(_NBS_RETRIES):
            try:
                frame = ak.macro_china_nbs_nation(
                    kind=spec.nbs_kind, path=spec.nbs_path, period=_NBS_PERIOD)
                break
            except (AttributeError, TypeError) as exc:
                # **接口形状/调用错误**（akshare 改名、形参变了）：重试没有任何意义，
                # 只会白睡 2+4+6=12 秒再报同一个错 —— 取数路径上的每一秒都是用户的。
                raise DataFetchError(
                    f"{spec.indicator} 备源(NBS {spec.nbs_path})调用方式失效"
                    f"（akshare 接口形状变了？）：{brief(exc, BRIEF_TIGHT)}"
                    f"；主源失败原因：{primary_error}") from exc
            except Exception as exc:  # noqa: BLE001 NBS 新站偶发 WAF 挑战（返回 HTML）
                last_exc = exc
                logger.warning("NBS(%s) 第%d/%d次取数失败：%s",
                               spec.nbs_path, attempt + 1, _NBS_RETRIES,
                               brief(exc, BRIEF_TIGHT))
                time.sleep(_NBS_BACKOFF_SEC * (attempt + 1))
        if frame is None:
            raise DataFetchError(
                f"{spec.indicator} 备源(NBS {spec.nbs_path})连续 {_NBS_RETRIES} 次失败："
                f"{brief(last_exc, BRIEF_TIGHT)}；主源失败原因：{primary_error}")
        if len(frame) == 0:
            raise DataFetchError(
                f"{spec.indicator} 备源(NBS {spec.nbs_path})返回空帧"
                f"（窗口 {_NBS_PERIOD}）")

        row_name = _pick_column(
            [str(i) for i in frame.index], spec.nbs_rows, f"NBS:{spec.nbs_path}")
        rows: list[tuple[str, float]] = []
        for column in frame.columns:
            period = spec.period_of(column)
            value = _to_float(frame.loc[row_name, column])
            if period is None or value is None:
                continue
            if not spec.lo <= value <= spec.hi:
                logger.warning("%s(NBS %s) 数值越界(%s)已弃用该期",
                               spec.indicator, spec.nbs_path, value)
                continue
            rows.append((period, value))
        if not rows:
            raise DataFetchError(
                f"{spec.indicator} 备源(NBS {spec.nbs_path})未产出可用期别"
                f"（行={row_name!r}，列={[str(c) for c in frame.columns]}）")
        rows.sort()
        logger.warning("%s 由 NBS 备源取到 %d 期（%s → %s）；主源失败：%s",
                       spec.indicator, len(rows), rows[0][0], rows[-1][0],
                       primary_error)
        return rows
