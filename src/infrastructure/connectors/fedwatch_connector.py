"""CME FedWatch美联储利率概率连接器（cme-fedwatch开源库，免费免Key）。

数据来源全部官方：结算价CME Group、EFFR来自FRED、FOMC日程来自美联储。

## ⚠️ CME 在本机网络上不可达（2026-09-26 实测，这不是猜测）

    DNS   www.cmegroup.com -> 108.160.165.211（Akamai 边缘，属被阻断网段）
    TCP   www.cmegroup.com:443 -> **不通**（curl 报 Failed to connect after 21.3s）
    对照  api.stlouisfed.org:443 -> **通**
    对照  push2.eastmoney.com:443 / qt.gtimg.cn:443 -> 通

所以 `cme_fedwatch` 库必然失败，而它失败前要等满 `_TIMEOUT_SEC` —— 这一跳
曾把整条投研链路的数据采集阶段从 ~5s 拖到 **23.4s**（A01 用 asyncio.gather
并发，墙钟 = 最慢那个）。

## 因此增加 **FRED 兜底源**（feeds the same question with reachable data）

`fed:rate_prob:next` 想回答的是"美联储政策利率在哪、下一步往哪走"。
CME FedWatch 给"下次会议各区间概率"，而 **FRED 给当前目标区间与有效利率** ——
后者在 CME 不可达时是**唯一还能拿到的官方口径**，且实测稳定：

    DFEDTARU（目标区间上限）  HTTP 200  1.1~1.3s  最新 2026-09-25 = 4.00
    DFEDTARL（目标区间下限）  HTTP 200  1.0~1.2s  最新 2026-09-25 = 3.75
    DFF   （有效联邦基金利率） HTTP 200  1.8s      最新 2026-09-24 = 3.88

⚠️ 注意这与 AkShare 的 `us_fed_rate` 形成鲜明对比：后者实测**最新有效值停在
2025-07-31**（上游未回填，见 `scheduler/registry.py` 的说明）。
也就是说 FRED 这条路同时修掉了"美联储利率陈旧 14 个月"的问题。

指标约定：
- "fed:rate_prob:next"        → 下一次FOMC各利率区间概率（每个区间一个DataPoint，
                                value=概率%，extra含区间标签/会议日/EFFR/降息-不变-加息概率）
- "fed:rate_prob:{YYYY-MM-DD}" → 指定会议日
- "fed:policy_range"          → 当前目标区间上下限+有效利率（FRED 源）
- "fed:target_upper"          → ★ 第二十三轮：目标区间**上限**（DFEDTARU，独立指标）
- "fed:target_lower"          → ★ 第二十三轮：目标区间**下限**（DFEDTARL，独立指标）
- "fed:effr"                  → ★ 第二十三轮：**有效联邦基金利率**（DFF，独立指标）

## ★★★ 2026-09-28 第二十三轮：为什么必须拆出三个独立指标

`fed:policy_range` 原先把 **三个 FRED 序列写进同一个 indicator** ——
一天产 3 条点（上限/下限/有效利率），全部标着 `indicator="fed:policy_range"`
和同一个 `period_date`。于是**"这个指标的值是多少"这个问题没有答案**：
它同时是 4.00、3.75 和 3.88。

read 侧的实测后果（比"缺数据"更危险，因为数字看着有据）：

    A08 的 `_latest_numeric(pts, "fed:policy_range")` 取"最新一条"
    → 同日三条里挑中**上限或下限之一**
    → 结论写成「目标区间 3.75%（FRED 口径）」
    → **把区间下限当成政策利率报给用户**（3.75 是 DFEDTARL，不是利率）

拆开之后每个 indicator 各自是**单值时间序列**，"最新一条"才有意义。
`fed:policy_range` 继续产出（向后兼容：历史库里那 3 条点不能读不出来），
但 **read 侧要优先用拆开的三个**（见 `macro/agent.py::_build_rule_only_result`）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import date
from typing import Any

import httpx

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors.base import BaseConnector
from src.infrastructure.connectors.source_cooldown import get_cooldown

logger = logging.getLogger(__name__)

_FED_RE = re.compile(r"^fed:rate_prob:(next|\d{4}-\d{2}-\d{2})$")
#: FRED 兜底源。
#:
#: ★ 第二十三轮：增加三个**独立单值序列**（`fed:target_upper` /
#: `fed:target_lower` / `fed:effr`），与旧的混合口径 `fed:policy_range` 并存。
#: 拆分理由见模块 docstring（"把区间下限当利率"那个 bug）。
_FRED_RANGE_RE = re.compile(
    r"^fed:(policy_range|target_range|target_upper|target_lower|effr)$")
#: 单次调用的硬超时。**这个值是投研链路的墙钟瓶颈**（2026-09-26 实测：
#: 外网不可达时这里等满 23.5s，而 A01 用 asyncio.gather 并发采集，
#: 于是整个"数据采集"阶段就被它拖到 23.5s）。配合下面的失败冷却，
#: 只有**第一次**会等满，之后 60s 内直接返回空。
_TIMEOUT_SEC = 25
_COOLDOWN_KEY = "fedwatch:rate_prob"
_FRED_COOLDOWN_KEY = "fred:policy_range"

#: FRED 序列 ID → (字段含义, 单位, **独立指标名**)。
#:
#: ★ 第二十三轮：加了第三列 —— 每个序列有自己的 indicator id。
#: 第三列为空串表示"挂到请求的那个 indicator 上"（旧行为）。
#: 这样：
#:   · 请求 `fed:policy_range` → 三条点都挂 `fed:policy_range`（**兼容历史**）
#:   · 请求 `fed:target_upper` → 只取 DFEDTARU 一条，挂 `fed:target_upper`
#:   · 请求 `fed:effr`        → 只取 DFF 一条
_FRED_SERIES: tuple[tuple[str, str, str], ...] = (
    ("DFEDTARU", "目标区间上限", "fed:target_upper"),
    ("DFEDTARL", "目标区间下限", "fed:target_lower"),
    ("DFF", "有效联邦基金利率", "fed:effr"),
)

#: 旧混合口径 → 它包含哪些独立指标（read 侧与采集侧共用这张映射）。
FRED_SERIES_BY_RANGE_ID: dict[str, tuple[str, ...]] = {
    "fed:policy_range": ("fed:target_upper", "fed:target_lower", "fed:effr"),
    "fed:target_range": ("fed:target_upper", "fed:target_lower", "fed:effr"),
}
_FRED_CSV = "https://fred.stlouisfed.org/graph/fredgraph.csv"
#: FRED 单序列请求超时（实测 1.0~1.3s，给 20s 很宽）
_FRED_TIMEOUT_SEC = 20.0


class FedWatchConnector(BaseConnector):
    """CME FedWatch FOMC利率路径概率；网络不可达时优雅降级为空结果。"""

    source_name = "CME FedWatch(cme-fedwatch开源库)"
    source_url = "https://www.cmegroup.com/markets/interest-rates/cme-fedwatch-tool.html"

    def get_capabilities(self) -> dict[str, Any]:
        return {
            "name": self.source_name,
            "source_type": DataSourceType.API.value,
            "indicators": ["fed:rate_prob:next", "fed:rate_prob:{YYYY-MM-DD}",
                           "fed:policy_range",
                           # ★ 第二十三轮：三个独立单值序列
                           "fed:target_upper", "fed:target_lower", "fed:effr"],
            "notes": ("概率单位%；依赖CME/FRED外网。"
                      "⚠️ CME 在本机网络不可达（TCP 443 不通），"
                      "fed:rate_prob:* 会记冷却并降级；"
                      "fed:policy_range / fed:target_upper / fed:target_lower / "
                      "fed:effr 走 FRED（实测可达）。"
                      "★ 判断政策利率请用 `fed:effr`（单值）或 "
                      "`fed:target_upper`+`fed:target_lower`（区间成对）；"
                      "**不要**用 `fed:policy_range` 取单点 —— 它同日有上下限两条，"
                      "取最新一条会把区间下限当成利率"),
        }

    @staticmethod
    def supports(indicator: str) -> bool:
        return bool(_FED_RE.match(indicator)
                    or _FRED_RANGE_RE.match(indicator))

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        if _FRED_RANGE_RE.match(indicator):
            return await self._fetch_fred_range(indicator)
        m = _FED_RE.match(indicator)
        if not m:
            raise DataFetchError(f"FedWatch连接器不支持的指标: {indicator}")

        # 失败冷却（negative cache）：外网不可达时**不要每次都等满 25 秒**。
        # 实测这一个指标就能把整条数据采集阶段从 ~5s 拖到 23.5s，
        # 而它返回的永远是空列表 —— 必然失败的等待是纯浪费。
        cooldown = get_cooldown()
        if cooldown.is_cooling(_COOLDOWN_KEY):
            left = cooldown.remaining(_COOLDOWN_KEY)
            logger.debug(
                "CME FedWatch 处于失败冷却中（剩 %.0fs），跳过网络请求直接返回空", left)
            return []

        # ★ TCP 预检：CME 在本机**确定性不可达**（实测 443 不通），
        # 而 `cme_fedwatch` 内部要等满 21.3s 才抛错 —— 那 21 秒把整条
        # 数据采集阶段（gather 墙钟）从 ~5s 拖到 23.4s。
        # 先用 2 秒预检（带 60s 结论缓存）判定"这台主机通不通"，
        # 不通就直接返回空，把代价从 23.4s 降到 ~2s。
        from src.infrastructure.connectors.net_probe import probe_tcp

        if not await probe_tcp("www.cmegroup.com", 443):
            cooldown.record_failure(_COOLDOWN_KEY, reason="TCP 预检不可达")
            logger.info(
                "CME FedWatch 主机 TCP 预检不可达，跳过调用（省去 ~21s 超时等待）；"
                "政策利率请用 fed:policy_range（FRED 源，实测可达）")
            return []

        meeting_arg = m.group(1)
        try:
            data = await asyncio.wait_for(
                asyncio.to_thread(self._call_lib, meeting_arg),
                timeout=_TIMEOUT_SEC,
            )
        except Exception as exc:  # noqa: BLE001 网络/超时统一降级为空结果
            from src.core.redaction import sanitize_error

            wait = cooldown.record_failure(_COOLDOWN_KEY, reason=sanitize_error(exc))
            logger.warning(
                "CME FedWatch不可达（降级为数据缺口，%.0fs 内不再重试）: %s",
                wait, exc)
            return []
        points = self._to_points(indicator, data)
        if points:
            cooldown.record_success(_COOLDOWN_KEY)
        else:
            # 连上了但拿不到概率（如 CME 只保留最近 5 个交易日、非会议窗口）：
            # 同样记冷却 —— 这是"当前没有可用数据"，不是"网络坏了"，
            # 但重复调用同样拿不到，没必要每次都付一次外网往返。
            cooldown.record_failure(_COOLDOWN_KEY, reason="返回空概率")
        return points

    @staticmethod
    def _call_lib(meeting_arg: str) -> dict[str, Any]:
        from cme_fedwatch import get_probabilities

        return get_probabilities(meeting_arg)

    # ---------- FRED 兜底源 ----------

    async def _fetch_fred_range(self, indicator: str) -> list[DataPoint]:
        """从 FRED 取政策利率口径（CME 不可达时的官方替代）。

        为什么这条能work而 CME 不行：实测 `api.stlouisfed.org:443` /
        `fred.stlouisfed.org:443` 都通，而 `www.cmegroup.com:443` 不通。
        序列串行取（各自 1~2s），最多 3 条总计约 4s —— 远低于 CME 那 23.4s 超时。

        ## ★ 第二十三轮：支持"只取单个序列"

        `indicator` 可能是**混合口径**（`fed:policy_range`，取全部 3 个序列、
        全部挂同一个 id —— 兼容历史库那 3 条点），
        也可能是**独立序列**（`fed:target_upper` / `fed:target_lower` /
        `fed:effr`，只取对应那一个）。

        为什么独立序列是必需的：混合口径下"这个指标的值是多少"**没有答案**
        （同日三条：4.00 / 3.75 / 3.88），read 侧取"最新一条"会挑中
        上限或下限之一 —— 实测把**区间下限 3.75 当成政策利率**报了出去。
        拆开后每个 id 各自是单值时间序列，"最新一条"才有意义。

        失败降级为空列表并记冷却（与 FedWatch 同一套机制），不阻断主链路。
        """
        cooldown = get_cooldown()
        if cooldown.is_cooling(_FRED_COOLDOWN_KEY):
            logger.debug("FRED 处于失败冷却中，跳过")
            return []

        # 决定本次要取哪些序列、各自挂到哪个 indicator 上。
        wanted: list[tuple[str, str, str]] = []   # (series_id, label, out_id)
        if indicator in FRED_SERIES_BY_RANGE_ID:
            # 混合口径：全部序列都挂到请求的这个 id（保持历史行为）
            for series_id, label, _own_id in _FRED_SERIES:
                wanted.append((series_id, label, indicator))
        else:
            # 独立序列：只取匹配的那一个
            for series_id, label, own_id in _FRED_SERIES:
                if own_id == indicator:
                    wanted.append((series_id, label, own_id))
        if not wanted:
            logger.debug("FRED 不认识的指标 %s（支持的：%s）", indicator,
                         [i for _, _, i in _FRED_SERIES]
                         + list(FRED_SERIES_BY_RANGE_ID))
            return []

        points: list[DataPoint] = []
        failures: list[str] = []
        async with httpx.AsyncClient(timeout=_FRED_TIMEOUT_SEC,
                                     follow_redirects=True) as client:
            for series_id, label, out_id in wanted:
                try:
                    resp = await client.get(
                        _FRED_CSV, params={"id": series_id,
                                           "cosd": _fred_start_date()})
                    resp.raise_for_status()
                    parsed = _parse_fred_csv(resp.text)
                except Exception as exc:  # noqa: BLE001 单序列失败不阻断其余
                    failures.append(f"{series_id}: {exc}")
                    continue
                if not parsed:
                    failures.append(f"{series_id}: 无有效观测")
                    continue
                period, value = parsed
                points.append(DataPoint(
                    indicator=out_id, value=value, unit="%",
                    period_date=period,
                    extra={"series_id": series_id, "label": label,
                           "source": "FRED (St. Louis Fed)",
                           "retrieved_date": date.today().isoformat()},
                    source_name="FRED",
                    source_url=f"https://fred.stlouisfed.org/series/{series_id}",
                    fetch_method=FetchMethod.API_CALL, confidence=0.95,
                ))

        if points:
            cooldown.record_success(_FRED_COOLDOWN_KEY)
            logger.info("FRED 政策利率取数成功：%d 个序列（%s）",
                        len(points), indicator)
        else:
            cooldown.record_failure(_FRED_COOLDOWN_KEY,
                                    reason="; ".join(failures)[:80])
            logger.warning("FRED 政策利率取数失败（降级为数据缺口）: %s",
                           "; ".join(failures)[:200])
        return points

    @staticmethod
    def _to_points(indicator: str, data: dict[str, Any]) -> list[DataPoint]:
        meetings = data.get("meetings") or []
        if not meetings:
            return []
        meeting = meetings[0]
        probs: dict[str, float] = meeting.get("probabilities") or {}
        if not probs:
            return []
        current_target = data.get("current_target", "")
        move_probs = _classify_moves(probs, current_target)
        today = date.today().isoformat()
        common_extra = {
            "meeting_date": meeting.get("date"),
            "contract": meeting.get("contract"),
            "effr": data.get("effr"),
            "current_target": current_target,
            "cut_prob": move_probs["cut"],
            "hold_prob": move_probs["hold"],
            "hike_prob": move_probs["hike"],
            "dominant_range": max(probs, key=probs.get),
            "source": "CME Group结算价+FRED EFFR(cme-fedwatch)",
            "retrieved_date": today,
        }
        return [
            DataPoint(
                indicator=indicator, value=round(float(p), 1), unit="%",
                period_date=meeting.get("date"),
                extra={**common_extra, "rate_range": label},
                source_name="CME FedWatch", source_url=FedWatchConnector.source_url,
                fetch_method=FetchMethod.API_CALL, confidence=0.85,
            )
            for label, p in sorted(probs.items(), key=lambda kv: kv[1], reverse=True)
        ]


def _fred_start_date() -> str:
    """FRED 查询起始日：只取最近约 120 天，避免拉回几十年的历史。

    实测：不带 `cosd` 时 DFEDTARU 返回 6494 行（约 120 KB）；带 `cosd` 后
    只返回 4314 B。在链路里我们只需要"当前值"，不需要历史。
    """
    from datetime import timedelta

    return (date.today() - timedelta(days=120)).isoformat()


def _parse_fred_csv(text: str) -> tuple[str, float] | None:
    """解析 FRED CSV，返回**最后一个有效观测** `(日期, 值)`。

    FRED 的缺失值写作 `.`（不是空串），必须显式跳过 —— 否则 `float('.')`
    抛错，或更糟：把缺失值当成 0 存进库，分析层就会看到"利率 0%"。
    """
    last: tuple[str, float] | None = None
    for line in text.splitlines()[1:]:            # 跳过表头 observation_date,XXX
        parts = line.strip().split(",")
        if len(parts) < 2:
            continue
        period, raw = parts[0].strip(), parts[1].strip()
        if not period or raw in (".", "", "NaN", "null"):
            continue
        try:
            last = (period, float(raw))
        except ValueError:
            continue
    return last


def _range_midpoint(label: str) -> float:
    """'3.50%-3.75%' → 3.625。"""
    lo_hi = label.replace("%", "").split("-")
    return (float(lo_hi[0]) + float(lo_hi[1])) / 2


def _classify_moves(probs: dict[str, float], current_target: str) -> dict[str, float]:
    """以当前目标区间中点为基准，汇总降息/不变/加息概率。"""
    cur = _range_midpoint(current_target) if current_target else None
    cut = hold = hike = 0.0
    for label, p in probs.items():
        if cur is None:
            continue
        mid = _range_midpoint(label)
        if abs(mid - cur) < 1e-9:
            hold += p
        elif mid < cur:
            cut += p
        else:
            hike += p
    return {"cut": round(cut, 1), "hold": round(hold, 1), "hike": round(hike, 1)}
