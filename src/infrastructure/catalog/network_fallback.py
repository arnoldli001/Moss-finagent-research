"""联网兜底：**最后一跳**（本地全给不出数据时才允许联网），三条护栏缺一不可。

## 为什么需要它（用户 2026-09-28 的要求）

> 「连接器找不到、指标登记里无 id、planner 目录里没有，都要**自动去联网获取数据**，
>   做最差的兜底，一定要找到数据。」

`catalog/local_data.py` 打通了"库里有、契约里没有"这一层之后，仍然剩下一类
**真缺口**：库里就是没有（`NO_DATA` / `NO_TABLE`）。而 `SmartFetcher` 那条链是
**契约驱动**的 —— 「指标登记里没有 id」的指标根本走不到它。于是用户看到的结论是
「缺数据，无法判定」，而数据源上其实有。

本模块补的就是这一层：**不在既有链路上再加一个源**，而是"当所有本地路径都给不出
数据时，才允许花一次联网调用"（触发判据见 `should_fallback()`）。

## 但这条必须配护栏，否则它就是"花着钱编数据"（AGENTS.md 硬约束）

三条护栏**缺一不可**，每一条都有**代码里的默认值**与**超限行为**：

| # | 护栏 | 默认值（代码常量） | 超限行为 |
|---|---|---|---|
| 1 | 合法源白名单 | `DEFAULT_ALLOWLIST = ()` | 空 = **不允许任何兜底**（fail-closed） |
| 2 | 每日成本上限 | `DEFAULT_DAILY_BUDGET_CNY = 2.0` 元/天 | 拒绝，理由含「已用尽 X/Y 元」 |
| 2 | 小时调用上限 | `DEFAULT_MAX_CALLS_PER_HOUR = 30` 次 | 拒绝，理由含「再过 N 分钟可以再试」 |
| 2 | 失败冷却 | `FAILURE_COOLDOWN_SECONDS = 600` 秒/指标 | 拒绝，理由含「冷却中，还剩 N 分钟」 |
| 3 | 失败可观测 | 落盘账本 + `snapshot()` | 「没量到」与「量到 0」分开（`counters=None`） |

三条纪律的落地方式：

- **默认值即护栏**：不传 `allowlist` 就是**不允许任何源**（安全的一侧是默认）。
  想开启必须显式传入 —— 与 `configs/models.yaml` 里给 `light`/`medium` 钉
  `local_only` 是同一个思路：**新调用方默认就是安全的**。

## ★ 怎么开启（2026-09-28 接线：`supervisor._query_data` 第四跳）

**默认是关的**：`DEFAULT_ALLOWLIST = ()` → `can_fallback()` 恒 `False` →
链路走到第四跳会**如实回一句拒绝理由**（"白名单为空 —— fail-closed"），
一次网络都不会发。这是刻意的：`AGENTS.md` 说"默认值即护栏，安全的一侧做成默认"，
而"一定会去联网"在没人看着的时候就是"一定会花钱"。

开启只有**两种**方式，都不需要改代码：

1. **环境变量**（推荐，一行）：`MOSS_NETWORK_FALLBACK_ALLOWLIST` = 逗号分隔的
   **连接器类名**（机器可读标识，见 `RouterSourceBridge.source_key()`）：

   ```bash
   # .env 或启动环境
   MOSS_NETWORK_FALLBACK_ALLOWLIST=AkshareConnector,TencentDailyConnector
   ```

   空/未设置 → 空白名单 → 全拒（**与 `DEFAULT_ALLOWLIST` 同一含义**）。
   建议值见 `SUGGESTED_ALLOWLIST`（每条都有理由见 `SUGGESTED_ALLOWLIST_REASONS`）；
   `NOT_SUGGESTED_SOURCES` 里的源写明"为什么不建议放进去"。
   白名单条目**不是** `source_name` 那种给人看的标签 —— 标签会被"优化文案"改掉，
   白名单跟着漂移就会变成"改了文案，兜底静默失效"。

2. **代码装配**：`set_network_fallback(NetworkFallback.from_connector_routes(
   routes, allowlist=(...)))` —— 显式装入的实例**永远优先**于环境变量。
   `/health` 与请求链路读的必须是**同一个**实例（否则会出现"界面显示没兜底、
   实际在兜底"这种两边都不报错的共存）。

阈值（日预算 / 小时上限 / 冷却）**刻意不做环境变量覆盖** —— 见
`NetworkFallback.__init__` 的说明（AGENTS.md《红灯纪律》）。
- **上限写进代码**：上面每个数字都是模块级常量，不是注释里的"一般不会超过"。
- **同一判断只允许一份实现**（复用既有机制，不自造一套）：
  · 熔断 → `llm/circuit_breaker.get_circuit_registry()`（按 `fallback:<源名>` 隔离）；
  · 限流判据 → `llm/rate_limit_guard.looks_rate_limited()`（只认 HTTP 状态码这类
    机器可读标识，不认文案）；
  · 成本口径 → `core/budget.PER_CALL_ESTIMATE_CNY`（保守单次估算，宁可高估）；
  · 落盘方式 → 学 `rate_limit_guard` 的"按实例隔离路径 + 原子替换"。

## 为什么账本不用 `core/budget.CostBudget`

三条硬约束它一条也不满足：① 它是**进程内**的（"不落盘也不回读"），重启即失效；
② 它只统计 `provider == "deepseek"` 的 LLM token，数据源调用记进去恒为 0；
③ 它的 `daily <= 0` 语义是"**不限**预算" —— 与本模块"0 元 = 一分钱都不许花"
恰好相反，混用必然读错方向。所以本模块只复用它的**成本口径常量**，账本自己做，
并且把口径（`cost_basis`）随 `snapshot()` 一起下发。

## 失败绝不抛到调用方

`fetch()` 在任何情况下都返回列表（取不到就是 `[]`），异常只记账。
**唯一透传的是取消信号** —— `asyncio.CancelledError` 继承 `BaseException`，
不在 `except Exception` 的范围内，这里刻意不吞它：吞掉取消会让上层永远取消不掉。

## 已知边界（显式登记，不假装通用）

- 账本是"读-改-写 + 原子替换"。单写者部署（本项目 SQLite 单写者架构）够用；
  **多副本/多进程并发写会互相覆盖** —— 与 `rate_limit_guard` 登记的是同一个边界。
- 成本是**估算**（`DEFAULT_CALL_COST_CNY` 元/次，含失败尝试），公开源实际不产生
  账单。这是刻意的"宁可高估"：账本要能拦住**调用次数**，只记真实账单就拦不住。
- ⚠️ 本模块在**顶层** import 了 `local_data.DiagCode` 的**字面值**副本（见
  `FALLBACK_TRIGGER_CODES`），并有测试断言它与 `DiagCode` 相等。将来在
  `local_data.py` 里接线时请在**函数内** import 本模块，避免模块级互相 import 成环。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Final

from src.core.budget import PER_CALL_ESTIMATE_CNY
from src.infrastructure.llm.circuit_breaker import get_circuit_registry
from src.infrastructure.llm.rate_limit_guard import looks_rate_limited

logger = logging.getLogger(__name__)

from src.infrastructure.catalog.data_stores import store_rel  # noqa: E402

#: 快照/日志里的模块标识（`/health` 里一眼知道这段是谁）。
MODULE: Final[str] = "network_fallback"

#: 「没量到」的**统一说法**（AGENTS.md：读不到数据时显示"未量到/无法统计"，
#: 绝不用 0 糊过去）。
#:
#: ⚠️ 做成常量而不是各处写字面量：链路结果、`/health`、拒绝理由、审计
#: 会各自写一遍，写歪一个（"无数据"/"为空"/"暂缺"）就会让下游的
#: `in` 判据**静默不命中** —— 而"没量到"这类判据一旦不命中，表现是
#: "看起来有数据"。本仓库实测过同类问题（文案一改，隐藏分支静默失效）。
UNMEASURED: Final[str] = "未量到"

#: 白名单的**环境变量**开关（唯一的环境变量入口）。
#:
#: 未设置/为空 → `DEFAULT_ALLOWLIST`（空 = 全拒，fail-closed）。
#: 值 = 逗号分隔的连接器类名，如
#: `AkshareConnector,TencentDailyConnector`（建议值见 `SUGGESTED_ALLOWLIST`）。
ALLOWLIST_ENV: Final[str] = "MOSS_NETWORK_FALLBACK_ALLOWLIST"

#: 熔断器名字前缀：`fallback:<源名>`。
#: ⚠️ **必须带前缀**：熔断注册表是**共享**的（LLM provider 也在里面），
#: 用源名裸名去 `get_or_create("akshare")` 会与将来的同名 provider 撞在一起。
_CIRCUIT_PREFIX: Final[str] = "fallback:"

# ============================================================
# 护栏 ①：合法源白名单（**默认空 = 不允许任何兜底**）
# ============================================================

#: 白名单默认值：**空**。空 = 不允许任何源被兜底调用（fail-closed）。
#:
#: 这是本模块最重要的一条默认值：安全的一侧是默认。
#: 靠"每个调用点记得传一个白名单"必然漏 —— 用户的要求是"一定要找到数据"，
#: 而"一定会去联网"在没人看着的时候就是"一定会花钱"，所以默认必须是"不许去"。
DEFAULT_ALLOWLIST: Final[tuple[str, ...]] = ()

#: **建议**白名单（供接入方 `allowlist=SUGGESTED_ALLOWLIST` 一键启用）。
#:
#: 它**不是**默认值 —— 默认值永远是 `DEFAULT_ALLOWLIST`（空）。
#: 条目是**机器可读标识**（连接器类名，见 `RouterSourceBridge.source_key()`），
#: 不是给人看的文案：文案会被本地化、会被"优化得更友好"，每一次都会让判据静默失效。
#: 每个条目的理由见 `SUGGESTED_ALLOWLIST_REASONS`（有测试断言"不许有无理由的条目"）。
SUGGESTED_ALLOWLIST: Final[tuple[str, ...]] = (
    "AkshareConnector",
    "TencentDailyConnector",
    "BaostockConnector",
    "SWIndustryValuationConnector",
    "IndexValuationConnector",
    "StarChinextConnector",
)

#: 建议白名单的逐条理由（**代码里**，不是注释里）。
SUGGESTED_ALLOWLIST_REASONS: Final[dict[str, str]] = {
    "AkshareConnector": (
        "覆盖面最广的免费公开源（宏观 CPI/PPI/M2/社融 + 行情 + 个股估值），"
        "且 `build_runtime` 把它排在链首 —— 缺口最可能在这里被补上"),
    "TencentDailyConnector": (
        "独立于东财/新浪的通道：实测 2026-09-17 东财被阻断 + 新浪断连时它仍可用，"
        "日线缺口的第一备源（qt.gtimg.cn）"),
    "BaostockConnector": (
        "免 token 的独立故障域（免费），日线/指数的第四备源 —— "
        "前三个源同时挂掉时它是唯一还能给出真数据的"),
    "SWIndustryValuationConnector": (
        "申万行业估值（一级/二级/三级 PE/PB/股息率截面），行业层缺口的专用源"),
    "IndexValuationConnector": (
        "宽基指数 PE/PB 历史分位（乐咕 legulegu.com，日频）—— "
        "估值分位只有这条链能给"),
    "StarChinextConnector": (
        "科创板/创业板成交额/估值分位/个股截面（腾讯+东财+乐咕多源冗余），"
        "双创板块缺口的专用源"),
}

#: **不建议**放进白名单的源 + 理由（写进代码，避免下一个人"顺手加上去"）。
NOT_SUGGESTED_SOURCES: Final[dict[str, str]] = {
    "TushareConnector": (
        "唯一的**有凭据/消耗积分**源（tushare.pro）：它产生真实成本，"
        "要放进白名单请显式加，并同步确认日预算"),
    "XtQuantConnector": (
        "本机 QMT 已失去行情权限且不在运行（127.0.0.1:58610 拒连），"
        "放进去只会贡献一次必然失败的连接等待（实测 4~5s）"),
    "LocalCsvConnector": (
        "本地 CSV（QMT 导出）：不是联网源（属本地链），且 2026-09-28 起 "
        "`LOCAL_QUOTE_DIR` 留空、这一跳已不在链上"),
    "FedWatchConnector": (
        "CME 外网实测不可达（要等满 23.4s 超时），兜底里放它只会拖慢整个请求"),
    "PenetrationRateConnector": (
        "`multi://penetration` 是「静态基准+新闻提取」的合成源，不是可复现的取数通道"),
}

# ============================================================
# 护栏 ②：预算 / 频率上限（数字都写进代码）
# ============================================================

#: 每日成本上限（元/天）。**0 = 一分钱都不许花**（fail-closed）。
#:
#: ⚠️ 与 `core/budget.CostBudget` 的 `daily <= 0 = 不限预算` **语义相反**，是刻意的：
#: "0 元"必须读作"没钱"，不能读作"随便花"（安全的一侧是默认）。
#:
#: 取 2.00 元的依据（全部来自本仓库的既有基线）：
#:   · LLM 日预算池 `MOSS_LLM_DAILY_BUDGET_CNY` 默认 **20.00 元/天**；
#:     兜底取它的 **10%** —— 即使天天跑满也挤不掉日常投研的额度；
#:   · 单次成本口径 `PER_CALL_ESTIMATE_CNY = 0.02 元/次`（见 `core/budget.py`：
#:     实测 mainline_relevance 0.0151 元/次，取 1.3 倍）→ **2.00 元 ≈ 100 次/天**；
#:   · 量级与 `SCRIPT_COST_CAP_CNY = 1.0`（单次高额脚本上限）同阶。
#: 公开源（AkShare/腾讯/baostock）实际不产生账单，所以这是**上限而非预期**，
#: 日常期望用量 ≈ 0（`DEFAULT_ALLOWLIST` 为空时恒为 0）。
DEFAULT_DAILY_BUDGET_CNY: Final[float] = 2.0

#: 单次兜底调用的**估算**成本（元/次）。复用 `core/budget.PER_CALL_ESTIMATE_CNY`
#: （"同一判断只允许一份实现"）—— 它是本仓库唯一的"保守单次成本"常量。
#: 记账口径 = **发起即计费**（含失败尝试）：护栏要拦住的正是"反复重试"。
DEFAULT_CALL_COST_CNY: Final[float] = PER_CALL_ESTIMATE_CNY

#: 小时调用上限（次）。`<= 0` = 一次都不许（fail-closed）。
#:
#: 取 30 的依据：一次投研请求的缺口通常是几个到几十个指标，"一轮补齐"要放得过去；
#: 30 次/小时 ≈ 每 2 分钟 1 次，而一个失控的重试循环（比如按秒重试）会立刻被挡住。
#: 它与日预算**不重复**：日预算管"总共花多少"，它管"单位时间问多少次"（对源友好）。
DEFAULT_MAX_CALLS_PER_HOUR: Final[int] = 30

#: 失败冷却（秒/指标）：某指标兜底失败后，这段时间内**不再**为它联网。
#: 取 600（10 分钟）与 `rate_limit_guard.LOCK_MINUTES` 同量级：
#: 短于它会让"源真的没有"变成每轮重问，长于它会让源恢复后仍被冷落。
FAILURE_COOLDOWN_SECONDS: Final[float] = 600.0

#: 被限流（429）时的加长冷却（秒/指标）：源在限流，冷却短了只会继续白撞。
RATE_LIMIT_COOLDOWN_SECONDS: Final[float] = 1800.0

#: 滚动窗口（秒）——"每小时 N 次"里的那个小时。
_RATE_WINDOW_SECONDS: Final[float] = 3600.0

#: 账本 schema 版本。读到**更高版本**的文件时只认识自己认识的字段（向前兼容），
#: 未知字段原样丢弃而不是崩 —— 账本读不动等于护栏失效，比少记一个字段严重。
STATE_VERSION: Final[int] = 1

#: 状态文件默认路径（无隔离配置时的兜底；正常路径由 `resolve_state_path()` 决定）。
DEFAULT_STATE_PATH: Final[str] = store_rel("run_dir") + "/network_fallback.json"

#: 快照里保留的最近事件条数（有界：`/health` 不是一个会无限膨胀的接口）。
RECENT_EVENTS_MAX: Final[int] = 20

#: 单条事件里 detail/context 的截断长度。
_DETAIL_MAX: Final[int] = 200


# ============================================================
# 触发判据（**来自 local_data 的 12 个诊断码**，机器可读）
# ============================================================

#: 哪些诊断码**允许**触发联网兜底：`码 → 人话理由`。
#:
#: 键是 `local_data.DiagCode` 的**字面值副本**（不是 import —— 避免将来在
#: `local_data` 里接线时模块级互相 import 成环）；`tests/unit/test_network_fallback.py`
#: 里有一条断言钉住"这些键必须真实存在于 `DiagCode`"（两条路径的结果必须相等，
#: 这样改了码名/漏了一个码，测试立刻报错而不是静默少算）。
_FALLBACK_TRIGGER_REASONS: Final[dict[str, str]] = {
    "NO_DATA": "表/列/实体/时间都在，就是没记录 —— 唯一的真缺口，只能去源上取",
    "NO_TABLE": "该数据集尚未落库（本地压根没有这张表）—— 联网是唯一来源",
    "NO_COLUMN": "本地与全部同义词都没有这个口径（本地执行器已先试过同义词）—— 去源上找",
    "STALE_BEYOND_TOLERANCE": "本地有值但过旧 —— 联网取一份新的",
    "CONN_FAIL": "本地库/源连不上 —— 在线源是此时唯一的出路",
}

#: 哪些诊断码**不**触发联网（联网也拿不到，花了预算还会引入错口径）。
#:
#: 这是"修 bug 必须配护栏"的另一半：只说"什么该联网"不够，
#: 还要说清"什么不该"——否则下一个人会把它们一起加进上面那张表。
_NO_FALLBACK_REASONS: Final[dict[str, str]] = {
    "NOT_APPLICABLE_FOR_ENTITY": (
        "该口径不适用于此类实体（如银行无流动比率）—— 联网拿不到同一口径，"
        "该换适用口径（这是「口径问题」，不是「数据缺失」）"),
    "NO_PERMISSION": "权限问题（只读账号/RLS 拦截）—— 不是缺数据，报权限比联网对",
    "ENTITY_UNMAPPED": "实体没解析出来（「招商银行」→600036 失败）—— 先修实体字典",
    "TIME_OUT_OF_RANGE": "时间超出覆盖范围 —— 放宽时间比联网对（数据本来就在）",
    "FREQ_MISMATCH": "频率不匹配（要日频只有季频）—— 上卷/下钻口径",
    "UNIT_MISMATCH": "单位不一致（元/万元）—— 换算单位即可，不用取数",
    "DIM_MISMATCH": "维度不匹配（要行业级只有个股级）—— 换维度或先聚合",
    # —— 环境维度（2026-09-28 第二十四轮：`local_data.DiagCode` 补了 5 个环境码）——
    #
    # 归类理由：**环境差异不是数据缺失**。联网兜底解决的是"这个口径没有源"，
    # 而"本环境没同步/没发布/没权限"要的是**同步或换环境** ——
    # 联网只会白花预算，还可能把另一个环境的数当成本环境的（更危险）。
    "ENV_NOT_COVERED": (
        "任何环境的库里都没有 → 这是覆盖缺口：应走**补采/入队**，"
        "而不是逐次联网兜底（后者解决不了「没有源」）"),
    "PROD_ONLY": "只有生产库有 —— 去生产环境查或等同步，联网拿不到本环境的口径",
    "DEV_ONLY": "只有 dev 库有（尚未发布）—— 确认发布状态，别在生产联网凑数",
    "DEV_SYNC_DELAY": "本环境落后于 dev —— 跑同步即可，数据本来就有",
    "PROD_PERMISSION_DENIED": "生产库拒绝访问 —— 这是权限问题，报权限比联网对",
}

#: 允许兜底的诊断码集合（单一事实源 = 上面那张 `码 → 理由` 表）。
FALLBACK_TRIGGER_CODES: Final[frozenset[str]] = frozenset(_FALLBACK_TRIGGER_REASONS)

#: "本地有数据但过旧"的**触发**阈值（天）。
#:
#: ⚠️ 与 `local_data.DEFAULT_STALE_DAYS = 400` **不是**同一个判据：那个是"容忍展示"
#: （超过就标注，但数据照给），这个是"该补采"（日频指标停更 30 天已经是真缺口）。
#: 两个阈值混用会让"标注"变成"每轮都去联网"。
STALE_TRIGGER_DAYS: Final[int] = 30


class ReasonCode:
    """`FallbackDecision.code` 的取值 —— **机器可读判据**（理由文案另给）。

    AGENTS.md：**判据只认机器可读的标识，不认给人看的文案**。
    所以每个拒绝都有两份表达：`code` 给代码（可断言、可统计），
    `reason` 给人（人话 + 剩余额度/剩余冷却时间）。
    """

    OK = "OK"
    ALLOWLIST_EMPTY = "ALLOWLIST_EMPTY"
    SOURCE_NOT_ALLOWED = "SOURCE_NOT_ALLOWED"
    STATE_UNAVAILABLE = "STATE_UNAVAILABLE"
    DAILY_BUDGET_EXHAUSTED = "DAILY_BUDGET_EXHAUSTED"
    HOURLY_LIMIT_REACHED = "HOURLY_LIMIT_REACHED"
    COOLDOWN = "COOLDOWN"
    CIRCUIT_OPEN = "CIRCUIT_OPEN"
    NO_SOURCE_AVAILABLE = "NO_SOURCE_AVAILABLE"
    #: 护栏**放行了**、源也都试了，但一个源都没取到（含"返回空"）。
    #: 值刻意等于账本事件里既有的码 `ALL_FAILED` —— 两份表达必须同一个词，
    #: 否则"按码统计"会把同一件事数成两类。
    ALL_SOURCES_FAILED = "ALL_FAILED"
    #: 兜底内部异常（`fetch()` 的兜底 catch）。**不等于**"源没有数据"。
    EXCEPTION = "EXCEPTION"



# ============================================================
# 路径（按实例隔离，学 rate_limit_guard）
# ============================================================

def resolve_state_path() -> str:
    """账本落盘路径 —— **必须按实例隔离**（照抄 `rate_limit_guard` 的结论）。

    为什么不能写死 `data/run/network_fallback.json`：`data/run/` 是三个实例
    （dev 8100 / pilot 8110 / 生产）**共用**的目录。写死它意味着
    dev 里跑几次兜底调试，就把**生产的日预算额度一起吃掉**
    （账本按路径共享），而生产侧看到的是"今天怎么不兜底了"——不报错。

    优先级：
      1. `MOSS_NETWORK_FALLBACK_PATH`（显式指定，最高优先）
      2. `LLM_AUDIT_DIR` 同级目录（dev/pilot 隔离**已经**在设这个变量，
         所以不必在 `manage.py` 里再加一处 key —— 少一个 key 就少一处漏改）
      3. `data/run/network_fallback.json`（无隔离配置时的兜底）
    """
    explicit = (os.environ.get("MOSS_NETWORK_FALLBACK_PATH") or "").strip()
    if explicit:
        return explicit
    audit_dir = (os.environ.get("LLM_AUDIT_DIR") or "").strip()
    if audit_dir:
        return str(Path(audit_dir) / "network_fallback.json").replace("\\", "/")
    return DEFAULT_STATE_PATH


# ============================================================
# 白名单：环境变量开关（**唯一**的"不改代码就能开"的入口）
# ============================================================

def allowlist_from_env(env: dict[str, str] | None = None) -> tuple[str, ...]:
    """从 `MOSS_NETWORK_FALLBACK_ALLOWLIST` 读白名单 → 元组。

    判据（**fail-closed**，与 `DEFAULT_ALLOWLIST` 同一含义）：

      · 变量未设置 / 空白 → `DEFAULT_ALLOWLIST`（**空 = 全拒**）；
      · 值按逗号切分、去空白、去重（保持录入顺序 —— 顺序就是源的优先级）；
      · **只接受 ASCII 机器可读标识**：非 ASCII 条目会被丢弃并 `warning`。
        这条是刻意的 —— 白名单是**花钱**的判据，而中文标签（"腾讯财经"）
        会随文案优化而变；放它进去等于把"允不允许花钱"绑在文案上。
        丢掉的条目**必须留日志**（悄悄丢掉会让"我明明配了"变成查不出的悬案）。
      · 全空的结果**就是空白名单**，不会退回任何"默认放行"的集合。

    ⚠️ 刻意**不**在这里校验"这个类名真的存在"：源清单来自运行时的连接器
    routes，模块级拿不到；真配错了的表现是 `NO_SOURCE_AVAILABLE`
    （在 `fetch()` 里记成可见的拒绝，不是静默不兜底）。

    ## ★ 2026-09-29：`os.environ` 缺值时**回落到 Settings**

    实测陷阱：pydantic-settings 只把 `.env` 读进 **Settings 对象**，
    **不写回 `os.environ`** ⇒ 在 `.env` 里配了白名单，这里读到的仍是空，
    表现为"联网兜底被拒绝：[ALLOWLIST_EMPTY]" —— **像护栏在正常工作，
    其实是开关没接上**。所以 `os.environ` 没有该变量时，回落到
    `settings.network_fallback_allowlist`（它同时认环境变量与 `.env`）。
    """
    if env is not None:
        raw = env.get(ALLOWLIST_ENV, "") or ""
    else:
        raw = os.environ.get(ALLOWLIST_ENV, "") or ""
        if not raw:
            # `.env` 走 Settings 这一路（pydantic-settings 不会写回 os.environ）
            try:
                from src.core.config import get_settings

                raw = str(getattr(
                    get_settings(), "network_fallback_allowlist", "") or "")
            except Exception as exc:  # noqa: BLE001 配置读不到 → 保持 fail-closed
                logger.debug("读 settings.network_fallback_allowlist 失败：%s", exc)
    out: list[str] = []
    dropped: list[str] = []
    for chunk in raw.split(","):
        item = chunk.strip()
        if not item:
            continue
        if not item.isascii():
            dropped.append(item)
            continue
        if item not in out:
            out.append(item)
    if dropped:
        logger.warning(
            "%s 里有非 ASCII 条目（已忽略）：%s —— 白名单只认连接器类名这类"
            "机器可读标识；中文标签会随文案优化而变，绑上去等于把『允不允许花钱』"
            "绑在文案上", ALLOWLIST_ENV, dropped)
    if not out and raw.strip():
        logger.warning("%s=%r 解析后为空 → 按 fail-closed 处理（不允许任何兜底）",
                       ALLOWLIST_ENV, raw)
    return tuple(out) if out else DEFAULT_ALLOWLIST


# ============================================================
# 白名单判据（**一份实现**，供本模块与外部预检共用）
# ============================================================

def source_allowed(source: str, allowlist: Sequence[str]) -> str:
    """该源是否被白名单允许；返回**命中的条目**（`""` = 不允许）。

    判据（机器可读标识，不认文案）：
      · 大小写不敏感**完全相等** —— 连接器名，如 `AkshareConnector`；
      · **域名后缀**且带点边界 —— `push2.eastmoney.com` 命中 `eastmoney.com`。

    ⚠️ 不做"模糊包含"：`noteastmoney.com` **不**匹配 `eastmoney.com`。
    子串匹配会让一个无关域名混进白名单，而这是**花钱**的判据 ——
    判据宽一格，代价是钱；严一格，代价是一次拒绝（且有记录）。
    """
    key = (source or "").strip().lower()
    if not key:
        return ""
    for entry in allowlist:
        item = (entry or "").strip().lower()
        if not item:
            continue
        if key == item or key.endswith("." + item):
            return entry
    return ""


def human_seconds(seconds: float | None) -> str:
    """把秒数说成**人话**（"还剩 12 分钟"里的那个部分）。

    `None` = **未量到**（不是 0 秒）—— 与 `snapshot()` 的口径一致：
    读不到就说读不到，绝不用 "0 秒" 糊过去。
    """
    if seconds is None:
        return UNMEASURED
    s = max(0.0, float(seconds))
    if s >= 3600:
        hours = int(s // 3600)
        minutes = int((s - hours * 3600) // 60)
        return f"{hours} 小时 {minutes} 分钟" if minutes else f"{hours} 小时"
    if s >= 60:
        return f"{int(s // 60)} 分钟"
    return f"{int(s)} 秒"


# ============================================================
# 决策
# ============================================================

@dataclass(frozen=True)
class FallbackDecision:
    """一次"能不能兜底"的结论。

    `reason` 是**人话 + 剩余量**（"冷却中，还剩 9 分钟"、"今日预算已用尽
    0.04/0.04 元"），不是枚举值裸输出 —— 因为它会直接进日志与 `/health`，
    而看它的人（包括未来的我）需要当场知道"为什么不行、还差多少"。
    `code` 是同一件事的机器可读版本（可用于断言与统计）。
    """

    allowed: bool
    reason: str
    code: str = ReasonCode.OK
    #: 今日预算剩余（元）；`None` = 未量到（账本读不到）。
    remaining_cny: float | None = None
    #: 最近 1 小时还能调用几次；`None` = 未量到。
    remaining_calls: int | None = None
    #: 建议重试等待秒数（冷却/小时窗口）；`None` = 无需等待。
    retry_after_s: float | None = None
    #: 命中的白名单条目（`SOURCE_NOT_ALLOWED` 时为空）。
    matched_entry: str = ""
    #: 被问的那个源（空 = 未指定具体源）。
    source: str = ""


@dataclass(frozen=True)
class FallbackFetch:
    """一次联网兜底的**完整结论**：数据点 + 护栏判定 + 人话理由。

    为什么要有它（而不是继续只返回 `list`）：`fetch()` 只回一个列表，
    调用方**分不清**三种"空"——

      · 护栏拦住了（白名单未开 / 预算耗尽 / 冷却中 / 熔断）；
      · 护栏放行了，但源上真的没有；
      · 取到了，但拿到的是空列表。

    三者的恢复动作完全不同（去配置 / 换口径 / 不用管），而 `[]` 把它们
    压成了同一件事 —— 于是上层只能写"没找到数据"，把**护栏拒绝**说成
    "数据不存在"。这正是 `AGENTS.md` 要防的"没量到 vs 量到 0"。

    `points` 只包含**真实数据点**（源原样返回的对象，带 `source_name` /
    `source_url` / `fetch_time` 溯源字段）；`fetch_outcome()` 不做任何
    补值/估算 —— 拿不到就是 `points == []` 且 `measured is False`。
    """

    #: 源返回的真实数据点（空 = 没量到）。
    points: list[Any] = field(default_factory=list)
    #: 护栏是否**放行**了这次尝试（与"有没有取到"是两件事）。
    allowed: bool = False
    #: 机器可读判据（`ReasonCode`）。
    code: str = ReasonCode.ALLOWLIST_EMPTY
    #: 人话 + 剩余量（"冷却中，还剩 9 分钟"）—— 直接可进日志/结果/告警。
    reason: str = ""
    #: 实际成功的源（`RouterSourceBridge.source_key()` 口径）；空 = 没成功。
    source: str = ""
    #: 触发理由（`should_fallback()` 的那一句），便于审计"为什么允许联网"。
    trigger: str = ""

    @property
    def measured(self) -> bool:
        """是否**量到**了真实数据点（不是"有没有试过"）。"""
        return bool(self.points)



# ============================================================
# 账本（落盘）
# ============================================================

@dataclass
class _Ledger:
    """联网兜底的**唯一账本**（成本 + 调用次数 + 失败计数 + 冷却）。"""

    version: int = STATE_VERSION
    #: 本地日期（跨日重置 `spent_cny` / `calls_today`）。
    day: str = ""
    #: 今日已花（元，**估算**口径：发起即计费，含失败尝试）。
    spent_cny: float = 0.0
    #: 今日已发起次数。
    calls_today: int = 0
    #: 累计计数（不跨日重置：它们是"这套兜底历史上健不健康"的证据）。
    attempts: int = 0
    successes: int = 0
    failures: int = 0
    rejections: int = 0
    #: 拒绝按原因码分账（回答"到底是哪条护栏在挡"）。
    rejected_by: dict[str, int] = field(default_factory=dict)
    #: 滚动 1 小时窗口内的发起时刻（epoch 秒）。
    recent_calls: list[float] = field(default_factory=list)
    #: 指标 → 冷却截止时刻（epoch 秒）。
    cooldowns: dict[str, float] = field(default_factory=dict)
    #: 最近一次拒绝/失败的原因（人话）。
    last_reason: str = ""
    last_event_ts: float = 0.0
    #: 最近事件（有界，给 `/health` 的"最近原因"）。
    events: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _LoadResult:
    """账本读取结果 —— **「没量到」与「量到 0」在这里分开**。"""

    ledger: _Ledger | None
    #: `ok`（读到且可信）/ `absent`（文件不存在 = 冷启动）/ `unreadable`（读不到）
    status: str
    error: str = ""


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _ledger_from_dict(raw: dict[str, Any]) -> _Ledger:
    """从 JSON 字典还原账本：**只取自己认识的字段**，坏值一律取默认。

    向前/向后兼容：将来加字段时，老文件照样能读（少一个字段 ≠ 账本坏了）；
    读到更高版本也不崩 —— 账本读不动等于护栏失效。
    """
    recent = raw.get("recent_calls")
    cooldowns = raw.get("cooldowns")
    events = raw.get("events")
    rejected_by = raw.get("rejected_by")
    return _Ledger(
        version=_int(raw.get("version"), 0),
        day=str(raw.get("day") or ""),
        spent_cny=max(0.0, _num(raw.get("spent_cny"))),
        calls_today=max(0, _int(raw.get("calls_today"))),
        attempts=max(0, _int(raw.get("attempts"))),
        successes=max(0, _int(raw.get("successes"))),
        failures=max(0, _int(raw.get("failures"))),
        rejections=max(0, _int(raw.get("rejections"))),
        rejected_by=(
            {str(k): max(0, _int(v)) for k, v in rejected_by.items()}
            if isinstance(rejected_by, dict) else {}),
        recent_calls=(
            [_num(x) for x in recent if isinstance(x, (int, float))]
            if isinstance(recent, list) else []),
        cooldowns=(
            {str(k): _num(v) for k, v in cooldowns.items()}
            if isinstance(cooldowns, dict) else {}),
        last_reason=str(raw.get("last_reason") or "")[:_DETAIL_MAX],
        last_event_ts=_num(raw.get("last_event_ts")),
        events=(
            [e for e in events if isinstance(e, dict)][-RECENT_EVENTS_MAX:]
            if isinstance(events, list) else []),
    )


# ============================================================
# 取数编排：把 `ConnectorRouter` 的 routes 适配成"按源取数"
# ============================================================

class RouterSourceBridge:
    """把 `ConnectorRouter` 用的 `routes` 列表适配成兜底的「候选源 + 取数」。

    只使用连接器的**公开契约**（`source_name` / `supports` / `fetch`），
    不碰 router 的私有字段。接线时把与 router **同一份** routes 传进来即可：

        routes = [...]                      # 与 ConnectorRouter(routes) 同一份
        fb = NetworkFallback.from_connector_routes(routes, allowlist=(...))

    `source_key()` 是白名单条目的**唯一口径**：默认取连接器**类名**
    （`AkshareConnector`），而不是 `source_name`（`"AkShare"`、
    `"腾讯财经/东方财富(A股流动性)"` 这类是**给人看的标签**，会随文案优化而变 ——
    白名单跟着它漂移，就会变成"改了文案，兜底静默失效"）。
    """

    def __init__(self, routes: Sequence[Any]) -> None:
        self._routes: list[tuple[Any, Any]] = list(routes or ())

    @staticmethod
    def source_key(connector: Any) -> str:
        """源的机器可读标识（连接器类名）；类名为空时退回 `source_name`。"""
        return type(connector).__name__ or str(
            getattr(connector, "source_name", "") or "unknown")

    def _matched(self, indicator: str) -> list[Any]:
        out: list[Any] = []
        for connector, supports in self._routes:
            try:
                if supports(indicator):
                    out.append(connector)
            except Exception:  # noqa: BLE001 某个 supports 判据坏了不该拖垮整条链
                logger.debug("supports() 判据异常，跳过该源: %s",
                             self.source_key(connector), exc_info=True)
        return out

    def candidates(self, indicator: str) -> list[str]:
        """支持该指标的**全部**源名（按 routes 顺序，即链的优先级顺序）。"""
        seen: set[str] = set()
        out: list[str] = []
        for connector in self._matched(indicator):
            key = self.source_key(connector)
            if key not in seen:
                seen.add(key)
                out.append(key)
        return out

    def connector_for(self, indicator: str, source: str) -> Any | None:
        """`source` 对应的连接器（`source` 按 `source_key()` 口径）。"""
        for connector in self._matched(indicator):
            if self.source_key(connector) == source:
                return connector
        return None

    async def fetch(self, indicator: str, source: str) -> list[Any]:
        """调用**指定源**取数。

        ⚠️ 刻意**不**吞异常：异常要原样上抛给 `NetworkFallback`，
        它才能用 `looks_rate_limited(..., http_status=...)` 做机器可读的限流判定
        （包一层 `RuntimeError` 会把 `http_status` 这个结构化字段弄丢，
        限流判据就退化成文案匹配 —— 文案一改，护栏静默失效）。
        """
        connector = self.connector_for(indicator, source)
        if connector is None:
            raise LookupError(f"没有支持 {indicator} 的源：{source}")
        return list(await connector.fetch(indicator))


def routes_from_router(router: Any) -> list[Any] | None:
    """从既有 `ConnectorRouter` 上取 **raw routes**（只读，取不到返回 `None`）。

    `ConnectorRouter` 没有公开的 routes 访问器（`get_capabilities()` 只给字典，
    里面没有连接器对象），所以这里按属性名取一次；拿不到就返回 `None`，
    由接入方显式传 `routes` —— **不猜、不造**，缺口在 `fetch()` 里会记成
    `NO_SOURCE_AVAILABLE`（可见，不静默）。

    与 `router_source_bridge()` 共用这一份实现（"同一判断只允许一份实现"）：
    那个函数返回适配器对象，本函数返回原始列表，给 `get_network_fallback(routes=...)`
    这类只想要列表的调用方用。
    """
    for attr in ("_routes", "routes"):
        routes = getattr(router, attr, None)
        if isinstance(routes, (list, tuple)) and routes:
            return list(routes)
    return None


def router_source_bridge(router: Any) -> RouterSourceBridge | None:
    """从既有 `ConnectorRouter` 实例上取 routes 并包成适配器（取不到返回 None）。

    判据见 `routes_from_router()`（**唯一实现**）。
    """
    routes = routes_from_router(router)
    return RouterSourceBridge(routes) if routes else None


# ============================================================
# 主体
# ============================================================

class NetworkFallback:
    """联网兜底（最后一跳）：**白名单 + 预算 + 频率 + 冷却**四道闸门。

    用法（接入方）：

        fb = NetworkFallback(allowlist=SUGGESTED_ALLOWLIST)          # 显式开启
        fb = NetworkFallback.from_connector_routes(routes, allowlist=...)  # 带取数实现
        ok, why = should_fallback(local_result)                      # 触发判据
        if ok and fb.can_fallback(ind).allowed:                      # 问一句（纯查询）
            points = await fb.fetch(ind, context=trace_id)           # 取数（绝不抛）

    默认**什么都不做**：`allowlist` 为空 → `can_fallback()` 恒 `False`
    （reason = "白名单为空"）。这是刻意的 —— 见模块 docstring 的护栏表。
    """

    def __init__(
        self,
        *,
        allowlist: Sequence[str] | None = None,
        daily_budget_cny: float | None = None,
        max_calls_per_hour: int | None = None,
        state_path: str | None = None,
        fetcher: Callable[[str, str], Awaitable[list[Any]]] | None = None,
        candidates: Callable[[str], list[str]] | None = None,
        call_cost_cny: float | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        """构造。

        - `allowlist`：合法源白名单。`None` → `DEFAULT_ALLOWLIST`（**空 = 全拒**）。
        - `daily_budget_cny`：每日上限（元）。`None` → `DEFAULT_DAILY_BUDGET_CNY`；
          `<= 0` = 一分钱都不许花。
        - `max_calls_per_hour`：小时上限。`None` → `DEFAULT_MAX_CALLS_PER_HOUR`；
          `<= 0` = 一次都不许。
        - `state_path`：账本路径。`None` → `resolve_state_path()`（**按实例隔离**）。
        - `fetcher`：真正的取数函数 `async (indicator, source) -> list`。
          `None` = 没有取数实现（接线缺口，会在 `fetch()` 里记成
          `NO_SOURCE_AVAILABLE`，可见而不是静默）。
        - `candidates`：`indicator -> [源名]` 解析器。`None` = **白名单即候选**
          （按白名单录入顺序逐个尝试）。
        - `call_cost_cny`：单次估算成本（元）。`None` → `DEFAULT_CALL_COST_CNY`。
        - `clock`：可注入时钟（测试用；默认 `time.time`）。

        ⚠️ 阈值**不做环境变量覆盖**：AGENTS.md《红灯纪律》——"既能写默认值、
        又能被环境变量覆盖的阈值都必然漂移"。要改就显式传参或改常量，
        生效值因此永远等于代码里读到的那个。
        """
        self._allowlist: tuple[str, ...] = tuple(
            DEFAULT_ALLOWLIST if allowlist is None
            else [str(x) for x in allowlist if str(x or "").strip()])
        self._daily = max(
            0.0, float(DEFAULT_DAILY_BUDGET_CNY if daily_budget_cny is None
                       else daily_budget_cny))
        self._max_per_hour = max(
            0, int(DEFAULT_MAX_CALLS_PER_HOUR if max_calls_per_hour is None
                   else max_calls_per_hour))
        self._cost = max(
            0.0, float(DEFAULT_CALL_COST_CNY if call_cost_cny is None
                       else call_cost_cny))
        self.path = str(state_path or resolve_state_path())
        self._fetcher = fetcher
        self._candidates = candidates
        self._clock: Callable[[], float] = clock or time.time
        # 进程内锁：账本是"读-改-写"，同一进程里并发请求必须串行（否则计数会丢）。
        # 用 RLock 而不是 Lock：`_decide`/`_record` 之间存在可重入的调用路径，
        # 嵌套取锁写成死锁的代价远高于 RLock 的那点开销。
        self._lock = threading.RLock()
        logger.info(
            "联网兜底已装配：白名单=%s，日预算=%.2f 元，小时上限=%d 次，"
            "失败冷却=%.0fs，账本=%s，取数实现=%s",
            list(self._allowlist) or "（空 → fail-closed，不允许任何兜底）",
            self._daily, self._max_per_hour, FAILURE_COOLDOWN_SECONDS, self.path,
            "已注入" if fetcher is not None else "未注入")

    # ---------- 构造便捷入口 ----------

    @classmethod
    def from_connector_routes(cls, routes: Sequence[Any], **kwargs: Any
                              ) -> NetworkFallback:
        """用 `ConnectorRouter` 那份 `routes` 直接装配（接线只需要一行）。"""
        bridge = RouterSourceBridge(routes)
        return cls(fetcher=bridge.fetch, candidates=bridge.candidates, **kwargs)

    # ---------- 只读属性 ----------

    @property
    def allowlist(self) -> tuple[str, ...]:
        return self._allowlist

    @property
    def enabled(self) -> bool:
        """是否**可能**兜底（白名单非空）。空 = 全部拒绝（默认）。"""
        return bool(self._allowlist)

    @property
    def daily_budget_cny(self) -> float:
        return self._daily

    @property
    def max_calls_per_hour(self) -> int:
        return self._max_per_hour

    @property
    def call_cost_cny(self) -> float:
        return self._cost

    @property
    def has_fetcher(self) -> bool:
        """是否注入了取数实现（`False` = 接线缺口，会记成 `NO_SOURCE_AVAILABLE`）。

        做成属性而不是让调用方去看 `_fetcher`：**懒建**那一侧要用它判断
        "这个实例能不能真的取数"，否则会出现"白名单开了、链路也走到了、
        但永远取不到"这种**不报错**的共存。
        """
        return self._fetcher is not None

    def match_allowlist(self, source: str) -> str:
        """该源命中的白名单条目（`""` = 不允许）。唯一实现 = `source_allowed`。"""
        return source_allowed(source, self._allowlist)

    # ---------- 账本：读 / 写 ----------

    def _now(self) -> float:
        return float(self._clock())

    def _today(self) -> str:
        # 用**注入的时钟**推日期（而不是 date.today()）：两者在真实运行时一致，
        # 但只有这样才能把"跨日自动重置"写成一条可断言、不用等一天的测试。
        return datetime.fromtimestamp(self._now()).date().isoformat()

    def _read_locked(self) -> _LoadResult:
        """读账本（**不缓存**）。

        为什么不缓存：账本是跨进程可见的预算状态。缓存在内存里会让
        "另一个进程已经花掉的钱"在本进程看不见 → 继续放行 → 超额。
        代价是每次判定一趟小文件读（kB 级），而兜底判定的频率以分钟计。
        """
        path = Path(self.path)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return _LoadResult(None, "absent")
        except OSError as exc:
            return _LoadResult(None, "unreadable", f"{type(exc).__name__}: {exc}")
        try:
            raw = json.loads(text)
        except ValueError as exc:               # JSONDecodeError 是 ValueError 子类
            return _LoadResult(None, "unreadable", f"JSON 解析失败: {exc}")
        if not isinstance(raw, dict):
            return _LoadResult(None, "unreadable", "顶层不是 JSON 对象")
        try:
            return _LoadResult(_ledger_from_dict(raw), "ok")
        except (TypeError, ValueError) as exc:
            return _LoadResult(None, "unreadable", f"{type(exc).__name__}: {exc}")

    def _write_locked(self, ledger: _Ledger) -> None:
        """落盘（原子替换）。**写失败绝不抛** —— 护栏坏掉不能拖垮主链路。"""
        ledger.version = STATE_VERSION
        path = Path(self.path)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.dumps(
                asdict(ledger), ensure_ascii=False, indent=2, sort_keys=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(payload, encoding="utf-8")
            os.replace(tmp, path)               # 原子替换，避免读到半截文件
        except OSError:
            logger.warning("联网兜底账本落盘失败（不影响本次结果）：%s",
                           self.path, exc_info=True)

    def _rollover(self, ledger: _Ledger, now: float) -> None:
        """跨日重置 + 窗口/冷却清理（调用方持锁）。"""
        today = self._today()
        if ledger.day != today:
            if ledger.day:
                logger.info(
                    "联网兜底账本跨日重置：%s 已花 %.4f 元 / %d 次 → %s",
                    ledger.day, ledger.spent_cny, ledger.calls_today, today)
            ledger.day = today
            ledger.spent_cny = 0.0
            ledger.calls_today = 0
        ledger.recent_calls = [t for t in ledger.recent_calls
                               if now - t < _RATE_WINDOW_SECONDS]
        ledger.cooldowns = {k: v for k, v in ledger.cooldowns.items() if v > now}

    # ---------- 判据（**唯一实现**：纯查询与扣费共用） ----------

    def _decide(self, ledger: _Ledger, indicator: str, source: str,
                now: float, *, charge: bool) -> FallbackDecision:
        """四条闸门按顺序判定；`charge=True` 时**当场记账**（原子）。

        顺序是有意的：白名单（最外层：默认关闭）→ 账本可用性（读不到就别花）
        → 源是否被允许 → 冷却（刚失败过）→ 日预算 → 小时频率。

        `charge=True` 把"检查"与"记账"合成一步，关掉 TOCTOU 窗口：
        并发的两个请求不会都看到"预算还有一点"然后一起花掉它。
        """
        # ① 白名单：空 = 默认关闭（安全的一侧是默认）
        if not self._allowlist:
            return FallbackDecision(
                False,
                "联网兜底默认关闭（白名单为空 —— fail-closed）：没有允许任何源；"
                "要开启必须显式传入 allowlist（建议值见 SUGGESTED_ALLOWLIST）。"
                f"今日预算 {ledger.spent_cny:.2f}/{self._daily:.2f} 元",
                ReasonCode.ALLOWLIST_EMPTY,
                remaining_cny=max(0.0, self._daily - ledger.spent_cny),
                matched_entry="", source=source)

        # ② 指定了源但不在白名单 → 拒绝（并告诉对方白名单里有什么）
        if source:
            entry = self.match_allowlist(source)
            if not entry:
                return FallbackDecision(
                    False,
                    f"源『{source}』不在联网兜底白名单里（白名单："
                    f"{'、'.join(self._allowlist)}）—— 拒绝；"
                    f"今日预算剩余 {max(0.0, self._daily - ledger.spent_cny):.2f} 元",
                    ReasonCode.SOURCE_NOT_ALLOWED,
                    remaining_cny=max(0.0, self._daily - ledger.spent_cny),
                    source=source)

        # ③ 冷却（刚失败过：源没坏，只是这份数据还没恢复，别每轮重问）
        until = ledger.cooldowns.get(indicator)
        if until and until > now:
            left = until - now
            return FallbackDecision(
                False,
                f"{indicator} 上次联网兜底失败，冷却中 —— 还剩 {human_seconds(left)}"
                f"（今日预算剩余 {max(0.0, self._daily - ledger.spent_cny):.2f} 元）",
                ReasonCode.COOLDOWN,
                remaining_cny=max(0.0, self._daily - ledger.spent_cny),
                retry_after_s=left, source=source)

        # ④ 日预算（含"剩余不够本次"：宁可少花一次，也不越过上限）
        remaining = max(0.0, self._daily - ledger.spent_cny)
        if self._daily <= 0 or ledger.spent_cny + self._cost > self._daily + 1e-9:
            if self._daily <= 0:
                short = (f"今日联网兜底预算为 0 元（{self._daily:.2f} 元 = "
                         "一分钱都不许花）—— 拒绝；要开启请显式传 daily_budget_cny>0")
            elif remaining <= 0:
                short = (f"今日联网兜底预算已用尽 {ledger.spent_cny:.2f}/"
                         f"{self._daily:.2f} 元（今日 {ledger.calls_today} 次调用）"
                         "—— 拒绝；明天（本地日期）自动重置")
            else:
                short = (f"今日联网兜底预算剩余 {remaining:.2f} 元 < 本次 "
                         f"{self._cost:.2f} 元 —— 拒绝；明天（本地日期）自动重置")
            return FallbackDecision(False, short, ReasonCode.DAILY_BUDGET_EXHAUSTED,
                                    remaining_cny=remaining, source=source)

        # ⑤ 小时频率（对源友好：日预算管"总共花多少"，它管"多快问"）
        used = len(ledger.recent_calls)
        if used >= self._max_per_hour:
            oldest = min(ledger.recent_calls) if ledger.recent_calls else now
            wait = max(0.0, _RATE_WINDOW_SECONDS - (now - oldest))
            return FallbackDecision(
                False,
                f"最近 1 小时已联网兜底 {used}/{self._max_per_hour} 次（距上限 "
                f"{max(0, self._max_per_hour - used)} 次）—— 拒绝；"
                f"再过 {human_seconds(wait)}可以再试",
                ReasonCode.HOURLY_LIMIT_REACHED,
                remaining_cny=remaining, remaining_calls=0,
                retry_after_s=wait, source=source)

        # ⑥ 放行
        entry = self.match_allowlist(source) if source else ""
        if charge:
            # **发起即计费**（含失败尝试）：护栏要拦住的正是"反复重试"，
            # 所以失败也要占额度和频次 —— 记在真实账单上则不占（公开源不花钱）。
            ledger.spent_cny += self._cost
            ledger.calls_today += 1
            ledger.attempts += 1
            ledger.recent_calls.append(now)
            remaining = max(0.0, self._daily - ledger.spent_cny)
            used += 1
        return FallbackDecision(
            True,
            f"允许联网兜底：今日预算剩余 {remaining:.2f}/{self._daily:.2f} 元，"
            f"最近 1 小时 {used}/{self._max_per_hour} 次"
            + (f"；源『{source}』命中白名单『{entry}』" if entry else ""),
            ReasonCode.OK,
            remaining_cny=remaining,
            remaining_calls=max(0, self._max_per_hour - used),
            matched_entry=entry, source=source)

    # ---------- 公开：问一句（**纯查询**，不改状态） ----------

    def can_fallback(self, indicator: str, *, source: str = "") -> FallbackDecision:
        """能不能为 `indicator` 做联网兜底 —— **纯查询，不记账**。

        ⚠️ 刻意不在这里记"拒绝计数"：这个方法会被调用方当**预检**用
        （可能被轮询、可能在真正需要之前就先问一句），让它写账本等于
        "谁问得多，谁的拒绝计数就高" —— 观测数据会被自己的查询污染。
        拒绝的落账由 `fetch()` 负责（那里才是"决定不去了"这件事真的发生的地方）。

        `source=""` = 未指定具体源（问"兜底这个指标总体上允不允许"）：
        此时**不做**源级白名单判定 —— 真正的源级判定在 `fetch()` 里逐个源做，
        所以不指定源**不会**成为绕过白名单的口子。
        """
        now = self._now()
        with self._lock:
            load = self._read_locked()
            if load.status == "unreadable":
                return self._state_unavailable(load)
            ledger = load.ledger or _Ledger(day=self._today())
            self._rollover(ledger, now)
            decision = self._decide(ledger, indicator, source, now, charge=False)
        # 源指定了且在白名单里、但该源正在熔断 → 明确告诉调用方（含剩余冷却）
        if source and decision.allowed:
            return self._with_circuit_verdict(decision, source, now)
        return decision

    def _state_unavailable(self, load: _LoadResult) -> FallbackDecision:
        """账本读不到 → **拒绝**（fail-closed），并给出人话的修法。

        ⚠️ 这一条与 `rate_limit_guard` 的选择**方向相反**，是刻意的：
        那边"探测失败 ≠ 判为限流"（误锁一个可用模型是净损失）；
        这边判的是**花钱** —— 读不到账本时放行等于"无法记账地超支"，
        而它的恢复动作很轻（修好或删掉那个文件），且全程可见。
        """
        return FallbackDecision(
            False,
            f"联网兜底账本读不到（{load.status}：{load.error or '未知原因'}；"
            f"路径 {self.path}）—— 无法确认今天已经花了多少，按 fail-closed 拒绝。"
            "修好或删掉该文件后即可恢复（删除只会丢掉历史计数）。",
            ReasonCode.STATE_UNAVAILABLE, source="")

    def _with_circuit_verdict(self, decision: FallbackDecision, source: str,
                              now: float) -> FallbackDecision:
        """该源正在熔断 → 把"允许"改成"熔断中"（含剩余冷却时间）。"""
        try:
            cb = get_circuit_registry().get_or_create(_CIRCUIT_PREFIX + source)
            if cb.allow_request():
                return decision
            snap = cb.snapshot()
            left = max(0.0, cb.recovery_cooldown_sec - float(snap["uptime_sec"]))
            return FallbackDecision(
                False,
                f"源『{source}』熔断中（累计失败 {snap['total_failures']} 次）"
                f"—— 本轮跳过；约 {human_seconds(left)}后自动半开重试",
                ReasonCode.CIRCUIT_OPEN,
                remaining_cny=decision.remaining_cny,
                remaining_calls=decision.remaining_calls,
                retry_after_s=left, source=source)
        except Exception:  # noqa: BLE001 熔断器读不到不该拦住兜底判定本身
            logger.debug("熔断器判定异常（忽略）", exc_info=True)
            return decision

    # ---------- 记账 ----------

    def _record_event(self, *, indicator: str, source: str, outcome: str,
                      code: str = "", reason: str = "", detail: str = "",
                      context: str = "", cooldown_s: float = 0.0,
                      counters: dict[str, int] | None = None,
                      rejected_code: str = "") -> None:
        """落一条事件 + 计数（**唯一写入口**，所有失败/拒绝/成功都走这里）。

        一次调用 = **一次读-改-写**（`rejected_code` 与计数在同一把锁里落），
        不为了多记一个字段而再开一轮读改写 —— 那既慢又给并发留了缝。
        """
        now = self._now()
        with self._lock:
            load = self._read_locked()
            if load.status == "unreadable":
                # 账本读不到时**不写**：覆盖掉那个文件等于把证据也一起删了，
                # 而"读不到"这件事本身必须留在盘上等人处理（见 `_state_unavailable`）。
                logger.warning(
                    "联网兜底账本不可读，事件只记日志不落盘：%s %s %s %s",
                    outcome, indicator, source, reason)
                return
            ledger = load.ledger or _Ledger(day=self._today())
            self._rollover(ledger, now)
            for key, delta in (counters or {}).items():
                setattr(ledger, key, max(0, int(getattr(ledger, key, 0)) + int(delta)))
            if rejected_code:
                ledger.rejections += 1
                ledger.rejected_by[rejected_code] = (
                    ledger.rejected_by.get(rejected_code, 0) + 1)
            if cooldown_s > 0 and indicator:
                ledger.cooldowns[indicator] = now + cooldown_s
            ledger.last_reason = (reason or "")[:_DETAIL_MAX]
            ledger.last_event_ts = now
            ledger.events.append({
                "ts": round(now, 3),
                "indicator": indicator,
                "source": source,
                "outcome": outcome,
                "code": code,
                "reason": (reason or "")[:_DETAIL_MAX],
                "detail": (detail or "")[:_DETAIL_MAX],
                "context": (context or "")[:_DETAIL_MAX],
            })
            ledger.events = ledger.events[-RECENT_EVENTS_MAX:]
            self._write_locked(ledger)

    def _record_rejection(self, indicator: str, decision: FallbackDecision,
                          *, source: str = "", context: str = "") -> None:
        """记一次"决定不去了"（计数 + 原因码分账 + 最近原因）。"""
        self._record_event(
            indicator=indicator, source=source or decision.source,
            outcome="rejected", code=decision.code, reason=decision.reason,
            context=context, rejected_code=decision.code)

    # ---------- 公开：取数（**绝不抛**） ----------

    async def fetch(self, indicator: str, *, context: str = "") -> list[Any]:
        """联网兜底取数：取到就返回数据点，取不到返回 `[]`（**绝不抛异常**）。

        流程（编排，判据全在 `_decide`）：

            预检（can_fallback）→ 列候选源 ∩ 白名单 − 熔断
              → 逐源：再判一次 + **当场扣费** → 调用 → 成功即返回
              → 全失败：记失败 + 冷却（该指标 10 分钟内不再联网）

        ⚠️ 唯一透传的是**取消信号**（`asyncio.CancelledError` 继承
        `BaseException`，不在 `except Exception` 范围内）：吞掉取消会让上层
        永远取消不掉一个已经在飞的请求。

        ⚠️ **只有一个列表就分不清"被护栏拒绝"与"源上没有"**。
        需要理由的调用方请用 `fetch_outcome()`（本方法就是它的薄封装，
        判据/记账/冷却全部只有一份实现）。本方法保留是为了不改变既有调用方
        的形状 —— `tests/unit/test_network_fallback.py` 全程用它。
        """
        return (await self.fetch_outcome(indicator, context=context)).points

    async def fetch_outcome(self, indicator: str, *, context: str = "",
                            trigger: str = "") -> FallbackFetch:
        """与 `fetch()` 同一条链路，但把**拒绝/失败的原因**一起带回来。

        `trigger` 由调用方传入（`should_fallback()` 的那句人话，原样带上），
        只做透传、不参与任何判据 —— 审计要能回答"为什么允许联网"，
        而触发判据的**唯一实现**在 `should_fallback()` 里。

        **绝不抛异常**：任何内部异常都变成 `code=EXCEPTION` 的结论，
        并且 `points == []`（"没量到"），绝不用 0 或占位值代替。
        """
        try:
            return await self._fetch_inner(indicator, context=context,
                                           trigger=trigger)
        except Exception as exc:  # noqa: BLE001 兜底是"最差的一层"，坏了不能拖垮主链路
            logger.warning("联网兜底异常（已吞，返回空）：%s %s -> %s",
                           indicator, context, exc, exc_info=True)
            reason = f"兜底内部异常：{type(exc).__name__}: {exc}"
            try:
                self._record_event(
                    indicator=indicator, source="", outcome="failure",
                    code=ReasonCode.EXCEPTION, reason=reason,
                    context=context, counters={"failures": 1})
            except Exception:  # noqa: BLE001 连记账都坏了就只剩日志
                logger.debug("兜底异常记账失败（忽略）", exc_info=True)
            return FallbackFetch(points=[], allowed=False,
                                 code=ReasonCode.EXCEPTION,
                                 reason=reason + "（未量到：异常不等于源上没有数据）",
                                 trigger=trigger)

    async def _fetch_inner(self, indicator: str, *, context: str = "",
                           trigger: str = "") -> FallbackFetch:
        def _out(decision: FallbackDecision, *, points: list[Any] | None = None,
                 source: str = "") -> FallbackFetch:
            return FallbackFetch(
                points=list(points or []), allowed=decision.allowed,
                code=decision.code, reason=decision.reason,
                source=source or decision.source or "", trigger=trigger)

        decision = self.can_fallback(indicator)
        if not decision.allowed:
            self._record_rejection(indicator, decision, context=context)
            logger.info("联网兜底被拒绝：%s -> [%s] %s",
                        indicator, decision.code, decision.reason)
            return _out(decision)

        sources = self._candidate_sources(indicator)
        if not sources:
            rejected = FallbackDecision(
                False,
                f"{indicator} 没有可用的兜底源（白名单内没有支持它的源，"
                f"或对应源正在熔断，或未注入取数实现）—— 拒绝；"
                f"今日预算剩余 {(decision.remaining_cny or 0.0):.2f} 元",
                ReasonCode.NO_SOURCE_AVAILABLE,
                remaining_cny=decision.remaining_cny, source="")
            self._record_rejection(indicator, rejected, context=context)
            logger.info("联网兜底无可用源：%s -> %s", indicator, rejected.reason)
            return _out(rejected)

        errors: list[str] = []
        #: 循环里被护栏拒掉的那次判定（`None` = 没被拒，就是源上没取到）。
        #: 用变量而不是直接 `return`：被拒时若**前面已有源失败过**，
        #: 原实现仍会补一条 `ALL_FAILED` 汇总事件 —— 直接 return 会把那条吃掉。
        denied: FallbackDecision | None = None
        for source in sources:
            attempt = self._authorize_attempt(indicator, source)
            if not attempt.allowed:
                # 预算/频率/冷却是**全局或按指标**的 → 换源也没用，直接收工
                self._record_rejection(indicator, attempt, source=source,
                                       context=context)
                logger.info("联网兜底第 %s 跳被拒：%s -> [%s] %s",
                            source, indicator, attempt.code, attempt.reason)
                denied = attempt
                break
            try:
                points = await self._call_source(indicator, source)
            except Exception as exc:  # noqa: BLE001 单源失败 → 记冷却 + 试下一源
                self._note_source_failure(indicator, source, exc, errors,
                                          context=context)
                continue
            if points:
                self._record_event(
                    indicator=indicator, source=source, outcome="success",
                    code=ReasonCode.OK,
                    reason=f"兜底成功：{len(points)} 条（源『{source}』）",
                    detail=f"context={context}" if context else "",
                    context=context, counters={"successes": 1})
                logger.info("联网兜底成功：%s <- %s（%d 条）",
                            indicator, source, len(points))
                return FallbackFetch(
                    points=list(points), allowed=True, code=ReasonCode.OK,
                    reason=f"联网兜底成功：{len(points)} 条（源『{source}』）",
                    source=source, trigger=trigger)
            # 空结果 = 没拿到（不是"拿到了 0 条"）：记冷却，避免反复白问同一个源
            errors.append(f"[{source}] 返回空")
            self._record_event(
                indicator=indicator, source=source, outcome="failure",
                code="EMPTY", reason=f"源『{source}』返回空结果（不算成功）",
                context=context, cooldown_s=FAILURE_COOLDOWN_SECONDS,
                counters={"failures": 1})

        reason = (f"{indicator} 全部兜底源都没取到数据：{' | '.join(errors)}"
                  if errors else f"{indicator} 没有可尝试的兜底源")
        if errors:
            # ⚠️ 这条是**汇总**，既不累加 `failures`、也**不设冷却**：
            # · `failures`：逐个源的失败已经在 `_note_source_failure` / EMPTY
            #   分支里计过了。再计一次会让"attempts=1 而 failures=2"。
            # · 冷却：**这正是被测试抓出来的一个真 bug** —— 限流（429）时
            #   单源分支设的是 30 分钟冷却，汇总这里若无条件再设 10 分钟，
            #   就把 30 分钟**覆盖成**10 分钟（护栏被层叠吃掉，且不报错）。
            #   冷却只由"知道原因的那一层"设：限流 30 分钟 / 普通失败 10 分钟。
            # 被预算/频率拒绝（`errors` 为空）不是"源坏了"，更不能挂冷却 ——
            # 那会让之后的拒绝理由变成误导性的"上次兜底失败"。
            self._record_event(
                indicator=indicator, source="", outcome="failure",
                code=ReasonCode.ALL_SOURCES_FAILED, reason=reason[-_DETAIL_MAX:],
                context=context)
            logger.info("联网兜底失败：%s", reason[:_DETAIL_MAX])
        if denied is not None:
            # 护栏拒绝优先于"源上没取到"：**先解释为什么没去**。
            # 两者的恢复动作完全不同（去配置额度 vs 换口径），
            # 把拒绝说成"数据不存在"正是 AGENTS.md 点名的那类假绿。
            return _out(denied, source=denied.source)
        return FallbackFetch(
            points=[], allowed=True, code=ReasonCode.ALL_SOURCES_FAILED,
            reason=(f"联网兜底已放行但**未量到**数据：{reason}"
                    if errors else f"联网兜底已放行但没有可尝试的源：{indicator}"),
            trigger=trigger)

    def _note_source_failure(self, indicator: str, source: str, exc: Exception,
                             errors: list[str], *, context: str) -> None:
        """单个源失败：限流判据走既有实现 + 熔断记账 + 冷却 + 事件。"""
        status = getattr(exc, "http_status", None)
        limited = looks_rate_limited(exc, http_status=status)
        cooldown = RATE_LIMIT_COOLDOWN_SECONDS if limited else FAILURE_COOLDOWN_SECONDS
        label = "被限流(429)" if limited else "失败"
        errors.append(f"[{source}] {label}: {type(exc).__name__}: {exc}")
        try:
            get_circuit_registry().get_or_create(
                _CIRCUIT_PREFIX + source).record_failure()
        except Exception:  # noqa: BLE001 熔断器记账失败不影响兜底结论
            logger.debug("熔断器记账失败（忽略）", exc_info=True)
        logger.warning("联网兜底源%s：%s <- %s（冷却 %s）：%s",
                       label, indicator, source,
                       human_seconds(cooldown), exc)
        self._record_event(
            indicator=indicator, source=source, outcome="failure",
            code="RATE_LIMITED" if limited else "SOURCE_ERROR",
            reason=f"源『{source}』{label}：{type(exc).__name__}: {exc}",
            context=context, cooldown_s=cooldown, counters={"failures": 1})

    def _call_source(self, indicator: str, source: str) -> Awaitable[list[Any]]:
        """调用注入的取数实现（`None` → 抛错，由上层记成可见的接线缺口）。"""
        if self._fetcher is None:
            raise RuntimeError(
                "未注入取数实现（fetcher=None）：兜底已开启但没有可调用的源")
        return self._fetcher(indicator, source)

    # ---------- 内部：候选源 ----------

    def _candidate_sources(self, indicator: str) -> list[str]:
        """可尝试的源 = 候选 ∩ 白名单 − 熔断中（按优先级顺序）。"""
        try:
            names = (list(self._candidates(indicator))
                     if self._candidates is not None else list(self._allowlist))
        except Exception:  # noqa: BLE001 候选解析失败 → 退回白名单本身（仍是白名单内）
            logger.warning("兜底候选源解析失败，退回白名单本身：%s",
                           indicator, exc_info=True)
            names = list(self._allowlist)
        out: list[str] = []
        for name in names:
            if not self.match_allowlist(name):
                continue
            if self._circuit_open(name):
                logger.info("源『%s』熔断中，本次跳过（%s）", name, indicator)
                continue
            out.append(name)
        return out

    @staticmethod
    def _circuit_open(source: str) -> bool:
        try:
            return not get_circuit_registry().get_or_create(
                _CIRCUIT_PREFIX + source).allow_request()
        except Exception:  # noqa: BLE001 判不了就当它是通的（由调用失败再记账）
            return False

    def _authorize_attempt(self, indicator: str, source: str) -> FallbackDecision:
        """第二次判定（并发下第一次的结论可能已过期）并**当场记账**。"""
        now = self._now()
        with self._lock:
            load = self._read_locked()
            if load.status == "unreadable":
                return self._state_unavailable(load)
            ledger = load.ledger or _Ledger(day=self._today())
            self._rollover(ledger, now)
            decision = self._decide(ledger, indicator, source, now, charge=True)
            if decision.allowed:
                self._write_locked(ledger)
            return decision

    # ---------- 公开：可观测（给 /health 或审计脚本） ----------

    def snapshot(self) -> dict[str, Any]:
        """状态快照（`/health` / 审计脚本读它）。

        **「没量到」与「量到 0」分开**（AGENTS.md 硬约束）：
        账本读不到时 `available=False` 且 `counters=None` ——
        **不是**一组 0。`state_status` 进一步区分"文件不存在（冷启动）"
        与"文件在但读不动（要人处理）"，因为两者的恢复动作完全不同。

        另外：`state_status == "unreadable"` 时 `admission == "closed"`
        （本模块会拒绝兜底），而 `absent` 时 `admission == "open"`
        （账本为空，允许花但从 0 开始记）—— 这两个字段合起来回答
        "现在到底能不能兜底、为什么"。
        """
        try:
            return self._snapshot_inner()
        except Exception as exc:  # noqa: BLE001 健康检查不能因为快照崩掉
            return {
                "module": MODULE, "available": False, "state_status": "error",
                "state_error": f"{type(exc).__name__}: {exc}",
                "state_path": self.path, "counters": None,
                "note": "快照构建失败：计数**未量到**（≠ 0）",
            }

    def _snapshot_inner(self) -> dict[str, Any]:
        now = self._now()
        with self._lock:
            load = self._read_locked()
            ledger = load.ledger
            counters: dict[str, Any] | None = None
            if ledger is not None:
                self._rollover(ledger, now)
                remaining = max(0.0, self._daily - ledger.spent_cny)
                used = len(ledger.recent_calls)
                counters = {
                    "day": ledger.day,
                    "spent_cny": round(ledger.spent_cny, 4),
                    "calls_today": ledger.calls_today,
                    "remaining_cny": round(remaining, 4),
                    "calls_last_hour": used,
                    "remaining_calls_this_hour": max(
                        0, self._max_per_hour - used),
                    "totals": {
                        "attempts": ledger.attempts,
                        "successes": ledger.successes,
                        "failures": ledger.failures,
                        "rejections": ledger.rejections,
                    },
                    "rejected_by": dict(sorted(
                        ledger.rejected_by.items(), key=lambda kv: -kv[1])),
                    "cooldowns": {
                        k: {
                            "remaining_s": round(v - now, 1),
                            "remaining": human_seconds(v - now),
                        } for k, v in sorted(ledger.cooldowns.items())
                        if v > now
                    },
                    "last_reason": ledger.last_reason,
                    "last_event_ts": ledger.last_event_ts or None,
                    "recent_events": list(ledger.events),
                }
            ok = load.status == "ok"
            snap: dict[str, Any] = {
                "module": MODULE,
                # available=False 的两种情况都由 state_status 说清；
                # 无论哪种，**不要把 counters 读成 0**（它是 None）。
                "available": ok,
                "state_status": load.status,
                "state_error": load.error,
                "state_path": self.path,
                "admission": "closed" if load.status == "unreadable" else "open",
                "enabled": self.enabled,
                "allowlist": list(self._allowlist),
                "daily_budget_cny": self._daily,
                "max_calls_per_hour": self._max_per_hour,
                "call_cost_cny": self._cost,
                "cost_basis": (
                    f"imputed: {self._cost:.4f} 元/次（含失败尝试的估算，"
                    "口径复用于 core.budget.PER_CALL_ESTIMATE_CNY；"
                    "公开源实际不产生账单 → 这是上限而非账单）"),
                "failure_cooldown_s": FAILURE_COOLDOWN_SECONDS,
                "rate_limit_cooldown_s": RATE_LIMIT_COOLDOWN_SECONDS,
                "counters": counters,
                "sources": self._sources_snapshot(),
                "summary": self._summary(load, counters),
            }
        return snap

    def _summary(self, load: _LoadResult,
                 counters: dict[str, Any] | None) -> str:
        """一行人话（`/health` 直接显示它，不用前端自己拼）。"""
        if load.status == "unreadable":
            return (f"账本读不到（{load.error or '未知原因'}）→ 已按 fail-closed "
                    f"拒绝全部兜底；修好或删除 {self.path} 后恢复")
        if not self.enabled:
            return ("未启用（白名单为空，fail-closed）：联网兜底默认关闭，"
                    "要开启需显式传 allowlist")
        if counters is None:
            return (f"账本尚未建立（{self.path}）：本实例还没有兜底记录 —— "
                    "计数为空**不等于**没有失败")
        return (f"已启用 {len(self._allowlist)} 个源；今日 "
                f"{counters['spent_cny']:.2f}/{self._daily:.2f} 元、"
                f"最近 1 小时 {counters['calls_last_hour']}/"
                f"{self._max_per_hour} 次、累计失败 "
                f"{counters['totals']['failures']} 次")

    def _sources_snapshot(self) -> dict[str, dict[str, Any]]:
        """各兜底源的熔断状态（复用既有熔断器，不另造一套）。"""
        out: dict[str, dict[str, Any]] = {}
        try:
            registry = get_circuit_registry()
            for name, snap in registry.snapshot_all().items():
                if not name.startswith(_CIRCUIT_PREFIX):
                    continue
                key = name[len(_CIRCUIT_PREFIX):]
                out[key] = {
                    **snap,
                    "recovery_cooldown_s": getattr(
                        registry.get_or_create(name), "recovery_cooldown_sec", None),
                }
        except Exception:  # noqa: BLE001 熔断状态拿不到不影响护栏结论
            logger.debug("熔断状态快照失败（忽略）", exc_info=True)
        return out


# ============================================================
# 触发判据（**一份实现**：接入方不要自己再写一套）
# ============================================================

def should_fallback(result: Any) -> tuple[bool, str]:
    """本地取数结果是否**该**触发联网兜底 → `(是否, 人话理由)`。

    `result` 是 `local_data.MetricSeries`（鸭子类型：只读 `diag.code` 与
    `plan["stale_days"]`，**不 import** 那个模块 —— 将来在 `local_data` 里接线时
    才不会有模块级循环 import）。

    判据（机器可读的诊断码，**不认文案**）：

    | 诊断码 | 联网？ | 为什么 |
    |---|---|---|
    | `NO_DATA` / `NO_TABLE` / `NO_COLUMN` | ✅ | 真缺口：本地压根没有/取不到 |
    | `STALE_BEYOND_TOLERANCE` / `CONN_FAIL` | ✅ | 本地过旧 / 本地不可用 |
    | `NOT_APPLICABLE_FOR_ENTITY` | ❌ | 口径不适用（银行无流动比率），联网也拿不到同一口径 |
    | `NO_PERMISSION` | ❌ | 权限问题不是缺数据 |
    | `ENTITY_UNMAPPED` / `TIME_OUT_OF_RANGE` | ❌ | 是我们的口径/参数问题 |
    | `FREQ_MISMATCH` / `UNIT_MISMATCH` / `DIM_MISMATCH` | ❌ | 先修口径 |

    有数据但**过旧**（`plan["stale_days"] > STALE_TRIGGER_DAYS`）也允许补采 ——
    但触发阈值与 `local_data` 的"容忍展示"阈值**不是**同一个数（见常量说明）。
    """
    diag = getattr(result, "diag", None)
    code = str(getattr(diag, "code", "") or "").strip()
    if code:
        if code in _FALLBACK_TRIGGER_REASONS:
            return True, f"本地诊断码 {code}：{_FALLBACK_TRIGGER_REASONS[code]}"
        if code in _NO_FALLBACK_REASONS:
            return False, f"本地诊断码 {code} 不触发联网兜底：{_NO_FALLBACK_REASONS[code]}"
        return False, (f"未知诊断码『{code}』—— 不触发联网兜底"
                       "（未知即不许花钱；请把它登记进触发表）")

    plan = getattr(result, "plan", None)
    stale_raw = plan.get("stale_days") if isinstance(plan, dict) else None
    try:
        stale = int(stale_raw) if stale_raw is not None else None
    except (TypeError, ValueError):
        stale = None
    if stale is None:
        return False, "本地已有数据且新鲜度未量到 —— 不触发联网兜底（缺量到就别花钱）"
    if stale > STALE_TRIGGER_DAYS:
        return True, (f"本地最新期间距今 {stale} 天（超过触发阈值 "
                      f"{STALE_TRIGGER_DAYS} 天）→ 允许联网补采")
    return False, (f"本地已有数据且距今 {stale} 天 ≤ 触发阈值 "
                   f"{STALE_TRIGGER_DAYS} 天 —— 不触发联网兜底")


# ============================================================
# 进程级单例（`/health` 要读"这一个"生效中的实例）
# ============================================================

_FALLBACK: NetworkFallback | None = None
_FALLBACK_LOCK = threading.Lock()


def get_network_fallback(routes: Sequence[Any] | None = None) -> NetworkFallback:
    """进程级单例（懒建）—— **请求链路与 `/health` 读的必须是这一个**。

    读取顺序（**显式 > 环境变量 > 默认关闭**）：

      1. 已由 `set_network_fallback()` 装入的实例 → **原样返回**（最高优先；
         测试与宿主装配都走这条路，`routes` 被忽略）；
      2. 否则懒建**一次**：白名单取 `allowlist_from_env()`
         （`MOSS_NETWORK_FALLBACK_ALLOWLIST`；**未设置 = 空白名单 = 全拒**），
         并把 `routes` 交给 `from_connector_routes()` 作为取数实现；
      3. 懒建结果被装入单例，之后的调用（含 `/health`）拿到的是**同一个**。

    `routes` 只影响**首次**懒建：`ConnectorRouter` 的那份 `routes`
    （`connector, supports` 二元组列表）。传 `None` 时懒建出来的实例
    **没有取数实现** —— 表现是 `NO_SOURCE_AVAILABLE`
    （在 `fetch()` 里记成可见的拒绝），不是静默不兜底。

    ⚠️ 懒建出来的是**未启用**的实例（白名单为空时）→ `can_fallback()` 恒 `False`。
    所以"忘了配置"的表现是"兜底不生效且 `/health` 明说未启用"，
    而不是"悄悄开始联网"。这是刻意的 fail-closed。
    """
    global _FALLBACK
    if _FALLBACK is not None:
        return _FALLBACK
    with _FALLBACK_LOCK:
        if _FALLBACK is None:
            allowlist = allowlist_from_env()
            if routes:
                built = NetworkFallback.from_connector_routes(
                    routes, allowlist=allowlist)
            else:
                built = NetworkFallback(allowlist=allowlist)
            _FALLBACK = built
            if allowlist and not built.has_fetcher:
                # 开启了白名单却没有取数实现 = 必然取不到东西。
                # 这条**必须响**：否则表现是"兜底开了但永远没数据"，两边都以为对方的问题。
                logger.warning(
                    "联网兜底白名单已开启（%s）但**未注入取数实现**："
                    "每次兜底都会记 NO_SOURCE_AVAILABLE。"
                    "请用 set_network_fallback(NetworkFallback.from_connector_routes(...))"
                    "装配，或让链路把 ConnectorRouter 的 routes 传进来。",
                    list(allowlist))
    return _FALLBACK


def set_network_fallback(instance: NetworkFallback) -> NetworkFallback:
    """装入接入方配置好的实例（**唯一正确的开启方式**）。"""
    global _FALLBACK
    with _FALLBACK_LOCK:
        _FALLBACK = instance
    logger.info("联网兜底实例已装入：白名单=%s，日预算=%.2f 元",
                list(instance.allowlist) or "（空 → fail-closed）",
                instance.daily_budget_cny)
    return instance


def reset_network_fallback() -> None:
    """丢弃单例（**仅测试用**）。"""
    global _FALLBACK
    with _FALLBACK_LOCK:
        _FALLBACK = None


__all__ = [
    "ALLOWLIST_ENV",
    "DEFAULT_ALLOWLIST",
    "DEFAULT_CALL_COST_CNY",
    "DEFAULT_DAILY_BUDGET_CNY",
    "DEFAULT_MAX_CALLS_PER_HOUR",
    "DEFAULT_STATE_PATH",
    "FAILURE_COOLDOWN_SECONDS",
    "FALLBACK_TRIGGER_CODES",
    "MODULE",
    "NOT_SUGGESTED_SOURCES",
    "NetworkFallback",
    "RATE_LIMIT_COOLDOWN_SECONDS",
    "RECENT_EVENTS_MAX",
    "ReasonCode",
    "RouterSourceBridge",
    "STALE_TRIGGER_DAYS",
    "STATE_VERSION",
    "SUGGESTED_ALLOWLIST",
    "SUGGESTED_ALLOWLIST_REASONS",
    "UNMEASURED",
    "FallbackDecision",
    "FallbackFetch",
    "allowlist_from_env",
    "get_network_fallback",
    "human_seconds",
    "reset_network_fallback",
    "resolve_state_path",
    "router_source_bridge",
    "routes_from_router",
    "set_network_fallback",
    "should_fallback",
    "source_allowed",
]
