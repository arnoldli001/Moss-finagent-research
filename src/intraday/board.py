"""关联板块上下文与大盘状态（市场情绪因子/情绪面板的数据来源）。

三个来源各有明确分工（全部为真实源，失败即记缺口）：
  1. 同花顺概念板块实时快照（stock_board_concept_info_ths）：
     板块涨跌幅、**涨跌家数**、成交额、资金净流入、涨幅排名
     —— 涨跌家数即「板块内 19/33 家上涨」的数据来源；
  2. 同花顺行业板块汇总（stock_board_industry_summary_ths）：
     行业级涨跌家数（概念板块缺家数时的替补）；
  3. 板块分时序列：东财概念分钟 → **自选同业等权合成**
     （无成分股接口时的真实替代口径）。

板块分时为何要「等权合成」兜底：
  东财 push2his 板块分钟接口在本机网络被阻断，且当前 akshare 版本无同花顺概念成分股函数，
  拿不到成分股清单就无法直接画板块分时。此时用「已配置的自选同业股票池」等权合成一条
  日内指数线（各股以 09:30 开盘价为基期归一后等权平均），是可解释、可复现的真实数据口径，
  并在返回体中明确标注 kind="synthetic_peers"，绝不冒充官方板块指数。
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import pandas as pd

from src.core.errors import (
    BRIEF_DEFAULT,
    BRIEF_TIGHT,
    brief,
)
from src.core.exceptions import DataFetchError
from src.intraday.config import IntradayConfig
from src.intraday.features import safe_float
from src.intraday.models import BoardSeries, BoardSnapshot, TrendPoint
from src.intraday.sources import SOURCE_LABELS, IntradayDataProvider

logger = logging.getLogger(__name__)

_INDEX_LABELS = {"000001": "上证指数", "399001": "深证成指", "000300": "沪深300"}

# 板块名里的通用后缀：用户常写「CPO概念」，同花顺官方名却是「共封装光学(CPO)」。
_GENERIC_SUFFIXES = ("概念", "板块", "指数", "行业", "产业链")


def _normalize_board_name(text: str) -> str:
    """去空白/标点/大小写差异，便于板块名比对。"""
    return "".join(
        char for char in str(text).lower()
        if char.isalnum() or "\u4e00" <= char <= "\u9fff")


def _ascii_tokens(text: str) -> list[str]:
    """板块名里的 ASCII 片段（CPO/PCB/PET…），长度≥2 才算有辨识度。"""
    import re

    return [token.lower() for token in re.findall(r"[A-Za-z]{2,}", str(text))]


def match_board_name(query: str, names: list[str]) -> str | None:
    """把用户写的板块名匹配到数据源的官方名（匹配不到返回 None）。

    实测动机：用户把 300308 绑定到「CPO概念」，但同花顺官方名是
    「共封装光学(CPO)」—— 直接拿用户输入去查会 IndexError（官方名列表里没有），
    再退到新浪（只有 175 个粗概念板块，根本没有 CPO/PCB/铜箔）也必然失败，
    结果板块情绪/板块排行两个维度直接变成不可用。逐级匹配如下：

      1. 归一化后完全相同（PCB概念 → PCB概念）；
      2. 去掉「概念/板块/指数/行业」后缀后相同；
      3. ASCII 片段命中（CPO概念 → 共封装光学(CPO)）：按「查询字符覆盖率」打分，
         **只在最优解唯一时采纳**，避免把名字随便套到别的板块上；
      4. 双向包含关系且唯一。

    匹配不到就返回 None（宁可不给分，也不给错板块）。
    """
    if not names:
        return None
    normalized = _normalize_board_name(query)
    if not normalized:
        return None

    buckets: dict[str, list[str]] = {}
    for name in names:
        buckets.setdefault(_normalize_board_name(name), []).append(name)
    if normalized in buckets:
        return buckets[normalized][0]

    stripped = normalized
    for suffix in _GENERIC_SUFFIXES:
        if stripped.endswith(suffix) and len(stripped) > len(suffix):
            stripped = stripped[: -len(suffix)]
            break
    if stripped and stripped in buckets:
        return buckets[stripped][0]

    query_tokens = _ascii_tokens(query)
    if query_tokens:
        hits = [name for name in names
                if all(token in name.lower() for token in query_tokens)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            query_chars = set(stripped or normalized)
            scored = sorted(
                ((len(query_chars & set(_normalize_board_name(name))), len(name),
                  name) for name in hits),
                key=lambda item: (-item[0], item[1]))
            if scored[0][0] > scored[1][0]:
                return scored[0][2]

    if stripped:
        contained = [name for name in names
                     if stripped in _normalize_board_name(name)
                     or _normalize_board_name(name) in stripped]
        if len(contained) == 1:
            return contained[0]
    return None


def parse_concept_info(frame: Any) -> dict[str, Any]:
    """同花顺概念板块快照（项目/值两列表）→ 结构化字典（纯函数，便于单测）。"""
    if frame is None or len(frame) == 0:
        return {}
    if "项目" not in frame.columns or "值" not in frame.columns:
        return {}
    raw = {
        str(row["项目"]).strip(): row["值"]
        for _, row in frame.iterrows()
    }
    result: dict[str, Any] = {"raw": raw}
    result["open_price"] = safe_float(raw.get("今开"))
    result["prev_close"] = safe_float(raw.get("昨收"))
    result["high"] = safe_float(raw.get("最高"))
    result["low"] = safe_float(raw.get("最低"))
    result["amount"] = safe_float(raw.get("成交额(亿)"))
    result["net_inflow"] = safe_float(raw.get("资金净流入(亿)"))
    result["rank"] = str(raw.get("涨幅排名") or "").strip() or None
    change_raw = raw.get("板块涨幅")
    result["change_pct"] = _parse_pct(change_raw)
    up, down = _parse_breadth(raw.get("涨跌家数"))
    result["up_count"], result["down_count"] = up, down
    if up is not None and down is not None and (up + down) > 0:
        result["breadth"] = up / (up + down)
    return result


def _parse_pct(value: Any) -> float | None:
    """'0.58%' / 0.58 → 0.58。"""
    if value is None:
        return None
    text = str(value).strip().replace("%", "").replace(",", "")
    return safe_float(text)


def _parse_breadth(value: Any) -> tuple[int | None, int | None]:
    """'119/115' → (119, 115)。"""
    if value is None:
        return None, None
    text = str(value).strip()
    if "/" not in text:
        return None, None
    left, _, right = text.partition("/")
    up = safe_float(left.strip())
    down = safe_float(right.strip())
    return (
        None if up is None else int(up),
        None if down is None else int(down),
    )


def _series_to_points(frame: pd.DataFrame, *, price_column: str,
                      ts_column: str = "ts") -> list[TrendPoint]:
    points: list[TrendPoint] = []
    if frame is None or len(frame) == 0:
        return points
    for _, row in frame.iterrows():
        price = safe_float(row.get(price_column))
        if price is None:
            continue
        points.append(TrendPoint(
            ts=str(row.get(ts_column)),
            price=price,
            avg_price=safe_float(row.get("avg_price")),
            volume=safe_float(row.get("volume")) or 0.0,
        ))
    return points


class BoardContextProvider:
    """关联板块快照 + 板块分时 + 大盘状态。"""

    def __init__(self, config: IntradayConfig,
                 data: IntradayDataProvider) -> None:
        self._config = config
        self._data = data
        # 东财板块分钟接口失败冷却：该接口被阻断时 akshare 内部会走完整分页重试，
        # 单次耗时约 13s（实测），而它在链上优先级最高 → 每次快照都白等 13s。
        # 与行情源用同一套冷却窗口口径。
        self._em_cooldown: dict[str, float] = {}
        # 板块名解析（用户写法 → 同花顺官方名）：进程内缓存 + 官方名列表只取一次
        self._concept_name_cache: dict[str, str] = {}
        self._ths_names: list[str] | None = None
        self._ths_names_cooldown_until = 0.0
        # 同花顺快照成功结果的小缓存：一次子进程调用约 0.3~1s，而 WS 每 15s 推一次，
        # 不缓存就等于每 15s 为每个板块起一个子进程。
        self._snapshot_cache: dict[str, tuple[float, BoardSnapshot]] = {}
        # 正在后台刷新的板块名（去重：同一个板块不会堆出多个并发子进程）
        self._refresh_inflight: set[str] = set()
        # 冷取失败后的重试窗口：冷取失败不写缓存，若不设窗口，每个请求都会再排一个
        # 注定失败的子进程（顺序请求下 `_refresh_inflight` 拦不住）。
        self._cold_retry_after: dict[str, float] = {}
        # 保留后台刷新任务的强引用，否则任务可能被 GC 掉（asyncio 的经典坑）
        self._refresh_tasks: set[Any] = set()
        # 板块**分时序列**缓存：单次 22 秒（akshare 每次都重拉全市场板块名单），
        # 而它是 5 分钟 K 线 —— 不缓存等于每次刷新重付一遍。
        # TTL 取板块快照 TTL 的 2 倍且不低于 120 秒：5 分钟线本来就不会更快变。
        self._series_cache: dict[str, tuple[float, BoardSeries]] = {}
        self._series_inflight: set[str] = set()
        self._series_ttl = max(120.0, float(self._config.data.board_cache_ttl) * 2)
        # 东财"整条链路被阻断"的截止时间：实测本机 push2 域名稳定不可达，
        # 识别到连接级失败后按小时级停用，避免每轮冷却到期都空转 17 秒重试。
        self._em_blocked_until = 0.0
        self._em_block_window = 3600.0
        # 单个板块名的失败冷却：名字写错时会**每次快照都重跑一遍注定失败的子进程**
        # （实测 3 分钟内刷了 14 次 IndexError），冷却后直接复用上次的失败原因。
        self._fail_cooldown: dict[str, tuple[float, str]] = {}

    # ---------- 板块实时快照 ----------

    def _schedule_refresh(self, name: str, kind: str = "concept") -> None:
        """后台刷新一个板块的缓存（不阻塞调用方，同名去重）。

        为什么需要它：板块冷取要 3~7 秒（失败重试最长 25 秒），若让它留在请求路径上，
        自选 7 只票的刷新会被板块拖到几十秒（实测正是"开盘两分钟还没数据"，
        以及 2026-09-16 复现的"20 次快照里 1 次 7.42s、而所有行情源只要 62~103ms"）。
        后台刷新把这段成本彻底挪出关键路径：用户立刻拿到**略旧的**板块数据，
        下一次刷新（15~20 秒后）就自然变新了。

        任务引用必须留在集合里：asyncio 只持弱引用，任务对象被 GC 掉会
        在运行中被取消（且不报错），是这类"发后不管"最容易踩的坑。
        """
        if name in self._refresh_inflight:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:      # 无事件循环（同步调用）时不做后台刷新
            return
        self._refresh_inflight.add(name)

        async def _refresh() -> None:
            try:
                if kind == "industry":
                    await self._industry_snapshot(name, [name], _force=True)
                else:
                    await self._concept_snapshot(name, _force=True)
            except Exception as exc:  # noqa: BLE001 后台失败只记日志
                logger.debug("后台刷新板块 %s 失败：%s", name, brief(exc, BRIEF_TIGHT))
            finally:
                self._refresh_inflight.discard(name)

        task = loop.create_task(_refresh())
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    def _cached_or_schedule(self, name: str, kind: str, _force: bool) -> BoardSnapshot | None:
        """板块数据的「永不阻塞请求」统一入口。

        返回快照 = 请求路径到此结束（缓存命中，或返回旧值/获取中占位）；
        返回 None = 调用方必须真正去取数（只有 `_force=True` 的后台任务会走到）。
        """
        cached = self._snapshot_cache.get(name)
        if cached is not None and not _force:
            ttl = self._config.data.board_cache_ttl
            if ttl > 0 and time.monotonic() - cached[0] < ttl:
                return cached[1].model_copy(deep=True)
            self._schedule_refresh(name, kind)
            return cached[1].model_copy(deep=True)
        if cached is None and not _force:
            now = time.monotonic()
            retry_after = self._cold_retry_after.get(name, 0.0)
            if now < retry_after:
                return BoardSnapshot(
                    name=name, kind=kind, available=False,
                    source_name=SOURCE_LABELS["router"],
                    gap=(f"板块快照上次获取失败，冷却中"
                         f"（剩余 {int(retry_after - now)}s 后重试）"))
            self._cold_retry_after[name] = now + self._config.data.source_cooldown_seconds
            self._schedule_refresh(name, kind)
            return BoardSnapshot(
                name=name, kind=kind, available=False,
                source_name=SOURCE_LABELS["router"],
                gap=("板块快照首次获取中：已在后台拉取（实测冷取 7~25 秒，"
                     "不阻塞面板），下一次刷新即出现"))
        return None

    async def fetch_snapshot(self, name: str, kind: str = "concept") -> BoardSnapshot:
        """单个板块实时快照（同花顺概念/行业）。"""
        board_cfg = self._config.board_config(name)
        aliases = [name, *(board_cfg.aliases if board_cfg else [])]
        last_gap: str | None = None
        if kind == "industry":
            return await self._industry_snapshot(name, aliases)
        for candidate in aliases:
            snapshot = await self._concept_snapshot(candidate)
            if snapshot.available:
                if candidate != name:
                    snapshot.name = name
                return snapshot
            last_gap = snapshot.gap
        return BoardSnapshot(
            name=name, kind=kind, available=False,
            source_name=SOURCE_LABELS["router"],
            gap=(f"板块快照不可用（试过别名：{'/'.join(aliases)}）"
                 + (f"；最近一次原因：{last_gap}" if last_gap else "")),
        )

    async def _ths_concept_names(self) -> list[str]:
        """同花顺官方概念板块名列表（子进程内取，进程内只取一次；失败冷却300s）。"""
        if self._ths_names is not None:
            return self._ths_names
        now = time.monotonic()
        if now < self._ths_names_cooldown_until:
            return []
        from src.intraday.subproc import run_json_subprocess

        payload = await run_json_subprocess(
            """
