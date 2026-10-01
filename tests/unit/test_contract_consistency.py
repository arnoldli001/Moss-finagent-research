"""★★★ 契约**四向**一致性护栏：抓「A 面声明了、B 面没实现」的**静默**缺口。

## 为什么还需要这个文件（既有护栏为什么没抓到）

用户三轮报障「数据在库里 / 在数据源上却取不到」。根因之一是**契约四处不一致
且不报错**。实测现场：

    A11_fin_risk 的 system_prompt 声称会读「ROE / ROA / 商誉 / 应收账款」，
    而数据采集侧只实现了 `资产负债率:` 与 `流动比率:` —— 一个都没实现。
    这个缺口在仓库里存在了很多轮，**没有任何测试发现它**，因为：

| 既有护栏 | 它验什么 | 它对什么无感 |
|---|---|---|
| `test_indicator_prefix_wiring.py` | **正向**：「我在 `LIVE_PREFIXES` 里声明了前缀 X →
每个 Agent 都要放行它、都要教 prompt」 | 「**没人声明过**的指标」 |
| `test_whitelist_coverage.py` | 三向：`indicators.yaml` ↔ 白名单 ↔ Agent 域 |
「**连接器根本没实现**」 |

本文件补的是**采集侧（连接器）这一面**，并把四个面两两对上：

| 面 | 取自哪里（全部**从代码/配置现读**，不硬编码清单） |
|---|---|
| ① 连接器实际实现 | `src/infrastructure/connectors/` 里 `BaseConnector` 子类的
`supports()` 与 `get_capabilities()["indicators"]`；连接器清单从
`src/api/runtime.py` 的路由表 **AST 现读**（与 router 实际收到的是同一批） |
| ② 指标登记 | `configs/indicators.yaml` 的 `id` 列表
（读法复用 `test_whitelist_coverage.py::_registered_ids`） |
| ③ planner 目录 | `src/orchestration/planner.py::INDICATOR_CATALOG` |
| ④ 分析层白名单 | `src/orchestration/supervisor.py::_AGENT_DATA_WHITELIST` |
| ⑤ 消费侧本地规则 | `compliance/logic.py` 的 `_RATIO_RULES` 键 +
AST 扫出的 `_find_value(data_points, "X")` 字面量 |

## 七条断言（每条都在下面函数里写了"为什么"）

1. ③ → ① `INDICATOR_CATALOG` 每个 id 必须有连接器 `supports()`（否则 planner
   选得出、采集侧取不到 → 必然"数据缺失"）
2. ② → ① `indicators.yaml` 每个登记指标要么有连接器支持、要么在豁免表里
   **明确登记为不可得**（豁免必须能被代码复现 + 带过期检查）
3. prompt → ① 分析/行业 Agent 的 `system_prompt` 里**声称**会读的指标族，
   必须至少有一个连接器 supports（这是抓「ROE 那种缺口」的关键一条）
4. ① → ② 连接器 `supports()` 能认的**个股类**前缀（`XXX:{code}`）必须在
   `indicators.yaml` 里登记（否则取到了也无处引用）；其它占位符的模板前缀同理
5. ③ → ② planner 目录里的 id 必须是登记指标（否则 SmartFetcher 判"未登记"
   → 每次联网）
6. ① ↔ ① `get_capabilities()` 声称的模板指标必须真的被 `supports()` 接受
   （否则"派生前缀"这套判据本身就不可信）
7. ⑤ → ① 本地规则**直接消费**的指标族必须有人生产（否则规则永不触发，
   而它的输出是"无风险"这种**看起来正常**的结论）

## 判据纪律（照 AGENTS.md）

- **只认机器可读的标识**：指标 id、`supports()` 的布尔、`{code}` 模板前缀、
  prompt 里的**原文子串** —— 不解析自然语言去猜语义。
- **不许硬编码会和实现漂移的清单**：连接器清单取自路由表 AST，指标清单取自
  YAML，planner 目录取自常量，白名单取自 `supervisor`。
- **豁免必须带过期检查**：下面每张豁免表都配了一条"条目一旦变得能被满足就必须
  删掉"的断言（照 `test_whitelist_coverage.py::test_allowlisted_phantoms_have_no_registered_hit`）。
- **自己的判据先自证**：`test_prompt_table_quotes_are_real` 断言映射表里的每一句
  "出处"真的是该 Agent prompt 的原文子串 —— 表一旦漂移成虚构就红。
- **不联网、不扫全库**：只 import 19 个连接器类 + 3 个常量模块。
"""

from __future__ import annotations

import ast
import functools
import importlib
import inspect
import pkgutil
import re
from pathlib import Path

import pytest
import yaml

from src.infrastructure.connectors.base import BaseConnector
from src.orchestration.planner import INDICATOR_CATALOG
from src.orchestration.supervisor import (
    _AGENT_DATA_WHITELIST,
)
from src.orchestration.supervisor import (
    ANALYSIS_AGENTS as _ANALYSIS_AGENTS,
)

#: 仓库根（`tests/unit/xxx.py` → parents[2]）
_ROOT = Path(__file__).resolve().parents[2]

#: 路由表的单一事实源（用 AST 读，**不 import** —— `build_runtime()` 会连带装配
#: LLM 网关 / 图 / 多个子系统，单测里跑它又慢又脆）。
#: 见 `tests/unit/test_core_config_schema.py::test_daily_chain_order_in_runtime_source`
#: 的同款做法。
_RUNTIME_SOURCE = _ROOT / "src" / "api" / "runtime.py"

#: 个股类指标的采样代码（真实存在的 6 位代码，与既有测试一致）
_SAMPLE_CODE = "601088"
#: 日期类占位符的采样值
_SAMPLE_DATE = "2026-09-28"

#: 连接器 `get_capabilities()["indicators"]` 里出现的占位符 → 采样值。
#:
#: 遇到表中没有的占位符时**必须报错**（`_materialize` 抛 `_UnknownPlaceholder`）——
#: 静默跳过会让"模板前缀"这套判据凭空少算，正是本文件要防的失败模式。
_PLACEHOLDER_SAMPLES: dict[str, str] = {
    "code": _SAMPLE_CODE,
    "YYYY-MM-DD": _SAMPLE_DATE,
    "指数名": "沪深300",
    "行业名": "半导体",
    "赛道名": "新能源汽车",
    "行业关键词": "半导体",
}

_PLACEHOLDER_RE = re.compile(r"\{([^}]+)\}")


class _UnknownPlaceholder(ValueError):
    """模板里出现了没登记的占位符（判据缺采样值，不能静默跳过）。"""


def _materialize(indicator: str) -> str:
    """把模板 id 展开成**具体**指标（供 `supports()` 探测）。

    `{code}` → `601088`、`{YYYY-MM-DD}` → `2026-09-28` …
    未知占位符直接抛错：宁可红，也不静默少算一条。
    """

    def _sub(match: re.Match[str]) -> str:
        key = match.group(1)
        sample = _PLACEHOLDER_SAMPLES.get(key)
        if sample is None:
            raise _UnknownPlaceholder(
                f"模板 {indicator!r} 的占位符 {{{key}}} 没有采样值 —— "
                f"请在 _PLACEHOLDER_SAMPLES 里补一行"
                f"（可选：{sorted(_PLACEHOLDER_SAMPLES)}）")
        return sample

    return _PLACEHOLDER_RE.sub(_sub, indicator)


# ============================================================
# 面 ①：连接器实际实现（supports + capabilities）
# ============================================================


@functools.lru_cache(maxsize=1)
def _connector_classes() -> dict[str, type]:
    """连接器包内定义的全部 `BaseConnector` 子类（现读，不硬编码）。

    排除基类本身与 `ConnectorRouter`（路由器不是数据源）。
    """
    import src.infrastructure.connectors as pkg

    found: dict[str, type] = {}
    for info in pkgutil.iter_modules(pkg.__path__):
        try:
            module = importlib.import_module(f"{pkg.__name__}.{info.name}")
        except Exception:  # noqa: BLE001 可选依赖缺失的连接器不该拖垮整个文件
            continue
        for name, obj in vars(module).items():
            if (
                inspect.isclass(obj)
                and issubclass(obj, BaseConnector)
                and obj is not BaseConnector
                and obj.__module__ == module.__name__
                and name != "ConnectorRouter"
            ):
                found[name] = obj
    return found


def _route_connector_names() -> list[str]:
    """从 `src/api/runtime.py` 的 AST 里取**实际注册进路由**的连接器类名。

    判据是路由元组的第二个元素（`XxxConnector.supports`）的 `value.id` ——
    它比第一个元素（局部变量名，如 `sw_valuation`）稳定，且与
    `ConnectorRouter._routes` 收到的东西一一对应。
    受开关守卫的分支（`LOCAL_QUOTE_DIR` / `QMT_ENABLED`）也会被读到 ——
    本文件验的是"实现面声明了什么"，不是"本机这次装配了哪些"。
    """
    tree = ast.parse(_RUNTIME_SOURCE.read_text(encoding="utf-8"))
    names: list[str] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "append"
        ):
            continue
        for arg in node.args:
            if not (isinstance(arg, ast.Tuple) and len(arg.elts) >= 2):
                continue
            second = arg.elts[1]
            if isinstance(second, ast.Attribute) and isinstance(second.value, ast.Name):
                names.append(second.value.id)
    return names


@functools.lru_cache(maxsize=1)
def _wired_connector_classes() -> dict[str, type]:
    """路由表里**真的**注册了的连接器类（名字 → 类）。"""
    all_classes = _connector_classes()
    return {
        name: all_classes[name]
        for name in dict.fromkeys(_route_connector_names())
        if name in all_classes
    }


def _supporting_connectors(indicator: str) -> list[str]:
    """哪些连接器的 `supports()` 认这个指标（空 = 采集侧没人实现）。"""
    hits: list[str] = []
    for name, cls in sorted(_wired_connector_classes().items()):
        try:
            if bool(cls.supports(indicator)):
                hits.append(name)
        except Exception:  # noqa: BLE001 崩溃的 supports 不算"支持"
            continue
    return hits


@functools.lru_cache(maxsize=1)
def _declared_templates() -> tuple[tuple[str, str, str], ...]:
    """连接器**自己声明**的模板指标 → `(连接器类名, 声明原文, 前缀)`。

    `"PE(TTM):{code}"` → 前缀 `"PE(TTM):"`。
    只取带占位符的条目：不带占位符的精确条目由 `StarChinextConnector` 这类
    连接器写成**相对键**（`"spot_summary"`），拿它们做"是否已登记"的比较是误报
    （见 `dynamic_loader.covering` 的同款教训）。
    """
    out: list[tuple[str, str, str]] = []
    for name, cls in sorted(_wired_connector_classes().items()):
        try:
            caps = cls().get_capabilities()
        except Exception:  # noqa: BLE001 capabilities 读不出来 → 由下面的断言报
            continue
        for raw in caps.get("indicators") or []:
            text = str(raw)
            if not _PLACEHOLDER_RE.search(text):
                continue
            prefix = text[: _PLACEHOLDER_RE.search(text).start()]  # type: ignore[union-attr]
            out.append((name, text, prefix))
    return tuple(out)


