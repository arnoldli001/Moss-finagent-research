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

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import date, datetime
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
                  tokens_out: int, provider_cache_hit: bool = False,
                  prices: dict[str, tuple[float, float, float]] | None = None
                  ) -> float:
    """一次调用的费用（元）。**唯一的计价实现**。

    - `provider != "deepseek"`（本地 Ollama 等）→ 0 元；
    - 未登记价格的模型 → 按 `_FALLBACK_PRICE` 估算（宁可高估，不可漏记）；
    - 计价口径：`tokens_in × 命中/未命中价 + tokens_out × 输出价`（元/百万 token）。

    `CostBudget.record`（在线账本）与运维页的费用统计都调它 ——
    两份计价实现迟早会分叉，而"账本说 3 元、监控页说 5 元"这种矛盾
    会让所有金额都失去可信度。

    ## ★ 参数名为什么是 `provider_cache_hit` 而不是 `cache_hit`（2026-09-30）

    这个参数说的是「**提供商的上下文缓存**命中 ⇒ 输入 token 按更便宜的
    `input_cache_hit` 单价计」。

    而 LLM 审计（`src/infrastructure/llm/audit.py` @145）里那个**同名字段**
    `cache_hit` 说的是完全不同的一件事：「**本地响应缓存**命中，这次
    **根本没有调用提供商**」（`gateway.py` @463–469 命中即 return）。

    两个语义的**代价差一个数量级**：前者是"便宜一点"，后者是"一分钱没花"。
    原先两边都叫 `cache_hit`，于是运维页把审计字段直接喂进这个参数
    （`llm_cost.py` 旧版 @189），把**31,233 次没花钱的调用计成了 ¥408.75**，
    运维页总额因此虚高 65.5% —— 而**两边各自都是对的**，错在名字。

    所以这里**刻意改名**：名字不同，误传就变成 `TypeError`（调用点立刻炸），
    而不是一个静默偏高的账单。判据
    `tests/unit/test_llm_cost_accounting.py::test_pricing_cannot_be_fed_the_audit_field`
    钉住这一点。

    ⚠️ 生产上目前**没有任何调用点**传 `provider_cache_hit=True`：提供商侧
    缓存命中数还没有被采集。它保留为**扩展点**（DeepSeek 的用法统计里有
    `prompt_cache_hit_tokens`，接入后在此传入），而不是一个等着被误用的坑。
    """
    if str(provider) != "deepseek":
        return 0.0
    table = model_prices() if prices is None else prices
    hit_rate, miss_rate, out_rate = table.get(str(model), _FALLBACK_PRICE)
    in_rate = hit_rate if provider_cache_hit else miss_rate
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
        ledger_dir: str | Path | None = None,
        hard_cap_cny: float | None = None,
    ) -> None:
        self._daily = max(0.0, float(daily_budget))
        self._model_config_path = model_config_path
        self._prices = model_prices(model_config_path)
        self._fallback_used = False
        self._task_reserve = max(0.0, float(task_reserve))
        #: 跨进程当日花费账本。默认**关闭**（直接 `CostBudget(...)` 的形态保持
        #: 原样，测试与临时实例不受真实账本影响）；生产单例由 `get_budget()`
        #: 显式传入 `resolve_ledger_dir()` 打开。
        self._spend = DaySpendLedger(ledger_dir) if ledger_dir else None
        self._hard_cap = (resolve_hard_cap(self._daily)
                          if hard_cap_cny is None else max(0.0, float(hard_cap_cny)))
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
        provider_cache_hit: bool = False, source: str = "other",
    ) -> float:
        """记一次**真实调用**，返回到目前为止的当日累计消耗（元）。

        本地模型（ollama）不产生费用，直接返回当前累计。

        ⚠️ 这里**只应该在真的调用了提供商之后**调用 ——
        本地响应缓存命中（`gateway.py` @463–469 命中即 return）不该进账本，
        因为它没有产生任何费用。参数名与 `call_cost_cny` 一致地叫
        `provider_cache_hit`（见那边的说明：它与审计字段 `cache_hit`
        **不是**同一个东西）。
        """
        if str(provider) != "deepseek":
            return self.used
        if not model_is_priced(model, self._prices) and not self._fallback_used:
            self._fallback_used = True
            logger.debug("模型 %s 未登记价格，使用兜底价", model)
        cost = call_cost_cny(provider=provider, model=model,
                             tokens_in=tokens_in, tokens_out=tokens_out,
                             provider_cache_hit=provider_cache_hit,
                             prices=self._prices)
        with self._lock:
            self._rollover_locked()
            self._ledger.spent += cost
            self._ledger.by_source[source] = (
                self._ledger.by_source.get(source, 0.0) + cost)
            self._ledger.calls += 1
            self._ledger.tokens += int(tokens_in or 0) + int(tokens_out or 0)
            used = self._ledger.used
        # 跨进程账本：写失败不抛（护栏不是业务前置条件）
        if self._spend is not None and cost > 0:
            self._spend.add(cost, source)
        if used >= self._daily > 0:
            logger.warning(
                "LLM 日预算已用尽：%.4f/%.2f 元（今日 %d 次调用）。"
                "后续付费调用将被准入层拒绝。分账：%s",
                used, self._daily, self._ledger.calls, self.snapshot()["by_source"])
        return used

    # ---------- 跨进程硬闸 ----------

    @property
    def hard_cap(self) -> float:
        """全局日硬上限（元）；`<=0` = 不限。"""
        return self._hard_cap

    def day_total(self) -> float:
        """**全局**（所有进程 + 本进程）当日花费（元）。

        账本没开时退回本进程的 `spent` —— 这个退化必须能被看见，
        所以 `snapshot()` 里有 `ledger_enabled` 字段（避免把"本进程"读成"全局"）。
        """
        own = self.used
        if self._spend is None:
            return own
        return max(own, self._spend.total())

    def hard_cap_exceeded(self) -> bool:
        return bool(self._hard_cap > 0 and self.day_total() >= self._hard_cap)

    def check_hard_cap(self, source: str = "") -> None:
        """当日花费达硬上限就抛 `BudgetExhaustedError`。

        与 `reserve_task` 的分工：那个管**在线请求准入**（预扣，防突发）；
        这个管**批处理/脚本**（它们只记账不拦截，原设计就如此）。
        所以硬闸必须由批处理入口显式调用 —— `ScriptCostGuard.check_entry`
        已经接了，新的批处理入口也应照做。
        """
        if not self.hard_cap_exceeded():
            return
        raise BudgetExhaustedError(
            f"当日 LLM 花费已达硬上限：{self.day_total():.4f}/"
            f"{self._hard_cap:.2f} 元（全局，含其它进程）"
            + (f"，来源={source}" if source else "")
            + "。\n    · 等跨日自动归零；或调高 MOSS_LLM_DAILY_BUDGET_CNY / "
              "MOSS_LLM_HARD_CAP_CNY（请确认这是你要的）。")

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
        """还剩多少（元）。**以全局当日花费为准**（账本开着时跨进程）。

        为什么不是 `daily - self.used`：那样 api 进程花的钱 worker 看不到，
        于是"全局预算"在每个新进程里都变成"整份额度"（见 `DaySpendLedger`）。
        """
        if self._daily <= 0:
            return float("inf")  # 0 = 不限预算
        with self._lock:
            self._rollover_locked()
            reserved = self._ledger.reserved
        spent = self.day_total()
        return max(0.0, self._daily - spent - reserved)

    def can_afford(self, amount: float) -> bool:
        """额度是否够（amount<=0 表示不限预算时恒真）。"""
        if self._daily <= 0:
            return True
        return self.remaining >= max(0.0, amount)

    def reserve_task(self, source: str = "research") -> bool:
        """为一次投研任务预扣额度。成功返回 True。

        预扣而不是"事后记账"：并发突发时所有任务都还没产生 token 消耗，
        只看 spent 会集体放行、集体超额（TOCTOU）。预扣把这个窗口关掉。

        ★ 判据用的是**全局**当日花费（含其它进程）：否则 api 进程今天花掉的
        额度，worker 进程完全看不到（见 `DaySpendLedger`）。
        """
        if self._daily <= 0:
            return True
        need = self._task_reserve
        global_spent = self.day_total()      # 锁外取：day_total 自己要拿这把锁
        with self._lock:
            self._rollover_locked()
            if global_spent + self._ledger.reserved + need > self._daily:
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
        day_total = self.day_total()
        hard_cap_exceeded = self.hard_cap_exceeded()
        with self._lock:
            self._rollover_locked()
            led = self._ledger
            by_source = {k: round(v, 4) for k, v in led.by_source.items()}
            reserved = led.reserved
            return {
                "day": led.day,
                "budget_cny": self._daily,
                "spent_cny": round(led.spent, 4),
                "reserved_cny": round(led.reserved, 4),
                # ★ 新增：**全局**当日花费（所有进程）与硬闸状态。
                #   没有这两个字段，运维页会把"本进程花的"读成"今天花的"。
                "day_total_cny": round(day_total, 4),
                "hard_cap_cny": (None if self._hard_cap <= 0
                                 else round(self._hard_cap, 4)),
                "hard_cap_exceeded": hard_cap_exceeded,
                "ledger_enabled": self._spend is not None,
                "ledger_path": (str(self._spend.path) if self._spend else ""),
                "ledger_calls": (self._spend.calls() if self._spend else led.calls),
                "remaining_cny": (
                    None if self._daily <= 0
                    else round(max(0.0, self._daily - day_total - reserved), 4)),
                "used_pct": (
                    None if self._daily <= 0
                    else round(min(100.0, day_total / self._daily * 100), 1)),
                "calls": led.calls,
                "tokens": led.tokens,
                "by_source": dict(sorted(by_source.items(),
                                        key=lambda kv: -kv[1])),
                "task_reserve_cny": self._task_reserve,
                "prices_loaded": sorted(self._prices.keys()),
            }