import akshare as ak
frame = ak.stock_board_concept_name_ths()
__emit({"names": [str(v) for v in frame["name"].tolist()]})
""",
            timeout=60.0, label="同花顺概念板块名列表")
        names = [str(value) for value in (payload or {}).get("names", []) if value]
        if names:
            self._ths_names = names
            logger.info("同花顺概念板块名列表已加载：%d 个", len(names))
            return names
        self._ths_names_cooldown_until = now + 300.0
        logger.warning("同花顺概念板块名列表不可用，300s 内不再重试"
                       "（这期间板块名不做官方名校验，按原名直达子进程）")
        return []

    async def _resolve_concept_name(self, name: str) -> str:
        """用户写的板块名 → 同花顺官方板块名；**解析不到返回 ""**。

        ## 为什么"解析不到"不能原样返回

        原实现是 `match_board_name(name, names) or name`，解析不到就把用户原名
        交给 akshare。但 `ak.stock_board_concept_info_ths(symbol=...)` 内部是
        `map_df[map_df["name"] == symbol]["code"].values[0]` —— 名字不在官方列表里
        时 `.values[0]` 直接抛 `IndexError: index out of bounds`（实测 2026-09-17，
        `覆铜板 / 印制电路板 / 锂电铜箔 / 电子铜箔` 都不在同花顺 375 个概念里），
        子进程崩掉 → 白等一次子进程启动 → 回退新浪 → 这两个板块永远不可用。

        现在先判"查得到"，查不到就直接返回空串让上层走新浪回退并给出明确原因，
        **不再无谓地起一个注定崩溃的子进程**（自选池预热时每次启动都要报 4 条
        `子进程内报错: IndexError`，就是这四个名字）。

        ⚠️ 只有"官方列表确实拿到了"才算权威判据。列表因冷却/网络失败为空时
        回落成**原名**（旧行为）—— 否则一次列表拉取失败会让所有板块在 300 秒内
        全部跳过同花顺，那是比多起一个子进程严重得多的退化。
        """
        cached = self._concept_name_cache.get(name)
        if cached is not None:
            return cached
        names = await self._ths_concept_names()
        if not names:
            self._concept_name_cache[name] = name      # 不缓存空串判定，列表可用后再解析
            return name
        resolved = match_board_name(name, names) or ""
        self._concept_name_cache[name] = resolved
        if resolved and resolved != name:
            logger.info("板块名自动解析：%s → %s", name, resolved)
        elif not resolved:
            logger.info("板块名 %s 不在同花顺概念列表中（官方共 %d 个），"
                        "跳过同花顺快照、改用回退源", name, len(names))
        return resolved

    async def warm_concept_snapshots(self, names: list[str], *,
                                     limit: int = 14) -> dict[str, int]:
        """**一次子进程**把多个概念板块的快照全拿回来并写入缓存（自选池预热用）。

        ## 为什么必须有这个批量入口（2026-09-17 实测）

        原来每个板块各起一个子进程，而子进程的**固定成本**是 `import akshare`
        约 **0.99s**（实测：空进程 0.03s）。自选 26 只票 × 每只 2~4 个板块
        = 几十次子进程 → 整表重算 **110 秒**。

        批量版把 N 个板块放进**同一个**子进程里循环，那 0.99s 只付一次。
        口径完全不变（同一个 `stock_board_concept_info_ths`、同一套 `parse_concept_info`），
        只是把"N 次进程启动"压成"1 次"。

        Args:
            names: 配置里的板块名（内部先解析成同花顺官方名）。
            limit: 单批上限（子进程有超时，太多会被打断）。

        Returns:
            `{板块名: 1/0}` 表示各板块是否成功，仅用于日志与排障。
        """
        wanted = [str(name).strip() for name in names if str(name).strip()][:limit]
        if not wanted:
            return {}
        resolved: dict[str, str] = {}
        for name in wanted:
            try:
                official = await self._resolve_concept_name(name)
            except Exception:  # noqa: BLE001 解析失败就跳过这个板块
                continue
            # 空串 = 官方概念列表里没有这个名字：**不要**把它放进批量目标，
            # 否则 akshare 的 `.values[0]` 会 IndexError 崩掉整个批量子进程，
            # 连带把同批其它正常板块的结果也一起丢掉。
            if official:
                resolved[name] = official
        if not resolved:
            return {}
        from src.intraday.subproc import run_json_subprocess

        payload = await run_json_subprocess(
            f"""