def _declared_code_prefixes() -> frozenset[str]:
    """连接器声明的**个股类**前缀（占位符恰为 `code`，形如 `XXX:{code}`）。"""
    return frozenset(
        prefix
        for _name, raw, prefix in _declared_templates()
        if _PLACEHOLDER_RE.search(raw).group(1) == "code"  # type: ignore[union-attr]
    )


def _declared_other_prefixes() -> frozenset[str]:
    """连接器声明的**其它占位符**前缀（`{指数名}` / `{行业名}` / `{YYYY-MM-DD}` …）。"""
    return frozenset(
        prefix
        for _name, raw, prefix in _declared_templates()
        if _PLACEHOLDER_RE.search(raw).group(1) != "code"  # type: ignore[union-attr]
    )


# ============================================================
# 面 ②：指标登记（读法复用 test_whitelist_coverage.py::_registered_ids）
# ============================================================


def _registered_ids() -> list[str]:
    """`configs/indicators.yaml` 里登记的全部指标 id（现读，不硬编码）。"""
    raw = yaml.safe_load(
        (_ROOT / "configs" / "indicators.yaml").read_text(encoding="utf-8"))
    out: list[str] = []

    def walk(node) -> None:
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            if isinstance(node.get("id"), str):
                out.append(node["id"])
            for value in node.values():
                walk(value)

    walk(raw)
    return sorted(set(out))


def _prefix_registered(prefix: str, registered: list[str]) -> bool:
    """`prefix`（形如 `XXX:`）在登记表里有对应条目吗。

    两种合法形态：登记了模板根（`资产负债率` ↔ `资产负债率:{code}`）或登记了
    该前缀下的具体指标（`ind:sw_third_pe_ttm:all` ↔ `ind:sw_third_pe_ttm:`）。
    """
    bare = prefix.rstrip(":")
    return any(r == bare or r.startswith(prefix) for r in registered)


# ============================================================
# 面 ④：分析层白名单（用于"豁免必须对分析层可见"这条前置条件）
# ============================================================


def _whitelist_agents_passing(indicator: str) -> list[str]:
    """复刻 `_filter_points_for_agent` 的子串匹配语义，返回放行它的 Agent。"""
    out: list[str] = []
    for agent_id, keywords in _AGENT_DATA_WHITELIST.items():
        lowered = tuple(str(k).lower() for k in keywords)
        low = indicator.lower()
        if any(k in low or k in indicator for k in lowered):
            out.append(agent_id)
    return out


# ============================================================
# 面 ⑥：**分析层之外**的消费者（"不该登记"这类豁免的可复现理由）
# ============================================================


def _out_of_analysis_layer_consumers(prefix: str) -> list[str]:
    """该前缀在**分析层之外**的真实消费者（全部是**行为/常量**判据）。

    刻意**不** grep 源码：本项目实测过"搜字符串确认改动在产物里"照样通过的假绿
    （规则在文件里、被后面的原始定义覆盖、根本不生效）。这里真调用构造函数、
    真读路由常量、真查新鲜度规则的档位。

    返回消费者描述列表；**空 = 这个前缀没有分析层之外的消费者**
    （那么"不该登记"的豁免理由就不成立了，见调用它的测试）。
    """
    prefix = str(prefix)
    hits: list[str] = []

    if prefix == "etf_close:":
        # `close_indicator()` 是**分流**函数：ETF 走 etf_close、个股走 stock_close。
        # 两个方向都断言 —— 只断言 ETF 那一支的话，有人把它改成硬编码常量也照样过。
        from src.intraday.sources import close_indicator

        if (close_indicator("510300") == "etf_close:510300"
                and close_indicator("600036") == "stock_close:600036"):
            hits.append("src.intraday.sources.close_indicator()（ETF/个股分流）")

    if prefix in ("index_close:", "etf_close:"):
        asset = "etf" if prefix == "etf_close:" else "index"
        from src.api.routes.backtest import _ASSET_QUOTE_PREFIX

        if _ASSET_QUOTE_PREFIX.get(asset) == prefix.rstrip(":"):
            hits.append(
                f"src.api.routes.backtest._ASSET_QUOTE_PREFIX[{asset!r}]")

        from src.core.data_freshness import _FREQ_BY_PREFIX

        daily = tuple(_FREQ_BY_PREFIX[0][0]) if _FREQ_BY_PREFIX else ()
        if prefix in daily:
            hits.append("src.core.data_freshness._FREQ_BY_PREFIX（日频档）")

    return hits


# ============================================================
# 豁免表（**已知在飞项**；每条都配了过期检查）
# ============================================================

#: ★ ③ planner 目录 / ② 登记表里的"**个股模板根**"（裸名字，如 `ROE`）：
#: `supports("ROE")` 为假是**正常的** —— 真实指标形如 `ROE:300308`。
#:
#: ⚠️ 这里**刻意不写清单**：判据从连接器**自己声明**的 `XXX:{code}`
#: 前缀派生（见 `_declared_code_prefixes`）。写死清单必然漂移 ——
#: 实测本仓库正在并发补这一族（20 条财务指标在几分钟内从"未登记"变成"已登记"），
#: 任何手写清单都会在下一分钟过期。
#: 非空性/锚定性由 `test_code_suffix_rule_is_anchored_in_connector_declarations` 盯住。

#: ★ ② 已登记、但 ① 采集侧**没有实现**（已知在飞项，不是"合法不可得"）。
#:
#: 每一条都是**真缺陷的显式登记**：登记表说"我有这个指标"，`supports()` 说
#: "我不认"。取数时报 `无连接器支持指标 …`。
#: 过期条件（见 `test_in_flight_exemptions_are_still_unobtainable`）：
#:   · 某个连接器开始 `supports()` 它 → 删（缺口已修）；
#:   · 它已不在登记表里 → 删（登记侧已改名/摘除）。
_KNOWN_IN_FLIGHT: dict[str, str] = {
    "净息差:{code}": (
        "**派生指标，尚未落事实表**（`CHG-0102`）：公式与求值流水线都已就绪"
        "（`configs/derived_indicators.yaml` + `src/domain/indicators/derive.py`，"
        "实测 600036/2026-06-30 = 0.8126%，输入 `利息净收入` 来自新浪利润表），"
        "但**结果是按需算的、没有写进 `fact_data_points`** ⇒ 事实表里查不到、"
        "也没有任何连接器 `supports('净息差:600036')` ⇒ 本表按"
        "「登记了却取不到」如实拦下（**不许靠「取不到就算了」**）。"
        "→ 修法：把派生结果落事实表（新作业或 A04 侧写入）+ 在 A11 的 "
        "system_prompt 里教这一族（§15 纪律：数据进得了上下文 ≠ 模型知道那是信号）。"),
    "stock_open:{code}": (
        "登记表 + A10 白名单 + `catalog/fetch_depth.py` 都声明了它，"
        "但 19 个连接器**没有一个人实现** `stock_open:`（实测 "
        "`supports('stock_open:601088')` 全为 False；日线链只实现 `stock_close:`）。"
        "→ 修法：要么在 `QUOTE_PREFIXES` 侧补 open/high/low/volume，"
        "要么承认只有 `stock_close:` 并从登记表摘掉这四个。"),
    "stock_high:{code}": "同上（`stock_high:` 无任何连接器实现）。",
    "stock_low:{code}": "同上（`stock_low:` 无任何连接器实现）。",
    "stock_volume:{code}": "同上（`stock_volume:` 无任何连接器实现）。",
    "存货周转": (
        "**同名漂移**：连接器实现的是 `存货周转率:{code}`"
        "（`akshare_connector._FIN_RATIO_INDICATORS` 的键 = 「存货周转率」），"
        "登记表写的是「存货周转」→ `supports('存货周转:601088')=False`。"
        "→ 修法二选一：登记表改名 `存货周转率`，或映射表加别名键。"),
    "cal:trade_day:flag": (
        "登记表说它是「交易所规则」的交易日标记，但**没有任何生产者**："
        "`calendar_store._KIND_METRICS` 只产出 unlock/earnings 两类"
        "（该模块 docstring 明写 macro/trade_day 不生成点）。"
        "→ 修法：要么在 calendar_store 里真的生成它，要么从登记表摘掉。"),
}

#: ★ ② 已登记、① 采集侧没实现，但**这是合法的**：生产者不是连接器。
#:
#: 豁免成立的前提必须能被**代码复现**（不是散文）：
#: `src/domain/intel/calendar_store.py` 的 `CAL_PREFIX`/`_KIND_METRICS`
#: 必须真的能拼出这个指标（见 `test_non_connector_producers_still_produce`）。
#: 过期条件：某个连接器开始支持它 → 删；生产者拼不出它了 → **红**。
_NON_CONNECTOR_PRODUCERS: dict[str, str] = {
    "cal:unlock:market_cap": "投资日历解禁链路落库（非 BaseConnector 路由）。",
    "cal:unlock:company_count": "同上。",
    "cal:unlock:top_stock_cap": "同上。",
    "cal:earnings:company_count": "投资日历财报披露链路落库（非连接器）。",
}

#: 生产 `cal:*` 的模块与它的判据常量（供上面那张表做代码级自证）。
_CALENDAR_PRODUCER_MODULE = "src.domain.intel.calendar_store"

