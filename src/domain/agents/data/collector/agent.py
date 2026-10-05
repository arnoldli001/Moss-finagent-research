"""A01 数据采集Agent：从数据源获取原始数据并携带溯源元数据。

无LLM（configs/agents.yaml model=none）；数据后端通过FetchBackend结构化
协议注入（infrastructure.connectors实现），domain不直接依赖infrastructure。

## 本 Agent 现在要回答的四件事（不再是"有没有取到"一个比特）

1. **为什么没有**：`gap_kind`（`logic.classify_gap_kind`）—— 语义不适用 /
   专题表未收录（两者都不是缺陷、都不该联网硬试）与真失败分开；
2. **查了几条路径、命中哪一条**：`path_stats`（`collector/path_stats.py`）；
3. **护栏有没有拦**：防撞钟是否作用于本次取数、有没有证据表明它拦下了；
4. **这一次由哪条近路服务**（2026-10-01 接上）：`ConnectorRouter` 自报的**子路径**
   （TTL 缓存 / 本地库 / 真联网 / 被冷却或预算拒绝）。它是「本地命中率 / 联网触发率」
   在**采集侧**唯一能落地的形态：必须知道"**这一次**走的是哪条"，而不是"最近一次"
   （并发下那是别人的结果，属"看起来完全正常的错数"）。

## 「逐次归属」是怎么接上的（三处，缺一不可）

* **后端**：`ConnectorRouter.fetch()` 支持一个可选出参 —— 调用方**自己新建**一个
  `Outcome` 传进来，router 把**本次**的子路径 mark 到**那个对象**上（链上各处只
  mark，计数仍在 `fetch()` 的 finally 里写**一次**；唯一事实源是
  `src/infrastructure/connectors/subpath_stats.py`）；
* **采集侧**（本文件）：每次取数建**一个** `Outcome`（局部、不上锁、不共享）、
  调用后读它，把结果交给 `path_stats.record(subpath=...)`；
* **审计/日志**：`subpath` 随 `record()` 的报告一起进产出、异常属性、台账与日志行
  （渲染归 `path_stats.format_tokens`）。

⚠️ 为什么不"取完数去读全局最近一次"：那是**别人的**结果（并发/交错下必然发生），
而它读起来完全正常 —— 本仓库把这类数当最贵的缺陷（`subpath_stats` 模块头）。
所以这一维**只信调用方自己持有的那个对象**；拿不到就记「未量到」，绝不编。

## 绝不新增加 I/O

路径计数全是进程内整数与一次内存匹配（`ConnectorRouter` 的匹配本身
"不试网络、不花时间、不产生冷却"，见其 `supports()` docstring）；
台账也是纯内存。子路径持有者只是一个 dataclass 实例（进程内分配、无锁、
零 I/O）—— 观测不许给采集加往返。

## `gap_kind` 走的**是哪一条载本**（这里必须说清楚，否则会被当成死字段）

豁免类缺口是**抛异常**传达的（`NoApplicableData`）—— A01 抛出后由 supervisor
分流成"非缺陷、不联网硬试"，**不产出 `AgentOutput`**。所以：

* 异常对象上挂**结构化属性**（`gap_kind` / `path_stats`，供上游按属性分流，
  不必再去猜消息文本）；
* 同时记进 `gap_ledger`（进程内、按 task 收敛），**审计（A18）今天就能读到**；
* 产出里只放 `path_stats`（它每次都真的有值）。**不**放一个永远为 `None`
  的 `gap_kind` —— 那种字段读起来像"已经接好了"，实际是假的。
"""

from __future__ import annotations

import logging
import time
from typing import Any, Protocol

from src.core.base_agent import BaseAgent
from src.core.exceptions import AgentExecutionError
from src.core.models import AgentInput, AgentOutput
from src.core.schemas import Confidence, DataPoint
from src.domain.agents.data.collector import gap_ledger, path_stats
from src.domain.agents.data.collector.logic import (
    build_collection_summary,
    build_reasoning_steps,
    classify_gap_kind,
    deadline_cut,
    format_gap_log,
    format_ok_log,
)
from src.domain.agents.data.collector.models import CollectorPayload

