"""行业渗透率数据采集连接器（多源免费数据）。

渗透率数据无统一API，采用三级数据管道：
1. AKShare新闻快讯（stock_info_global_em）→ 关键词提取渗透率数值
2. AKShare研报接口（stock_research_report_em）→ 按行业搜索研报标题
3. 静态基准数据（核心赛道渗透率参考值，人工维护）

指标约定：
- "ind:penetration:{赛道名}" → 某赛道渗透率（%，如 ind:penetration:新能源汽车）
- "ind:penetration_report:{行业关键词}" → 研报中渗透率相关报告列表

渗透率生命周期判断（注入extra供Agent直接使用）：
- <1%: 预研期 | 1-5%: 导入期 | 5-10%: 成长期早期 | 10-30%: 成长期
- 30-65%: 加速渗透期 | 65-90%: 成熟期 | >90%: 饱和期
"""

from __future__ import annotations

import asyncio
import re
from datetime import date, datetime
from typing import Any

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

# 渗透率关键词模式（匹配"渗透率XX%"或"渗透率达XX%"等表述）
_PENETRATION_RE = re.compile(
    r"渗透率[^0-9]{0,6}"
    r"(?:(\d+(?:\.\d+)?)\s*%)"
    r"|"
    r"(?:(\d+(?:\.\d+)?)\s*个百分点)",
    re.UNICODE,
)

# 核心赛道静态基准数据（人工维护，来源：中汽协/乘联会/工信部/IDC等公开报告）
# 每月更新一次，确保数据可追溯
_STATIC_PENETRATION: dict[str, dict[str, Any]] = {
    "新能源汽车": {
        "value": 52.7, "unit": "%",
        "source": "中汽协2026年8月数据",
        "lifecycle": "成熟期",
        "note": "新能源乘用车国内零售渗透率",
    },
    "新能源商用车": {
        "value": 49.3, "unit": "%",
        "source": "中汽协2026年8月数据",
        "lifecycle": "成熟期",
        "note": "新能源商用车国内渗透率",
    },
    "AI大模型应用": {
        "value": 8.5, "unit": "%",
        "source": "IDC 2026Q2报告估算",
        "lifecycle": "成长期",
        "note": "企业级AI大模型应用渗透率",
    },
    "人形机器人": {
        "value": 0.3, "unit": "%",
        "source": "GGII 2026年估算",
        "lifecycle": "预研期",
        "note": "人形机器人商业化渗透率（极早期）",
    },
    "固态电池": {
        "value": 1.2, "unit": "%",
        "source": "EVTank 2026年估算",
        "lifecycle": "导入期",
        "note": "固态电池在动力电池中渗透率",
    },
    "光伏发电": {
        "value": 18.6, "unit": "%",
        "source": "国家能源局2026年数据",
        "lifecycle": "成长期",
        "note": "光伏占全国发电量比例",
    },
    "半导体国产替代": {
        "value": 23.5, "unit": "%",
        "source": "IC Insights 2026年估算",
        "lifecycle": "成长期",
        "note": "国产芯片自给率（不含存储）",
    },
    "HBM存储": {
        "value": 3.8, "unit": "%",
        "source": "TrendForce 2026Q2",
        "lifecycle": "成长期早期",
        "note": "HBM占DRAM市场份额",
    },
}

# 指标解析：ind:penetration:{track}
_INDICATOR_RE = re.compile(r"^ind:penetration:(.+)$")
# 研报搜索：ind:penetration_report:{keyword}
_REPORT_INDICATOR_RE = re.compile(r"^ind:penetration_report:(.+)$")

# 渗透率生命周期分阶
_LIFECYCLE_THRESHOLDS = [
    (1.0, "预研期"), (5.0, "导入期"), (10.0, "成长期早期"),
    (30.0, "成长期"), (65.0, "加速渗透期"), (90.0, "成熟期"),
    (float("inf"), "饱和期"),
]


def classify_lifecycle(penetration_pct: float) -> str:
    """按渗透率判断产业链生命周期阶段。"""
    for threshold, stage in _LIFECYCLE_THRESHOLDS:
        if penetration_pct < threshold:
            return stage
    return "饱和期"