#: ★ ① 连接器已实现、② 登记表**没登记**（取到了也无处引用）。
#:
#: ⚠️ 这些条目必须**逐个**删除：任何一条被登记后，对应测试立刻报"过期"。
#: 过期条件（见 `test_unregistered_prefix_exemptions_are_still_unregistered`）：
#: 该前缀在 `indicators.yaml` 里能匹配到条目 → 删。
#: 另配一条：该前缀必须仍被某个连接器 `supports()`（否则豁免理由不成立）。
#:
#: ★★★ 2026-09-28 第二十五轮：**已删除 8 条 —— 缺口是真修掉的，不是继续豁免**：
#:   · `股息率TTM:` `总市值:` `流通市值:` `换手率:` `量比:` `市销率:`
#:     → `configs/indicators.yaml` 补登记（`akshare_connector._QUANT_COLUMN_INDICATORS`
#:       的**整个列族，6 条一起**；只登记几条会让下次报障从另一个列名重新长出来）。
#:     ⚠️ **同时作废一条错误事实**：旧注释写「该表实测停在 2023-11-10（历史截面可查、
#:       当前值不可信）」—— **那句是错的**（同 `CHG-0059`）。停在 2023-11-10 的是
#:       `data/dev/moss_dev.db` 里的**化石副本**；权威副本在**共享行情仓**
#:       `data/quant/warehouse.db`，且持续更新到最近交易日。
#:       实测（as-of 2026-09-28，**会漂移，复核请现算**）：
#:         quant_daily_basic  rows = 15,426,153
#:                            trade_date = 20060104..20260928（最新就是当天）
#:                            600036 最新一条 dv_ttm=4.9606 / total_mv=1024934525184
#:       ⚠️ 行数与最新日期**不许当常量引用** —— `akshare_connector` 那段注释写过代价：
#:       写死的行数曾被抄到 6 处，其中一处据此决定"不走本地、改走网络自算股息率"，
#:       为一个**不成立的前提**长期付网络成本。
#:   · `idx_val:pe_ttm:` `idx_val:pb:`
#:     → 补登记为 `idx_val:pe_ttm:{指数名}` / `idx_val:pb:{指数名}`。
#:       旧理由说"落库口径走 `idx_val:snapshot:all`，所以不需要单独登记" ——
#:       但单指数入口 `fetch()` 产出的是**以自己为名**的 DataPoint
#:       （`index_valuation_connector.py` 的 `indicator=indicator`），
#:       所以它确实"取到了也无处引用"。
#:
#: 剩下的两条**不是遗漏，是"不该登记"**（理由可被代码复现，见
#: `_OUT_OF_ANALYSIS_LAYER_PREFIXES` 与
#: `test_out_of_analysis_layer_prefixes_still_have_consumers`），
#: 加上一条语义上不属于"数值指标元数据"的 `ind:penetration_report:`。
_UNREGISTERED_CONNECTOR_PREFIXES: dict[str, str] = {
    "index_close:": (
        "指数日线（日线链 6 个连接器全实现，实测在用）。**不该登记**："
        "实测 `passers == []` —— 没有任何分析层白名单放行它；消费者在"
        "**分析层之外**（`src/api/routes/backtest.py::_ASSET_QUOTE_PREFIX['index']`、"
        "`src/core/data_freshness._FREQ_BY_PREFIX` 日频档），"
        "它们不走 `_AGENT_DATA_WHITELIST` 那扇门 → 登记进来会被 "
        "`test_whitelist_coverage.py::test_every_registered_indicator_is_visible_to_some_agent` "
        "判成**僵尸登记**（把缺口从一张表搬到另一张表）。"
        "\n⚠️ 2026-09-28 更正（**旧口径作废，不删**）：本条目最初把 "
        "`src/mainline/sources.py::index_closes()` 举为消费者 —— **那是错的**。"
        "它直接 `SELECT ... FROM quant_index_daily` 读**表**"
        "（`src/mainline/sources.py:781-787`），**从不构造** `index_close:` "
        "这个 indicator id。拿它当锚点会变成**假绿**（锚在一个永远不会失效的东西上）。"
        "真锚点是上面那两处 + `etf_close:` 的 `close_indicator()`，"
        "且已配行为判据断言。**别再用 `index_closes()` 当这个前缀的锚点。**"
        "消费者是否仍然存在由 `test_out_of_analysis_layer_prefixes_still_have_consumers` 锚定。"),
    "etf_close:": (
        "ETF 日线（日线链 6 个连接器全实现，实测在用）。**不该登记**，理由同 "
        "`index_close:`；它的消费者锚点是 "
        "`src/intraday/sources.py::close_indicator('510300') == 'etf_close:510300'`"
        "**且** `close_indicator('600036') == 'stock_close:600036'` —— "
        "两个方向都断言：只断言 ETF 那一支的话，有人把它改成硬编码常量也照样通过。"
        "\n⚠️ 2026-09-28 同上更正：`src/mainline/sources.py::index_closes()` "
        "不是这两个前缀的消费者（它读表、不构造 indicator id）。"),
    "ind:penetration_report:": (
        "研报检索结果（标题列表，供 Agent 阅读），**不是时序数值指标**，"
        "登记进 `indicators.yaml` 会污染「数值指标元数据」这份契约 —— "
        "理由可被代码复现：`penetration_rate_connector.py::_fetch_reports()` "
        "**写死 `value=0.0`**（数值在 `extra.report_title` 里），"
        "登记后任何按 value 聚合/取最新的路径都会把 0.0 当观测值，"
        "正是 AGENTS.md「『没量到』≠『量到 0』」要防的那类假绿。"),
}

#: ★ 面 ⑥：**分析层之外**的消费者 —— 「不该登记」这类豁免的**可复现理由**。
#:
#: 为什么单列一张表：`_UNREGISTERED_CONNECTOR_PREFIXES` 里混着两类完全不同的东西，
#: 混在一起会让"理由"退化成散文：
#:   · A 类 **真缺口**（该登记却没登记）→ 归 `_KNOWN_IN_FLIGHT` / 直接修掉；
#:   · B 类 **不该登记**（消费者不经过分析层）→ **本表**。
#: B 类的豁免**只有在消费者仍然存在时才成立** —— 消费者被删掉，这张表就该红
#: （否则"理由"会像本项目实测过的那样腐烂成永久豁免）。
#:
#: 判据纪律：探测**用行为**，不用源码字符串 ——
#: "源码里出现过 `etf_close:`"会被注释与文档字符串骗过（本项目实测过同款假绿：
#: 搜索字符串确认改动"在产物里"，而 CSS 前置被后面的原始定义覆盖、根本不生效）。
_OUT_OF_ANALYSIS_LAYER_PREFIXES: frozenset[str] = frozenset({
    "index_close:",
    "etf_close:",
})

#: ★ ④ 分析层**声称会读**、但 ① 没人生产、② 也没登记的真缺口（已知在飞项）。
#:
#: ★★★ 2026-09-29 第二十五轮：**本表已退役（清空并删除配套的过期检查）**。
#:
#: 【为什么不再需要】原先这里登记着 `PMI` / `GDP` —— 当时的事实是
#: "A08 白名单写着它们、`fetch_depth`/`synonym_dict` 也当真实指标对待，
#: 但 19 个连接器没有一个 `supports()`，登记表也没有"。
#: 本轮实测（可复跑）：
#:     supports('PMI')       -> ['MacroExtraConnector']
#:     supports('GDP')       -> ['MacroExtraConnector']
#:     supports('GDP:同比')   -> ['MacroExtraConnector']
#: 生产者 `src/infrastructure/connectors/macro_extra_connector.py` 到位后，
#: 缺口从"没人生产"变成"**没登记**"（与 planner 目录那 6 条同一类）→
#: `indicators.yaml` + `INDICATOR_CATALOG` 已补齐 → 手写豁免表没有存在理由了。
#: **这就是它自己的过期检查在干的事**：不是"我们决定删"，是"前提没了"。
#:
#: 【替代它的是一条**派生**判据，不是又一张手写表】见
#: `test_whitelist_keywords_a_connector_accepts_are_registered` ——
#: "白名单里**任何**能被连接器 `supports()` 认下的关键词，都必须登记"。
#: 它不写清单、自动覆盖下一个 PMI/GDP 式的缺口，且这一轮就是它把
#: `PMI` / `GDP` 逼出来的。
#:
#: ⚠️ 如果你发现某个指标"取不到、又确实该有"，**不要**在这里重建手写表：
#: 先把生产者补上（连接器 + `supports()`），登记那一面自会由上面那条派生判据盯住。



#: ★ ③ planner 目录里有、② 登记表里**没有**的 id —— **本表已于
#: 2026-09-28 第二十五轮清空并退役**，连同它的过期检查
#: `test_catalog_not_registered_exemptions_are_still_unregistered` 一起删除。
#:
#: 【为什么不再需要】表里那 6 条**全部补登记了**（不是继续豁免）：
#:   `ind:sw_second_pe_ttm:all` / `ind:sw_third_dividend_yield:all`
#:     → `configs/indicators.yaml` 的申万行业估值段；
#:   `ind:penetration:人形机器人` / `:固态电池` / `:半导体国产替代` / `:HBM存储`
#:     → 渗透率段。
#: 于是 `test_indicator_catalog_ids_are_registered` 现在**无条件**成立，
#: 少一张豁免表 = 少一处会长霉的地方。
#:
#: 【如果你又想加豁免】先读完上面两条断言里的修法 —— 这个仓库实测过
#: "豁免表没有过期检查就退化成永久豁免"。真要加，**必须同时**把
#: `_CATALOG_NOT_REGISTERED` 与配套的过期检查一起加回来（照
#: `test_in_flight_exemptions_are_still_unobtainable` 的形状），
#: 并在 PRD / 台账登记你为什么不能直接修。
#: ⚠️ 留一句实测事实帮你判断：连接器 `supports()` 早就认那 6 条
#: （`SWIndustryValuationConnector` / `PenetrationRateConnector`），
#: 所以它们**从来不是"取不到"，只是"没登记"** —— 直接补登记永远比豁免便宜。


#: ★ 面 ⑤ 消费侧：**本地规则直接消费**の指标族 → 采集侧却无人生产。
#:
#: 消费侧的清单是**派生**的（见 `_consumed_rule_families()`：读
#: `compliance.logic._RATIO_RULES` 的键 + AST 扫 `_find_value(data_points, "X")`
#: 的字面量），不手写。这里的键 = 那个族关键词。
#:
#: 为什么这一面值得单列：A12 的爆雷等级完全由本地规则算（`evaluate_compliance`
#: 的返回值是**权威值**，LLM 不得修改）。族没人生产 → 4 条比率规则 + 存贷双高
#: **永不触发**，而 `compliance_level_calc` 会稳稳地给出"无"
#: —— 看起来像"这家公司没有合规风险"，实际是"这条规则从来没有输入"。
#:
#: 过期条件（见 `test_rule_consumed_families_expirations_are_current`）：
#:   · 某个连接器**声明/支持**了该族（或某个可取到的登记/目录指标含它）→ 删；
#:   · 消费侧不再消费该关键词 → 删。
#:
#: ## ★ 2026-09-29：6 条里有 5 条被**过期检查自己赶走**了
#:
#: `ComplianceFinConnector`（`src/infrastructure/connectors/compliance_fin_connector.py`）
#: 落地后，`商誉` / `质押` / `担保` / `货币资金` / `有息负债` 五族的
#: `capabilities` 命中 → 本表过期 → 按上面的过期条件**删除**。
#: 只剩 `关联交易`。
#:
#: ⚠️ **判据的边界（必须知道，否则会误以为这族"有保障"）**：
#: 这里的"有生产者"= 连接器**声明**了它，**不是**"每次都能取到行"。
#: 实测 `对外担保占净资产比` 在 600036/600519/000001 上都是 **0 点**
#: （窗口内没有担保公告 = 真结论，不是取数失败）。于是"声明了但常空"
#: 不会被这条判据发现 —— 它由 `compliance/logic.py` 的
#: `compliance_families_unmeasured` 在**结果里**如实暴露（「未量到」≠「无风险」）。
#: 两条判据分工：本表管"有没有人实现"，溯源三件套管"这一次量到没有"。
_KNOWN_UNPRODUCED_RULE_FAMILIES: dict[str, str] = {
    "关联交易": "A12 `_RATIO_RULES` 的比率旗标族；**比率口径在免费源上取不到**："
              "akshare 无任何关联交易接口（按函数名与 doc 文本搜 `关联` 命中 0 个）；"
              "东财公告大全虽有 `关联交易` 公告类型，但只有标题/日期/网址、"
              "**无金额字段**，算不出「占营收比」。"
              "⚠️ 不许退化成计数口径（规则按子串取值，`关联交易公告数:{code}` "
              "会被当成百分数去比 `> 30`）—— 理由与证据见连接器的 `_UNSUPPORTED_FAMILIES`。",
}