import akshare as ak
targets = {list(resolved.values())!r}
out = {{"frames": {{}}}}
for name in targets:
    try:
        frame = ak.stock_board_concept_info_ths(symbol=name)
    except Exception as exc:
        out["frames"][name] = {{"error": f"{{type(exc).__name__}}: {{exc}}"}}
        continue
    out["frames"][name] = {{"rows": frame.to_dict("records")}}
__emit(out)
""",
            timeout=min(90.0, 8.0 + 6.0 * len(resolved)),
            label=f"同花顺概念快照批量({len(resolved)}个)")
        frames = (payload or {}).get("frames") or {}
        result: dict[str, int] = {}
        now = time.monotonic()
        for name, official in resolved.items():
            rows = (frames.get(official) or {}).get("rows") or []
            parsed = parse_concept_info(pd.DataFrame(rows)) if rows else {}
            if not parsed:
                result[name] = 0
                continue
            self._snapshot_cache[name] = (now, BoardSnapshot(
                name=name, kind="concept", available=True,
                change_pct=parsed.get("change_pct"),
                up_count=parsed.get("up_count"), down_count=parsed.get("down_count"),
                breadth=parsed.get("breadth"), amount=parsed.get("amount"),
                net_inflow=parsed.get("net_inflow"), rank=parsed.get("rank"),
                open_price=parsed.get("open_price"),
                prev_close=parsed.get("prev_close"), high=parsed.get("high"),
                low=parsed.get("low"), source_name="同花顺概念板块"))
            self._fail_cooldown.pop(official, None)
            self._cold_retry_after.pop(name, None)
            result[name] = 1
        logger.info("板块快照批量预热：%d 个板块成功 %d 个（**1 次**子进程；"
                    "原来的逐板块做法要 %d 次）",
                    len(result), sum(result.values()), len(result))
        return result

    async def _concept_snapshot(self, name: str,
                                *, _force: bool = False) -> BoardSnapshot:
        """同花顺概念快照 —— **必须在子进程里执行**，且**过期也不阻塞**。

        实测（2026-09-15，3/3 复现）：该接口底层用 py_mini_racer 执行 JS 生成
        hexin-v cookie，在 Windows + Python 3.12 上会让整个 Python 进程原生崩溃
        （退出码 -1 / 0x80000003），**try/except 完全无效**。放在服务进程里调用
        等于给投研服务埋了一颗随时会炸的雷，因此走 subproc 隔离；
        失败时回退新浪概念板块（有涨跌幅与自算排名，无涨跌家数）。

        进子进程前先把板块名解析成同花顺官方名（见 `match_board_name`），
        否则「CPO概念」这类用户写法会稳定报 IndexError。

        ## 为什么这里要"先回旧数据、后台再刷新"（stale-while-revalidate）

        实测单个板块冷取要 **3.2~6.5 秒**（其中 1.08s 只是子进程 `import akshare`），
        而一只票通常绑 2~4 个板块、自选 7 只票 → 一轮完整刷新要付 20~30 次这样的成本。
        配合 `board_cache_ttl=20s`，前端**几乎每次刷新都会过期**，于是每轮都重付一遍 ——
        这正是"开盘两分钟数据还没出来"的直接原因。

        但板块涨跌幅对做T信号的影响**远小于**价格本身：晚 30 秒拿到板块数据，
        和让整个面板卡住 6 秒，后者是明显更差的选择。所以**三种情况一律不阻塞请求**：

        - 有缓存且在 TTL 内 → 直接返回；
        - 有缓存但过期 → 立刻返回旧值，同时后台起一个刷新任务；
        - **无缓存（冷取）→ 也立刻返回「获取中」并后台拉取**。
          2026-09-16 实测：冷取 7.45s（板块快照）+ 3.56s（另一个板块），失败冷却
          到期后的重试最长 25s，全都压在用户请求上 —— 20 次快照里就出现 1 次
          **7.42s**，而那次**所有行情源都只要 62~103ms**（时间全花在板块上）。
          板块是展示项 + 14/100 的权重，不值得让面板等它；下一轮刷新自然就有值。
        - 同一板块的后台刷新**去重**（不会堆出多个并发子进程）。
        """
        from src.intraday.subproc import run_json_subprocess

        pre = self._cached_or_schedule(name, "concept", _force)
        if pre is not None:
            return pre

        resolved = await self._resolve_concept_name(name)
        if not resolved:
            # 不在同花顺官方概念列表里：不起那个注定 IndexError 的子进程，
            # 直接给出可取证的缺口说明（上层会走新浪概念回退）。
            return BoardSnapshot(
                name=name, kind="concept", available=False,
                source_name=SOURCE_LABELS["router"],
                gap=f"「{name}」不在同花顺概念板块列表中，同花顺快照不可用")
        cooldown = self._fail_cooldown.get(resolved)
        if cooldown is not None:
            remaining = int(cooldown[0] - time.monotonic())
            if remaining > 0 and not _force:
                return BoardSnapshot(
                    name=name, kind="concept", available=False,
                    source_name=SOURCE_LABELS["router"],
                    gap=f"同花顺板块快照失败冷却中（剩余{remaining}s）：{cooldown[1]}")

        payload = await run_json_subprocess(
            f"""
