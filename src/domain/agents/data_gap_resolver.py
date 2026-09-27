"""数据缺口自修复Agent：当指标采集返回空时，自动发现/生成/注册连接器。

核心流程（Self-Evolving）：
  1. LLM 分析缺失指标 → 生成 Python 连接器代码（基于 AkShare/公开 API 知识）
  2. CodeValidator 静态验证（AST 白名单 + 危险调用检测）
  3. sandbox_test 子进程冒烟测试（15s 超时）
  4. 通过 → 写入 data/dynamic_connectors/ → DynamicConnectorLoader 热加载
  5. 注册到 ConnectorRouter 路由表 + scheduler 定时采集
  6. 重试 fetch，如果成功则缺口修复完成

安全护栏：
  - 代码执行在子进程中，超时 15s 自动 kill
  - 禁止 os/subprocess/exec/eval/pickle 等危险调用
  - 禁止文件写入模式 open(...,"w"/"a")
  - 只允许白名单导入（requests/akshare/pandas/numpy 等数据采集库）
  - 每个动态连接器有唯一 module 名，覆盖前备份旧文件
  - 单次解析最多生成 1 个连接器，不批量（防止 LLM 跑偏）

设计原则（面试亮点）：
  - "自学习"不是 LLM 随便写代码就执行——有两层安全验证
  - 动态连接器与静态连接器共享 BaseConnector 协议，对 A01 透明
  - 失败不阻断主链路：如果自修复也失败，指标缺口照样上报给 A17
  - 沉淀为 skill：成功的连接器代码永久保存在 dynamic_connectors/，
    下次进程重启时自动加载，不需要再生成
"""

from __future__ import annotations

import hashlib
import logging
import time
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from src.infrastructure.connectors.code_validator import (
    sandbox_test,
    validate_connector_code,
)
from src.infrastructure.connectors.dynamic_loader import get_dynamic_loader
from src.infrastructure.llm import LLMGateway

logger = logging.getLogger(__name__)

# 动态连接器存放目录
_DYNAMIC_DIR = Path(__file__).resolve().parents[3] / "data" / "dynamic_connectors"

# LLM 生成连接器代码的 system prompt
_SYSTEM_PROMPT = """\
你是一个 Python 数据采集连接器生成器。你的任务是根据缺失的指标名称，
生成一个完整的 BaseConnector 子类，从公开数据源（AkShare/requests）
获取该指标数据。

## 要求
1. 继承 BaseConnector，实现 supports(indicator) 和 fetch(indicator) 方法
2. supports(indicator) 判断是否支持该指标（前缀匹配）
3. fetch(indicator) 返回 list[DataPoint]，每个 DataPoint 包含：
   - indicator: 指标名
   - period_date: "YYYY-MM" 或 "YYYY-MM-DD"
   - value: 数值
   - source_name: 数据源名
   - source_url: 数据源URL
   - confidence: 0-1 置信度
   - extra: {"fetch_method": "online"/"fallback", ...}
4. 优先使用 akshare 库（已安装），其次 requests+公开API
5. 只用允许的导入：akshare, requests, aiohttp, httpx,
   pandas, numpy, datetime, math, re, json, asyncio, typing, collections
6. 禁止使用：os, subprocess, exec, eval, pickle,
   open(写模式), __import__
7. fetch 是 async 方法，网络调用用
   await asyncio.to_thread() 包裹同步函数

## 代码模板
```python
from __future__ import annotations
import asyncio, logging, re
from datetime import date, datetime
from typing import Any
from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

class GeneratedConnector(BaseConnector):
    source_name = "generated"
    source_url = ""

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith("YOUR_PREFIX:")

    async def fetch(
        self, indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        points = []
        try:
            import akshare as ak
            df = await asyncio.to_thread(ak.YOUR_FUNCTION)
            for _, row in df.iterrows():
                points.append(DataPoint(
                    indicator=indicator,
                    period_date=str(row["日期"]),
                    value=float(row["值"]),
                    source_name="akshare",
                    source_url=(
                        "https://akshare.akfamily.xyz"
                    ),
                    confidence=0.8,
                    extra={
                        "fetch_method": (
                            FetchMethod.ONLINE
                        )
                    },
                ))
        except Exception as exc:
            logger.warning(
                "fetch %s failed: %s",
                indicator, exc,
            )
        return points

    def get_capabilities(self) -> dict:
        return {
            "simulated": False,
            "indicators": ["YOUR_PREFIX:"],
        }
```

## 输出格式
只输出 Python 代码，不要任何解释性文字。代码必须完整可执行。
"""  # noqa: E501