#: 上面那张表的**消费侧单一事实源**（声明式规则模块）。
_COMPLIANCE_LOGIC_MODULE = "src.domain.agents.analysis.compliance.logic"

# ============================================================
# 面 ③ / prompt：Agent 声称会读的指标族（显式映射表）
# ============================================================

#: Agent id → 类路径（用于读**它自己的** `system_prompt`）。
#: 覆盖性由 `test_prompt_family_table_covers_every_whitelisted_agent` 盯死：
#: 白名单里新增一个分析/行业 Agent，就必须在这里加一行，否则红。
_AGENT_CLASSES: dict[str, str] = {
    "A08_macro": "src.domain.agents.analysis.macro.agent:MacroAnalysisAgent",
    "A09_meso": "src.domain.agents.analysis.meso.agent:MesoAnalysisAgent",
    "A10_micro": "src.domain.agents.analysis.micro.agent:MicroAnalysisAgent",
    "A11_fin_risk": "src.domain.agents.analysis.risk.agent:RiskAnalysisAgent",
    "A12_compliance":
        "src.domain.agents.analysis.compliance.agent:ComplianceAnalysisAgent",
    "A13_tech": "src.domain.agents.industry.tech.agent:TechIndustryAgent",
    "A14_consumer":
        "src.domain.agents.industry.consumer.agent:ConsumerIndustryAgent",
    "A15_cyclical":
        "src.domain.agents.industry.cyclical.agent:CyclicalIndustryAgent",
    "A16_pharma": "src.domain.agents.industry.pharma.agent:PharmaIndustryAgent",
    # ★ 2026-09-29：兜底行业 Agent（`GenericIndustryAgent`）。
    #   它的行业名与关注指标都**按请求解析**，所以这里只声明它
    #   **声称会读的族**（申万截面 + 渗透率）—— 具体行业由运行时决定。
    "A20_generic_industry":
        "src.domain.agents.industry.generic.agent:GenericIndustryAgent",
}

#: ★★★ 本文件最关键的一张表：**Agent 在 prompt 里声称会读的指标族**。
#:
#: 结构：`agent_id -> ((prompt 原文出处, (指标探测串, ...)), ...)`
#:
#: 取值纪律：
#:   · 每一项的第一个元素必须是该 Agent `system_prompt` 的**原文子串**
#:     （`test_prompt_table_quotes_are_real` 会逐字校验 —— 表不许漂移成虚构）；
#:   · 第二个元素必须是**具体指标**（能用 `supports()` 直接问的串），
#:     不是中文概念 —— 判据只认机器可读的标识；
#:   · 只登记 prompt **真的声称**会读的族。没声称的不要加（那是扩权，不是修复）。
#:
#: 为什么需要它：这就是「ROE 那种缺口」的机器复现方式 ——
#: A11 曾经白纸黑字说要读 ROE/ROA/商誉/应收账款，而采集侧一个都没实现，
#: 且**不报错**（Agent 拿残缺数据给出自信结论）。
#:
#: 事件类声称（A12 的"诉讼/监管"）**故意不在本表**：它们走
#: `extracted_events`（信息层 A06）而不是 `data_points`，没有指标前缀可问
#: （其链路归 `test_compliance_*` 与白名单系列测试管）。
_PROMPT_DECLARED_FAMILIES: dict[str, tuple[tuple[str, tuple[str, ...]], ...]] = {
    # analysis/macro/agent.py 的 prompt 原文：
    #   "依据给定中国(CPI/PPI/M2/社融/PMI/GDP)与美国(CPI/核心CPI/非农/失业率/联邦利率/PCE)数据点"
    #   ⚠️ 2026-09-29 更正：出处原文原先**没有** PMI/GDP，而 `_AGENT_DATA_WHITELIST`
    #   早就放行它们（实测 PMI 224 期 / GDP 82 期都取得到）——
    #   数据进得了上下文，模型却不知道那是景气度信号。这与"数据落库了 Agent
    #   看不见"是**同一类缺陷的两个阶段**（前者卡在白名单，后者卡在 prompt）。
    #   现在 prompt 补齐，探测串与出处同步补齐（出处由本文件逐字自证）。
    "A08_macro": (
        # ★★ 2026-09-30（`CHG-0107`）：**"有哪些数据"的枚举从 prompt 移到数据侧**。
        #
        # > 旧口径：prompt 里写「中国(CPI/PPI/M2/社融/PMI/GDP)与美国(CPI/核心CPI/
        # >   非农/失业率/联邦利率/PCE)数据点」，本表逐字校验它 —— 立项理由是
        # >   "数据进上下文但 prompt 没提 ⇒ 模型不知道那是信号"。
        # > 废止理由（用户口径：「能精准解决的不写进 prompt」）：那份枚举
        # >   ① **会漂移**（美国那半边还是 `us_*` 时代的族名，而源停更后到货的是
        # >      `fred:*`）；② 它表达的是**数据事实**，正确落点是**数据本身**。
        # 现行：口径与族名随每行数据下发（`hint.macro_basis`，见
        #   `supervisor.macro_basis_for` / `_MACRO_BASIS`），prompt 只留语义判断。
        #   ⇒ 本表改为断言 **prompt 里那句"口径随数据下发"** 存在，且它对应的
        #   族在**数据侧**有口径；"模型认不认得出 PMI/GDP"由
        #   `tests/unit/test_macro_required_and_basis.py` 的
        #   `test_basis_covers_every_required_item`（现读 `_MACRO_BASIS`）守。
        ("口径与单位**随数据下发",
         ("CPI", "PPI", "M2", "社融", "PMI", "GDP",
          "fred:UNRATE", "fred:PAYEMS", "fred:DGS10")),
        # 同 prompt："问加息/降息而无 FedWatch：声明数据缺口"
        ("问加息/降息而无 FedWatch", ("fed:rate_prob:next",)),
        # unlock_teaching.py 的共享教学块（被拼进 A08 的 system_prompt）：
        #   "上下文里的 `cal:unlock:*` 是**限售解禁**"
        ("`cal:unlock:*` 是**限售解禁**", ("cal:unlock:market_cap",)),
    ),
    # analysis/meso/agent.py 的 prompt 原文：
    #   "流动性优先：先看两市总量阶段→三市成交额占比判风格→双创PE分位判冷热"
    "A09_meso": (
        ("先看两市总量阶段→三市成交额占比判风格→双创PE分位判冷热",
         ("mkt:turnover:total", "mkt:cybkcb:turnover:all", "mkt:cybkcb:val:all")),
        # 同 prompt："依据给定行业数据点（景气/库存/产能/价格）"
        ("依据给定行业数据点（景气/库存/产能/价格）",
         ("ind:sw_third_pe_ttm:all",)),
        ("`cal:unlock:*` 是**限售解禁**", ("cal:unlock:market_cap",)),
    ),
    # analysis/micro/agent.py 的 prompt 原文：
    #   "依据个股数据点与本地估值参考做个股深度研究。估值以本地计算为准"
    "A10_micro": (
        ("依据个股数据点与本地估值参考做个股深度研究",
         ("PE(TTM):" + _SAMPLE_CODE, "PB:" + _SAMPLE_CODE)),
        ("`cal:unlock:*` 是**限售解禁**", ("cal:unlock:top_stock_cap",)),
    ),
    # analysis/risk/agent.py 的 prompt 原文 + 它自己的本地判据：
    #   prompt："依据财务数据点与本地风险旗标识别造假征兆与偿债风险"
    #   `_prepare()` 读的正是指标「资产负债率」「流动比率」
    "A11_fin_risk": (
        ("依据财务数据点与本地风险旗标识别造假征兆与偿债风险",
         ("资产负债率:" + _SAMPLE_CODE, "流动比率:" + _SAMPLE_CODE)),
        # 同 prompt："上下文里的 `cal:unlock:*` 是**限售解禁**数据"
        ("上下文里的 `cal:unlock:*` 是**限售解禁**数据",
         ("cal:unlock:market_cap",)),
    ),
    # analysis/compliance/agent.py 的 prompt 原文：
    #   "依据财务比率、本地规则旗标与诉讼/监管事件研判"
    # （只取"财务比率"这半句 —— 事件类声称没有指标前缀，见本表 docstring）
    "A12_compliance": (
        ("依据财务比率、本地规则旗标",
         ("资产负债率:" + _SAMPLE_CODE, "流动比率:" + _SAMPLE_CODE)),
        ("`cal:unlock:*` 是**限售解禁**", ("cal:unlock:market_cap",)),
    ),
    # industry/*/agent.py 的 prompt 原文（四个行业 Agent 各自一句）
    "A13_tech": (
        ("资深科技行业分析师，熟悉半导体/消费电子/软件/AI产业链",
         ("ind:半导体销售额同比", "ind:芯片出货量同比")),
        ("`cal:unlock:*` 是**限售解禁**", ("cal:unlock:market_cap",)),
    ),
    "A14_consumer": (
        ("资深消费行业分析师，熟悉食品饮料/家电/零售/可选消费",
         ("ind:社会消费品零售总额同比", "ind:白酒批价(元/瓶)")),
        ("`cal:unlock:*` 是**限售解禁**", ("cal:unlock:market_cap",)),
    ),
    "A15_cyclical": (
        ("资深周期行业分析师，熟悉煤炭/有色/钢铁/化工/建材",
         ("PPI", "ind:动力煤价格(元/吨)", "ind:重点电厂煤炭库存(万吨)")),
        ("`cal:unlock:*` 是**限售解禁**", ("cal:unlock:market_cap",)),
    ),
    "A16_pharma": (
        ("资深医药行业分析师，熟悉创新药/器械/医疗服务/中药",
         ("ind:创新药IND申报数量(个)", "ind:医药行业PE(TTM)")),
        ("`cal:unlock:*` 是**限售解禁**", ("cal:unlock:market_cap",)),
    ),
    # ★ 2026-09-29：兜底行业 Agent。出处是 prompt 的**原文子串**（逐字校验）。
    #   它声称覆盖"没有专属行业 Agent 的行业"，并只用给定数据点说话；
    #   探测串取**它真的会读的两族**：申万行业估值截面 + 渗透率。
    #   ⚠️ 刻意**不**在这里列"银行/白酒/…"等具体行业指标 ——
    #   它的行业是运行时解析的，写死任何一个都会让这条判据变成假绿。
    "A20_generic_industry": (
        ("银行/非银金融/公用事业/交通运输/建筑等",
         ("ind:sw_third_pe_ttm:all", "ind:penetration:AI大模型应用")),
        ("`cal:unlock:*` 是**限售解禁**", ("cal:unlock:market_cap",)),
    ),
}