#: 子路径持有者：**唯一事实源**在 infrastructure（8 个互斥键 + 「还没定」哨兵都在那里）。
#:
#: 为什么 domain 直接 import 这一个类（而其余一切仍走 `FetchBackend` 协议注入）：
#: 持有者的初值是一个「还没定」哨兵（`UNRECORDED`），它**不是**任何一条子路径 ——
#: 在这边另写一个同形状的类就是第二份实现：哨兵写歪一个，router 在 finally 里
#: 就会把它记成"漏埋点"（`unexpected`），一次真取数会显示成埋点错误。
#: 方向上不新增依赖面：同包的 `path_stats.py` 早就为同一个理由 import 了
#: `infrastructure.catalog.network_fallback.UNMEASURED`（同一个常量，不另写字面量），
#: 而 `subpath_stats` 只依赖 stdlib 与那个模块（无环、无 I/O、无副作用）。
from src.infrastructure.connectors.subpath_stats import Outcome

logger = logging.getLogger(__name__)


class FetchBackend(Protocol):
    """采集后端结构化协议（duck typing，避免domain→infrastructure反向依赖）。"""

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
        *,
        deadline_sec: float | None = None,
        outcome: Outcome | None = None,
    ) -> list[DataPoint]: ...

    def get_capabilities(self) -> dict[str, Any]: ...


def _subpath_of(outcome: Outcome | None) -> str:
    """本次 fetch 走的是哪条近路（`UNMEASURED` = 还没量到）。

    两种"没量到"在这里**合成同一个值**，因为它们对采集侧的下一步动作是同一件事
    （这一维没有依据，别拿它算比率），但对**后端**仍是两件事、不许混淆：

    * `outcome is None` —— 后端不认这个出参（老实现/替身）：问不出来；
    * `outcome.resolved is False`（= `UNRECORDED`）—— 这次**没有定下**子路径
      （在决定之前就抛了 / 被取消）。

    ⚠️ 后者**不是** `miss`：`miss` 是"链上真的全失败"这个**结论**，只在链上全失败
    时出现。用 `miss` 冒充"取消/没定"会把缺口率算高，而它读起来完全正常。
    """
    if outcome is None or not outcome.resolved:
        return path_stats.UNMEASURED
    return outcome.subpath


