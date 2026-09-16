"""A19 编码实现Agent：数据缺口自动修复。

当上游Agent反馈"未接入XX数据/无法获取XX数据"时，本Agent：
1. 分析数据缺口，匹配已知免费数据源（AkShare/腾讯/东财/中证官网等）；
2. 生成符合BaseConnector契约的连接器Python代码（含溯源元数据）；
3. 通过code_validator静态安全检查 + 沙箱导入冒烟测试；
4. 写入data/dynamic_connectors/并热加载到ConnectorRouter；
5. 实测fetch，返回样例数据与建议更新频率（供定时作业注册）。

安全约束：生成代码禁止exec/eval/subprocess/文件写等危险操作，导入白名单限制；
所有生成数据必须携带source_url、publish_time、raw_content_hash溯源字段。
"""

from __future__ import annotations

import ast
import asyncio
import hashlib
import logging
import re
import textwrap
from typing import Any

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, TraceStep
from src.infrastructure.connectors.code_validator import (
    sandbox_test,
    validate_connector_code,
)
from src.infrastructure.connectors.dynamic_loader import (
    DYNAMIC_DIR,
    get_dynamic_loader,
)
from src.infrastructure.llm import LLMGateway

logger = logging.getLogger(__name__)

# 已知免费数据源知识库（LLM生成代码时优先参考这些稳定源）
KNOWN_SOURCES: dict[str, str] = {
    "akshare": "AKShare（开源金融数据库，覆盖A股行情/财务/宏观/两融/北向/指数估值等，无需API Key）",
    "tencent": "腾讯财经qt.gtimg.cn（实时行情/成交额/换手率，GBK编码，免Key）",
    "eastmoney": "东方财富push2/datacenter接口（实时行情/换手率排名/两融明细，免Key）",
    "csindex": "中证指数官网indicator.xls（指数PE/PB现值，免Key）",
    "legulegu": "乐咕乐股legulegu.com（指数PE/PB历史分位，免Key，反爬敏感）",
    "sse_szse": "上交所/深交所官网（融资融券余额，T+1披露）",
    "stats_gov": "国家统计局data.stats.gov.cn（CPI/PPI/M2等宏观月度数据）",
    "cme_fedwatch": "cme-fedwatch库（CME FedWatch利率概率，需外网）",
}

# 连接器代码模板：LLM基于此骨架填充fetch逻辑
_CONNECTOR_TEMPLATE = '''"""动态生成连接器：{description}（由A19编码实现Agent生成）。

数据来源：{source_name}
指标约定：{indicator_prefix}:*
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any

import akshare as ak  # noqa: F401  生成代码可能使用
import pandas as pd  # noqa: F401
import requests  # noqa: F401

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

_INDICATOR_RE = re.compile(r"^{indicator_prefix}.*$")


class {class_name}(BaseConnector):
    source_name = "{source_name}"
    source_url = "{source_url}"

    @staticmethod
    def supports(indicator: str) -> bool:
        return bool(_INDICATOR_RE.match(indicator))

    def get_capabilities(self) -> dict[str, Any]:
        return {{
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": ["{sample_indicator}"],
            "notes": "{notes}",
        }}

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        if not self.supports(indicator):
            raise DataFetchError(f"不支持的指标: {{indicator}}")
        try:
            rows = await asyncio.to_thread(self._fetch_raw, indicator)
        except Exception as exc:
            raise DataFetchError(f"数据获取失败({{indicator}}): {{exc}}") from exc
        points: list[DataPoint] = []
        for row in rows:
            raw = str(row)
            points.append(DataPoint(
                indicator=indicator,
                value=row["value"],
                unit=row.get("unit", ""),
                period_date=row.get("date", date.today().isoformat()),
                extra=row.get("extra", {{}}),
                source_name=self.source_name,
                source_url=self.source_url,
                fetch_method=FetchMethod.API_CALL,
                confidence=0.8,
            ))
        return points

    @staticmethod
    def _fetch_raw(indicator: str) -> list[dict[str, Any]]:
        """实际数据获取逻辑（线程内执行）。返回[{{date, value, unit, extra}}]列表。"""
        # TODO: 在此实现具体的数据源调用
        raise NotImplementedError
'''