def _agent_system_prompt(agent_id: str) -> str:
    """读该 Agent 类上**真实的** `system_prompt` 字符串。"""
    spec = _AGENT_CLASSES.get(agent_id)
    assert spec, f"{agent_id} 不在 _AGENT_CLASSES 里（新增 Agent 必须补一行）"
    module_path, class_name = spec.split(":")
    cls = getattr(importlib.import_module(module_path), class_name)
    return str(cls.system_prompt)


def _exempted_unobtainable() -> frozenset[str]:
    """允许"没有连接器实现"的登记指标（两张表的并集）。"""
    return frozenset(_KNOWN_IN_FLIGHT) | frozenset(_NON_CONNECTOR_PRODUCERS)


# ============================================================
# 面 ⑤（消费侧）：本地规则**直接消费**的指标族（派生，不手写）
# ============================================================


def _consumed_rule_families() -> dict[str, str]:
    """`指标族关键词 -> 消费它的判据出处`（全部从代码现读）。

    两个来源：
      1. `compliance.logic._RATIO_RULES` 的**键**（比率旗标族）；
      2. AST 扫该模块源码里 `_find_value(data_points, "X")` 的**字符串字面量**
         （存贷双高族）—— 用 `ast` 而不是正则：正则会把注释/文档里的示例
         也算进来（本项目实测过这类假告警）。
    """
    module = importlib.import_module(_COMPLIANCE_LOGIC_MODULE)
    out: dict[str, str] = {}
    for keyword, _bands in module._RATIO_RULES:
        out[str(keyword)] = "_RATIO_RULES（合规比率旗标）"

    tree = ast.parse(Path(inspect.getfile(module)).read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "_find_value" and len(node.args) >= 2):
            continue
        arg = node.args[1]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            out.setdefault(arg.value, "_find_value（本地旗标取值）")
    return out


def _producers_of_family(keyword: str) -> list[str]:
    """谁能生产这一族：连接器**声明**了它，或可取到的登记/目录指标含它。"""
    hits: list[str] = []
    for name, cls in sorted(_wired_connector_classes().items()):
        try:
            caps = cls().get_capabilities()
        except Exception:  # noqa: BLE001 读不出能力描述 → 不算生产者
            continue
        texts = [str(x) for x in (caps.get("indicators") or [])]
        if any(keyword in text for text in texts):
            hits.append(f"{name}(capabilities)")
    probes = [e["id"] for e in INDICATOR_CATALOG] + _registered_ids()
    for probe in dict.fromkeys(probes):
        if keyword in probe and _obtainable_connectors(probe):
            hits.append(f"{probe}(可取)")
    return hits


def _obtainable_connectors(reg_id: str) -> list[str]:
    """该登记指标实际能不能被取到（返回连接器类名，空 = 取不到）。

    三种形态：
      1. 模板 id（含 `{code}`）→ 展开成具体指标再问 `supports()`；
      2. 裸 id → 直接问；
      3. 裸 id 但连接器**自己声明**了 `X:{code}`（登记表把模板根写成了裸名字）
         → 问 `X:<code>`。⚠️ 只认连接器声明的，不靠名字猜 ——
         否则「存货周转」这种同名漂移会被"猜"成通过。
    """
    if "{" in reg_id:
        return _supporting_connectors(_materialize(reg_id))
    hits = _supporting_connectors(reg_id)
    if hits:
        return hits
    if (reg_id + ":") in _declared_code_prefixes():
        return _supporting_connectors(reg_id + ":" + _SAMPLE_CODE)
    return []


# ============================================================
# ① 枚举清单本身可信（否则下面全部空跑 = 假绿）
# ============================================================


def test_connector_enumeration_is_complete():
    """★ 自证：连接器枚举非空，且路由表里的类全部解析得到。

    没有这条，`pkgutil`/AST 任一环节坏掉都会让下面所有断言**空跑通过**
    （vacuously true）—— 本项目把这类"假绿"列为头号失败模式。
    """
    classes = _connector_classes()
    assert len(classes) >= 10, f"只发现 {len(classes)} 个连接器类，枚举坏了"

    route_names = list(dict.fromkeys(_route_connector_names()))
    assert route_names, "从 runtime.py 路由表里一个连接器都没读到（AST 判据坏了）"

    wired = _wired_connector_classes()
    unresolved = [n for n in route_names if n not in wired]
    assert not unresolved, (
        f"路由表引用了连接器包外的类：{unresolved}\n"
        "→ 本文件的「实现面」取自连接器包，请把它纳入枚举（或说明为什么不该纳入）。"
    )
    assert len(wired) >= 10, f"路由表里只解析出 {len(wired)} 个连接器"


# ============================================================
# 断言 1：③ planner 目录 → ① 连接器
# ============================================================


def test_indicator_catalog_ids_are_connector_supported():
    """★★★ ③→①：`INDICATOR_CATALOG` 里每个 id 必须**至少有一个连接器
    `supports()` 它**。

    ## 为什么

    `INDICATOR_CATALOG` 是喂给 LLM 规划器的"可选指标清单"。清单里有、采集侧
    没有 = **planner 选得出、取数必然失败**，而失败表现为
    「无连接器支持指标 X」或干脆"数据缺失"—— 用户看到的是后者。

    ## 判据（`_obtainable_connectors`，**派生**，无手写清单）

    ① 精确 `supports(id)`；或
    ② id 是**个股模板根**（裸名字，如 `ROE`），且连接器**自己声明**了
       `ROE:{code}` 并 `supports("ROE:300308")`（见 `_declared_code_prefixes`）。

    ② 不是放水：`supports()` 对 `XXX:{code}` 的判定就是"这条路真的能取到"，
    而"连接器声明了它"是同一份映射表派生的（`_FIN_PREFIXES` /
    `_QUANT_COLUMN_INDICATORS`）—— `load_dataframe` 走的就是它。
    反例（`存货周转`）会正确地被判为缺口：登记名与连接器实现的
    `存货周转率:` 差一个字，② 不成立。
    """
    missing = [
        f"{e['id']}（无人 supports()，也不是任何连接器声明的 `{e['id']}:{{code}}` 模板根）"
        for e in INDICATOR_CATALOG
        if not _obtainable_connectors(e["id"])
    ]
    assert not missing, (
        "INDICATOR_CATALOG 里以下指标**没有任何连接器实现**"
        "（planner 选得出、采集侧取不到 → 必然「数据缺失」）：\n"
        + "\n".join(f"  · {m}" for m in missing)
        + "\n→ 修法：① 真的实现它（补连接器）；"
        "② 或从 INDICATOR_CATALOG 摘掉（planner 不该承诺取不到的东西）；"
        "③ 若它其实是模板根，让连接器的 capabilities 声明 `X:{code}`（单一真值源），"
        "不要在本测试里加豁免 —— 判据是派生的，豁免会立刻漂移。"
    )


def test_code_suffix_rule_is_anchored_in_connector_declarations():
    """★ 自证：上面那条用的"模板根"规则**锚定在连接器自己的声明上**，且非空跑。

    三件事（缺一个都会让 ③→① 变成假绿）：
      1. `_declared_code_prefixes()` 非空 —— 否则"模板根"这条路永远走不通，
         所有裸名字都会被判成缺口（或者反过来，有人会顺手放宽判据）；
      2. 每个声明的前缀**真的**被 `supports(prefix + "<code>")` 接受 ——
         否则"连接器声明了"这句就是空话（capabilities ↔ supports 必须一致）；
      3. 确实有 catalog/登记 id 依赖这条路（否则它是死代码）。
    """
    prefixes = _declared_code_prefixes()
    assert prefixes, (
        "没有任何连接器声明 `XXX:{code}` 形态的模板指标 —— "
        "③→① 的「模板根」判据会全部走空，请检查 capabilities 读取路径"
    )

    fake = [
        f"{p}（capabilities 声明了它，但 supports({p + _SAMPLE_CODE!r}) 为假）"
        for p in sorted(prefixes)
        if not _supporting_connectors(p + _SAMPLE_CODE)
    ]
    assert not fake, (
        "以下 `XXX:{code}` 前缀是**空声明**（能力描述与路由判据不一致）：\n"
        + "\n".join(f"  · {f}" for f in fake)
    )

    users = [
        e["id"] for e in INDICATOR_CATALOG
        if not _supporting_connectors(e["id"]) and _obtainable_connectors(e["id"])
    ] + [
        i for i in _registered_ids()
        if "{" not in i and not _supporting_connectors(i) and _obtainable_connectors(i)
    ]
    assert users, (
        "没有任何 id 依赖「模板根」这条路 —— 判据已成死代码，"
        "请检查 `_declared_code_prefixes()` 是否还在被真正使用"
    )


# ============================================================
# 断言 5：③ planner 目录 → ② 登记表
# ============================================================


def test_indicator_catalog_ids_are_registered():
    """★★ ③→②：planner 目录里的 id 必须是 `indicators.yaml` 的登记指标。

    ## 为什么

    没登记 → `catalog/registry.resolve()` 判 "miss" → SmartFetcher 走
    "未登记 → daily/24h 兜底" → **每次都联网**（最差路径），
    而且新鲜度/保留策略都管不到它。症状是"慢 + 数据看着有"，不报错。

    ★ 2026-09-28 第二十五轮：本断言**不再有任何豁免** —— 原先 6 条在
    `_CATALOG_NOT_REGISTERED` 里的条目全部补登记了（见该处的退役说明）。
    """
    registered = _registered_ids()
    assert registered, "indicators.yaml 一个指标都没读到 —— 路径或结构变了"

    missing = [
        e["id"] for e in INDICATOR_CATALOG
        if not _prefix_registered(e["id"], registered)
    ]
    assert not missing, (
        "planner 能选、但 `indicators.yaml` **未登记**的指标"
        "（SmartFetcher 判未登记 → 每次联网）：\n"
        + "\n".join(f"  · {m}" for m in missing)
        + "\n→ 补登记（frequency / freshness_hours / primary_source 必填）。"
        "\n→ 真的无法登记时，**不要**在这里放宽判据：照 "
        "`_KNOWN_IN_FLIGHT` / `_CLAIMED_BUT_UNPRODUCED` 的形状新开一张"
        "**带过期检查**的豁免表，并在 PRD / 台账登记理由。"
    )