# ---------------------------------------------------------------- 跨进程账本

#: 落盘账本目录（覆盖用）。**显式置空字符串 = 关闭落盘**（只有测试/极端隔离才这么用）。
LEDGER_DIR_ENV = "MOSS_LLM_LEDGER_DIR"

#: 全局日硬上限（元）。不设则等于日预算；`<=0` = 不限。
HARD_CAP_ENV = "MOSS_LLM_HARD_CAP_CNY"

#: 重新读盘的最小间隔（秒）。见 `DaySpendLedger` 的刷新策略。
LEDGER_REFRESH_SEC = 5.0


class BudgetExhaustedError(RuntimeError):
    """当日 LLM 花费已达**硬上限**：批处理/脚本必须停下（在线请求另有准入层）。"""


class DaySpendLedger:
    r"""跨进程的**当日** LLM 花费账本（append-only JSONL，一天一个文件）。

    ## 为什么需要它（本轮补的短板）

    `CostBudget` 的账本是**进程内**的（`__init__` 开一个空账本，不落盘也不回读）。
    后果不是"数字略不准"，而是**"全局日预算硬闸"在跨进程时根本不存在**：

      · api 进程今天已经花了 18/20 元；
      · worker 进程（跑 4 个重作业）与任何本地脚本**看到的都是 0** ⇒
        `remaining` = 完整的 20 元 ⇒ 该拦的一个都没拦；
      · `ScriptCostGuard.check_entry` 那条"日预算剩余不够就拒绝启动"
        只在**同一进程内先花过钱**时才起作用（原 docstring 就如实写了这点）。

    这一步把"当日花费"变成**一份所有进程共享的事实**。

    ## 形态与取舍

    * 一行一次**真实**调用：`{"ts","day","cny","source","pid"}`，追加写、从不改写；
    * 只读**当天**那一个文件 ⇒ 跨日自动归零，**不需要清理任务**（旧文件留着当历史）；
    * 刷新策略：首次读 + **文件大小变化且距上次刷新 ≥ `LEDGER_REFRESH_SEC`**
      ⇒ 既拿到跨进程可见性，又不会"每次调用都读一遍文件"
      （监控页是 10 秒刷新的，这里 5 秒足够；`record()` 自己写的那部分直接进内存，
      不依赖下一次读盘）；
    * **写失败绝不抛给调用方**：账本是观测与护栏，不是业务前置条件
      （与 `audit.record` 同一条纪律：钱已经花了，记不下来也不能让调用失败）。
    """

    def __init__(self, directory: str | Path, *, refresh_sec: float = LEDGER_REFRESH_SEC
                 ) -> None:
        self.dir = Path(directory)
        self.refresh_sec = float(refresh_sec)
        self._lock = threading.Lock()
        self._total = 0.0
        self._calls = 0
        self._day = ""
        self._size = -1
        self._read_at = 0.0

    # ---------- 路径 ----------

    def path_for(self, day: str) -> Path:
        return self.dir / f"llm_spend-{day}.jsonl"

    @property
    def path(self) -> Path:
        return self.path_for(date.today().isoformat())

    # ---------- 读 ----------

    def _read_locked(self, *, force: bool = False) -> None:
        import time

        day = date.today().isoformat()
        path = self.path_for(day)
        try:
            size = path.stat().st_size
        except OSError:
            size = -1
        fresh_enough = (time.time() - self._read_at) < self.refresh_sec
        if not force and day == self._day and size == self._size and fresh_enough:
            return
        total = 0.0
        calls = 0
        if size > 0:
            try:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    for line in fh:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue            # 单行损坏只跳过它（账本要能自愈）
                        if str(row.get("day") or day) != day:
                            continue            # 跨日残留行不算今天
                        total += float(row.get("cny") or 0.0)
                        calls += 1
            except OSError:
                logger.debug("LLM 花费落盘账本读取失败（按已缓存值继续）", exc_info=True)
        self._day = day
        self._size = size
        self._read_at = time.time()
        self._total = total
        self._calls = calls

    def total(self) -> float:
        """当日**全局**花费（元，所有进程累计）。"""
        with self._lock:
            self._read_locked()
            return self._total

    def calls(self) -> int:
        with self._lock:
            self._read_locked()
            return self._calls

    def add(self, cny: float, source: str = "other") -> float:
        """追加一条（返回追加后的当日全局累计）。**失败不抛。**"""
        amount = float(cny)
        day = date.today().isoformat()
        line = json.dumps({
            "ts": datetime.now().isoformat(timespec="seconds"),
            "day": day, "cny": round(amount, 6), "source": str(source),
            "pid": os.getpid(),
        }, ensure_ascii=False)
        with self._lock:
            try:
                self.dir.mkdir(parents=True, exist_ok=True)
                with self.path_for(day).open("a", encoding="utf-8") as fh:
                    fh.write(line + "\n")
            except OSError:
                logger.debug("LLM 花费落盘账本写入失败（本次只在内存计）", exc_info=True)
                return self._total
            # 自己写的部分直接进内存：不等下一次读盘
            if day != self._day:
                self._day = day
                self._total = 0.0
                self._calls = 0
            self._total += amount
            self._calls += 1
            try:
                self._size = self.path_for(day).stat().st_size
            except OSError:
                self._size = -1
            return self._total


