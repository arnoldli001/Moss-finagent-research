"""概念→成分股提纯：按概念挑最相关的股票（方向与旧版相反）。

## 为什么反转方向

旧版是**股票找概念**：对每只股票，在**全局候选全集**里按相关性取 top-N 题材。
问题出在"全局"——行业/旧体系板块与概念板块一起竞争名额，而行业指数与个股的
beta 天然更接近，走势相关性系统性更高，于是概念板块被挤掉。实测后果：
贵州茅台在 17 个题材里**保留 0 个**，白酒概念因此丢掉茅台与五粮液。

新版是**概念找股票**：每个概念各自从自己的成分股里挑，**不存在跨板块竞争**。

## 三步筛选与两条边界

    ① 前置过滤：剔除 ST、剔除总市值 < 30 亿（省 token，且这两类本就不该在主线里）
    ② 走势相关性：查本地 `ml_member_corr`（240 交易日）—— 免费、可复现，不问 LLM
    ③ 主营业务相关度：查 `ml_stock_theme` 缓存命中则复用，未命中才调 LLM

    最终分 = 0.6 × 走势相关性的横截面分位 + 0.4 × (主营分 / 100)

**为什么走势相关性不问 LLM**：相关性是可验证的数值，而 LLM 给的是"听起来合理"
的名字，无法核对；而且这个数据本地就有。主营业务判断才需要 LLM —— 那是读
公司主营描述判断产业关联，属文本语义任务。

## 两条边界（用户 2026-09 口径）

    keep_max = floor(A × 0.9)      A = 该概念的**原始成分股总数**
    keep_min = 10                  仅当 A >= 10 时生效
    A < 10                         → 忽略边界，保留全部通过前置过滤的

⚠️ 90% 的上限意味着**每个概念必然剔掉至少 10%**，即使所有成分股都高度相关。
这是刻意的口径（宁可留一点噪声也不要"提纯后等于没提纯"），但它会让
成分股少、相关性又普遍很高的概念（如某些 20 只以内的小概念）被强制砍掉几只。
若某概念 A=12，则 keep_max = 10 = keep_min，结果唯一为 10 只。

## 为什么写新表而不是覆盖 `ml_member_clean`

旧表的 `relevant` 是在"股票找概念 + 全局 top-N"口径下算的，**结论不可复用**
（它依赖当时的候选全集）。新表 `ml_member_pure` 并存，两者可直接对照，
也让"哪一套更好"这个问题能用数据回答，而不是靠记忆。
"""

from __future__ import annotations

import logging
import re
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from src.core.errors import BRIEF_DEFAULT, brief
from src.mainline.relevance import MIN_CORR_SAMPLES
from src.mainline.warehouse import open_warehouse

logger = logging.getLogger(__name__)

# 两条路径都从 registry 取（原先在这里各写一份，CHG-0069）。
from src.infrastructure.catalog.data_stores import store_rel as _store_rel

CACHE_DB = _store_rel("mainline_cache")
WAREHOUSE = _store_rel("warehouse")

#: 与 `RelevanceConfig` 保持一致：走势 60% / 主营 40%
CORR_WEIGHT = 0.6
#: 总市值门槛（元）。维持 30 亿（用户 2026-09 确认）
MIN_TOTAL_MV = 3e9
#: 保留上限比例 / 下限只数。
#:
#: 用户 2026-09 口径：**上限 70%**（原 90%），且**每个概念下优先保证不低于 10 只**。
#: 上限是"最多留 A 的 70%"，下限是"至少留 10 只" —— 两者冲突时**下限优先**
#: （`keep_bounds` 里 `max(high, KEEP_MIN)` 保证了这一点：A=12 时上限 8 会被
#: 抬到 10，即宁可超过 70% 也不能少于 10 只）。
KEEP_RATIO, KEEP_MIN = 0.7, 10

#: **走势相关性达到此值即直接归属该概念，不再问主营**（用户 2026-09 口径）。
#:
#: 实测效果（42499 个候选对）：可省掉约三分之一的主营判定。
#: ⚠️ 但**调用次数几乎不降** —— 因为调用是**按股票**分批的，
#: 而几乎每只股票都还剩至少一个 corr 未达标的候选题材，股票出不了队列。
#: 真正省下的是**每次调用的 token**（候选列表变短）。
#: 若将来改成"一只股票的高相关题材全中就直接跳过该股票"，才会真的省调用。
#:
#: ## 为什么是 0.55 而不是 0.60（用户 2026-09-21 调整）
#:
#: corr 分布：中位数 0.545 / P75 0.630 / P90 0.699。
#: 0.60 略高于中位数、略低于 P75；0.55 **正好压在中位数上**，
#: 即"相关性高于一半成分股"就直接归属。
#:
#: 下调的理由不是"相关性变差了"，而是：**走 corr 直通是免费的
#: （本地算 240 日 Pearson），而走主营判定要花 token**。
#: 门槛每降一点，就有更多对不必再问 LLM —— 这是**既省 token 又少一层
#: 模型不确定性**的方向。代价是可能放进"走势像但业务不像"的票，
#: 但它们要进入最终股池仍需通过 `keep_bounds()` 的比例裁剪，
#: 所以影响被限制在边界附近。
#:
#: 判定用 `>=`（含边界）：实测数据里没有恰好等于阈值的相关系数
#: （240 日 Pearson 有 15 位小数）。
CORR_DIRECT = 0.55

#: 主营业务分阈值：达到即纳入（用户 2026-09 确认 70）
BUSINESS_PASS = 70

#: `corr_map` 里表示"该股票无相关性数据（新股）"的哨兵值。
#:
#: 为什么不用 `None`：`dict[str, float]` 的取值类型统一成 float 后，
#: `select()` 里只需判一次哨兵，不必在每个使用点处理 Optional。
#: 取负数而不是 0 —— 0 是合法的相关系数（无相关），不能与"缺失"混为一谈。
CORR_MISSING = -1.0

PURE_TABLE = "ml_member_pure"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS {PURE_TABLE} (
    board_code TEXT NOT NULL, code TEXT NOT NULL,
    corr REAL, business_score REAL, final_score REAL,
    rank_in_board INTEGER NOT NULL DEFAULT 0,
    relevant INTEGER NOT NULL DEFAULT 0,
    -- 'corr' = 仅走势相关（免费）；'cache' = 主营分来自 ml_stock_theme；
    -- 'llm' = 本次新调 LLM。用来核算真实 token 消耗与缓存复用率。
    source TEXT NOT NULL DEFAULT '',
    reason TEXT NOT NULL DEFAULT '',
    refreshed_at TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (board_code, code)
);
CREATE INDEX IF NOT EXISTS idx_member_pure_rel
    ON {PURE_TABLE}(board_code, relevant);