# ============================================================
# 断言 2：② 登记表 → ① 连接器
# ============================================================


def test_registered_indicators_are_obtainable_or_registered_unobtainable():
    """★★★ ②→①：每个登记指标要么有连接器支持、要么在豁免表里**登记为不可得**。

    ## 为什么这是"数据在库里却取不到"的第一现场

    `indicators.yaml` 是**对外的承诺**（"我有这个指标，多久更新一次，从哪来"）。
    承诺了而采集侧没有实现 → 取数时 `ConnectorRouter._fetch_uncached` 抛
    「无连接器支持指标 X」；如果它同时已在库里（历史回填过），则表现为
    "数据在库里，但本次取不到新的"，**且不报错**。

    ## 判据与两条豁免路径

    · `_NON_CONNECTOR_PRODUCERS`：**合法**不含连接器（如 `cal:` 走投资日历
      落库）—— 理由必须能被代码复现（见 `test_non_connector_producers_still_produce`）。
    · `_KNOWN_IN_FLIGHT`：**真缺口**的显式登记（已知在飞项）。表中的每一条都必须
      仍然"对分析层可见"（④），否则它既取不到又没人看得见 —— 那是纯粹的僵尸登记。
    """
    registered = _registered_ids()
    assert registered, "indicators.yaml 一个指标都没读到"

    exempt = _exempted_unobtainable()
    gaps: list[str] = []
    for ind in registered:
        if _obtainable_connectors(ind):
            continue
        if ind in exempt:
            continue
        gaps.append(ind)

    assert not gaps, (
        "以下**已登记**指标没有任何连接器实现，也没有在豁免表里登记：\n"
        + "\n".join(f"  · {g}" for g in gaps)
        + "\n→ 三种合法归宿（必须显式选一个）：\n"
        "   ① 补连接器实现它；\n"
        "   ② 生产者不是连接器（如投资日历落库）→ 加进 `_NON_CONNECTOR_PRODUCERS`，"
        "并保证理由能被代码复现；\n"
        "   ③ 已知缺口 → 加进 `_KNOWN_IN_FLIGHT` 写明修法（它会被过期检查盯住）。\n"
        "⚠️ 不要靠「取不到就算了」——登记了却取不到，只会让下一次排查多一个假线索。"
    )

    invisible = [
        ind for ind in sorted(exempt)
        if ind in registered and not _whitelist_agents_passing(ind)
    ]
    assert not invisible, (
        "以下不可得指标**对分析层也不可见**（④ 白名单无人放行）"
        "—— 既取不到又看不见，属于纯僵尸登记：\n"
        + "\n".join(f"  · {i}" for i in invisible)
        + "\n→ 要么摘掉登记，要么说清谁来消费它。"
    )


def test_in_flight_exemptions_are_still_unobtainable():
    """★ 过期检查：`_KNOWN_IN_FLIGHT` 的条目一旦能被取到，就必须删掉。

    没有这条，"已知在飞项"会退化成**永久豁免** —— 缺口修好了，豁免还留着，
    下一个人以为这里本来就有问题（正是红灯纪律想防的失败模式）。
    两个过期方向：
      · 某个连接器开始 `supports()` 它 → 缺口已修 → 删条目；
      · 它已不在 `indicators.yaml` 里（改名/摘除）→ 删条目。
    """
    registered = _registered_ids()
    stale: list[str] = []
    for ind in _KNOWN_IN_FLIGHT:
        hits = _obtainable_connectors(ind)
        if hits:
            stale.append(f"{ind}（现在由 {hits} 支持）")
        elif ind not in registered:
            stale.append(f"{ind}（已不在 indicators.yaml 里）")
    assert not stale, (
        "以下 `_KNOWN_IN_FLIGHT` 条目**已过期**，请删除"
        "（并确认登记表 / 连接器两侧已对齐）：\n"
        + "\n".join(f"  · {s}" for s in stale)
    )


def test_non_connector_producers_still_produce():
    """★ 过期检查 + **代码级自证**：豁免的"生产者不是连接器"必须真的成立。

    判据（不读散文，读常量）：`calendar_store.CAL_PREFIX` + `_KIND_METRICS`
    必须真的能拼出被豁免的那个指标。生产者的 kind/metric 一改名，这里立刻红。
    另：一旦某个连接器开始支持它，豁免即过期（理由不再成立）。
    """
    module = importlib.import_module(_CALENDAR_PRODUCER_MODULE)
    prefix = str(module.CAL_PREFIX)
    kind_metrics = dict(module._KIND_METRICS)

    problems: list[str] = []
    for ind in _NON_CONNECTOR_PRODUCERS:
        hits = _obtainable_connectors(ind)
        if hits:
            problems.append(f"{ind}（豁免过期：现在由 {hits} 支持）")
            continue
        if not ind.startswith(prefix):
            problems.append(f"{ind}（不以生产者前缀 {prefix!r} 开头）")
            continue
        kind, _, metric = ind[len(prefix):].partition(":")
        produced = metric in tuple(kind_metrics.get(kind) or ())
        if not produced:
            problems.append(
                f"{ind}（生产者拼不出它：_KIND_METRICS[{kind!r}]="
                f"{kind_metrics.get(kind)!r} 不含 {metric!r}）")

    assert not problems, (
        "以下 `_NON_CONNECTOR_PRODUCERS` 豁免的理由**不再成立**：\n"
        + "\n".join(f"  · {p}" for p in problems)
        + "\n→ 要么删条目（缺口/实现已变），要么改到能复现它的那张表里。"
    )


# ============================================================
# 断言 3：prompt 声称 → ① 连接器（抓「ROE 那种缺口」）
# ============================================================


@pytest.mark.parametrize("agent_id", sorted(_PROMPT_DECLARED_FAMILIES))
def test_agent_prompt_declared_families_have_a_connector(agent_id: str):
    """★★★ prompt→①：Agent **声称会读**的每个指标，必须至少有一个连接器支持。

    ## 这就是用户报障的那类缺口

    实测现场：`A11_fin_risk` 的 prompt 说会读「ROE / ROA / 商誉 / 应收账款」，
    而采集侧只实现了 `资产负债率:` / `流动比率:` —— 一个都没实现。
    后果不是报错，是 Agent **拿着残缺数据给出自信结论**（少一个维度它自己不知道）。

    ## 实现方式（刻意不解析自然语言）

    读每个 Agent 类上真实的 `system_prompt`，用**显式映射表**
    `_PROMPT_DECLARED_FAMILIES`（原文出处 → 具体指标）逐个问 `supports()`。
    表里的每一项都有原文出处，且 `test_prompt_table_quotes_are_real` 会逐字校验。
    """
    problems: list[str] = []
    for quote, probes in _PROMPT_DECLARED_FAMILIES[agent_id]:
        for probe in probes:
            hits = _supporting_connectors(probe)
            if hits:
                continue
            if probe in _NON_CONNECTOR_PRODUCERS:
                continue
            problems.append(f"{probe}（出处：{quote!r}）")

    assert not problems, (
        f"{agent_id} 的 system_prompt 声称会读以下指标，"
        "但**没有任何连接器实现**（采集侧取不到 → 该维度静默缺失）：\n"
        + "\n".join(f"  · {p}" for p in problems)
        + "\n→ 三种合法归宿：① 补连接器实现；② 若生产者在连接器之外，"
        "登记进 `_NON_CONNECTOR_PRODUCERS`（理由必须能被代码复现）；"
        "③ 若这个族**确实不该声称**（prompt 写过头了），"
        "从 prompt 里删掉 —— 声称与实现必须有一边让步，不许两边都不动。"
    )


@pytest.mark.parametrize("agent_id", sorted(_PROMPT_DECLARED_FAMILIES))
def test_prompt_table_quotes_are_real(agent_id: str):
    """★★ 自证：映射表里的"出处"必须真的是该 Agent prompt 的**原文子串**。

    为什么必须有（AGENTS.md："自己的检查脚本必须先自证"）：
    映射表是手写的，一旦它漂移成"我以为 prompt 里写了什么"，上面那条断言
    就会守着一个**不存在的声称**，给出虚假的安全感。
    这条测试把表钉回 prompt 原文：prompt 改了而表没改 → 立刻红。
    """
    prompt = _agent_system_prompt(agent_id)
    missing = [
        quote for quote, _probes in _PROMPT_DECLARED_FAMILIES[agent_id]
        if quote not in prompt
    ]
    assert not missing, (
        f"{agent_id} 的 prompt 里找不到以下「出处」原文"
        "（映射表已漂移，请按当前 prompt 改写或删除该条）：\n"
        + "\n".join(f"  · {q!r}" for q in missing)
        + f"\n当前 prompt（前 400 字）：\n{prompt[:400]}"
    )


def test_prompt_family_table_covers_every_whitelisted_agent():
    """★ ④→本表：白名单里的每个 Agent 都必须在映射表里有条目。

    没有这条，新增一个分析/行业 Agent 时映射表不会报错 —— 它就**静默不检查**
    新 Agent 的声称，缺口又从新地方长出来（本项目实测过同款漏层）。
    """
    missing = sorted(set(_AGENT_DATA_WHITELIST) - set(_PROMPT_DECLARED_FAMILIES))
    extra = sorted(set(_PROMPT_DECLARED_FAMILIES) - set(_AGENT_DATA_WHITELIST))
    assert not missing, (
        f"白名单里有、但 `_PROMPT_DECLARED_FAMILIES` 没覆盖的 Agent：{missing}\n"
        "→ 补条目（并回答「它的 prompt 声称会读哪些指标」）。"
    )
    assert not extra, (
        f"映射表里有、但白名单里没有的 Agent：{extra}\n"
        "→ 白名单删了就该同步删映射表条目。"
    )