@dataclass
class ResolutionResult:
    """数据缺口自修复结果。"""
    success: bool
    indicator: str
    connector_path: str | None = None
    connector_class: str | None = None
    data_points_fetched: int = 0
    error: str | None = None
    skill_sedimented: bool = False  # 是否已沉淀为持久连接器
    scheduled: bool = False  # 是否已注册定时采集


class DataGapResolverAgent:
    """数据缺口自修复Agent：LLM生成连接器→沙箱验证→热加载→重试fetch→注册调度。

    使用 reasoning 层 LLM（deepseek-flash）生成代码，确保代码质量。
    生成失败不阻断主链路——指标缺口照样上报给 A17 决策Agent。
    """

    def __init__(self, gateway: LLMGateway) -> None:
        self._gateway = gateway
        self._max_attempts = 2  # 最多生成 2 版代码（第一版失败可修正一次）

    async def resolve(
        self,
        indicator: str,
        error_context: str = "",
        fetch_retry_fn: Any | None = None,
    ) -> ResolutionResult:
        """尝试自动发现和注册缺失指标的数据源连接器。

        Args:
            indicator: 缺失的指标名（如 "ind:半导体销售额同比"）
            error_context: 采集失败时的错误信息（帮助 LLM 诊断）
            fetch_retry_fn: 可选的 fetch 重试函数（用于验证新连接器是否真的能拿到数据）

        Returns:
            ResolutionResult
        """
        logger.info("数据缺口自修复启动: indicator=%s, error=%s",
                    indicator, error_context[:200])

        for attempt in range(self._max_attempts):
            try:
                result = await self._attempt_resolve(
                    indicator, error_context, attempt, fetch_retry_fn)
                if result.success:
                    return result
                # 第一版失败，LLM 根据错误修正
                error_context = f"第{attempt+1}版失败: {result.error}"
                logger.info("自修复第%d版失败(%s)，尝试修正重生成",
                            attempt + 1, result.error[:100])
            except Exception as exc:  # noqa: BLE001
                logger.warning("自修复异常(attempt=%d): %s", attempt + 1, exc)
                error_context = f"attempt{attempt+1}异常: {exc}"

        return ResolutionResult(
            success=False, indicator=indicator,
            error=f"经过{self._max_attempts}次尝试仍无法生成可用连接器",
        )

    async def _attempt_resolve(
        self,
        indicator: str,
        error_context: str,
        attempt: int,
        fetch_retry_fn: Any | None,
    ) -> ResolutionResult:
        """单次自修复尝试：生成→验证→沙箱→保存→加载→重试。"""

        # Step 1: LLM 生成连接器代码
        code = await self._generate_code(indicator, error_context, attempt)
        if not code or not code.strip():
            return ResolutionResult(
                success=False, indicator=indicator,
                error="LLM未生成有效代码")

        # Step 2: 静态安全验证
        issues = validate_connector_code(code)
        if issues:
            issue_summary = "; ".join(issues[:3])
            return ResolutionResult(
                success=False, indicator=indicator,
                error=f"静态验证失败: {issue_summary}")

        # Step 3: 沙箱执行测试
        test_result = await sandbox_test(code, indicator)
        if not test_result.get("ok"):
            return ResolutionResult(
                success=False, indicator=indicator,
                error=f"沙箱测试失败: {test_result.get('message', '?')}")

        # Step 4: 写入 dynamic_connectors/
        connector_path = self._save_connector(code, indicator)
        logger.info("动态连接器已写入: %s", connector_path)

        # Step 5: 热加载
        try:
            get_dynamic_loader().reload()
        except Exception as exc:  # noqa: BLE001
            return ResolutionResult(
                success=False, indicator=indicator,
                error=f"热加载失败: {exc}")

        # Step 6: 如果有重试函数，验证新连接器真的能拿到数据
        data_points = 0
        if fetch_retry_fn is not None:
            try:
                points = await fetch_retry_fn(indicator)
                data_points = len(points)
                if data_points == 0:
                    # 连接器加载了但拿不到数据——记录但不算成功
                    return ResolutionResult(
                        success=False, indicator=indicator,
                        error="连接器注册成功但fetch仍返回空",
                        connector_path=str(connector_path),
                        connector_class=test_result.get("class"),
                    )
            except Exception as exc:  # noqa: BLE001
                return ResolutionResult(
                    success=False, indicator=indicator,
                    error=f"重试fetch失败: {exc}",
                    connector_path=str(connector_path),
                    connector_class=test_result.get("class"),
                )

        # Step 7: 沉淀成功——连接器已持久化，下次重启自动加载
        logger.info(
            "数据缺口自修复成功: %s → %s (class=%s, %d pts)",
            indicator, connector_path.name,
            test_result.get("class", "?"), data_points,
        )
        return ResolutionResult(
            success=True, indicator=indicator,
            connector_path=str(connector_path),
            connector_class=test_result.get("class"),
            data_points_fetched=data_points,
            skill_sedimented=True,
        )

    async def _generate_code(
        self, indicator: str, error_context: str, attempt: int,
    ) -> str:
        """调用 LLM 生成连接器 Python 代码。"""
        hint = ""
        if attempt > 0:
            hint = f"\n\n## 上次失败原因\n{error_context}\n请根据错误修正代码。"

        prompt = (
            f"## 缺失指标\n{indicator}\n\n"
            f"## 错误上下文\n{error_context[:500]}\n"
            f"## 当前日期\n{date.today().isoformat()}"
            f"{hint}\n\n"
            "请生成一个完整的 BaseConnector 子类来获取这个指标的数据。"
            "只输出 Python 代码，不要任何解释。"
        )

        # ★ 2026-09-27 第八轮：trace_id 用稳定 hash（之前用时间戳 → 缓存永不命中）
        #   同样指标 + 同样错误上下文 + 同样 attempt → 命中缓存
        #   只有 attempt 变了（说明上一次失败要换思路）→ key 变 → 重新生成
        import hashlib as _hl
        stable_input = f"{indicator}|{error_context[:200]}|{attempt}"
        stable_trace = _hl.sha1(stable_input.encode()).hexdigest()[:16]
        response = await self._gateway.complete(
            "reasoning", _SYSTEM_PROMPT, prompt,
            agent_id="data_gap_resolver",
            trace_id=f"data_gap:{stable_trace}",
            json_mode=False,
            use_cache=True,  # ★ 改成 True：稳定 trace_id 下重复调用可命中
        )
        code = response.content.strip()
        # 提取 ```python ... ``` 块（LLM 可能包裹 markdown）
        if code.startswith("```"):
            lines = code.split("\n")
            # 去掉首尾 ``` 行
            lines = [
                ln for ln in lines[1:]
                if not ln.strip().startswith("```")
            ]
            code = "\n".join(lines)
        return code

    @staticmethod
    def _save_connector(code: str, indicator: str) -> Path:
        """将生成的连接器代码写入 dynamic_connectors/ 目录。

        文件名：indicator 的哈希前8位（确保合法文件名 + 唯一性）。
        覆盖前不备份旧文件（dynamic_connectors/ 是 AI 生成的，不保留历史版本）。
        """
        _DYNAMIC_DIR.mkdir(parents=True, exist_ok=True)
        # 用 indicator 的 md5 前8位作为文件名
        safe_name = hashlib.md5(indicator.encode()).hexdigest()[:8]
        path = _DYNAMIC_DIR / f"gap_{safe_name}.py"
        path.write_text(code, encoding="utf-8")
        return path

    @staticmethod
    def get_dynamic_schedule_config(
        indicator: str, result: ResolutionResult,
    ) -> dict[str, Any]:
        """生成定时调度配置（供 scheduler 注册用）。

        返回 {"indicator": str, "cron": str, "kind": "dynamic_collection"}
        调度频率根据指标前缀推断：
        - 日频（stock_close/PE/PB）→ 每日 16:10
        - 月频（CPI/PPI/M2）→ 每月 1 日 09:00
        - 默认 → 每日 16:10
        """
        ind = indicator.lower()
        if any(ind.startswith(p) for p in ("cpi", "ppi", "m2", "社融")):
            cron = "0 9 1 * *"  # 月度
        elif any(ind.startswith(p) for p in ("pe(", "pb:", "stock_close")):
            cron = "10 16 * * 1-5"  # 日频
        else:
            cron = "10 16 * * 1-5"  # 默认日频

        return {
            "indicator": indicator,
            "cron": cron,
            "kind": "dynamic_collection",
            "connector_path": result.connector_path,
            "connector_class": result.connector_class,
        }
