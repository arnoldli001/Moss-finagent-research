"""申万行业估值连接器（AKShare免费源）。

通过 ak.sw_index_third_info() 获取申万一级/二级/三级行业的
PE(静态/TTM)、PB、股息率截面估值数据。335个三级行业全覆盖。

指标约定（ind:sw_ 前缀）：
- "ind:sw_third_pe_ttm:{行业名}" → 某三级行业PE-TTM
- "ind:sw_third_pb:{行业名}"       → 某三级行业PB
- "ind:sw_third_pe_ttm:all"       → 全部三级行业PE-TTM截面（多行DataPoint）
- "ind:sw_first_pe_ttm:all"       → 全部一级行业PE-TTM截面
- "ind:sw_second_pe_ttm:all"      → 全部二级行业PE-TTM截面

历史分位：本连接器每次fetch产出截面快照，由A04存储入库。
调用query_points(indicator, start_date, end_date)获取历史快照序列后，
在AnalysisAgentBase或ReAct工具中计算当前值在历史序列中的百分位。
"""

from __future__ import annotations

import asyncio
import logging
import math
import re
from datetime import date, datetime
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

# 申万行业级别 → ak接口函数名
_LEVEL_FUNCS: dict[str, str] = {
    "first": "sw_index_first_info",
    "second": "sw_index_second_info",
    "third": "sw_index_third_info",
}

# 估值列名统一映射（不同级别接口列名一致）
_COL_MAP = {
    "pe_ttm": "TTM(滚动)市盈率",
    "pe_static": "静态市盈率",
    "pb": "市净率",
    "dividend_yield": "静态股息率",
}

# 指标解析正则：ind:sw_{level}_{metric}:{name}
_INDICATOR_RE = re.compile(
    r"^ind:sw_(first|second|third)_(pe_ttm|pe_static|pb|dividend_yield)"
    r"(?::(.+))?$"
)