@pytest.mark.parametrize("agent_id", sorted(_AGENT_CLASSES))
def test_industry_watch_keywords_hit_obtainable_indicators(agent_id: str):
    """★★ ③'→①：Agent 用 `watch_keywords` 声明要看的词，命中的登记指标必须
    **至少有一个是采集侧真能取到的**。

    ## 为什么单独一条（现有护栏只查到白名单为止）

    `test_whitelist_coverage.py::test_industry_watch_keywords_survive_whitelist`
    验的是"声明 → **白名单**放行"。但白名单放行 ≠ 取得到 ——
    中间还隔着采集侧（本项目实测过「存货周转」这种：登记名与连接器实现的名
    差一个字，白名单照样放行，`supports()` 却全为 False）。

    ## 判据（**派生**，不手写清单）

    对每个 `watch_keyword`：在 `indicators.yaml` 里找命中的登记指标；
    若命中，则其中至少一个必须 `supports()` 得到。
    没命中任何登记指标的词**不参与**（那是"意图声明"，归
    `test_whitelist_coverage.py::_ALLOWED_PHANTOM_PREFIXES` 管）。
    """
    prompt = _agent_system_prompt(agent_id)
    assert prompt, f"{agent_id} 读不到 system_prompt"

    spec = _AGENT_CLASSES[agent_id]
    module_path, class_name = spec.split(":")
    cls = getattr(importlib.import_module(module_path), class_name)
    watch = tuple(getattr(cls, "watch_keywords", ()))

    if getattr(cls, "dynamic_industry", False):
        # ★ 2026-09-29：**兜底行业 Agent** 刻意不写死 `watch_keywords`
        #   （它的行业按每次请求解析，写死就等于只覆盖一个行业）。
        #   判据换成**更强**的一条：给它一个**具体行业**，
        #   它动态解析出的关注词必须至少命中一个**登记且可取到**的指标。
        #   —— 不是"放它一马"，而是"换一个更贴合它职责的问法"。
        from src.domain.agents.analysis.base import AnalysisPayload

        payload = AnalysisPayload(
            data_points=[], focus="600036", user_query="未来半年能否持有招商银行？")
        instance = cls.__new__(cls)          # 不跑 __init__（不需要 gateway）
        derived = tuple(instance._watch_keywords_for(payload))  # noqa: SLF001
        assert derived, (
            f"{agent_id} 声明自己按请求动态解析关注词，却解析出空集 —— "
            "那 `_skip_reason()` 会判「无本行业关注指标」并静默跳过 LLM"
        )
        registered = _registered_ids()
        hits = [
            i for i in registered
            if any(k in i for k in derived) and _obtainable_connectors(i)
        ]
        assert hits, (
            f"{agent_id} 动态解析出的关注词 {derived} 在 `indicators.yaml` 里"
            "**一个可取到的指标都命中不了**：\n"
            "→ 兜底行业 Agent 的价值就是「总有一个行业指标可用」；"
            "解析出的词取不到 = 它每次都会跳过 LLM"
        )
        return

    if not watch:
        # 分析层 Agent（`supervisor.ANALYSIS_AGENTS`）没有 `watch_keywords` ——
        # 那是**行业层**（`industry/base.py::_watched_points`）的机制，本条的语义
        # 只对行业 Agent 成立。这不是"本机能力不可用"的降级：分析层 Agent 的
        # 声称由 `test_agent_prompt_declared_families_have_a_connector` 逐条覆盖。
        assert agent_id in _ANALYSIS_AGENTS, (
            f"{agent_id} 既没有 watch_keywords，也不在 "
            "`supervisor.ANALYSIS_AGENTS` 里 —— 行业 Agent **必须**声明 "
            "`watch_keywords`（`industry/base.py::_skip_reason()` 靠它判"
            "「无本行业关注指标」），或者声明 `dynamic_industry = True`"
            "（按请求解析，见 A20）"
        )
        return

    registered = _registered_ids()
    dead: list[str] = []
    for kw in watch:
        matched = [i for i in registered if str(kw) in i or str(kw).lower() in i.lower()]
        if not matched:
            continue  # 意图声明：库里还没有这一族（归白名单系列测试管）
        if any(_obtainable_connectors(i) for i in matched):
            continue
        dead.append(f"{kw}（命中 {matched}，但没有一个能被取到）")

    assert not dead, (
        f"{agent_id} 的 watch_keywords 里有词**完全无法生效**"
        "（命中了登记指标，但采集侧一个都取不到）：\n"
        + "\n".join(f"  · {d}" for d in dead)
        + "\n→ 不补的后果不是报错：`industry/base.py::_skip_reason()` 会判"
        "「无本行业关注指标」并**静默跳过 LLM**，界面显示成"
        "「这个行业没什么可分析的」。"
    )


# ============================================================
# 断言 4：① 连接器 → ② 登记表（反向）
# ============================================================


def test_connector_declared_code_prefixes_are_registered():
    """★★★ ①→②：连接器 `supports()` 能认的**个股类**前缀（`XXX:{code}`）
    必须在 `indicators.yaml` 里登记。

    ## 为什么是反向（既有测试全是正向）

    正向测试问"我声明的有没有贯通"；这条问"**实现里有的，登记表认不认**"。
    没登记 → 取到了也无处引用：SmartFetcher 判未登记、新鲜度/保留策略管不到、
    planner 也不会选它。实测形状：AkShare 第二十四轮补齐了 `ROE:` / `ROA:` /
    `应收账款周转率:` 等一整族财务指标（`supports()` 现在全认），
    而登记表里一个都没有 —— 数据能取到，但**系统不知道它有**。

    ## 判据

    候选前缀取自连接器**自己声明**的 `get_capabilities()["indicators"]` 里
    `XXX:{code}` 形态的条目，并且必须真的被 `supports()` 接受
    （`test_connector_capabilities_agree_with_supports` 保证这条前提）。
    `_UNREGISTERED_CONNECTOR_PREFIXES` 里的条目允许豁免，且带过期检查。
    """
    registered = _registered_ids()
    missing = [
        prefix for prefix in sorted(_declared_code_prefixes())
        if not _prefix_registered(prefix, registered)
        and prefix not in _UNREGISTERED_CONNECTOR_PREFIXES
    ]
    assert not missing, (
        "以下**个股类前缀**连接器已实现，但 `indicators.yaml` 未登记"
        "（取到了也无处引用）：\n"
        + "\n".join(f"  · {p}" for p in missing)
        + "\n→ 在 `indicators.yaml` 补 `<前缀>:{code}` 条目（category: individual，"
        "frequency/freshness_hours/primary_source 必填）；"
        "或加进 `_UNREGISTERED_CONNECTOR_PREFIXES` 写清理由（会被过期检查盯住）。"
    )


def test_connector_declared_template_prefixes_are_registered():
    """★★ ①→②：非 `{code}` 占位符的模板前缀（`{指数名}` / `{行业名}` …）同理。

    与上一条同一个缺陷类别，只是占位符不同：连接器说"我能按指数名/行业名取"，
    而登记表里没有这一族 → 同样"取到了也无处引用"。
    共用 `_UNREGISTERED_CONNECTOR_PREFIXES`（一张表，一处删除）。
    """
    registered = _registered_ids()
    missing = [
        prefix for prefix in sorted(_declared_other_prefixes())
        if not _prefix_registered(prefix, registered)
        and prefix not in _UNREGISTERED_CONNECTOR_PREFIXES
    ]
    assert not missing, (
        "以下**模板前缀**连接器已实现，但 `indicators.yaml` 未登记：\n"
        + "\n".join(f"  · {p}" for p in missing)
        + "\n→ 同上：补登记，或加进 `_UNREGISTERED_CONNECTOR_PREFIXES` 并写清理由。"
    )


def test_unregistered_prefix_exemptions_are_still_unregistered():
    """★ 过期检查：`_UNREGISTERED_CONNECTOR_PREFIXES` 的条目必须仍然"未登记"。

    两个方向：
      · 前缀一旦在 `indicators.yaml` 里能匹配到 → 删条目（登记侧已补齐）；
      · 前缀不再被任何连接器 `supports()` → 删条目（理由不成立了）。
    """
    registered = _registered_ids()
    stale: list[str] = []
    for prefix in sorted(_UNREGISTERED_CONNECTOR_PREFIXES):
        if _prefix_registered(prefix, registered):
            stale.append(f"{prefix}（indicators.yaml 已登记）")
            continue
        probe = prefix + (
            _SAMPLE_CODE if prefix in _declared_code_prefixes() else "半导体")
        if not _supporting_connectors(probe):
            stale.append(f"{prefix}（已无连接器 supports({probe!r})）")
    assert not stale, (
        "以下 `_UNREGISTERED_CONNECTOR_PREFIXES` 条目**已过期**，请删除：\n"
        + "\n".join(f"  · {s}" for s in stale)
        + "\n→ 登记了就该删豁免；连接器不再支持它就该重新判断这是不是缺口。"
    )


def test_out_of_analysis_layer_prefixes_still_have_consumers():
    """★★ 过期检查 + **代码级自证**：「不该登记」这类豁免的理由必须仍然成立。

    ## 为什么需要（这条守的是"理由腐烂"）

    `_UNREGISTERED_CONNECTOR_PREFIXES` 里的 `index_close:` / `etf_close:`
    **不是缺口**，而是"**不该登记**"：实测 `passers == []`
    （复刻 `_filter_points_for_agent` 的子串语义，没有任何分析层白名单放行它们），
    消费者在分析层之外。所以豁免成立的**前提**是"那些消费者还存在" ——
    消费者被删掉之后，这条豁免必须红，否则它就变成了永久豁免
    （正是红灯纪律想防的那个失败模式）。

    ## 判据纪律：**用行为，不用源码字符串**

    "源码里出现过 `etf_close:`"会被注释与文档字符串骗过 —— 本项目实测过同款假绿
    （搜字符串确认改动"在产物里"，而 CSS 前置被后面的原始定义覆盖、根本不生效）。
    所以这里**真调用**构造函数、**真读**路由常量：

      · `src.intraday.sources.close_indicator("510300") == "etf_close:510300"`
        （ETF 走 `etf_close:` 而个股走 `stock_close:` —— 两个方向都断言，
         防止有人把它改成硬编码常量而判据照样通过）；
      · `src.api.routes.backtest._ASSET_QUOTE_PREFIX` 里 `index`/`etf` 两项；
      · `src.core.data_freshness._FREQ_BY_PREFIX` 的**日频档**含这两个前缀。

    另配两条**自证**（否则整条断言会空跑）：
      · `_OUT_OF_ANALYSIS_LAYER_PREFIXES` 必须非空；
      · 它的每个成员必须在 `_UNREGISTERED_CONNECTOR_PREFIXES` 里
        （不许出现"偷偷登记进豁免表却不写理由"的孤儿）。
    """
    assert _OUT_OF_ANALYSIS_LAYER_PREFIXES, (
        "`_OUT_OF_ANALYSIS_LAYER_PREFIXES` 为空 —— 本条断言会空跑（假绿）"
    )
    orphans = sorted(
        p for p in _OUT_OF_ANALYSIS_LAYER_PREFIXES
        if p not in _UNREGISTERED_CONNECTOR_PREFIXES
    )
    assert not orphans, (
        "以下前缀在 `_OUT_OF_ANALYSIS_LAYER_PREFIXES` 里、却不在 "
        f"`_UNREGISTERED_CONNECTOR_PREFIXES` 里（孤儿条目）：{orphans}\n"
        "→ 两处必须一致：豁免表写『不该登记』，这张表写『凭什么不该登记』。"
    )

    problems: list[str] = []
    for prefix in sorted(_OUT_OF_ANALYSIS_LAYER_PREFIXES):
        hits = _out_of_analysis_layer_consumers(prefix)
        if not hits:
            problems.append(
                f"{prefix}（分析层之外的消费者一个都探测不到 —— 豁免理由已腐烂）")

    assert not problems, (
        "以下前缀的豁免理由**不再成立**：\n"
        + "\n".join(f"  · {p}" for p in problems)
        + "\n→ 两条路（必须显式选一条）：\n"
        "   ① 消费者真的没了 → 从 `_OUT_OF_ANALYSIS_LAYER_PREFIXES` **和** "
        "`_UNREGISTERED_CONNECTOR_PREFIXES` 里一起删掉，并重新判断这个前缀"
        "该不该登记（登记前先量 `passers`）；\n"
        "   ② 消费者只是**改名/搬家**了 → 修 `_out_of_analysis_layer_consumers()`"
        "的锚点，别把锚点删掉了事。"
    )