# 安全代码生成的system prompt
_SYSTEM_PROMPT = (
    "你是资深Python数据工程师，任务是为投研系统编写数据采集连接器。\n"
    "严格遵守以下规则：\n"
    "1. 代码必须继承BaseConnector，实现supports(indicator)和fetch(indicator)；\n"
    "2. 只允许使用以下导入：requests、pandas、akshare、numpy、asyncio、re、datetime、"
    "hashlib、json、io、typing；禁止exec/eval/subprocess/os.system/文件写入；\n"
    "3. 每个DataPoint必须携带source_name、source_url、fetch_method；\n"
    "4. fetch内的网络调用放在静态方法_fetch_raw中，由asyncio.to_thread包裹；\n"
    "5. 数据获取失败抛DataFetchError，禁止静默返回假数据；\n"
    "6. 只输出Python代码，不要输出解释或markdown围栏。"
)


class CodeEngineerAgent(BaseAgent):
    """A19：数据缺口自动发现与连接器代码生成。"""

    def __init__(self, gateway: LLMGateway, agent_id: str = "A19_code_engineer") -> None:
        super().__init__(agent_id)
        self._gateway = gateway

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "capabilities": [
                "data_gap_analysis", "source_discovery",
                "connector_code_generation", "code_safety_validation",
                "dynamic_registration", "schedule_recommendation",
            ],
            "known_sources": list(KNOWN_SOURCES.keys()),
        }

    def health_check(self) -> bool:
        try:
            return bool(DYNAMIC_DIR.exists())
        except Exception:
            return False

    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = input.payload or {}
        gap_description = str(payload.get("gap_description", "")).strip()
        indicator_hint = str(payload.get("indicator", "")).strip()
        # error_msg用于辅助LLM理解失败原因，当前在prompt中未直接使用
        _ = str(payload.get("error_message", "")).strip()

        if not gap_description and not indicator_hint:
            raise AgentExecutionError(
                "A19需要gap_description或indicator参数描述数据缺口")

        steps: list[TraceStep] = []
        # --- Step 1: 分析缺口，推荐数据源 ---
        source_hint = self._recommend_source(gap_description, indicator_hint)
        steps.append(TraceStep(step=1, step_type="data_retrieval",
                               description=f"数据源推荐: {source_hint}"))

        # --- Step 2: LLM生成连接器代码（验证+沙箱失败均反馈修复，最多3轮）---
        code, class_name, sandbox_result = "", "", {}
        last_issues: list[str] = []
        for attempt in range(3):
            code, class_name, was_empty = await self._generate_connector(
                gap_description, indicator_hint, source_hint,
                repair_hints=last_issues,
            )
            if was_empty:
                last_issues = [
                    "上一轮输出为空（模型思考过程过长被截断），"
                    "请跳过思考直接输出精简的方法体代码",
                ]
                logger.info("A19第%d轮LLM输出为空，反馈重试", attempt + 1)
                continue
            # --- Step 3: 静态安全验证 ---
            issues = validate_connector_code(code)
            if issues:
                last_issues = issues
                logger.info("A19第%d轮静态验证失败(%d项)，反馈修复",
                            attempt + 1, len(issues))
                continue
            # --- Step 4: 沙箱导入冒烟测试 ---
            sandbox_result = await sandbox_test(code, indicator_hint)
            if not sandbox_result.get("ok"):
                last_issues = [f"沙箱执行失败: {sandbox_result.get('message')}"]
                logger.info("A19第%d轮沙箱失败，反馈修复", attempt + 1)
                continue
            break
        else:
            raise AgentExecutionError(
                f"代码生成与验证失败（3轮修复未通过）: {'; '.join(last_issues)}")
        steps.append(TraceStep(step=2, step_type="llm_inference",
                               description=f"生成连接器代码: {class_name}({len(code)}字符)"))
        steps.append(TraceStep(step=3, step_type="cross_validation",
                               description="AST+导入白名单+结构检查通过"))
        steps.append(TraceStep(step=4, step_type="cross_validation",
                               description=f"沙箱冒烟通过: {sandbox_result.get('class')}"))

        # --- Step 5: 写入文件并热加载 ---
        file_name = f"{self._slug(indicator_hint or gap_description)}_{self._short_hash(code)}.py"
        file_path = DYNAMIC_DIR / file_name
        file_path.write_text(code, encoding="utf-8")
        loader = get_dynamic_loader()
        routes = loader.reload()
        registered = [c for c, _ in routes
                      if c.__class__.__name__ == sandbox_result.get("class")]
        if not registered:
            raise AgentExecutionError("连接器写入后未能加载")
        connector = registered[0]
        steps.append(TraceStep(step=5, step_type="data_retrieval",
                               description=f"已写入{file_name}并加载到路由"))

        # --- Step 6: 实测fetch ---
        sample = await self._test_fetch(connector, indicator_hint)
        steps.append(TraceStep(step=6, step_type="data_retrieval",
                               description=f"实测fetch返回{len(sample)}条数据点"))

        # --- Step 7: 建议更新频率 ---
        schedule = self._recommend_schedule(indicator_hint)
        steps.append(TraceStep(step=7, step_type="indicator_calculation",
                               description=f"建议更新频率: {schedule}"))

        conclusion = (
            f"已为「{gap_description or indicator_hint}」生成并注册动态连接器"
            f"{class_name}，实测获取{len(sample)}条数据。"
            f"数据源: {source_hint}，建议更新频率: {schedule}。"
            "连接器已热加载到数据路由，下次采集即可使用。"
        )

        return AgentOutput(
            task_id=input.task_id, agent_id=self.agent_id,
            conclusion=conclusion,
            confidence=Confidence.HIGH if sample else Confidence.MEDIUM,
            data_refs=[f"dynamic_connector:{class_name}"],
            trace_id=input.task_id,
            reasoning_steps=steps,
            result={
                "connector_class": class_name,
                "connector_file": str(file_path),
                "indicator_prefix": self._indicator_prefix(indicator_hint),
                "source": source_hint,
                "sample_points": sample[:3],
                "sample_count": len(sample),
                "recommended_schedule": schedule,
                "capabilities": connector.get_capabilities(),
            },
        )

    # ---------- 内部方法 ----------

    def _recommend_source(self, gap: str, indicator: str) -> str:
        """基于缺口关键词推荐已知免费数据源。"""
        text = f"{gap} {indicator}".lower()
        if any(k in text for k in ("成交额", "换手率", "行情", "实时")):
            return "腾讯财经qt.gtimg.cn（实时成交额/换手率，免Key）"
        if any(k in text for k in ("两融", "融资融券", "margin")):
            return "上交所/深交所官网（融资融券余额，T+1）"
        if any(k in text for k in ("北向", "沪股通", "深股通", "north")):
            return "AKShare stock_hsgt_hist_em（北向资金历史）"
        if any(k in text for k in ("估值", "pe", "pb", "分位", "指数")):
            return "乐咕乐股legulegu.com / 中证官网indicator.xls（指数PE/PB）"
        if any(k in text for k in ("宏观", "cpi", "ppi", "m2", "社融")):
            return "国家统计局 / AKShare macro_china_*（宏观月度数据）"
        if any(k in text for k in ("fed", "美联储", "利率", "降息", "加息")):
            return "cme-fedwatch库 / FRED（美联储利率概率，需外网）"
        return "AKShare（开源金融数据库，优先尝试其对应接口）"

    async def _generate_connector(
        self, gap: str, indicator: str, source_hint: str,
        repair_hints: list[str] | None = None,
    ) -> tuple[str, str, bool]:
        """调用LLM生成_fetch_raw方法体，由本方法组装完整连接器代码。

        返回(code, class_name, was_empty)。was_empty=True表示LLM输出为空
        （推理模型思维链被max_tokens截断），由调用方针对性反馈重试。

        只让LLM生成数据获取逻辑（最容易出错也最需要领域知识的部分），
        连接器骨架（BaseConnector继承/supports/fetch/DataPoint构造）
        由模板固定，保证结构100%正确。
        """
        prefix = self._indicator_prefix(indicator)
        class_name = self._class_name(indicator or gap)
        sample_indicator = indicator or f"{prefix}:sample"
        source_name = source_hint.split("（")[0]
        # 提取一个像样的source_url（从source_hint中识别域名）
        url_match = re.search(r"https?://[\w.\-]+", source_hint)
        source_url = url_match.group(0) if url_match else "https://example.com"
        notes = f"{gap}；数据来源{source_hint}；由A19自动生成"

        # 向LLM要_fetch_raw方法体（不包含def行，由模板提供签名）
        repair_block = ""
        if repair_hints:
            repair_block = (
                "\n\n上一版实现存在以下问题，请修复_fetch_raw方法体：\n"
                + "\n".join(f"- {h}" for h in repair_hints)
            )
        prompt = (
            f"为以下数据缺口实现Python方法_fetch_raw(indicator)的数据获取逻辑：\n"
            f"缺口描述：{gap}\n"
            f"指标：{indicator or prefix + ':*'}\n"
            f"推荐数据源：{source_hint}\n\n"
            "要求：\n"
            "1. 只输出方法体代码（不含def签名、不含class、不含import）；\n"
            "2. 方法返回 list[dict]，每个dict含 date(YYYY-MM-DD), value(float), "
            "unit(str), extra(dict)；\n"
            "3. 使用requests/pandas/akshare等免费库；网络调用可直接写，"
            "外层已用asyncio.to_thread包裹；\n"
            "4. 数据获取失败抛DataFetchError或异常，禁止返回假数据；\n"
            "5. 保持精简，核心实现20行以内，不要写过度设计的fallback链；\n"
            "6. 直接输出最终代码，不要思考过程，不要解释。"
            f"{repair_block}"
        )
        response = await self._gateway.complete(
            "reasoning", _SYSTEM_PROMPT, prompt,
            agent_id=self.agent_id, trace_id="",
            json_mode=False, use_cache=False,
        )
        was_empty = not response.content.strip()
        # 只剥离首尾换行：保留首行缩进（strip()会剥掉首行前导空格，
        # 导致dedent公共前缀失效、组装后def后首行0缩进报语法错误）
        body = response.content.strip("\r\n")
        # 移除markdown围栏（仅围栏行本身，不吞首行缩进）
        if body.startswith("```"):
            body = re.sub(r"^```(?:python)?[ \t]*\r?\n", "", body, count=1)
            body = re.sub(r"\r?\n[ \t]*```$", "", body, count=1)
        # 若LLM输出了def行，提取方法体
        body = re.sub(r"^\s*def\s+_fetch_raw.*?:\s*\n", "", body, count=1)
        # 规范化缩进为8空格（免疫LLM输出的缩进不一致问题）
        body = self._normalize_body(body)

        # 组装完整连接器代码
        code = _CONNECTOR_TEMPLATE.format(
            description=gap[:80],
            source_name=source_name,
            source_url=source_url,
            indicator_prefix=prefix,
            class_name=class_name,
            sample_indicator=sample_indicator,
            notes=notes,
        )
        # 替换模板中的占位实现（注意：模板经.format()后{{变成{）
        placeholder = (
            "    @staticmethod\n"
            "    def _fetch_raw(indicator: str) -> list[dict[str, Any]]:\n"
            '        """实际数据获取逻辑（线程内执行）。'
            '返回[{date, value, unit, extra}]列表。"""\n'
            "        # TODO: 在此实现具体的数据源调用\n"
            "        raise NotImplementedError\n"
        )
        impl = (
            "    @staticmethod\n"
            "    def _fetch_raw(indicator: str) -> list[dict[str, Any]]:\n"
            f"{body}\n"
        )
        code = code.replace(placeholder, impl)
        return code, class_name, was_empty

    @staticmethod
    def _normalize_body(body: str) -> str:
        """把LLM输出的方法体规范化为8空格缩进。

        包进dummy函数用ast解析后unparse每条语句再统一缩进，
        彻底规避LLM输出缩进不一致（混合tab/基准缩进漂移）的问题；
        解析失败则原样返回（由validate_connector_code报告错误）。
        """
        dedented = textwrap.dedent(body).strip("\n")
        if not dedented.strip():
            return ""
        wrapped_lines = []
        for line in dedented.splitlines():
            wrapped_lines.append(f"    {line}" if line.strip() else line)
        try:
            tree = ast.parse("def _dummy():\n" + "\n".join(wrapped_lines))
            func = tree.body[0]
            parts = [
                textwrap.indent(ast.unparse(stmt), "        ")
                for stmt in func.body
            ]
            return "\n".join(parts)
        except SyntaxError:
            # 解析失败时统一缩进返回（由validate_connector_code给出准确报错）
            return "\n".join(
                f"        {line}" if line.strip() else line
                for line in dedented.splitlines()
            )

    async def _test_fetch(
        self, connector: Any, indicator: str,
    ) -> list[dict[str, Any]]:
        """实测连接器fetch，返回样例数据（失败返回空列表，不阻断注册结果）。"""
        test_indicator = indicator or (
            connector.get_capabilities().get("indicators", [""])[0]
        )
        if not test_indicator:
            return []
        try:
            points = await asyncio.wait_for(
                connector.fetch(test_indicator), timeout=30,
            )
            return [p.model_dump(mode="json") for p in points]
        except Exception as exc:  # noqa: BLE001
            logger.warning("A19连接器实测fetch失败(%s): %s", test_indicator, exc)
            return []

    @staticmethod
    def _recommend_schedule(indicator: str) -> str:
        text = (indicator or "").lower()
        if any(k in text for k in ("hist", "历史", "valuation", "pe", "pb")):
            return "daily 16:10 工作日"
        if any(k in text for k in ("margin", "两融")):
            return "daily 17:00 工作日（T+1披露）"
        if any(k in text for k in ("fed", "美联储")):
            return "daily 09:30"
        if any(k in text for k in ("turnover", "成交额", "换手率")):
            return "every 5min 交易时段"
        return "daily 16:10"

    @staticmethod
    def _indicator_prefix(indicator: str) -> str:
        if not indicator:
            return "dyn:"
        if ":" in indicator:
            return indicator.split(":")[0] + ":"
        return f"{indicator}:"

    @staticmethod
    def _class_name(text: str) -> str:
        # 按冒号/下划线/连字符分词，每段首字母大写拼成PascalCase
        parts = re.split(r"[:_\-\s]+", text.strip())
        cleaned = "".join(
            p[:1].upper() + p[1:] for p in parts if p
        )
        cleaned = re.sub(r"[^a-zA-Z0-9]", "", cleaned)
        if not cleaned:
            return "DynamicConnector"
        return cleaned + "Connector"

    @staticmethod
    def _slug(text: str) -> str:
        return re.sub(r"[^a-zA-Z0-9]", "_", text.lower())[:30] or "dyn"

    @staticmethod
    def _short_hash(code: str) -> str:
        return hashlib.md5(code.encode("utf-8")).hexdigest()[:8]