class DataCollectorAgent(BaseAgent):
    """A01：按指标拉取原始数据，输出DataPoint列表（含溯源元数据）。"""

    def __init__(self, backend: FetchBackend, agent_id: str = "A01_data_collector") -> None:
        super().__init__(agent_id)
        self._backend = backend
        #: 后端认不认各**可选**参数（懒探测一次，按名字缓存；见 `_accepts_param`）
        self._param_support: dict[str, bool] = {}

    def _accepts_param(self, name: str) -> bool:
        """后端认不认某个可选参数（**按签名判**，不靠 try/except 探）。

        为什么不能"先带参调用、TypeError 再退回"：那会**把后端内部的
        TypeError 也当成"它不认这个参数"**，于是同一个请求被重发一次 ——
        既可能重复副作用，又会把真正的 bug 掩盖成"参数不兼容"。

        `deadline_sec` 与 `outcome` 共用这一份探测：两个参数都是"新加的、
        老后端可能不认的"，各写一份必然漂移（本项目实测过这种形状）。
        """
        cached = self._param_support.get(name)
        if cached is None:
            try:
                import inspect

                params = inspect.signature(self._backend.fetch).parameters
                cached = bool(
                    name in params
                    or any(p.kind is inspect.Parameter.VAR_KEYWORD
                           for p in params.values()))
            except (TypeError, ValueError):   # 取不到签名（C 扩展/替身）→ 保守当不支持
                cached = False
            self._param_support[name] = cached
        return bool(cached)

    def _new_subpath_outcome(self) -> Outcome | None:
        """本次取数**专属**的子路径持有者（后端不认这个出参时返回 `None`）。

        ★ 每次取数**新建一个**（局部、不上锁、不共享）—— 这是「这一次走的是哪条
        近路」能成立的全部原因：`fetch()` 只往**这个对象**上 mark 本次的子路径，
        所以并发/交错下也各拿各的。去读全局"最近一次"会把别人的结果算给自己，
        而那种错数**读起来完全正常**（`subpath_stats` 模块头讲的就是它）。

        后端不认 `outcome`（老实现/测试替身）⇒ 返回 `None` ⇒ 这一维记「未量到」
        （`UNMEASURED`），**不编**：拿不到归属时给任何一条近路都是假数据。
        """
        if not self._accepts_param("outcome"):
            return None
        try:
            return Outcome()
        except Exception as exc:  # noqa: BLE001 造不出来 ⇒ 这一维未量到，绝不影响取数
            logger.debug("子路径持有者构造失败（本次记未量到）: %s", exc)
            return None

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "capabilities": ["fetch_api", "web_crawl", "file_download"],
            "backend": self._backend.get_capabilities(),
        }

    def health_check(self) -> bool:
        try:
            return bool(self._backend.get_capabilities())
        except Exception:
            return False

    def _available_paths(self, indicator: str) -> int | str:
        """本次取数**有几条候选路径**（问后端自己的匹配实现，零 I/O）。

        * 后端有 `_matched()`（`ConnectorRouter` 有）⇒ 直接数它给出的条数 ——
          **调它的实现，不在这里重写一份前缀匹配**：本项目实测过
          "同一个判断两份实现，改一处漏一处"的后果；
        * 只问得出"认不认"（`supports()`）⇒ 明确不认 = 0 条，认了但数不出条数
          = **未量到**（不许写成 1 —— 那是把"≥1"当成"恰好 1"）；
        * 什么都问不出来（测试替身/未来后端）⇒ **未量到**，**不是 0**：
          0 读起来是"这个指标一条路都没有"，那是另一个结论（契约/登记缺失）。
        """
        try:
            matched = getattr(self._backend, "_matched", None)
            if callable(matched):
                return len(matched(indicator))
            supports = getattr(self._backend, "supports", None)
            if callable(supports):
                return 0 if not supports(indicator) else path_stats.UNMEASURED
        except Exception as exc:  # noqa: BLE001 问不到就说"未量到"，绝不影响取数
            logger.debug("候选路径数探测失败(%s): %s", indicator, exc)
        return path_stats.UNMEASURED

    async def execute(self, input: AgentInput) -> AgentOutput:
        payload = CollectorPayload.model_validate(input.payload)
        started = time.perf_counter()
        points: list[DataPoint] = []
        failure: BaseException | None = None
        gap_kind: str | None = None
        # ★ 本次专属的子路径持有者（局部、不上锁、不共享）：`fetch()` 只往**它**
        #   上面 mark 本次走的近路 ⇒ 并发/交错下各拿各的，不靠"最近一次"。
        subpath_outcome = self._new_subpath_outcome()
        # ★ 可选参数按"后端认不认"逐个装：不认的**一个都不传**（老后端/替身
        #   的行为因此与加这些参数之前逐字一致）。
        extra: dict[str, Any] = {}
        if payload.deadline_sec is not None and self._accepts_param("deadline_sec"):
            # 防撞钟（`deadline_sec`）：**只有交互路径会传**（
            # `supervisor._live_fetch_one` / A17 的 `query_data`）。
            # 定时作业/预热路径不传 ⇒ 后端行为与原来逐字一致 ——
            # 重活正是要在那里做（用户口径：「10 秒找不到就自动终止，
            # 防止撞钟过度等待」，取值依据见
            # `src/core/intel_limits.py::QUERY_DEADLINE_SEC`）。
            extra["deadline_sec"] = payload.deadline_sec
        if subpath_outcome is not None:
            extra["outcome"] = subpath_outcome
        try:
            points = await self._backend.fetch(
                payload.indicator, payload.start_date, payload.end_date, **extra)
        except Exception as exc:
            # ★ 2026-09-29 用户要求：「数据采集 agent 要记录**任何未能获取到的**
            #   信息日志，展示在后端日志里，方便查看采集效果」。
            #   这里记 **ERROR**（不是 info）：取数抛异常是真故障，
            #   而全仓库没有 root handler ⇒ INFO 会被静默丢弃（只兜 WARNING+）。
            #
            # ★ 本轮新增：异常**先分类**再记 —— `gap_kind` 是"为什么没有"的
            #   机器可读结论；`path_stats` 是"查了几条路、命中哪一条、
            #   护栏拦没拦、这一次由哪条近路服务"。三者都进日志行尾（可 grep）
            #   与异常属性（给上游分流）。
            failure = exc
            gap_kind = classify_gap_kind(exc)

        # ★ 三个出口（成功/空/异常）**共用这一处**记账 —— 打点放在真正得出结论的
        #   地方；抽一个只有测试调的函数等于没接（本项目实测过这种形状）。
        guardrail_armed = (payload.deadline_sec is not None
                           and self._accepts_param("deadline_sec"))
        report = path_stats.record(
            path_stats.classify(got_points=bool(points), exc=failure,
                                gap_kind=gap_kind),
            paths_available=self._available_paths(payload.indicator),
            guardrail_armed=guardrail_armed,
            guardrail_stopped=bool(guardrail_armed and deadline_cut(
                payload.indicator, payload.deadline_sec, failure)),
            # ★ 正交的另一维（**不参与**上面的计数器）：这次由哪条近路服务。
            #   取值来自本次专属的持有者；拿不到 ⇒ 「未量到」，绝不编、绝不冒充。
            subpath=_subpath_of(subpath_outcome),
        )
        elapsed_s = time.perf_counter() - started

        if failure is not None:
            logger.error("%s", format_gap_log(
                payload.indicator, f"{type(failure).__name__}: {failure}",
                kind="error", seconds=elapsed_s,
                gap_kind=gap_kind, paths=report))
            # ★ 豁免留痕：审计（A18）读的是 `agent_outputs`，而豁免类缺口
            #   **不产出 AgentOutput** ⇒ 不进台账就等于"静默消失"
            #   （本项目纪律：不吓人 ≠ 不可见）。
            gap_ledger.record(
                task_id=input.task_id, indicator=payload.indicator,
                gap_kind=gap_kind, reason=f"{type(failure).__name__}: {failure}",
                path_stats=report, source=self.agent_id)
            err = AgentExecutionError(
                f"A01采集失败({payload.indicator}): {failure}")
            # ★ 结构化属性（**不是**新异常类型）：上游要按 gap_kind 分流时
            #   不必再猜消息文本；异常类型与文案**逐字未改**，
            #   既有契约（`errors` 面板的前缀、`AgentExecutionError` 断言）不受影响。
            err.gap_kind = gap_kind                       # type: ignore[attr-defined]
            err.path_stats = report                       # type: ignore[attr-defined]
            raise err from failure

        duration_ms = int(elapsed_s * 1000)
        if points:
            # ★ 成功行也带 `paths_*`（含 `subpath=`）：本地命中率/联网触发率的
            #   分子分母都落在采集日志里才算"算得出来"——只给缺口行带，
            #   而 TTL 命中这类**正常返回**的占比最大，比率就永远拼不齐。
            logger.info("%s", format_ok_log(
                payload.indicator, points, seconds=elapsed_s, paths=report))
        else:
            # 空结果 = "没量到"，**不是**"量到 0"（AGENTS.md 三种情形之一）。
            # ⚠️ 这里**没有** `gap_kind` 可传：豁免类是抛异常传达的
            # （走上面那条路），"后端返回空列表"本身**没有**豁免依据。
            logger.warning("%s", format_gap_log(
                payload.indicator,
                "所有数据源都没有返回数据点（没量到 ≠ 量到 0）",
                kind="empty", seconds=elapsed_s, paths=report))
            # 空结果同样入台账（`gap_kind=None` ⇒ 审计侧仍按缺陷处理）：
            # "为什么没取到"的计数与结论必须落到审计输入里，而不是只在日志里。
            gap_ledger.record(
                task_id=input.task_id, indicator=payload.indicator,
                gap_kind=None,
                reason="所有数据源都没有返回数据点（没量到 ≠ 量到 0）",
                path_stats=report, source=self.agent_id)

        data_refs = [p.data_id for p in points]
        has_publish_time = all(p.publish_time for p in points)
        confidence = (
            Confidence.HIGH
            if points and has_publish_time
            else (Confidence.MEDIUM if points else Confidence.LOW)
        )

        return AgentOutput(
            task_id=input.task_id,
            agent_id=self.agent_id,
            conclusion=build_collection_summary(payload.indicator, points),
            confidence=confidence,
            data_refs=data_refs,
            reasoning_steps=build_reasoning_steps(
                payload.indicator, len(points), data_refs, duration_ms
            ),
            result={
                "indicator": payload.indicator,
                "data_points": [p.model_dump(mode="json") for p in points],
                #: ★ 本次取数的路径结论（纯内存算出来的，含"命中哪一条/护栏/
                #: 这一次由哪条近路服务"）。审计侧据此回答"为什么慢/为什么没取到/
                #: 本地命中率是多少"，不用再靠猜，也不用去读全局"最近一次"。
                "path_stats": report,
            },
        )