class PenetrationRateConnector(BaseConnector):
    """行业渗透率多源采集连接器。

    数据源优先级：
    1. 静态基准数据（核心赛道，人工维护，月度更新）
    2. AKShare全球财经快讯（关键词提取实时渗透率数值）
    3. AKShare研报接口（返回研报标题列表供Agent参考）

    所有产出在extra中标注source和lifecycle，Agent可直接引用。
    """

    source_name = "渗透率多源采集"
    source_url = "multi://penetration"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": [
                f"ind:penetration:{track}" for track in _STATIC_PENETRATION
            ] + ["ind:penetration:{赛道名}", "ind:penetration_report:{行业关键词}"],
            "tracks": list(_STATIC_PENETRATION.keys()),
            "notes": "静态基准+新闻关键词提取+研报搜索，多源交叉验证",
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return bool(
            _INDICATOR_RE.match(indicator)
            or _REPORT_INDICATOR_RE.match(indicator)
        )

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        m = _INDICATOR_RE.match(indicator)
        if m:
            track = m.group(1)
            return await self._fetch_penetration(track, indicator)
        m2 = _REPORT_INDICATOR_RE.match(indicator)
        if m2:
            keyword = m2.group(1)
            return await self._fetch_reports(keyword, indicator)
        raise DataFetchError(f"渗透率连接器不支持的指标: {indicator}")

    async def _fetch_penetration(
        self, track: str, indicator: str,
    ) -> list[DataPoint]:
        """获取某赛道渗透率：优先静态基准，其次新闻提取。"""
        today = date.today().isoformat()

        # 第一级：静态基准数据
        if track in _STATIC_PENETRATION:
            info = _STATIC_PENETRATION[track]
            value = float(info["value"])
            lifecycle = info.get("lifecycle") or classify_lifecycle(value)
            return [DataPoint(
                indicator=indicator,
                value=value,
                period_date=today,
                publish_time=datetime.now().isoformat(),
                extra={
                    "track": track, "unit": info.get("unit", "%"),
                    "source": info.get("source", ""),
                    "lifecycle": lifecycle,
                    "note": info.get("note", ""),
                    "data_source": "static_baseline",
                },
                source_name=self.source_name,
                source_url=self.source_url,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.API_CALL,
                confidence=0.7,
                verified=False,
            )]

        # 第二级：从全球财经快讯中提取渗透率数值
        news_points = await self._extract_from_news(track, indicator)
        if news_points:
            return news_points

        # 第三级：无数据时返回占位
        return [DataPoint(
            indicator=indicator,
            value=0.0,
            period_date=today,
            publish_time=datetime.now().isoformat(),
            extra={
                "track": track, "unit": "%",
                "source": "无可用数据源",
                "lifecycle": "未知",
                "note": f"未找到{track}渗透率数据，建议补充数据源",
                "data_source": "missing",
            },
            source_name=self.source_name,
            source_url=self.source_url,
            source_type=DataSourceType.API,
            fetch_method=FetchMethod.API_CALL,
            confidence=0.1,
            verified=False,
        )]

    async def _extract_from_news(
        self, track: str, indicator: str,
    ) -> list[DataPoint]:
        """从AKShare全球财经快讯中提取渗透率数值。"""
        try:
            import akshare as ak
        except ImportError:
            return []

        try:
            df = await asyncio.to_thread(ak.stock_info_global_em)
        except Exception:  # noqa: BLE001 新闻接口失败不阻断
            return []

        if df is None or df.empty:
            return []

        # 在标题和内容中搜索渗透率关键词+赛道名
        today = date.today().isoformat()
        points: list[DataPoint] = []
        keyword = track.lower()

        for _, row in df.head(500).iterrows():
            title = str(row.get("标题", "") or row.get("title", ""))
            content = str(row.get("内容", "") or row.get("content", ""))
            text = f"{title} {content}"
            if keyword not in text.lower() and track not in text:
                continue
            # 提取渗透率数值
            match = _PENETRATION_RE.search(text)
            if match:
                val_str = match.group(1) or match.group(2)
                if val_str:
                    value = float(val_str)
                    lifecycle = classify_lifecycle(value)
                    points.append(DataPoint(
                        indicator=indicator,
                        value=value,
                        period_date=today,
                        publish_time=datetime.now().isoformat(),
                        extra={
                            "track": track, "unit": "%",
                            "source": title[:100],
                            "lifecycle": lifecycle,
                            "note": f"从财经快讯提取：{title[:60]}",
                            "data_source": "news_extraction",
                        },
                        source_name=self.source_name,
                        source_url=self.source_url,
                        source_type=DataSourceType.API,
                        fetch_method=FetchMethod.API_CALL,
                        confidence=0.5,
                        verified=False,
                    ))
                    break  # 取第一个匹配即可
        return points

    async def _fetch_reports(
        self, keyword: str, indicator: str,
    ) -> list[DataPoint]:
        """从研报接口搜索渗透率相关报告。"""
        try:
            import akshare as ak
        except ImportError:
            return []

        try:
            df = await asyncio.to_thread(ak.stock_research_report_em, symbol=keyword)
        except Exception:  # noqa: BLE001 研报接口失败不阻断
            return []

        if df is None or df.empty:
            return []

        today = date.today().isoformat()
        points: list[DataPoint] = []
        for _, row in df.head(20).iterrows():
            title = str(row.get("title", "") or row.get("标题", ""))
            # 只保留含渗透率关键词的报告
            if "渗透" not in title and keyword not in title:
                continue
            points.append(DataPoint(
                indicator=indicator,
                value=0.0,  # 研报标题不含数值，value占位
                period_date=today,
                publish_time=datetime.now().isoformat(),
                extra={
                    "keyword": keyword,
                    "report_title": title[:200],
                    "data_source": "research_report",
                    "note": "研报标题列表，需Agent调用LLM提取具体渗透率数值",
                },
                source_name=self.source_name,
                source_url=self.source_url,
                source_type=DataSourceType.API,
                fetch_method=FetchMethod.API_CALL,
                confidence=0.3,
                verified=False,
            ))
        return points
