"""东方财富 Choice EMQuantAPI 连接器（`CHG-0130`）—— **闸门开之前，它一个指标都不接**。

## 它在整条链上的位置（PRD 已经声明过，这里只是把它落成代码）

`docs/PRD.md` §19.33.5 写着：Choice 的接入点是
**「新连接器（`ConnectorRouter` 链上的一环）」**，前置条件是
`scripts/check_external_sources.py` **退出码 0**，并且按
`.trae/skills/backup-path-availability` 的 **R1**「**先建『断开主用』的离线演练再接线**」。

本模块就是那个连接器。**但它现在的形态是：接口齐、闸门关、口径空。**

## 为什么口径是空的（不是我偷懒，是 PRD 明令）

§19.33.6 原文：

> Choice 的**能力面尚未探明**：登录都没过，所以"它到底能供哪些数据、
> 与现有 AkShare/Tushare/行情仓如何互补"**一条都还没量**，
> 不要现在就按它的宣传口径去改指标登记表。

所以 `INDICATORS` **故意留空** —— 按宣传册填一串"它应该能取"的指标，
正是本项目反复付代价的那件事（"必然失败的指标不许留在菜单里"）。
填它的前提只有一个：**真登录成功、真取到数、口径亲手量过**。

## 三条纪律（都有判据守着，见 `tests/unit/test_choice_connector_offline.py`）

1. **`supports()` 只认 `INDICATORS`** —— 它现在是空的，所以本连接器
   **今天不可能被任何指标选中**，也不会抢走别的源的指标；
2. **`fetch()` 第一件事是过闸门**：非 `ok` 一律 `DataFetchError`，
   **绝不返回空列表** —— 空列表会被上层读成"这个指标没数据（也许就该没有）"，
   而真相是"**这个账号没权限**"。两者处置完全不同
   （前者进缺口队列，后者要找客户经理），把后者伪装成前者就是静默失败；
3. **`build_choice_routes()` 是唯一的接线入口**，它同时要求
   "口径非空" **且** "闸门 ok"；任一不满足就返回 `[]`（**不接线，也不声明可用**）。
   `INDICATORS` 为空时它**连网络都不打**（启动期不许有登录等待，见 `CHG-0101`）。
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from src.core.exceptions import DataFetchError
from src.core.schemas import DataPoint, DataSourceType, FetchMethod
from src.infrastructure.connectors import choice_gate
from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

#: ★ **已亲手量过**的指标口径 ⇒ 目前**故意为空**。
#:
#: 往里加东西之前必须同时满足两条，缺一条就是给自己造一条永远失败的路径：
#:   ① `uv run python scripts/check_external_sources.py` **退出码 0**（闸门真开了）；
#:   ② 该指标**真的取到过数**（口径 = SDK 的哪个函数 + 哪些参数 + 单位）。
#: 判据 `test_indicators_stay_empty_until_the_gate_is_open` 会拦住"提前填"，
#: 它红了要改的是**判断**（连同台账一起改），不是把它删掉。
INDICATORS: tuple[str, ...] = ()

#: SDK 调用注入点：`(indicator, start, end) -> list[DataPoint]`。
#: 默认 `None` = **口径未探明**，此时即使闸门开了也只如实报"口径未定"。
_FetchFn = Callable[[str, "str | None", "str | None"], list[DataPoint]]


class ChoiceConnector(BaseConnector):
    """Choice 连接器：**过闸门 → 查口径 → 取数**，任一步不过都拒绝而不是返回空。"""

    source_name = "东方财富Choice量化接口(EMQuantAPI)"
    source_url = "https://quantapi.eastmoney.com/"

    def __init__(
        self,
        *,
        probe_fn: Callable[[], choice_gate.ChoiceStatus] | None = None,
        fetch_fn: _FetchFn | None = None,
    ) -> None:
        #: 两个依赖都可注入 ⇒ 离线演练不必碰真 SDK（`backup-path-availability` R1）
        self._probe = probe_fn or choice_gate.probe
        self._fetch_fn = fetch_fn

    # ---------------------------------------------------------------- 能力面

    @staticmethod
    def supports(indicator: str) -> bool:
        """只认**量过**的口径。空 allowlist ⇒ 恒 False（今天不可能被选中）。"""
        return indicator in INDICATORS

    def get_capabilities(self) -> dict[str, Any]:
        """能力描述。★ `wireable=False` 是**如实**的，不是占位。"""
        return {
            "name": self.source_name,
            "source_url": self.source_url,
            "source_type": DataSourceType.API.value,
            "indicators": list(INDICATORS),
            "notes": "闸门未开（账号未开通量化接口权限）/ 口径未探明时，本连接器不接任何指标",
            "gate": "src.infrastructure.connectors.choice_gate",
            "wireable": False,
            "pending": "① 开通「量化接口(EMQuantAPI)」权限；② 亲手量出口径后填 `INDICATORS`",
        }

    # ---------------------------------------------------------------- 取数

    async def fetch(
        self,
        indicator: str,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[DataPoint]:
        """过闸门 → 查口径 → 取数。**每一步不过都抛错，不返回空列表。**

        为什么"抛错"而不是"返回 `[]`"：`[]` 在本项目的链上意味着
        "**这个源没有这个指标的数据**"，会被当成一次正常的空结果继续往下走
        （甚至进缺口队列让 A19 去补）—— 而真实原因是**权限**，
        补一万次也补不出来。拒绝要给出路（`AGENTS.md`）。
        """
        status = self._probe()
        try:
            choice_gate.assert_wireable(status)
        except RuntimeError as exc:
            raise DataFetchError(str(exc)) from exc

        if self._fetch_fn is None:
            raise DataFetchError(
                f"Choice 闸门已开（state={status.state}），但**指标口径尚未探明** —— "
                "`INDICATORS` 仍为空、且没有注入取数实现。"
                "按 `docs/PRD.md` §19.33.6：不要按宣传口径先填指标登记表；"
                "请先真取到一次数、记下 SDK 函数与参数，再填 `INDICATORS`。")

        rows = await asyncio.to_thread(self._fetch_fn, indicator, start_date, end_date)
        #: 口径实现必须自己带上溯源字段（与其余连接器一致）；
        #: 这里只补 `source_name`，避免"谁取的数"在链上丢失。
        return [
            point.model_copy(update={"source_name": self.source_name}) if point.source_name
            else point.model_copy(update={"source_name": self.source_name,
                                          "source_url": self.source_url})
            for point in rows
        ]


def build_choice_routes(
    *,
    probe_fn: Callable[[], choice_gate.ChoiceStatus] | None = None,
    fetch_fn: _FetchFn | None = None,
) -> list[tuple[BaseConnector, Callable[[str], bool]]]:
    """**唯一**的接线入口：满足条件才返回一条路由，否则返回 `[]`。

    两个条件**同时**满足才接线：

    ===========================  ==========================================
    口径非空（`INDICATORS`）      没有量过的口径 ⇒ 接了也只能返回错的东西
    闸门 `ok`                    账号没权限 ⇒ 接了就是一条必然失败的路径
    ===========================  ==========================================

    ⚠️ **`INDICATORS` 为空时连网络都不打**：否则每次启动都要做一次 Choice 登录
    （实测数秒、失败还会打一堆警告），而它今天根本不可能被选中 ——
    启动期多一次外部等待，正是 `CHG-0101` 那次 24 分钟不可用要避免的形状。
    """
    if not INDICATORS:
        return []
    probe = probe_fn or choice_gate.probe
    status = probe()
    if not status.wireable:
        logger.warning(
            "Choice 已登记口径但闸门未开（state=%s）：不接线。%s",
            status.state, status.detail)
        return []
    connector = ChoiceConnector(probe_fn=probe, fetch_fn=fetch_fn)
    return [(connector, ChoiceConnector.supports)]


__all__ = ["INDICATORS", "ChoiceConnector", "build_choice_routes"]