def resolve_ledger_dir() -> Path | None:
    """落盘账本目录：环境变量优先，否则用数据登记表的 `run_dir`。

    显式设成空串 = **关闭**（返回 None）—— 只有在"必须与真实账本隔离"的
    测试里才这么做，正常路径不该关。
    """
    raw = os.environ.get(LEDGER_DIR_ENV)
    if raw is not None and not raw.strip():
        return None
    if raw:
        return Path(raw)
    try:
        from src.infrastructure.catalog.data_stores import store_rel

        return Path(store_rel("run_dir"))
    except Exception:  # noqa: BLE001 登记表读不到不该拦住预算护栏
        logger.debug("run_dir 解析失败，落盘账本关闭", exc_info=True)
        return None


def resolve_hard_cap(daily_budget: float) -> float:
    """全局日硬上限：环境变量优先，否则等于日预算；`<=0` = 不限。"""
    raw = os.environ.get(HARD_CAP_ENV)
    if raw is None or not str(raw).strip():
        return float(daily_budget)
    try:
        return float(raw)
    except (TypeError, ValueError):
        logger.warning("%s=%r 不是数字，按日预算处理", HARD_CAP_ENV, raw)
        return float(daily_budget)


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
                    # ★ 生产路径**打开**跨进程账本：这是"全局日预算"成立的前提。
                    #   解析不出来（登记表异常）时退回 None = 关闭，
                    #   而 `snapshot()["ledger_enabled"]` 会让这件事可见。
                    ledger_dir=resolve_ledger_dir(),
                )
                snap = _budget.snapshot()
                logger.info(
                    "LLM 日预算护栏已启用：%.2f 元/天，单任务预扣 %.2f 元；"
                    "全局硬上限 %.2f 元；跨进程账本=%s",
                    daily, reserve, _budget.hard_cap,
                    snap["ledger_path"] or "关闭")
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

    ## ★ 2026-10-01：跨进程那一半补上了（`DaySpendLedger`）

    上面那段"别指望它去查 Web 进程的账"**已经不再成立**：
    `CostBudget` 现在带一份落盘的**当日全局**花费账本（生产单例由
    `get_budget()` 打开），所以：

    - `check_entry` 的日预算那条看的是**全局**剩余（api / worker / 其它脚本
      一起算）；
    - 新增 `BudgetExhaustedError` 硬闸：全局花费达 `hard_cap` 时**入口直接拒绝**；
    - `spent()` 仍然是本进程增量（它回答的是"本次跑了多少"，语义不变）。

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
        """入口检查；不通过抛 `ScriptCostError`（调用方打印后非零退出）。

        ★ 2026-10-01：日预算那一条现在看的是**全局**当日花费（含 api/worker
        进程与其它脚本）—— 原先只看本进程，新起的脚本看到的是"整份额度"，
        于是"全局日预算"在脚本入口等于不存在（见 `DaySpendLedger`）。
        另有 `BudgetExhaustedError` 的硬闸：已达 `hard_cap` 直接拒绝。
        """
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
        self._budget.check_hard_cap(self.name)      # 全局硬闸（跨进程）
        remaining = float(self._budget.remaining)
        if remaining < estimate:
            total = self._budget.day_total()
            raise ScriptCostError(
                f"{self.name}：本次预计 {estimate:.2f} 元 > 今日 LLM 预算剩余 "
                f"{remaining:.2f} 元 —— 已拒绝启动。\n"
                f"    · 今日**全局**已花 {total:.4f} 元"
                f"（含 api / worker 进程与其它脚本；账本="
                f"{'开' if self._budget.snapshot()['ledger_enabled'] else '关'}）；\n"
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
    "HARD_CAP_ENV",
    "LEDGER_DIR_ENV",
    "BudgetExhaustedError",
    "CostBudget",
    "DEFAULT_TASK_RESERVE",
    "DaySpendLedger",
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
    "resolve_hard_cap",
    "resolve_ledger_dir",
    "script_cost_cap",
]