import akshare as ak
frame = ak.stock_board_concept_info_ths(symbol={resolved!r})
__emit({{"rows": frame.to_dict("records")}})
""",
            timeout=25.0, label=f"同花顺概念快照({resolved})")
        failure: str | None = None
        if payload and payload.get("rows"):
            parsed = parse_concept_info(pd.DataFrame(payload["rows"]))
            if parsed:
                source_name = "同花顺概念板块"
                if resolved != name:
                    source_name = f"{source_name}（{name}→{resolved}）"
                snapshot = BoardSnapshot(
                    name=name, kind="concept", available=True,
                    change_pct=parsed.get("change_pct"),
                    up_count=parsed.get("up_count"),
                    down_count=parsed.get("down_count"),
                    breadth=parsed.get("breadth"), amount=parsed.get("amount"),
                    net_inflow=parsed.get("net_inflow"), rank=parsed.get("rank"),
                    open_price=parsed.get("open_price"),
                    prev_close=parsed.get("prev_close"), high=parsed.get("high"),
                    low=parsed.get("low"), source_name=source_name)
                self._snapshot_cache[name] = (time.monotonic(), snapshot)
                self._fail_cooldown.pop(resolved, None)
                self._cold_retry_after.pop(name, None)
                return snapshot
            failure = "同花顺概念快照结构无法解析"
        elif payload is None:
            failure = ("同花顺概念快照子进程失败/超时（该接口会原生崩溃主进程，"
                       "已隔离到子进程）")
        else:
            failure = "同花顺概念快照返回空表"
        # 名字对不上时是「每次都快照都重跑注定失败的子进程」，必须冷却
        self._fail_cooldown[resolved] = (
            time.monotonic() + self._config.data.source_cooldown_seconds, failure)
        fallback = await asyncio.to_thread(self._sina_sector_snapshot, name)
        if fallback.available:
            fallback.gap = (
                f"{failure}，已回退新浪概念板块：{fallback.source_name}；涨跌家数缺失")
        elif fallback.gap:
            fallback.gap = (
                f"{failure}；新浪概念板块也未匹配到「{name}」"
                "（新浪仅 175 个粗概念板块，无 CPO/PCB/铜箔等细分题材）")
        return fallback

    def _sina_sector_snapshot(self, name: str) -> BoardSnapshot:
        """新浪概念板块快照：涨跌幅 + 全市场涨幅排名（由全部板块排序自算）。

        不依赖 py_mini_racer，因此是崩溃安全的备用口径；
        缺点是没有「上涨/下跌家数」，该维度记缺口。
        """
        try:
            import akshare as ak
            frame = ak.stock_sector_spot(indicator="概念")
        except Exception as exc:  # noqa: BLE001
            return BoardSnapshot(
                name=name, available=False,
                gap=f"新浪概念板块快照失败：{brief(exc, BRIEF_TIGHT)}")
        if frame is None or len(frame) == 0:
            return BoardSnapshot(
                name=name, available=False, gap="新浪概念板块快照为空")
        aliases = [name]
        board_cfg = self._config.board_config(name)
        if board_cfg:
            aliases = [name, *board_cfg.aliases]
        matched = None
        for candidate in aliases:
            hit = frame[frame["板块"].astype(str).str.strip() == candidate.strip()]
            if hit.empty:
                hit = frame[frame["板块"].astype(str).str.contains(
                    candidate.strip(), na=False, regex=False)]
            if not hit.empty:
                matched = hit.iloc[0]
                break
        if matched is None:
            return BoardSnapshot(
                name=name, available=False,
                gap=f"新浪概念板块未匹配到「{name}」")
        # 排名与同花顺「10/390」同口径：全板块按涨跌幅降序取该板块名次
        rank = None
        try:
            ordered = frame.sort_values("涨跌幅", ascending=False).reset_index(
                drop=True)
            hits = ordered.index[
                ordered["板块"].astype(str) == str(matched["板块"])]
            if len(hits):
                rank = f"{int(hits[0]) + 1}/{int(len(ordered))}"
        except Exception:  # noqa: BLE001 排名算不出不影响主字段
            rank = None
        return BoardSnapshot(
            name=name, kind="concept", available=True,
            change_pct=safe_float(matched.get("涨跌幅")),
            amount=safe_float(matched.get("总成交额")),
            rank=rank, source_name="新浪概念板块")

    async def _industry_snapshot(self, name: str, aliases: list[str],
                                 *, _force: bool = False) -> BoardSnapshot:
        """同花顺行业汇总 —— 同样走子进程隔离（同一 JS 依赖）。

        与概念快照共用「永不阻塞请求」语义。**这里原本连缓存都没有**：
        行业汇总是全市场表，冷取 / 失败重试最长 25 秒，而它每次快照都会被调用 ——
        等于每轮刷新都可能白等 25 秒。
        """
        from src.intraday.subproc import run_json_subprocess

        pre = self._cached_or_schedule(name, "industry", _force)
        if pre is not None:
            return pre

        payload = await run_json_subprocess(
            """