"""


def is_st(name: str) -> bool:
    """名称是否像 ST 股。

    要求 ST 前缀后跟**非英文字母**：`STAR股份` 是正常公司名（Star 是品牌），
    早先版本用 `name.startswith("ST")` 判，把它误杀过。
    """
    text = str(name or "").upper().strip()
    for prefix in ("*ST", "ST", "SST", "S*ST"):
        if text.startswith(prefix):
            rest = text[len(prefix):]
            if rest and not rest[0].isascii():
                return True
            if rest and rest[0].isdigit():
                return True
    return "*ST" in text


@dataclass
class BoardPlan:
    """一个概念的提纯计划（干跑产物）。"""

    board_code: str = ""
    board_name: str = ""
    total: int = 0                 # A：原始成分股总数
    after_filter: int = 0          # 通过 ST / 市值过滤后的数量
    direct: int = 0                # corr >= CORR_DIRECT，直接归属（不问主营）
    kept: int = 0                  # 受两条边界约束后的保留数
    cache_hits: int = 0
    #: ⚠️ 这是**对数**（需要 LLM 判定的 (概念,股票) 对），**不是调用数**。
    #: 实际调用数 = "至少有一个此类对的**股票**数"（实测全量为 3989 次，
    #: 而对数是 27006）。因为调用按股票分批，一次调用覆盖该股票的全部候选。
    #: 字段名沿用 `llm_calls` 是历史原因，读的时候务必区分这两者 ——
    #: 混淆过一次，把 3989 说成了 12522。
    llm_calls: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"board_code": self.board_code, "board_name": self.board_name,
                "total": self.total, "after_filter": self.after_filter,
                "direct": self.direct,
                "kept": self.kept, "cache_hits": self.cache_hits,
                "llm_calls": self.llm_calls, "note": self.note}


@dataclass
class PlanReport:
    """全部概念的干跑汇总。"""

    boards: list[BoardPlan] = field(default_factory=list)
    gaps: list[str] = field(default_factory=list)

    @property
    def total_llm(self) -> int:
        return sum(item.llm_calls for item in self.boards)

    @property
    def total_pairs(self) -> int:
        return sum(item.after_filter for item in self.boards)

    def summary(self) -> dict[str, Any]:
        notes = Counter(item.note for item in self.boards if item.note)
        return {"boards": len(self.boards),
                "pairs_after_filter": self.total_pairs,
                "cache_hits": sum(item.cache_hits for item in self.boards),
                "llm_calls": self.total_llm,
                "kept_total": sum(item.kept for item in self.boards),
                "small_boards": notes.get("总数<10，忽略边界", 0),
                "capped_by_ratio": sum(
                    1 for item in self.boards
                    if item.note.startswith("被 ") and "上限截断" in item.note),
                "raised_to_min10": sum(
                    1 for item in self.boards if "放宽到" in item.note),
                "notes": dict(notes)}


#: 用户 2026-09 口径里"大概念"的阈值（>50 只时先按 corr 排序截断）。
#: 注意：`keep_bounds()` 的实际边界只由 `KEEP_RATIO`/`KEEP_MIN` 决定，
#: 这个常量目前只用于文档与提示，不参与计算 —— 保留是为了让口径可读。
LARGE_BOARD = 50


def build_tasks(conn: sqlite3.Connection, *, caps: dict[str, float],
                cache: dict[tuple[str, str], float],
                businesses: dict[str, str],
                boards: list[str] | None = None,
                ignore_cache: set[str] | None = None
                ) -> tuple[dict[str, list[str]], dict[str, dict[str, float]]]:
    """构造**按股票分组**的打分任务。

    返回 `(pending, corr_map)`：

        pending   `{股票代码: [需要判定的概念名, ...]}`  —— 每只股票一次调用
        corr_map  `{股票代码: {概念名: corr}}`           —— 供 `select()` 用

    ## 为什么按股票分组而不是按概念

    prompt 里最长的是**主营业务描述**（一次调用只该出现一次）。若按概念分组，
    一次调用要塞进 N 只股票的主营描述，token 会涨好几倍。
    旧实现（4995 次调用 / 4995 只股票）就是按股票分组的 —— 沿用同一形状。

    ## 为什么只有一部分进 pending

    三件事已经定了，不该再花 token：① `corr >= CORR_DIRECT` 直接归属；
    ② 新股（无 corr）直接归属；③ 主营分已有缓存。只有"有 corr 但 < 阈值、
    且无缓存、且有主营文本"的才需要新调 LLM。
    """
    wanted = set(boards) if boards else None
    force = set(ignore_cache or ())
    corr_map: dict[str, dict[str, float]] = {}
    pending: dict[str, list[str]] = {}
    for row in conn.execute("SELECT code, name FROM ml_board ORDER BY code"):
        code, name = str(row["code"]), str(row["name"])
        if wanted is not None and code not in wanted:
            continue
        corr = {str(r["code"]): float(r["corr"] or 0) for r in conn.execute(
            "SELECT code, corr FROM ml_member_corr WHERE board_code = ?",
            (code,))}
        for member in conn.execute(
                "SELECT code, name FROM ml_member WHERE board_code = ?", (code,)):
            stock = str(member["code"])
            if is_st(str(member["name"])):
                continue
            cap = caps.get(stock) or caps.get(stock.split(".")[0])
            if caps and (cap is None or cap < MIN_TOTAL_MV):
                continue
            value = corr.get(stock)
            corr_map.setdefault(stock, {})[name] = (
                value if value is not None else CORR_MISSING)
            if value is None or value >= CORR_DIRECT:
                continue                      # 新股 / 直接归属，不问 LLM
            if (stock, name) in cache and stock not in force:
                continue                      # 已有主营分，复用
            if stock not in businesses:
                continue                      # 没有主营文本，判不了
            pending.setdefault(stock, []).append(name)
    return pending, corr_map


def parse_scores(body: str, themes: list[str]) -> dict[str, float]:
    """解析 `{"s": [85, 70, ...]}` 为 `{题材名: 分数}`。

    位置化输出省 token 的代价是**必须严格对齐序号**：模型多给或少给一个数，
    后面的全部错位。所以只在**数量完全一致**时接受，否则返回空 dict
    让调用方按"解析失败"处理 —— 宁可重试，也不要把错位的结果写进库：
    错位不会报错，只会让一批股票被安到错误的概念上。

    ## 为什么要两层解析（实测 30% 的调用第一层就失败）

    实测 10 只股票里有 3 只第一层解析失败，原因不是格式约定不清，而是
    **模型偶尔把 JSON 包在解释性文字或代码块里**（推理层尤其如此）。
    所以第二层用正则把**第一个方括号数组**抠出来 —— 这是安全的降级：
    位置化的语义只依赖"数组长度 == 候选数"，抠出来的数组同样要过这个检查。
    """
    import json
    import re

    text = str(body or "").strip()
    if not text:
        return {}

    def _accept(values: Any) -> dict[str, float]:
        if not isinstance(values, list) or len(values) != len(themes):
            return {}
        out: dict[str, float] = {}
        for theme, raw in zip(themes, values, strict=True):
            try:
                score = float(raw)
            except (TypeError, ValueError):
                return {}
            out[theme] = max(0.0, min(100.0, score))
        return out

    # 第一层：规规矩矩的 JSON（可能被 ``` 包着）
    candidate = text
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        index = candidate.find("{")
        candidate = candidate[index:] if index >= 0 else candidate
    try:
        data = json.loads(candidate)
    except (TypeError, ValueError):
        data = None
    if isinstance(data, dict):
        got = _accept(data.get("s"))
        if got:
            return got
    elif isinstance(data, list):
        got = _accept(data)
        if got:
            return got

    # 第二层：抠第一个方括号数组（容忍前后夹带文字）
    match = re.search(r"\[[^\[\]]*\]", text, re.S)
    if match:
        try:
            return _accept(json.loads(match.group(0)))
        except (TypeError, ValueError):
            return {}
    return {}


async def score_pending(gateway: Any, pending: dict[str, list[str]], *,
                        businesses: dict[str, str], names: dict[str, str],
                        tier: str = "decision", concurrency: int = 10,
                        progress: Any = None, flush: Any = None,
                        flush_every: int = 100,
                        stats: dict[str, Any] | None = None,
                        cost_guard: Any = None
                        ) -> dict[tuple[str, str], float]:
    """并发调用 LLM，返回 `{(股票, 概念): 主营分}`。

    `tier` 默认 `decision`，**不是**配置里的 `relevance.llm_tier=reasoning`：
    这是**分类打分**任务，答案是整数数组，不需要思维链。`reasoning` 层会生成
    大段推理，在 4000 次调用的量级上是最主要的成本项。要更高质量可显式传
    `"reasoning"`。

    ## `flush`：长任务的分批落盘

    全量是 ~4000 次调用（一个多小时）。若只在**全部结束后**写库，中途任何
    崩溃/断网/被 kill 都会让已花的钱全部作废。传 `flush` 后每 `flush_every`
    只股票调用一次 `flush(当前结果字典)`，把已有分数写进缓存；下次运行时
    `build_tasks` 会跳过这些已缓存的对，**天然实现断点续跑**。

    `flush` 必须容忍被重复调用（传全量快照而不是增量），因为调用方通常
    做的是"整只股票覆盖写"。

    ## 成本护栏（`cost_guard`）

    传 `src.core.budget.ScriptCostGuard` 后**每开一批之前**检查一次；
    超限先 `flush` 把这一批之前的结果落盘，再抛 `ScriptCostError`。
    检查粒度是"批"（默认 10 只，即 0.2 元量级）而不是"只"：这里的分批
    `gather` 结构天然以批为单位，而多超支 0.2 元的代价远小于为它把
    并发编排改复杂。
    """
    import asyncio

    out: dict[tuple[str, str], float] = {}
    total = len(pending)
    state: dict[str, Any] = {"done": 0}
    bad_codes: list[str] = []
    lock = asyncio.Lock()

    async def one(stock: str, themes: list[str]) -> None:
        candidates = "\n".join(
            f"{index + 1}. {theme}" for index, theme in enumerate(themes))
        prompt = USER_PROMPT.format(
            name=names.get(stock, stock),
            business=(businesses.get(stock) or "")[:BUSINESS_CHARS],
            candidates=candidates, rubric=RUBRIC, focus=FOCUS_HINT,
            n=len(themes))
        errored = False
        try:
            # ⚠️ **必须设单次超时**：实测踩过一次 —— 没有超时时，一个挂住的
            # HTTP 调用会让它所在的整个 chunk 永远等下去（`asyncio.gather`
            # 要等全部完成），`done` 计数不再前进，于是**分批落盘也停了**。
            # 表现是"日志没有进度、存档时间戳不再更新"，而进程 CPU 很低、
            # 看起来像"在等网络"，与"正在慢慢跑"完全无法区分。
            # 超时按失败处理，该股票落进 `weak`，下次运行能重新判定 ——
            # 这比整批卡死好。
            resp = await asyncio.wait_for(
                gateway.complete(
                    tier, SYSTEM_PROMPT, prompt,
                    agent_id="mainline_member_pure", json_mode=True,
                    max_tokens=max_tokens_for(tier, len(themes)),
                    cache_ttl_hours=MEMBER_PURE_CACHE_TTL_HOURS),
                timeout=CALL_TIMEOUT_SECONDS)
            scores = parse_scores(resp.content or "", themes)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("主营打分超时 %s（%d 个候选，超过 %.0f 秒）",
                           stock, len(themes), CALL_TIMEOUT_SECONDS)
            scores, errored = {}, True
        except Exception as exc:  # noqa: BLE001 单只失败不该中断整轮
            logger.warning("主营打分异常 %s（%d 个候选）：%s: %s",
                           stock, len(themes), type(exc).__name__,
                           brief(exc, BRIEF_DEFAULT))
            scores, errored = {}, True
        async with lock:
            for theme, value in scores.items():
                out[(stock, theme)] = value
            state["done"] += 1
            # 失败必须是**可见的**：解析为空原先既不记日志也不计数，
            # 于是"输出上限不够 → 返回空字符串"表现为结果静默缺失。
            if errored:
                state["error"] = state.get("error", 0) + 1
            elif not scores:
                state["empty"] = state.get("empty", 0) + 1
            elif len(scores) < len(themes):
                state["partial"] = state.get("partial", 0) + 1
            else:
                state["ok"] = state.get("ok", 0) + 1
            if (errored or len(scores) < len(themes)) and len(bad_codes) < 20:
                bad_codes.append(f"{stock}({len(scores)}/{len(themes)})")
            if progress is not None and state["done"] % 50 == 0:
                progress(state["done"], total)
            if (flush is not None and state["done"] % max(flush_every, 1) == 0):
                try:
                    flush(dict(out))
                except Exception as exc:  # noqa: BLE001 落盘失败不该中断打分
                    logger.warning("分批落盘失败（继续打分）：%s",
                                   type(exc).__name__)

    queue = list(pending.items())
    width = max(int(concurrency), 1)
    for index in range(0, len(queue), width):
        if cost_guard is not None and cost_guard.over_cap():
            # 先把**已经拿到的**分数落盘再抛：分批落盘每 100 只一次，
            # 直接抛掉会丢掉最后不足 100 只的结果（真金白银买的）。
            if flush is not None and out:
                try:
                    flush(dict(out))
                except Exception as exc:  # noqa: BLE001 落盘失败不该掩盖中止
                    logger.warning("成本中止前的落盘失败：%s",
                                   type(exc).__name__)
            cost_guard.check_running()
        chunk = queue[index:index + width]
        await asyncio.gather(*(one(stock, themes) for stock, themes in chunk))
    if stats is not None:
        stats.update(state)
        stats["bad_codes"] = bad_codes
        if state.get("empty") or state.get("partial") or state.get("error"):
            logger.warning(
                "主营打分有失败：成功 %d / 空 %d / 不全 %d / 异常 %d；"
                "样例 %s", state.get("ok", 0), state.get("empty", 0),
                state.get("partial", 0), state.get("error", 0),
                bad_codes[:10])
    return out


def write_pure(conn: sqlite3.Connection, rows: list[tuple], *,
               replace: bool = False) -> int:
    """写 `ml_member_pure`（幂等 upsert）。

    `rows` 每项为 `(board_code, code, corr, business_score, final_score,
    rank_in_board, relevant, source, reason)`。
    """
    import datetime

    now = datetime.datetime.now().astimezone().isoformat(timespec="seconds")
    ensure_schema(conn)
    if replace:
        conn.execute(f"DELETE FROM {PURE_TABLE}")
    payload = [(board, code, corr, score, final, rank, 1 if rel else 0,
                source, reason, now)
               for (board, code, corr, score, final, rank, rel, source, reason)
               in rows]
    if not payload:
        conn.commit()
        return 0
    conn.executemany(
        f"INSERT INTO {PURE_TABLE}(board_code, code, corr, business_score,"
        " final_score, rank_in_board, relevant, source, reason, refreshed_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(board_code, code) DO UPDATE SET corr=excluded.corr,"
        " business_score=excluded.business_score,"
        " final_score=excluded.final_score,"
        " rank_in_board=excluded.rank_in_board,"
        " relevant=excluded.relevant, source=excluded.source,"
        " reason=excluded.reason, refreshed_at=excluded.refreshed_at", payload)
    conn.commit()
    return len(payload)


def keep_bounds(total: int, *, ratio: float = KEEP_RATIO) -> tuple[int, int]:
    """按用户口径算保留数量的上下界（含 A<10 的忽略情形）。

    返回 `(low, high)`；`A < 10` 时返回 `(0, total)` 表示"忽略边界"。

    **下限优先于上限**：`A=12` 时 `floor(12 × 0.7) = 8 < 10`，若让上限赢就会
    少于 10 只，违反"优先保证不低于 10 只"。所以用 `max(high, KEEP_MIN)`
    把上限抬到下限。代价是这类小概念实际保留比例会超过 70%（12 只里留 10 只
    = 83%），这是刻意的取舍。
    """
    if total < KEEP_MIN:
        return 0, int(total)
    high = int(total * ratio)           # floor
    return KEEP_MIN, max(high, KEEP_MIN)


def load_market_caps(warehouse: str | Path = WAREHOUSE) -> dict[str, float]:
    """`{code: 总市值(元)}`。仓库缺失时返回空（调用方降级为不做市值过滤）。"""
    path = Path(warehouse)
    if not path.exists():
        logger.warning("行情仓库不存在（跳过市值过滤）：%s", path)
        return {}
    # ⚠️ 必须走 `warehouse.open_warehouse` 这个**唯一入口**。
    # 这里原先自己 `sqlite3.connect(..., mode=ro)`，在 WAL/共享内存故障时
    # 直接抛 `disk I/O error`，把整次提纯重建打断（2026-09-21 实测，
    # 这是同一类 bug 第三次出现：sources → relevance → member_pure）。
    connection = open_warehouse(path, timeout=60.0)
    try:
        latest = connection.execute(
            "SELECT MAX(trade_date) AS d FROM quant_daily_basic").fetchone()["d"]
        out = {str(r["code"]): float(r["total_mv"] or 0)
               for r in connection.execute(
                   "SELECT code, total_mv FROM quant_daily_basic"
                   " WHERE trade_date = ?", (latest,))}
    finally:
        connection.close()
    return out


def normalize_theme(text: str) -> str:
    """题材名归一化（去掉括号内容与「概念/板块/指数/产业/行业」后缀）。

    ⚠️ 存在的理由：**板块名与 LLM 题材名的写法本来就不统一**。
    板块叫「氟化工**概念**」，而 `ml_stock_theme` 里同时存在「氟化工」与
    「氟化工概念」；`load_cache` 原来按 `(code, theme)` **精确**建键，
    `select` 又用 `board_name` 精确查 —— 于是「氟化工」那条判定**永远查不到**，
    该股票被当成"LLM 没判过"，转而靠 corr 入池。

    实测：在池纯化成员里 **460 条**是"归一化之后才命中"的，
    其中既有正向判定（芯片概念 688153 = 95 分）也有**反向判定**
    （AIGC概念 002315 = 58 分、光纤概念 002428 = 62 分）——
    后者本来该被上面那条"业务判定否决权"拦住，却因为 join 对不上而漏过。
    """
    body = re.sub(r"[（(].*?[)）]", "", str(text or "")).strip()
    return re.sub(r"(概念|板块|指数|产业|行业)$", "", body).strip()


def load_cache(conn: sqlite3.Connection) -> dict[tuple[str, str], float]:
    """可复用的主营分缓存 `{(code, theme): business_score}`。

    **同时建原始键与归一化键**（归一化键只在缺位时补），这样：
    既有行为（精确命中）一条不变，又多认出 460 条因写法差异而漏掉的判定。
    取"最大分"而不是"最后一条"：同一 `(code, 归一化题材)` 撞车时，
    按**最有利**的判定算 —— 这条规则是用来拦"明确不达标"的，
    不该因为撞车把达标的判定压掉。
    """
    out: dict[tuple[str, str], float] = {}
    normalized: dict[tuple[str, str], float] = {}
    for row in conn.execute(
            "SELECT code, theme, business_score FROM ml_stock_theme"):
        code = str(row["code"])
        score = float(row["business_score"] or 0)
        out[(code, str(row["theme"]))] = score
        key = (code, normalize_theme(str(row["theme"])))
        if key[1]:
            normalized[key] = max(normalized.get(key, float("-inf")), score)
    for key, score in normalized.items():
        out.setdefault(key, score)
    return out


def business_of(cache: dict[tuple[str, str], float], code: str,
                board_name: str) -> float | None:
    """查 `(股票, 板块)` 的主营分：先精确、再按归一化题材名兜底。"""
    if (code, board_name) in cache:
        return cache[(code, board_name)]
    return cache.get((code, normalize_theme(board_name)))


def plan(conn: sqlite3.Connection, *, caps: dict[str, float] | None = None,
         boards: list[str] | None = None) -> PlanReport:
    """干跑：算每个概念的 A、过滤后数量、缓存命中数与 LLM 调用数。

    **不写库、不调 LLM**。这是"要不要跑"的决策依据 —— 全量 LLM 判定是
    有成本的，先看清有多少对真的需要新调。
    """
    report = PlanReport()
    if caps is None:
        caps = load_market_caps()
        if not caps:
            report.gaps.append("行情仓库不可用，市值过滤未生效（30 亿门槛被跳过）")
    cache = load_cache(conn)

    rows = conn.execute(
        "SELECT code, name FROM ml_board ORDER BY code").fetchall()
    wanted = set(boards) if boards else None
    for row in rows:
        code, name = str(row["code"]), str(row["name"])
        if wanted is not None and code not in wanted:
            continue
        item = BoardPlan(board_code=code, board_name=name)
        members = conn.execute(
            "SELECT code, name FROM ml_member WHERE board_code = ?",
            (code,)).fetchall()
        item.total = len(members)
        if not item.total:
            item.note = "无成分股"
            report.boards.append(item)
            continue
        corr = {str(r["code"]): float(r["corr"] or 0) for r in conn.execute(
            "SELECT code, corr FROM ml_member_corr WHERE board_code = ?",
            (code,))}
        survivors: list[str] = []
        direct = 0
        for member in members:
            stock = str(member["code"])
            if is_st(str(member["name"])):
                continue
            cap = caps.get(stock) or caps.get(stock.split(".")[0])
            if caps and (cap is None or cap < MIN_TOTAL_MV):
                continue
            # ⚠️ **不因缺 corr 而剔除**（方案 A，用户 2026-09 确认）：
            # 缺相关性只意味着"不能走 corr 路径"，不代表不相关。
            # 实测这类是刚上市的新股（如长鑫科技 688825 只有 39 个交易日），
            # 它们仍可通过主营路径纳入。
            survivors.append(stock)
            value = corr.get(stock)
            if value is None:
                # 新股：直接归属，不进 LLM 队列（也没有主营文本可用）
                direct += 1
            elif value >= CORR_DIRECT:
                direct += 1              # 直接归属，不进 LLM 队列
        item.after_filter = len(survivors)
        item.direct = direct
        item.cache_hits = sum(
            1 for s in survivors
            if corr.get(s) is not None and corr[s] < CORR_DIRECT
            and (s, name) in cache)
        # 需 LLM 的对数：既不是直接归属/新股、也没有缓存主营分的
        item.llm_calls = sum(
            1 for s in survivors
            if corr.get(s) is not None and corr[s] < CORR_DIRECT
            and (s, name) not in cache)

        low, high = keep_bounds(item.total)
        if item.total < KEEP_MIN:
            item.kept = max(low, min(item.after_filter, high))
            item.note = "总数<10，忽略边界"
        else:
            item.kept = max(low, min(item.after_filter, high))
            if item.after_filter > high:
                item.note = f"被 {KEEP_RATIO:.0%} 上限截断"
            elif item.after_filter < low:
                item.note = f"不足 {KEEP_MIN} 只，放宽到 {KEEP_MIN}"
        report.boards.append(item)
    return report


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


#: 主营业务描述截断长度（字符）。
#:
#: ⚠️ **这个值踩过两次坑，两次都是"压太狠"，结论是：宁可多发，不要砍**。实测（10 只
#: 股票 A/B，对照旧跑 `ml_stock_theme` 的分数，平均绝对差越小越好）：
#:
#:     BUSINESS_CHARS=300                       平均差 44.2   亿道信息 10/10/10 ❌
#:     BUSINESS_CHARS=900                       平均差  6.7   亿道信息 65/70/75
#:     BUSINESS_CHARS=900 + "忽略定语"指示      平均差 25.8   亿道信息  5/5/5  ❌
#:     BUSINESS_CHARS=2400（本值）              平均差  5.6   亿道信息 65/70/75 ✅
#:
#: 两次翻车的共同点：**都是在砍"看起来没用"的内容**。
#: `001314 亿道信息`（主营消费电子、确有 XR 业务）的判断依据在描述后半段，
#: 300 字截断直接把它删了。而"哪一段是依据"事前无从得知 —— 多业务公司的
#: XR 业务常常只是一句经营范围，长得像"沿革"而不像"主营"。
#:
#: 描述本身很短（实测均值 747 字、最长 3486），**全文送进去的代价有限，
#: 而漏掉依据的代价是一次错误归属**。所以这里取 2400，让绝大多数描述完整送入。
BUSINESS_CHARS = 2400

#: 读主营描述的额外指示。**当前为空 —— 这是实测否决过的方案，不要重新加回**。
#:
#: 曾尝试用"语义压缩"替代"位置截断"：告诉模型**只关注产品名/服务名/行业名，
#: 忽略形容词、定语、荣誉资质、规模描述与公司沿革**。动机合理（描述里确实有
#: 大量"国内领先""两百多人研发团队"这类无信息量的修饰），但实测**反而更差**：
#: 平均差从 5.6 涨到 25.8，亿道信息从 65/70/75 掉到 5/5/5。
#:
#: 原因：那条指示的措辞把"经营范围里的一句 XR 业务"也划进了"公司沿革/规模描述"，
#: 模型照做就把它丢了。**"哪些内容是噪声"这个判断本身就不可靠** ——
#: 让模型自己读全文并取舍，比替它定规则更稳。
#:
#: 保留这个占位符是为了让"再试一次别的措辞"成本很低（`USER_PROMPT` 里有 `{focus}`），
#: 但**改之前必须跑 `scripts/validate_pure_prompt.py` 看平均差有没有变差**。
FOCUS_HINT = ""

#: 单次调用的**输出**上限（token）。
#:
#: ⚠️ **必须按 tier 区分** —— 这是实测踩出来的：
#: 推理模型**先用输出预算生成思维链**，再产出最终答案。把上限压到 512 时，
#: 思维链把预算吃光，最终答案是空的（实测 `reasoning` 层 10/10 返回 `''`），
#: 而**空回复不会报错**，只表现为"解析失败"，很容易被误判成提示词格式问题。
#:
#: 两者都远低于旧实现的 **32768**（那是最大的成本敞口）。
SCORE_MAX_TOKENS = 1024
#: 推理层的输出上限（固定值）。
#:
#: 实测需要多少（5 只曾因上限不足而失败的股票，各自的实际 `out_tok`）：
#:
#:     600509 |  8 候选 | 2465        000803 |  6 候选 | **7723**（最大）
#:     002853 |  4 候选 | 1497        300203 | 10 候选 | 3244
#:     600879 |  9 候选 | 448
#:
#: 取 **16384**（约为实测最大值的 2 倍）而不是原来的 32768。
#:
#: ⚠️ **缩小这个数不会省钱，只会把失败买回来。** 理由是
#: `max_tokens` 是**上限不是计费**：实测未触顶的调用在 4096 与 32768
#: 两个上限下 `out_tok` **完全相同**（1497/3244/448），模型生成到够用就停。
#: 而一旦上限不够，`out_tok` 正好等于上限、**返回空字符串** ——
#: 于是既付了上限那么多 token，又什么都没拿到，还得重跑。
#:
#: 所以上限的正确取值逻辑是"**覆盖最坏情况**"，不是"省 token"。
#: 若后续发现仍有触顶失败，应调**大**而不是调小。
SCORE_MAX_TOKENS_REASONING = 16384

#: 本模块 LLM 缓存条目的存活小时数（按调用覆盖全局 24h）。
#: 理由同 `relevance.py`：prompt 确定（只有公司名/主营文本/题材集/rubric），
#: 内容季度级才变；24h 会让"下一次全量提纯"把同一批 prompt 全部重新计费。
MEMBER_PURE_CACHE_TTL_HOURS = 24.0 * 30


#: 多少个候选算"一个放宽单位"（decision 层按候选数留余量用）
CANDIDATES_PER_UNIT = 10

#: 单次 LLM 调用的超时（秒）。
#:
#: 实测推理层最慢的调用约 120 秒（生成 7700 个思维链 token），180 秒留足余量。
#: **不设超时的后果**：一个挂住的 HTTP 调用会让它所在的整个 chunk 永远等下去
#: （`asyncio.gather` 要等全部完成），`done` 计数不再前进，分批落盘随之停止。
#: 现象是"日志没有进度、存档时间戳不再更新"，而进程 CPU 很低 ——
#: 与"正在慢慢跑"**完全无法区分**。所以超时不只是加快失败，更是让
#: "卡住"与"在跑"变得可分辨。
CALL_TIMEOUT_SECONDS = 180.0


def max_tokens_for(tier: str, candidates: int = 10) -> int:
    """按 tier 取输出上限。

    ## 为什么推理层的上限是**固定的大值**，而不是按候选数缩放

    实测（5 只曾解析失败的股票，各自给两个上限）：

        600509 |  8 候选 | max=4096  -> out_tok=4096（正好触顶）-> 返回空 ❌
                        | max=32768 -> out_tok=2465            -> 8/8 ✅
        000803 |  6 候选 | max=4096  -> out_tok=4096（触顶）    -> 返回空 ❌
                        | max=32768 -> out_tok=**7723**        -> 6/6 ✅
        002853 |  4 候选 | max=4096  -> out_tok=1497            -> 4/4 ✅
        300203 | 10 候选 | max=4096  -> out_tok=3244            -> 10/10 ✅
        600879 |  9 候选 | max=4096  -> out_tok=448             -> 9/9 ✅

    两条结论：

    1. **`out_tok == 上限` 就是"返回空"的充要条件** —— 预算全被思维链吃掉，
       最终答案没有产出。而空回复不报错，只表现为解析失败。
    2. **思维链长度与候选数几乎无关**（6 个候选要 7723，9 个候选只要 448）。
       所以"按候选数 ÷10 缩放"是**用错了变量** —— 它修好了 42/45 那批，
       但 6 个候选的 `000803` 照样失败。

    因此推理层取**固定大值**。`max_tokens` 是**上限不是计费**：
    实测未触顶的调用在两个上限下 `out_tok` 完全相同（1497/3244/448），
    说明放宽不让模型多生成 —— 只让被截断的那些能写完。
    而本来失败的那批**每次已经烧掉 4096 token 且产出为零**，
    所以修好它们的**额外成本约为零**。

    `decision` 层没有思维链，答案本身就是 `{"s":[...]}` 几十个 token，
    因此保留按候选数缩放（留余量，但不需要很大）。
    """
    import math

    text = str(tier or "").lower()
    if "reason" in text or "think" in text:
        return SCORE_MAX_TOKENS_REASONING
    factor = max(1, math.ceil(max(int(candidates), 1) / CANDIDATES_PER_UNIT))
    return SCORE_MAX_TOKENS * factor


#: 压缩版评分档位说明。旧版是 5 行、约 150 字；这里压成 1 行。
#: 档位含义必须保留（否则"40-59 有实质交叉但不是主营重点"这类判断会漂），
#: 但"只输出 JSON、不要解释"那几句可以合并进 system。
RUBRIC = ("0-19 无关(蹭概念) / 20-39 间接沾边(参股/意向) / 40-59 有实质交叉"
          "但非主营重点 / 60-79 主营明确涉及或直接上下游 / 80-100 主营核心业务")

SYSTEM_PROMPT = (
    "你是A股题材分类专家，判断个股主营业务与概念题材是否有**真实产业关联**。"
    "只看公司主营的产品/技术/服务是否属于该题材产业或处于其直接上下游；"
    "**不要**因题材名含通用词、股价走势、市场热度、板块归属而判为相关。"
    "按给定档位打分，只输出 JSON，不要任何解释。"
)

#: 候选题材**按位置编号**输出，模型只回分数数组 —— 不复述题材名、不给理由。
#: 旧格式 `[{"name":"题材名","score":85,"reason":"不超过15字依据"}]` 对
#: 8 个候选要约 250 token；本格式 `{"s":[85,70,...]}` 约 30 token（−88%）。
USER_PROMPT = """公司：{name}
主营：{business}
候选题材（按序号）：
{candidates}
档位：{rubric}
{focus}
只输出 JSON：{{"s": [第1个分数, 第2个分数, ...]}}，共 {n} 个整数，顺序与序号一致。"""


@dataclass
class MemberPick:
    """一个概念里某只成分股的去留决定。"""

    code: str = ""
    #: **None = 没有足够历史算相关性**（不是 0）。下游据此区分"未知"与"很低"。
    corr: float | None = None
    business_score: float | None = None
    final_score: float | None = None
    #: 'direct' = corr 达标直接归属 / 'business' = 主营分达标 /
    #: 'capped' = 相关性排名在 70% 之外被截断 / 'weak' = 两条都不达标
    decision: str = ""
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "corr": self.corr,
                "business_score": self.business_score,
                "final_score": self.final_score,
                "decision": self.decision, "note": self.note}


def select(corr: dict[str, float | None], *, board_name: str,
           cache: dict[tuple[str, str], float],
           total: int, business_pass: float = BUSINESS_PASS,
           corr_direct: float = CORR_DIRECT,
           keep_ratio: float = KEEP_RATIO,
           board_samples: int | None = None,
           business_veto: bool = False) -> list[MemberPick]:
    """按用户口径挑选一个概念的成分股（纯函数，不查库、不调 LLM）。

    ## 规则（用户 2026-09 口径）

    **成分股 > `LARGE_BOARD`(50) 的概念**：

        ① 先按 corr 从高到低排序，**只留前 70%**（`floor(A × 0.7)`）
        ② 留下来的里面，`corr >= CORR_DIRECT(0.60)` → **直接归属**
        ③ 其余按 `final_score = 0.6 × corr 分位 + 0.4 × 主营分/100` 降序，
           主营分达标的纳入

    **≤ 50 只的概念**：沿用 `keep_bounds`（上限 `floor(A×0.7)`、下限 10 只）。

    ## `business_veto` 是**默认关闭**的开关（Task 1）

        False（默认）  `corr >= corr_direct` → 直接归属，业务分不参与 ← 现行口径
        True           LLM 判过该题材且 < `business_pass` → 拦下

    默认值必须与现行 `ml_member_pure` 一致：Task 1 **实测跑过一次完整全流程后
    按预设判据回退了**（详见本函数内 `business_veto` 分支处的说明与
    `docs/MAINLINE_MINING.md` §16.74）。开关在 `configs/mainline.yaml` 的
    `relevance.business_veto`，命令行可用 `--business-veto` 临时覆盖。

    ⚠️ 打开它 **不是只改这一处**：池子变了，历史分数与拥挤度都得重算，
    否则会停在"新池子 + 旧分数"的混合态。

    ## `corr` 为 None 的股票：**新股，不做过滤、直接归属**（用户 2026-09 口径）

    `corr=None` 表示**该股票没有足够历史算相关性**（实测是刚上市的新股，
    只有 9~50 个交易日，`MIN_CORR_SAMPLES=60` 挡住了），**不是"相关性为 0"**。
    典型例子：长鑫科技 `688825` 在存储芯片 `886042.TI` 里——它是成分股、
    市值 3.6 万亿，但 2026-07-27 才上市，日线只有 39 行。

    **口径：直接归属，不做任何过滤**（`decision="new"`）。理由：

    - 板块的**成分股名单本身已经断言了归属**（交易所/数据商的分类），
      "没有价格历史"不是"不相关"的证据；
    - 这类只占 1232/44484 = 2.8%，放行不影响整体提纯力度；
    - 它**绕开了对主营业务描述的依赖** —— 实测 `stock_company` 对新股没有记录
      （长鑫科技抓不到主营文本），若要求走主营路径，这类票就永远进不来。

    ⚠️ 代价必须说清：新股**不受 70% 上限约束**（没有 corr 就没有名次可排），
    也不经过任何相关性/主营筛选，因此**它们是完全未经验证的一批**。
    `decision="new"` 这个独立取值就是为了让下游能把它们单独统计 ——
    将来若发现某概念的新股把噪声带进来，可以只回看这一类。

    刻意**不**把 `corr` 填成 0 或中位数：那是往表里写替代值，
    与"让 LLM 编一个 corr"性质相同 —— 下游无法区分测量值与填充值。

    ## ⚠️⚠️ `corr=None` 还有**第二种**成因：板块自己太年轻

    上面的说明只覆盖了"**股票**没有历史"。但只要**板块**的行情短于
    `MIN_CORR_SAMPLES`(60)，共同交易日就永远凑不够 60，于是该板块的
    **每一只**成分股都拿不到 corr —— 整块概念一次过滤都不做，
    却被记成 `decision="new"`，note 还写着"新股无价格历史"。
    实测（2026-09-21）：886111 玻璃基板（板块行情 58 日）56/56 只 corr=NULL；
    886112 MLCC概念（36 日）33/33 只 corr=NULL。

    这与"某只新股没有历史"是两件事：后者只占全表 0.4% 的行，
    前者是**整个概念的提纯完全没生效**，而且从表里看不出来。
    所以用 `board_samples` 把两者分开：

        板块行情 < MIN_CORR_SAMPLES  → `decision="board_young"`
                                      （仍然放行，但标注"本概念未过滤"）
        否则                        → `decision="new"`（个股新股）

    放行行为没变，变的只是**可审计性** —— 这是本项目反复踩的"静默 no-op"坑：
    一个声称做了过滤的流程，实际什么都没做，而下游分不出来。
    """
    low, high = keep_bounds(total, ratio=keep_ratio)
    # 有 corr 的按相关性排序；corr=None 的新股单独一组
    ranked = sorted(((c, v) for c, v in corr.items() if v is not None),
                    key=lambda kv: -kv[1])
    unranked = sorted(c for c, v in corr.items() if v is None)
    if total < KEEP_MIN:
        head = ranked                      # 忽略边界
    else:
        head = ranked[: max(high, 0)]      # 先按 corr 截断
    head_codes = {code for code, _ in head}

    # corr 分位在**该概念内部**算（与旧版"池内横截面"同口径，只是范围收窄到本概念）
    values = sorted(value for _, value in head)
    size = len(values) or 1

    def pct(value: float) -> float:
        return sum(1 for v in values if v <= value) / size

    out: list[MemberPick] = []
    for code, value in list(ranked) + [(c, None) for c in unranked]:
        score = business_of(cache, code, board_name)
        pick = MemberPick(code=code, corr=value, business_score=score)
        if value is None:
            if board_samples is not None and board_samples < MIN_CORR_SAMPLES:
                # 板块自己太年轻：**整块概念**都没算相关性，等于没过滤
                pick.decision = "board_young"
                pick.note = (f"板块自身行情只有 {board_samples} 个交易日"
                             f"（<{MIN_CORR_SAMPLES}），算不出相关性 → "
                             "本概念**未过滤、全量放行**")
            else:
                # 个股新股：直接归属，不过滤、不受 70% 上限约束
                pick.decision = "new"
                pick.note = "新股无价格历史（<60 交易日），按口径直接归属"
            out.append(pick)
            continue
        if code not in head_codes:
            pick.decision = "capped"
            pick.note = f"corr 排名在 {keep_ratio:.0%} 之外（前 {len(head)} 名）"
            out.append(pick)
            continue
        pick.final_score = (CORR_WEIGHT * pct(value)
                            + (1 - CORR_WEIGHT) * ((score or 0) / 100.0))
        # ⚠️ **业务判定的否决权**（Task 1，2026-09-22）—— 由 `business_veto` 控制，
        # **默认关闭**（见 `configs/mainline.yaml` 的 `relevance.business_veto`）。
        #
        # 原规则是「corr ≥ `corr_direct` 就**直接归属**」，业务分完全不看。
        # 后果：LLM 明确判过这个题材、且只给了 <70 分（不达标）的股票，
        # 只要股价跟着板块涨，照样被纳入 —— 实测在池成员里这类有 **2880 条**
        # （占已纳入的 23.5%），集中在国企改革 116 / 人形机器人 111 /
        # 储能 108 / 商业航天 95 / 人工智能 89。规则打开后：**判过且不达标
        # → 相关性不得覆盖**。
        #
        # ## 为什么默认关闭（不是漏做，是实测回退）
        #
        # 这条规则**跑过一次完整全流程**（提纯 + 903 天全量重打分）：
        # 聚合指标变好（20 日均值 +2.56%→+2.72%、大跌占比 7.4%→6.9%、
        # 真值漏报 4→3），但**启动集 FP/TP 在留出窗口变差（1.27→1.38）**；
        # 用户说过"重点是误报影响大"，工具里**事先写死**的判据是
        # 「③④变差就该回退」→ 按规则回退。详见 docs/MAINLINE_MINING.md
        # §16.74 / §16.75。
        #
        # ⚠️ 三种情况必须分清（这是本条规则最容易写错的地方）：
        #   `score is None`  → LLM **没判过**这个题材 → **不罚**（corr 照旧直通）。
        #                       实测这类占 corr 成员的 52%，把它们一并删掉
        #                       会把"只是没排上 LLM 候选"的真成分股误伤。
        #                       **这条与开关无关，恒成立。**
        #   `score >= 阈值`  → 判过且达标 → 直通（与业务路径同结论）。
        #   `score <  阈值`  → **判过且不达标** → 只有这种才拦，即 Task 1。
        if value >= corr_direct:
            if business_veto and score is not None and score < business_pass:
                pick.decision = "weak"
                pick.note = (f"corr {value:.3f} ≥ {corr_direct}，但 LLM 判过"
                             f"主营只有 {score:.0f} 分（<{business_pass}）"
                             "→ 业务判定优先，不纳入")
            else:
                pick.decision = "direct"
                pick.note = f"corr {value:.3f} ≥ {corr_direct}"
        elif score is not None and score >= business_pass:
            pick.decision = "business"
            pick.note = f"主营分 {score:.0f} ≥ {business_pass}"
        else:
            pick.decision = "weak"
            pick.note = ("主营分未达阈值" if score is not None
                         else "无主营分（需 LLM 判定）")
        out.append(pick)
    # 纳入的排前面（新股/年轻板块没有 final_score，排在直接/主营之后）
    include = ("direct", "business", "new", "board_young")
    inside = [p for p in out if p.decision in include]
    outside = [p for p in out if p.decision not in include]
    inside.sort(key=lambda p: (p.decision in ("new", "board_young"),
                               -(p.final_score or 0)))
    return inside + outside


def print_plan(report: PlanReport, limit: int = 15) -> None:
    summary = report.summary()
    print("=== 干跑汇总（未调 LLM、未写库）===")
    for key in ("boards", "pairs_after_filter", "cache_hits", "llm_calls",
                "kept_total", "small_boards", "capped_by_ratio",
                "raised_to_min10"):
        print(f"  {key:<20} {summary[key]}")
    if summary["llm_calls"]:
        rate = summary["cache_hits"] / max(summary["pairs_after_filter"], 1)
        print(f"  缓存复用率           {rate:.1%}")
    for gap in report.gaps:
        print(f"  ⚠️ {gap}")
    print()
    print(f"{'概念':<18}{'总数A':>6}{'过滤后':>7}{'保留':>6}{'缓存':>6}{'需LLM':>7}  说明")
    for item in sorted(report.boards, key=lambda x: -x.llm_calls)[:limit]:
        print(f"{item.board_name[:16]:<18}{item.total:>6}{item.after_filter:>7}"
              f"{item.kept:>6}{item.cache_hits:>6}{item.llm_calls:>7}  {item.note}")


def _main(argv: list[str] | None = None) -> int:
    import argparse
    import sys

    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except (ValueError, OSError):  # pragma: no cover
                pass
    parser = argparse.ArgumentParser(description="概念→成分股提纯（干跑）")
    parser.add_argument("--plan", action="store_true", help="干跑，不调 LLM")
    parser.add_argument("--limit", type=int, default=15)
    args = parser.parse_args(argv)

    conn = sqlite3.connect(CACHE_DB)
    conn.row_factory = sqlite3.Row
    try:
        report = plan(conn)
        print_plan(report, args.limit)
    finally:
        conn.close()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())

__all__ = ["BoardPlan", "PlanReport", "ensure_schema", "is_st", "keep_bounds",
           "load_cache", "load_market_caps", "plan", "print_plan"]