# ============================================================
# 面 ④′：分析层**声称**会读 + 连接器**真能取到** → 必须登记（派生，无清单）
# ============================================================


def test_whitelist_keywords_a_connector_accepts_are_registered():
    """★★★ ④→①→②：白名单里**任何被连接器 `supports()` 认下的关键词**都必须登记。

    ## 这条为什么是**派生**的（而不是又一张手写豁免表）

    本文件原先有一张手写的 `_CLAIMED_BUT_UNPRODUCED`（登记 `PMI` / `GDP`）。
    那张表的问题是**它不会自己长大**：下一个"白名单声称了、连接器也能取、
    就是没人登记"的指标，它一个字都不会说 —— 于是缺口从新地方长出来。
    本轮 `MacroExtraConnector` 一落地，`PMI`/`GDP` 就从"没人生产"变成
    "没登记"，手写表只能靠人记得去改。

    这条判据不写任何清单：**遍历 `_AGENT_DATA_WHITELIST` 的每个关键词**，
    只要某个连接器 `supports()` 认它，就说明它**是一个真实可取到的指标**
    （不是 `A12` 的 `"诉讼"` 那种意图声明），那它就必须有 `indicators.yaml`
    的元数据（来源 / 频率 / 新鲜度 / 保留策略）—— 否则 SmartFetcher 判
    "未登记" → 每次联网，且保留策略管不到它。

    ## 判据

    对每个白名单关键词 `kw`：
      · `_supporting_connectors(kw)` 为空 → **跳过**（意图声明，归
        `test_whitelist_coverage.py::_ALLOWED_PHANTOM_PREFIXES` 管）；
      · 否则 `_prefix_registered(kw, registered)` 必须为真。
        ⚠️ 用 `_prefix_registered` 而不是精确相等：白名单里有 `"stock_close:"`
        这种**带冒号的前缀关键词**，而登记表里是模板 `"stock_close:{code}"` ——
        精确比较会把它们全报成缺口（本轮实测过的假告警形状）。

    ## 它就是本轮把 `PMI`/`GDP` 逼出来的那条判据

    ⚠️ **口径的由来（先量错、再收紧，别重犯）**：第一版用**精确相等**比较，
    结果报出 **7 条** —— 其中 **5 条是假告警**（`A10_micro` / `A13_tech` /
    `A14_consumer` / `A15_cyclical` / `A16_pharma` 的 `"stock_close:"`：
    它是个**带冒号的前缀关键词**，而登记表里是模板 `"stock_close:{code}"`）。
    改成 `_prefix_registered` 之后只剩 `{PMI, GDP}` 两条真缺口。
    **假告警比漏报更贵**（本项目实测过自写正则报 6 个假告警、差点去修没坏的东西），
    所以这条口径必须留在代码里，而不是留在我脑子里。
    """
    registered = _registered_ids()
    assert registered, "indicators.yaml 一个指标都没读到 —— 路径或结构变了"

    problems: list[str] = []
    for agent_id, keywords in sorted(_AGENT_DATA_WHITELIST.items()):
        for kw in keywords:
            kw = str(kw)
            hits = _supporting_connectors(kw)
            if not hits:
                continue  # 意图声明：连接器不认它，不是缺口
            if _prefix_registered(kw, registered):
                continue
            problems.append(
                f"{agent_id}: {kw!r}（连接器 {hits} 真的 supports() 它，"
                "但 `indicators.yaml` 没有对应条目）")

    assert not problems, (
        "以下**分析层声称要读、连接器也真能取到**的关键词**没有登记**"
        "（登记了取到了也无处引用；不登记则 SmartFetcher 判未登记 → 每次联网）：\n"
        + "\n".join(f"  · {p}" for p in problems)
        + "\n→ 补登记（frequency / freshness_hours / primary_source 必填）—— "
        "**不要**给这条判据加豁免：它一旦有豁免，下一个 PMI/GDP 就不会被发现。\n"
        "→ 若这个关键词其实**不该被登记**（不是数值指标、或口径不成立），"
        "那该改的是白名单本身（从 `_AGENT_DATA_WHITELIST` 里删掉它），"
        "而不是在这里放行。"
    )


# ============================================================
# 面 ① 内部一致性：capabilities ↔ supports（让"派生前缀"这套判据可信）
# ============================================================


def test_connector_capabilities_agree_with_supports():
    """★★ ①↔①：`get_capabilities()` 声明的模板指标必须真的被 `supports()` 接受。

    为什么需要（否则上面两条反向断言是**假绿**）：
    反向断言的候选前缀取自 capabilities。如果某个连接器的 capabilities 写了
    `XXX:{code}` 而 `supports()` 根本不认，那么：
      · 这个前缀永远不会被路由命中（能力描述是谎言）；
      · 而按"已实现"去要求登记，是在要求登记一个取不到的东西。
    所以两者必须一致 —— 不一致时要么改 capabilities，要么改 supports。
    """
    problems: list[str] = []
    for name, raw, _prefix in _declared_templates():
        try:
            probe = _materialize(raw)
        except _UnknownPlaceholder as exc:
            problems.append(f"{name}: {exc}")
            continue
        if not _supporting_connectors(probe):
            problems.append(
                f"{name}: capabilities 声明 {raw!r}，但 supports({probe!r}) 为假")
    assert not problems, (
        "以下连接器的**能力描述与路由判据不一致**：\n"
        + "\n".join(f"  · {p}" for p in problems)
        + "\n→ capabilities 是给人看的能力声明，supports 是路由判据；"
        "两者不一致时，监控/审计会以为有能力而路由永远不命中（或反之）。"
    )


# ============================================================
# 面 ⑤（消费侧）：本地规则消费的族 → ① 连接器
# ============================================================


def test_rule_consumed_families_have_a_producer():
    """★★★ 消费侧→①：**本地规则直接消费**的指标族必须有人生产。

    ## 为什么这一条是本文件里"最像用户报障"的那一条

    A12 的爆雷等级是**本地规则算出来的权威值**（`evaluate_compliance`，
    LLM 不得修改）。它的输入分两路：`extracted_events`（信息层，有链路）
    与 `data_points`（本地比率族）。后者实测**一族都没有生产者**：

        `_RATIO_RULES` 的 4 族（关联交易/商誉/质押/担保）
        + `_double_high_flag` 的 2 族（货币资金/有息负债）
        = 6 个关键词，19 个连接器的 capabilities 里**零命中**，
          登记表与 planner 目录里也一条都没有。

    后果不是报错，是 `compliance_level_calc` 稳稳地给出 **"无"**
    —— 用户看到的是"未见明显合规风险"，实际是"这条规则从来没有输入"。
    这与"A11 声称读 ROE 而采集侧没实现"是**同一类**缺陷，只是消费方从
    prompt 换成了规则代码。

    ## 判据

    族清单**派生**自消费侧代码（`_consumed_rule_families()`），
    生产者判定 = 连接器 capabilities 声明了它，或存在含它的、可取到的
    登记/目录指标。`_KNOWN_UNPRODUCED_RULE_FAMILIES` 里的条目允许豁免，
    且带双向过期检查。
    """
    problems = [
        f"{keyword}（消费出处：{source}）"
        for keyword, source in sorted(_consumed_rule_families().items())
        if not _producers_of_family(keyword)
        and keyword not in _KNOWN_UNPRODUCED_RULE_FAMILIES
    ]
    assert not problems, (
        "以下指标族被**本地规则消费**，但采集侧无人生产"
        "（规则永不触发，且不报错）：\n"
        + "\n".join(f"  · {p}" for p in problems)
        + "\n→ 修法：① 补采集（连接器 + 登记 + planner 目录 + 白名单，四层都要过）；"
        "② 或把该族从规则里删掉（规则消费一个取不到的族 = 假装有这道防线）；"
        "③ 已知缺口 → 加进 `_KNOWN_UNPRODUCED_RULE_FAMILIES` 写明修法。"
    )


def test_rule_consumed_families_expirations_are_current():
    """★ 过期检查：`_KNOWN_UNPRODUCED_RULE_FAMILIES` 必须"仍然既被消费、又无生产者"。

    两个方向都查（任意一边变了就该删条目）：
      · 消费侧不再消费该关键词 → 删（规则改了）；
      · 采集侧开始生产它 → 删（缺口修了）。
    没有这条，"已知在飞项"会退化成**永久豁免**。
    """
    consumed = _consumed_rule_families()
    stale: list[str] = []
    for keyword in sorted(_KNOWN_UNPRODUCED_RULE_FAMILIES):
        if keyword not in consumed:
            stale.append(f"{keyword}（消费侧已不再使用该关键词）")
            continue
        hits = _producers_of_family(keyword)
        if hits:
            stale.append(f"{keyword}（已有生产者：{hits}）")
    assert not stale, (
        "以下 `_KNOWN_UNPRODUCED_RULE_FAMILIES` 条目**已过期**，请删除：\n"
        + "\n".join(f"  · {s}" for s in stale)
        + "\n→ 缺口修好后必须删豁免，否则下一个人会以为这里本来就有问题。"
    )