import akshare as ak
frame = ak.stock_board_industry_summary_ths()
__emit({"rows": frame.to_dict("records")})
""",
            timeout=25.0, label="同花顺行业汇总")
        if not payload or not payload.get("rows"):
            return BoardSnapshot(
                name=name, kind="industry", available=False,
                gap="同花顺行业汇总不可用（已子进程隔离）")
        frame = pd.DataFrame(payload["rows"])
        for candidate in aliases:
            hit = frame[frame["板块"].astype(str).str.strip() == candidate.strip()]
            if hit.empty:
                hit = frame[frame["板块"].astype(str).str.contains(
                    candidate.strip(), na=False, regex=False)]
            if hit.empty:
                continue
            row = hit.iloc[0]
            up = safe_float(row.get("上涨家数"))
            down = safe_float(row.get("下跌家数"))
            breadth = None
            if up is not None and down is not None and (up + down) > 0:
                breadth = up / (up + down)
            snapshot = BoardSnapshot(
                name=name, kind="industry", available=True,
                change_pct=safe_float(row.get("涨跌幅")),
                up_count=None if up is None else int(up),
                down_count=None if down is None else int(down),
                breadth=breadth, amount=safe_float(row.get("总成交额")),
                net_inflow=safe_float(row.get("净流入")),
                source_name="同花顺行业板块",
            )
            self._snapshot_cache[name] = (time.monotonic(), snapshot)
            self._cold_retry_after.pop(name, None)
            return snapshot
        return BoardSnapshot(
            name=name, kind="industry", available=False,
            gap=f"同花顺行业板块未找到「{name}」")

    # ---------- 板块分时 ----------

    async def fetch_series(self, name: str, kind: str = "concept",
                           peer_codes: list[str] | None = None,
                           *, wait: bool = True) -> BoardSeries:
        """板块分时序列：东财概念分钟 → 自选同业等权合成 → 缺口。

        ## 为什么必须缓存（这里曾经是全链路最慢的一环）

        实测（2026-09-16 盘中）：**单次 `fetch_series` 要 22.3 秒**，其中
        14.4 秒花在 akshare 内部为解析一个板块名而**重新拉取全市场概念板块名单**
        （分页请求），另外 ~8 秒才是板块的分钟线本身。而这里原先**没有任何缓存** ——
        自选 7 只票各有 1~3 个板块，每次前端刷新都要重付一遍，
        于是"开盘两分钟数据还没出来"。

        而板块分时是 **5 分钟 K 线**：同一条线在 5 分钟内根本不会变。

        ## `wait=False`：首次也不阻塞

        `wait=True`（默认）在无缓存时同步取，会阻塞 22 秒；
        `wait=False` 用于**面板路径**：无缓存时立刻返回一条"后台获取中"的缺口，
        同时起后台任务，下一次刷新（几秒后）就有数据了。
        价格/打分是用户要立刻看到的东西，板块分时晚几秒完全可以接受。
        """
        cached = self._series_cache.get(name)
        if cached is not None:
            age = time.monotonic() - cached[0]
            if age < self._series_ttl:
                return cached[1].model_copy(deep=True)
            self._schedule_series_refresh(name, kind, peer_codes)
            return cached[1].model_copy(deep=True)

        # 源已在冷却中 → **不要再起后台任务**，也不要承诺"稍后会出现"。
        # 实测本机东财 push2/push2his 域名被阻断（0.1 秒 RemoteDisconnected），
        # 而 akshare 会在这个失败上重试 17.5 秒 —— 起一个注定失败的 20 秒后台任务
        # 纯属浪费，且"正在后台获取"这句话会让用户以为数据马上就到。
        remaining = self._em_remaining(name)
        if remaining > 0:
            return BoardSeries(
                name=name, kind=kind, available=False, source_name="",
                gap=(f"板块分时不可用：东财板块分钟接口冷却中（剩余 {remaining}s）。"
                     f"本机实测东财 push2 行情域名被阻断，该源在当前网络下不可用；"
                     f"配置 self.peers 同业股票池可用同业等权合成替代"))

        if not wait:
            self._schedule_series_refresh(name, kind, peer_codes)
            return BoardSeries(
                name=name, kind=kind, available=False, source_name="",
                gap=("板块分时不可用：首次获取需约 20 秒（东财接口 + 名单解析），"
                     "已在后台获取，成功后会出现在后续刷新中；"
                     "价格与打分不受影响"))

        series = await self._fetch_series_uncached(name, kind, peer_codes)
        if series.available:
            self._series_cache[name] = (time.monotonic(), series)
        return series

    async def _fetch_series_uncached(self, name: str, kind: str,
                                     peer_codes: list[str] | None) -> BoardSeries:
        remaining = self._em_remaining(name)
        if remaining:
            series = BoardSeries(
                name=name, kind=kind, available=False,
                gap=f"东财板块分钟不可用（冷却剩 {remaining}s），直接走同业合成")
        elif kind == "concept":
            series = await asyncio.to_thread(self._eastmoney_board_series, name)
            self._mark_em(name, series.available, gap=series.gap)
        else:
            series = await asyncio.to_thread(self._eastmoney_industry_series, name)
            self._mark_em(name, series.available, gap=series.gap)
        if series.available:
            return series
        if peer_codes:
            synthetic = await self._synthetic_peer_series(name, peer_codes)
            if synthetic.available:
                return synthetic
        return BoardSeries(
            name=name, kind=kind, available=False, source_name="",
            gap=(series.gap or "") + "；且未配置可用于等权合成的同业股票池"
            if not peer_codes else (series.gap or ""))

    def _schedule_series_refresh(self, name: str, kind: str,
                                 peer_codes: list[str] | None) -> None:
        """后台刷新板块分时（不阻塞请求，同名去重）。"""
        if name in self._series_inflight:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._series_inflight.add(name)

        async def _refresh() -> None:
            try:
                series = await self._fetch_series_uncached(name, kind, peer_codes)
                if series.available:
                    self._series_cache[name] = (time.monotonic(), series)
            except Exception as exc:  # noqa: BLE001 后台失败只记日志
                logger.debug("后台刷新板块分时 %s 失败：%s", name, brief(exc, BRIEF_TIGHT))
            finally:
                self._series_inflight.discard(name)

        task = loop.create_task(_refresh())
        self._refresh_tasks.add(task)
        task.add_done_callback(self._refresh_tasks.discard)

    def _em_remaining(self, name: str) -> int:
        """东财板块数据的剩余冷却秒数（取"单板块冷却"与"整体被阻断"的较大值）。"""
        now = time.monotonic()
        until = self._em_cooldown.get(name)
        if self._em_blocked_until > now:
            # 网络级阻断优先：对单个板块的冷却没有意义，本来就是整条链路不通
            until = max(until or 0.0, self._em_blocked_until)
        if until is None:
            return 0
        return max(0, int(until - now))

    def _mark_em(self, name: str, ok: bool, *, gap: str = "") -> None:
        window = self._config.data.source_cooldown_seconds
        if ok:
            self._em_cooldown.pop(name, None)
            self._em_blocked_until = 0.0
            return
        if self._looks_like_block(gap):
            # 阻断：整条链路都不通，按小时级冷却，避免每 5 分钟空转 17 秒重试
            self._em_blocked_until = time.monotonic() + self._em_block_window
            logger.warning(
                "东财板块数据源看起来被网络阻断（%s），%d 秒内不再重试；"
                "板块分时图将不可用，板块涨跌幅走同花顺快照",
                gap[:80], int(self._em_block_window))
        if window > 0:
            self._em_cooldown[name] = time.monotonic() + window

    @staticmethod
    def _eastmoney_board_series(name: str) -> BoardSeries:
        try:
            import akshare as ak
            frame = ak.stock_board_concept_hist_min_em(symbol=name, period="5")
        except Exception as exc:  # noqa: BLE001 东财接口常被阻断
            return BoardSeries(
                name=name, available=False,
                gap=f"东财概念分钟接口失败：{brief(exc, BRIEF_TIGHT)}")
        return _frame_to_board_series(frame, name, "东方财富概念分钟", "concept")

    @staticmethod
    def _looks_like_block(exc_text: str) -> bool:
        """判断这次失败是"域名被阻断"还是"偶发抖动"。

        判据来自实测：本机东财 push2/push2his 域名的表现是
        `RemoteDisconnected('Remote end closed connection without response')`，
        而且是**稳定复现**（0.1 秒就断，不是超时）。对这种情况继续按 5 分钟冷却重试
        没有意义 —— 每次要空转 17.5 秒（akshare 内部指数退避），
        网络级阻断不会在 5 分钟内自愈。
        """
        markers = ("RemoteDisconnected", "Connection aborted",
                   "ConnectionError", "Name or service not known",
                   "Max retries exceeded")
        return any(marker in exc_text for marker in markers)

    @staticmethod
    def _eastmoney_industry_series(name: str) -> BoardSeries:
        try:
            import akshare as ak
            frame = ak.stock_board_industry_hist_min_em(symbol=name, period="5")
        except Exception as exc:  # noqa: BLE001
            return BoardSeries(
                name=name, available=False,
                gap=f"东财行业分钟接口失败：{brief(exc, BRIEF_TIGHT)}")
        return _frame_to_board_series(frame, name, "东方财富行业分钟", "industry")

    async def _synthetic_peer_series(self, name: str,
                                     peer_codes: list[str]) -> BoardSeries:
        """自选同业等权合成的板块日内指数线（各股以当日首个价格为基期归一）。"""
        async def one(code: str) -> pd.DataFrame | None:
            try:
                frame, _, _ = await self._data.fetch_trend(code)
                return frame
            except Exception:  # noqa: BLE001 单只失败不拖垮整体
                return None

        results = await asyncio.gather(*(one(code) for code in peer_codes))
        frames = [f for f in results if f is not None and len(f)]
        if not frames:
            return BoardSeries(
                name=name, available=False,
                gap="同业分时全部取数失败，无法合成板块线")
        # 以「当日第几分钟」为轴对齐（不同源时间戳可能略有差异）
        merged: dict[str, list[float]] = {}
        for frame in frames:
            working = frame.copy()
            working["minute"] = working["ts"].astype(str).str.slice(11, 16)
            base = None
            for minute, price in zip(working["minute"], working["price"], strict=True):
                value = safe_float(price)
                if value is None or value <= 0:
                    continue
                if base is None:
                    base = value
                merged.setdefault(minute, []).append(value / base)
        points: list[TrendPoint] = []
        for minute in sorted(merged):
            ratios = merged[minute]
            if not ratios:
                continue
            points.append(TrendPoint(
                ts=f"{frames[0]['ts'].iloc[0][:10]} {minute}",
                price=round(sum(ratios) / len(ratios) * 1000.0, 3),
            ))
        if not points:
            return BoardSeries(
                name=name, available=False, gap="同业分时合成后为空")
        return BoardSeries(
            name=name, kind="synthetic_peers", available=True, points=points,
            source_name=f"自选同业等权合成（{len(frames)}只，基期1000）",
            gap=None,
        )

    # ---------- 大盘状态 ----------

    async def fetch_index(self, index_code: str = "000001") -> dict[str, Any]:
        """大盘指数快照 + 状态判定（上涨/下跌/震荡）。"""
        label = _INDEX_LABELS.get(index_code, index_code)
        try:
            quote, source, attempts = await self._data.fetch_index_quote(index_code)
        except DataFetchError as exc:
            return {
                "index_code": index_code, "index_name": label, "available": False,
                "gap": brief(exc, BRIEF_DEFAULT), "attempts": [],
            }
        change_pct = quote.change_pct
        if change_pct is None:
            state = "未知"
        elif change_pct >= 0.3:
            state = "大盘偏强"
        elif change_pct <= -0.3:
            state = "大盘偏弱"
        else:
            state = "大盘震荡"
        return {
            "index_code": index_code, "index_name": quote.name or label,
            "index_price": quote.price, "index_change_pct": change_pct,
            "index_state": state, "available": True, "source": source,
            "attempts": attempts,
        }


def _frame_to_board_series(frame: Any, name: str, source_name: str,
                           kind: str) -> BoardSeries:
    """东财板块分钟帧 → BoardSeries（列名容错）。"""
    if frame is None or len(frame) == 0:
        return BoardSeries(name=name, kind=kind, available=False,
                           gap="东财板块分钟返回空")
    columns = {str(c): c for c in frame.columns}
    ts_col = next((columns[k] for k in columns if "时间" in k or k == "datetime"), None)
    price_col = next((columns[k] for k in columns if "收盘" in k), None)
    if ts_col is None or price_col is None:
        return BoardSeries(name=name, kind=kind, available=False,
                           gap=f"东财板块分钟列名异常：{list(frame.columns)[:8]}")
    points: list[TrendPoint] = []
    for _, row in frame.tail(120).iterrows():
        price = safe_float(row.get(price_col))
        if price is None:
            continue
        points.append(TrendPoint(ts=str(row.get(ts_col)), price=price))
    return BoardSeries(name=name, kind=kind, available=bool(points),
                       points=points, source_name=source_name,
                       gap=None if points else "东财板块分钟无有效点")