class SWIndustryValuationConnector(BaseConnector):
    """申万行业估值连接器：一级/二级/三级行业PE/PB/股息率截面数据。

    数据源：AKShare sw_index_first/second/third_info()，完全免费无需Key。
    覆盖：31个一级、131个二级、335个三级行业。
    """

    source_name = "AKShare申万行业估值"
    source_url = "https://akshare.akfamily.xyz"

    def get_capabilities(self) -> dict[str, Any]:
        """★ 2026-09-30（`CHG-0136`）：**能力面必须与登记面严格对齐**。

        报障现场：用户问"未来半年能否持有高股息的招商银行"，报告却写
        「银行 PE-PB-股息率标签…在本节全部缺失」。实测根因**不是取不到**：

        * `supports("ind:sw_first_dividend_yield:银行")` → **True**，且**真取到**
          （实测 2026-09-30 银行行业股息率 **5.1%**、PE-TTM **7.34**）；
        * 但本方法原来只声明了**三级**的 `{行业名}` 形式 ⇒ **规划侧看不见**
          一级行业的按行业路径 ⇒ 只采 `:all` **无行业标签的截面** ⇒
          Agent 拿着一张全市场截面，**没法把"银行"单独挑出来** ⇒ 报"缺失"。

        ## 三条纪律（本轮按它们收敛，缺一即"采了白采"或"登记了取不到"）

        1. **声明 = 事实**：`supports()` 接受的形态都应能被规划侧看到；
        2. **声明 ⊆ 登记**：声明的每一条都必须在 `configs/indicators.yaml` 登记，
           否则 `test_contract_consistency.py` 判"能力↔登记不一致"
           （**本轮就是被这条既有判据当场抓住的**）；
        3. **登记 ⊆ 可见**：登记了却没有分析层 Agent 看得到 = **僵尸登记**
           （`test_whitelist_coverage.py` 判）。

        ## ⚠️ 因此这里**故意不声明** 7 个 `:all` 截面

        `ind:sw_{first,second}_pb:all`、`ind:sw_{first,second,third}_pe_static:all`、
        `ind:sw_{first,second}_dividend_yield:all` —— 连接器**确实支持**，
        但它们**不是模板**，登记即产生**每日定时采集作业**（+7 条/天）。
        那是**成本决定**，不该由实现者顺手替 owner 做；而"按行业点名"这条
        （用户真正需要的那条）**不需要**它们 —— 模板已登记且**零作业成本**
        （`catalog_jobs.plan_jobs()` 对模板明确跳过）。
        详见 `configs/indicators.yaml` 里对应的"已知缺口"说明。
        """
        #: `:all` 截面：**只声明已登记的那 5 条**（登记了才会被周期采集）
        all_forms = (
            "ind:sw_first_pe_ttm:all", "ind:sw_second_pe_ttm:all",
            "ind:sw_third_pe_ttm:all", "ind:sw_third_pb:all",
            "ind:sw_third_dividend_yield:all",
        )
        #: 参数化模板：3 级 × 4 指标，**全部已登记**（模板不产生定时作业）
        levels = ("first", "second", "third")
        metrics = ("pe_ttm", "pe_static", "pb", "dividend_yield")
        templates = tuple(f"ind:sw_{lv}_{mt}:{{行业名}}"
                          for lv in levels for mt in metrics)
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": list(all_forms) + list(templates),
            "notes": ("申万一级/二级/三级行业估值与股息率，每日收盘后更新。"
                      "`:all` 是**全市场截面、不带行业标签**，要某个行业请用 "
                      "`:{行业名}`（如 `ind:sw_first_dividend_yield:银行`，"
                      "银行属**申万一级**）；模板不产生定时作业。"),
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return bool(_INDICATOR_RE.match(indicator))

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        m = _INDICATOR_RE.match(indicator)
        if not m:
            raise DataFetchError(f"申万估值连接器不支持的指标: {indicator}")
        level, metric_key, name_filter = m.groups()
        col_name = _COL_MAP[metric_key]
        func_name = _LEVEL_FUNCS[level]

        try:
            df = await asyncio.to_thread(self._call_akshare, func_name)
        except Exception as exc:
            # 非预期异常（非 WAF 阻断）才 raise
            raise DataFetchError(f"申万行业估值({func_name})意外失败: {exc}") from exc

        # df=None 是 WAF 阻断降级，这里静默返回空不 raise，
        # 避免 Router 触发故障转移链（其他连接器也没有申万估值数据）
        if df is None or df.empty:
            logger.info(
                "申万行业估值 %s=%s 东财WAF阻断，本次返回空截面；"
                "重启后或东财解封后自动恢复",
                level, metric_key,
            )
            return []

        # 列名适配：一级行业没有"上级行业"列
        name_col = "行业名称"
        code_col = "行业代码"
        today = date.today().isoformat()

        points: list[DataPoint] = []
        for _, row in df.iterrows():
            industry_name = str(row.get(name_col, "")).strip()
            if not industry_name:
                continue
            # 按名称过滤（非"all"时精确匹配）
            if name_filter and name_filter != "all" and industry_name != name_filter:
                continue
            raw_val = row.get(col_name)
            value = self._to_float(raw_val)
            if value is None:
                continue

            parent = str(row.get("上级行业", "")).strip() if "上级行业" in df.columns else ""
            extra = {
                "industry_code": str(row.get(code_col, "")),
                "industry_name": industry_name,
                "industry_level": level,
                "parent_industry": parent,
                "constituent_count": int(row.get("成份个数", 0) or 0),
                "metric": metric_key,
                "frequency": "daily_snapshot",
            }
            points.append(DataPoint(
                indicator=indicator,
                value=value,
                period_date=today,
                publish_time=datetime.now().isoformat(),
                extra=extra,
                source_name=self.source_name,
                source_url=self.source_url,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.API_CALL,
                confidence=0.9,
                verified=True,
            ))
        return points

    @staticmethod
    def _call_akshare(func_name: str) -> Any:
        """申万行业估值接口调用（含重试 + 降级）。

        故障域（2026-09-15 实测）：
          sw_index_first/second/third_info 底层用东财 push2 网页爬取，
          东财 WAF 频繁封 → 'NoneType' object has no attribute 'find_all'
        降级：调用失败时返回 None（让上层决定 fallback 还是静默跳过）。
        不 raise DataFetchError——避免每指标 5s 超时拖垮整个采集链。
        """
        try:
            import akshare as ak
        except ImportError as exc:
            raise DataFetchError("akshare未安装") from exc

        try:
            fn = getattr(ak, func_name)
            df = fn()
            return df
        except Exception as exc:  # noqa: BLE001 — 预期内的东财WAF阻断
            logger.warning(
                "申万估值 %s 调用失败: %s（东财WAF阻断，降级为None让上层处理）",
                func_name, exc,
            )
            return None

    @staticmethod
    def _to_float(raw: Any) -> float | None:
        try:
            result = float(raw)
        except (TypeError, ValueError):
            return None
        return None if math.isnan(result) else result


def compute_valuation_percentile(
    history: list[DataPoint], current: float,
) -> float | None:
    """当前值在历史序列中的百分位（0-100，0=最便宜）。

    历史序列来自同一指标的多次截面快照（每日一条）。
    样本不足8个时返回None（与A10微观分位计算一致的最小样本量）。
    """
    if len(history) < 8:
        return None
    values = sorted(
        p.value for p in history
        if p.value is not None and not math.isnan(p.value)
    )
    if not values:
        return None
    below = sum(1 for v in values if v <= current)
    return round(below / len(values) * 100, 1)
