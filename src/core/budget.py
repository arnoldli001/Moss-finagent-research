"""LLM 成本预算护栏（按日核算，跨进程内全局）。

## 为什么需要它（2026-09-25 实测）

审计日志显示真实成本分布极不均衡：

    mainline_relevance      484.11 元   <-- 主线相关性逐股打分
    mainline_member_pure    117.25 元   <-- 成分股提纯
    A17_recommend             7.28 元   <-- 投研决策层
    其余投研 Agent           < 0.5 元 each
    ------------------------------------
    合计                    617.53 元（其中 2026-09-20 单日 487.57 元）

而**投研分析单次成本中位仅 0.0527 元**（80 次分析共 8.62 元）。
也就是说：投研链路本身很便宜，真正烧钱的是主线模块的批量打分。

原有的护栏只有 `llm_token_budget_per_task`（**单任务** 20 万 token），
它防的是"单个任务失控"，**完全不防"很多任务累加"**。在日预算 20 元的
约束下，一次没有节制的批量打分就能把一整天的额度用光，之后的投研请求
会全部失败 —— 且失败原因是"余额不足"这种**看起来像配置错误**的形态。

## 设计

- **按日累计**（本地日期），跨所有调用方共享：投研请求、定时作业、
  批量脚本都计入同一个池子。
- **预扣 + 结算**：投研任务开始前按保守单位成本**预扣**一笔额度，
  结束后按实际消耗**结算**差额。这样并发突发不会因为"都还没记账"
  而集体超额（TOCTOU）。
- **只拦投研入口**：批量作业（mainline_relevance 等）不在本模块拦截，
  它们的用量只**记账**。理由：那些作业有自己的调度与重试语义，
  在这里硬拦会把"跑了一半的批处理"变成脏状态。它们该在自己的入口
  判断 `remaining()`。
- **高额脚本另外有硬上限**：上面那句"该在自己的入口判断 remaining()"原先
  只是一句**约定** —— 而约定没有生效：2026-09-20 `mainline_relevance` 一天
  花了 487.57 元（日预算 20 元的 24 倍）。现在由 `ScriptCostGuard` 把这个
  约定变成机制：入口按**预计花费**拒绝启动（> 1 元直接禁用），跑到一半
  再按**实际花费**主动中止。见下面那一节的说明。

## 成本口径

价格从 `configs/models.yaml` 的 `cost` 字段读取（元/百万 token），
不在代码里硬编码 —— 改价只改配置。本地 Ollama 记为 0。
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: 兜底单价（元/百万token）：(输入命中, 输入未命中, 输出)
#: 用于 models.yaml 读不到或模型未登记时；取 deepseek-flash 的价。
_FALLBACK_PRICE: tuple[float, float, float] = (0.02, 1.0, 4.0)

#: 投研任务的保守成本估算（元）：实测 p90 = 0.2237，向上取整到 0.25。
#: 预扣用 p90 而非中位 —— 用中位会让长尾任务穿透预算。
DEFAULT_TASK_RESERVE = 0.25


@dataclass
class _DayLedger:
    """单日账本。"""

    day: str
    #: 已确认消耗（元）
    spent: float = 0.0
    #: 已预扣未结算（元）
    reserved: float = 0.0
    #: 按来源分账（元），便于回答"钱花在哪了"
    by_source: dict[str, float] = field(default_factory=dict)
    calls: int = 0
    tokens: int = 0

    @property
    def used(self) -> float:
        return self.spent + self.reserved


def _load_prices(models_path: str) -> dict[str, tuple[float, float, float]]:
    """从 models.yaml 读每模型单价（元/百万token）。读不到返回空表。"""
    path = Path(models_path)
    if not path.is_file():
        return {}
    try:
        import yaml

        raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except Exception:  # noqa: BLE001 价格读不到不该拦住启动
        logger.debug("models.yaml 价格读取失败，用兜底价", exc_info=True)
        return {}
    out: dict[str, tuple[float, float, float]] = {}
    for name, spec in (raw.get("models") or {}).items():
        if not isinstance(spec, dict):
            continue
        cost = spec.get("cost")
        if not isinstance(cost, dict):
            continue
        try:
            out[str(name)] = (
                float(cost.get("input_cache_hit", 0.0)),
                float(cost.get("input_cache_miss", 0.0)),
                float(cost.get("output", 0.0)),
            )
        except (TypeError, ValueError):
            continue
    return out


#: 价格表缓存（按配置文件路径）。`models.yaml` 是**部署配置**、改动要走重启，
#: 所以进程内缓存一份即可；`get_budget()` 与运维页的费用统计共用它，
#: 避免"两个地方各读一次 yaml，改价后一半新一半旧"。
_PRICES_CACHE: dict[str, dict[str, tuple[float, float, float]]] = {}
_PRICES_LOCK = threading.Lock()


def model_prices(models_path: str = "configs/models.yaml",
                 ) -> dict[str, tuple[float, float, float]]:
    """价格表（进程内缓存）。键 = 模型名，值 = (命中价, 未命中价, 输出价)。"""
    with _PRICES_LOCK:
        cached = _PRICES_CACHE.get(models_path)
        if cached is None:
            cached = _load_prices(models_path)
            _PRICES_CACHE[models_path] = cached
        return cached


def reset_prices_cache() -> None:
    """清空价格表缓存（**仅测试用**）。"""
    with _PRICES_LOCK:
        _PRICES_CACHE.clear()


def model_is_priced(model: str,
                    prices: dict[str, tuple[float, float, float]] | None = None
                    ) -> bool:
    """该模型名在 `models.yaml` 里登记了价格没有。

    ⚠️ 未登记**不等于免费**：`call_cost_cny` 会用兜底价估算并把这种调用
    单独计数（运维页要能把它们列出来）—— 实测有 309 次调用用了
    `deepseek-v4-flash` 这个**已改名**的旧模型名，它不在配置里，
    费用此前被静默算成 0。
    """
    table = model_prices() if prices is None else prices
    return str(model) in table


def call_cost_cny(*, provider: str, model: str, tokens_in: int,
                  tokens_out: int, cache_hit: bool = False,
                  prices: dict[str, tuple[float, float, float]] | None = None
                  ) -> float:
    """一次调用的费用（元）。**唯一的计价实现**。

    - `provider != "deepseek"`（本地 Ollama 等）→ 0 元；
    - 未登记价格的模型 → 按 `_FALLBACK_PRICE` 估算（宁可高估，不可漏记）；
    - 计价口径：`tokens_in × 命中/未命中价 + tokens_out × 输出价`（元/百万 token）。

    `CostBudget.record`（在线账本）与运维页的费用统计都调它 ——
    两份计价实现迟早会分叉，而"账本说 3 元、监控页说 5 元"这种矛盾
    会让所有金额都失去可信度。
    """
    if str(provider) != "deepseek":
        return 0.0
    table = model_prices() if prices is None else prices
    hit_rate, miss_rate, out_rate = table.get(str(model), _FALLBACK_PRICE)
    in_rate = hit_rate if cache_hit else miss_rate
    return ((int(tokens_in or 0) * in_rate
             + int(tokens_out or 0) * out_rate) / 1_000_000.0)


class CostBudget:
    """按日 LLM 成本预算（进程内单例，线程安全）。"""

    def __init__(
        self,
        *,
        daily_budget: float,
        model_config_path: str = "configs/models.yaml",
        task_reserve: float = DEFAULT_TASK_RESERVE,
    ) -> None:
        self._daily = max(0.0, float(daily_budget))
        self._model_config_path = model_config_path
        self._prices = model_prices(model_config_path)
        self._fallback_used = False
        self._task_reserve = max(0.0, float(task_reserve))
        self._lock = threading.Lock()
        self._ledger = _DayLedger(day=self._today())

    # ---------- 内部 ----------

    @staticmethod
    def _today() -> str:
        return date.today().isoformat()

    def _rollover_locked(self) -> None:
        """跨日重置（调用方必须持锁）。"""
        today = self._today()
        if self._ledger.day != today:
            logger.info(
                "LLM 成本账本跨日重置：%s 花费 %.4f 元 / %d 次调用 → %s",
                self._ledger.day, self._ledger.spent, self._ledger.calls, today)
            self._ledger = _DayLedger(day=today)

    def _price(self, model: str) -> tuple[float, float, float]:
        """该模型单价；未登记时用兜底价（记账口径，**不**意味着免费）。"""
        return self._prices.get(str(model), _FALLBACK_PRICE)

    # ---------- 记账 ----------

    def record(
        self, *, provider: str, model: str, tokens_in: int, tokens_out: int,
        cache_hit: bool = False, source: str = "other",
    ) -> float:
        """记一次真实调用，返回到目前为止的当日累计消耗（元）。

        本地模型（ollama）不产生费用，直接返回当前累计。
        """
        if str(provider) != "deepseek":
            return self.used
        if not model_is_priced(model, self._prices) and not self._fallback_used:
            self._fallback_used = True
            logger.debug("模型 %s 未登记价格，使用兜底价", model)
        cost = call_cost_cny(provider=provider, model=model,
                             tokens_in=tokens_in, tokens_out=tokens_out,
                             cache_hit=cache_hit, prices=self._prices)
        with self._lock:
            self._rollover_locked()
            self._ledger.spent += cost
            self._ledger.by_source[source] = (
                self._ledger.by_source.get(source, 0.0) + cost)
            self._ledger.calls += 1
            self._ledger.tokens += int(tokens_in or 0) + int(tokens_out or 0)
            used = self._ledger.used
        if used >= self._daily > 0:
            logger.warning(
                "LLM 日预算已用尽：%.4f/%.2f 元（今日 %d 次调用）。"
                "后续付费调用将被准入层拒绝。分账：%s",
                used, self._daily, self._ledger.calls, self.snapshot()["by_source"])
        return used

    # ---------- 准入 ----------

    @property
    def budget(self) -> float:
        return self._daily

    @property
    def used(self) -> float:
        with self._lock:
            self._rollover_locked()
            return self._ledger.used

    @property
    def remaining(self) -> float:
        if self._daily <= 0:
            return float("inf")  # 0 = 不限预算
        return max(0.0, self._daily - self.used)

    def can_afford(self, amount: float) -> bool:
        """额度是否够（amount<=0 表示不限预算时恒真）。"""
        if self._daily <= 0:
            return True
        return self.remaining >= max(0.0, amount)

    def reserve_task(self, source: str = "research") -> bool:
        """为一次投研任务预扣额度。成功返回 True。

        预扣而不是"事后记账"：并发突发时所有任务都还没产生 token 消耗，
        只看 spent 会集体放行、集体超额（TOCTOU）。预扣把这个窗口关掉。
        """
        if self._daily <= 0:
            return True
        need = self._task_reserve
        with self._lock:
            self._rollover_locked()
            if self._ledger.used + need > self._daily:
                return False
            self._ledger.reserved += need
        return True

    def release_task(self, actual_cost: float = 0.0,
                     source: str = "research") -> None:
        """结算一次投研任务：退还预扣，并把实际消耗计入 spent。

        `actual_cost` 为 0（如任务失败未调用 LLM）时，等价于全额退还。
        """
        if self._daily <= 0:
            return
        need = self._task_reserve
        with self._lock:
            self._rollover_locked()
            self._ledger.reserved = max(0.0, self._ledger.reserved - need)
            if actual_cost > 0:
                self._ledger.spent += float(actual_cost)
                self._ledger.by_source[source] = (
                    self._ledger.by_source.get(source, 0.0) + float(actual_cost))

    # ---------- 观测 ----------

    def snapshot(self) -> dict[str, Any]:
        """当日预算快照（供 /health 与运维页）。"""
        with self._lock:
            self._rollover_locked()
            led = self._ledger
            by_source = {k: round(v, 4) for k, v in led.by_source.items()}
            return {
                "day": led.day,
                "budget_cny": self._daily,
                "spent_cny": round(led.spent, 4),
                "reserved_cny": round(led.reserved, 4),
                "remaining_cny": (
                    None if self._daily <= 0
                    else round(max(0.0, self._daily - led.used), 4)),
                "used_pct": (
                    None if self._daily <= 0
                    else round(min(100.0, led.used / self._daily * 100), 1)),
                "calls": led.calls,
                "tokens": led.tokens,
                "by_source": dict(sorted(by_source.items(),
                                        key=lambda kv: -kv[1])),
                "task_reserve_cny": self._task_reserve,
                "prices_loaded": sorted(self._prices.keys()),
            }


# ---------------------------------------------------------------- 进程单例

_budget: CostBudget | None = None
_budget_lock = threading.Lock()


def get_budget() -> CostBudget:
    """进程内单例（懒建，读环境变量与 models.yaml）。"""
    global _budget
    if _budget is None:
        with _budget_lock:
            if _budget is None:
                import os

                from src.core.config import get_settings

                settings = get_settings()
                daily = float(os.environ.get(
                    "MOSS_LLM_DAILY_BUDGET_CNY",
                    getattr(settings, "llm_daily_budget_cny", 20.0)))
                reserve = float(os.environ.get(
                    "MOSS_RESEARCH_TASK_RESERVE_CNY",
                    DEFAULT_TASK_RESERVE))
                _budget = CostBudget(
                    daily_budget=daily,
                    model_config_path=getattr(
                        settings, "model_config_path", "configs/models.yaml"),
                    task_reserve=reserve,
                )
                logger.info(
                    "LLM 日预算护栏已启用：%.2f 元/天，单任务预扣 %.2f 元",
                    daily, reserve)
    return _budget


def reset_budget_for_test(daily: float | None = None) -> CostBudget:
    """测试用：重建单例（可指定预算）。"""
    global _budget
    with _budget_lock:
        _budget = CostBudget(
            daily_budget=20.0 if daily is None else daily,
            model_config_path="configs/models.yaml",
        )
        return _budget


# ---------------------------------------------------------------- 高额脚本护栏

#: 高额一次性脚本的**单次**上限（元）。超过就拒绝启动 / 中途主动中止。
#:
#: 为什么是 1 元而不是"日预算的百分之几"：这两个脚本（`mainline_relevance`、
#: `purify_members`）是一次性的全量重算，**不是日常链路** —— 2026-09 的实测
#: 花费是 478 元与 117 元，而日常投研单次中位仅 0.05 元。把它们和日预算
#: 挂钩（例如"留 10 元给投研"）会让"偶尔重算一次"继续合法地吃掉一整天额度；
#: 用绝对值 1 元表达的是**这个动作本身就该被禁用**，要跑必须显式抬闸。
#: 可用 `MOSS_SCRIPT_COST_CAP_CNY` 覆盖（<=0 表示关掉护栏）。
SCRIPT_COST_CAP_CNY = 1.0

#: 单次 LLM 调用的**保守**成本估算（元），用于入口的"预计花费"。
#:
#: 实测口径（`data/audit/llm_audit.jsonl`，2026-09）：
#:     mainline_relevance    478.38 元 / 31770 次 = 0.0151 元/次
#:     mainline_member_pure  117.26 元 /  9582 次 = 0.0122 元/次
#: 两者都是"一次调用判一只股票"，取 0.02（最高值的 1.3 倍）作为估算：
#: 入口估算宁可偏大 —— 偏大只是"少跑一次试跑"，偏小会让护栏在跑到一半才响。
PER_CALL_ESTIMATE_CNY = 0.02


class ScriptCostError(RuntimeError):
    """脚本成本超限：入口拒绝启动，或跑到一半主动中止。"""


def script_cost_cap() -> float:
    """从环境变量读单次上限（元）；未设置或写坏时用 1.0。"""
    import os

    raw = os.environ.get("MOSS_SCRIPT_COST_CAP_CNY")
    if raw is None or not str(raw).strip():
        return SCRIPT_COST_CAP_CNY
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("MOSS_SCRIPT_COST_CAP_CNY=%r 不是数字，按 %.2f 元处理",
                       raw, SCRIPT_COST_CAP_CNY)
        return SCRIPT_COST_CAP_CNY


def estimate_script_cost(calls: int,
                         per_call: float = PER_CALL_ESTIMATE_CNY) -> float:
    """按调用次数估个花费（元）。用于入口判断，不做记账。"""
    return max(0, int(calls)) * float(per_call)


class ScriptCostGuard:
    """高额一次性脚本的成本护栏：**入口判 + 跑到一半判**。

    ## 两个口径各防什么

    - `check_entry(estimate)`：入口判。**预计花费超过上限就一次调用都不发**，
      直接拒绝启动（用户口径："花费超过 1 元就拦住，直接禁用"）。
      同时看日预算 `remaining()` —— 预计花费还没出门就超过今天剩的额度，
      同样拒绝（否则跑到一半被别处的准入拒绝，留下的是一份更难看的状态）。
    - `check_running()`：跑到一半判。以**本进程启动时的当日累计**为基线，
      追加上限；超过就抛 `ScriptCostError`，把已落库的部分保住、让人重跑
      （这些作业都是"签名一致就跳过"，重跑不重复计费）。

    ## 基线是"**本进程**的当日累计"，不是全机器

    账本本身是**进程内**的（`CostBudget.__init__` 直接开一个空账本，不落盘也
    不回读），所以一个刚启动的脚本进程看到的"当日累计"是 0、`remaining` 就是
    完整日预算。这意味着：

    - `check_entry` 里那条"日预算剩余不够"只在**同一进程内已经花过钱**时
      才起作用（例如脚本自己先跑了别的阶段）—— 别指望它去查 Web 进程的账；
    - 真正防"这一次跑飞"的是 `spent()`，它是**本进程的增量**，不依赖任何
      跨进程状态，也正好等于"本次运行花了多少"。这也是这个护栏该管的范围：
      上限是**单次运行**的，不是全机器的。

    用增量而不是绝对额还有个好处：跨日（账本 `_rollover_locked` 重置）
    不会出现负数或误判。

    ## 用法

        guard = ScriptCostGuard("mainline_relevance")
        guard.check_entry(estimate_script_cost(len(tasks)))   # 不通过就抛
        ...  score_stocks(..., cost_guard=guard)              # 中途由它兜住
    """

    def __init__(self, name: str, *, cap_cny: float | None = None,
                 budget: CostBudget | None = None) -> None:
        self.name = str(name)
        self.cap = (script_cost_cap() if cap_cny is None
                    else max(0.0, float(cap_cny)))
        self._budget = budget if budget is not None else get_budget()
        self._baseline = (float(self._budget.snapshot()["spent_cny"])
                          if self.enabled else 0.0)

    # ---------- 读 ----------

    @property
    def enabled(self) -> bool:
        """`cap <= 0` = 关掉护栏（`MOSS_SCRIPT_COST_CAP_CNY=0`）。"""
        return self.cap > 0

    def spent(self) -> float:
        """本次运行已花（元）= 当日累计 − 启动基线。"""
        if not self.enabled:
            return 0.0
        return max(0.0,
                   float(self._budget.snapshot()["spent_cny"]) - self._baseline)

    def over_cap(self) -> bool:
        return bool(self.enabled and self.spent() >= self.cap)

    def describe(self) -> str:
        """一行说明（脚本启动时打印，让"有没有护栏"是可见的）。

        `本进程已花` 这个措辞是刻意的：账本是进程内的，新起的脚本一定看到 0，
        写成"今日已花"会让人以为它查了全机器的账（见类 docstring）。
        """
        if not self.enabled:
            return (f"成本护栏：**已关闭**"
                    f"（MOSS_SCRIPT_COST_CAP_CNY={self.cap:g}）")
        return (f"成本护栏：单次上限 {self.cap:.2f} 元，"
                f"本进程已花 {self._budget.snapshot()['spent_cny']:.4f} 元 / "
                f"日预算 {self._budget.budget:.2f} 元")

    # ---------- 判 ----------

    def check_entry(self, estimate_cny: float) -> None:
        """入口检查；不通过抛 `ScriptCostError`（调用方打印后非零退出）。"""
        if not self.enabled:
            return
        estimate = max(0.0, float(estimate_cny))
        if estimate > self.cap:
            raise ScriptCostError(
                f"{self.name}：预计花费约 {estimate:.2f} 元，超过单次上限 "
                f"{self.cap:.2f} 元 —— 已拒绝启动（一次调用都不发）。\n"
                f"    · 想小批量试跑：用 --limit / --boards 把预计花费压到 "
                f"{self.cap:.2f} 元以内；\n"
                f"    · 确实要跑全量：显式设 "
                f"MOSS_SCRIPT_COST_CAP_CNY={estimate:.0f} 再执行"
                f"（这会把上限抬到 {estimate:.0f} 元，请确认这是你要的）。")
        remaining = float(self._budget.remaining)
        if remaining < estimate:
            raise ScriptCostError(
                f"{self.name}：本次预计 {estimate:.2f} 元 > 今日 LLM 预算剩余 "
                f"{remaining:.2f} 元 —— 已拒绝启动。\n"
                f"    · 等账本跨日重置（本地日期）后重跑；或调高 "
                f"MOSS_LLM_DAILY_BUDGET_CNY。")

    def check_running(self) -> None:
        """跑到一半的检查；超限抛 `ScriptCostError`。"""
        if not self.over_cap():
            return
        raise ScriptCostError(
            f"{self.name}：本次已花 {self.spent():.4f} 元，达到单次上限 "
            f"{self.cap:.2f} 元 —— 已主动中止（不是崩溃）。\n"
            f"    · 已落库的结果保留；重跑同一命令会命中缓存、不重复计费；\n"
            f"    · 要跑完整轮请显式设 MOSS_SCRIPT_COST_CAP_CNY=<更大的数>。")


__all__ = [
    "CostBudget",
    "DEFAULT_TASK_RESERVE",
    "PER_CALL_ESTIMATE_CNY",
    "SCRIPT_COST_CAP_CNY",
    "ScriptCostError",
    "ScriptCostGuard",
    "call_cost_cny",
    "estimate_script_cost",
    "get_budget",
    "model_is_priced",
    "model_prices",
    "reset_budget_for_test",
    "reset_prices_cache",
    "script_cost_cap",
]
