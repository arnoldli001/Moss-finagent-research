"""A20 兜底行业 Agent：**A13-A16 覆盖不到的行业由它接管**。

## 报障现场（用户 2026-09-29 原话）

> 「600036（招商银行属银行，非本行业消费）在数据缺口下无法给出明确持有结论：
>   **缺少银行 agent 吗？那就生成一个负责其他产业的 agent。做个兜底行业的 agent，
>   支持根据输入内容，动态注册对应行业 agent**」

修前的真实表现：一条问"高股息招商银行"的复合问，落到了 **A14（消费）** 手里，
它的结论第一句就是「600036（招商银行）**非本框架（消费）覆盖标的**」——
数据全在，但**没有一个 Agent 该管银行**。

## 它和 A13-A16 的三个区别

| | A13-A16 | A20（本类） |
|---|---|---|
| 行业名 | 类级常量（写死） | **按每次请求解析**（`_industry_name_for`） |
| 关注指标 | 类级 `watch_keywords` | **按行业动态解析** |
| 路由 | `INDUSTRY_KEYWORDS` 关键词命中 | **兜底**：命中不了任何专属 Agent 时接管 |

## 行业怎么确定（**确定性，不猜**）

1. 文中的 **6 位代码** → `catalog/industry_of.industry_of()`（本地行情仓
   `quant_stock_basic.industry`，Tushare 名录）→ 实测 `600036 → 银行`；
2. 文中的**股票简称** → `synonym_dict.resolve_entity()` 解析成代码 → 同上；
3. 文中**直接出现行业名**（拿本地 110 个行业名做**最长匹配**）→ 用它；
4. 都拿不到 → 返回空，由基类按"无关注指标"如实说明（**不猜行业** ——
   猜错会让它用错误的框架分析，比"没有结论"更糟）。

## 关注指标怎么定（**动态，不写死**）

- 申万截面里 `extra.industry_name` 命中该行业的行（如"银行"→ 申万一级银行 PE 7.27）；
- `indicator` 里含该行业名或行业词的时序点；
- 渗透率点（若问句涉及）。

⚠️ **`watched_indicator_count > 0` 是关键**：基类
`_skip_reason()` 在它为 0 且无事件/无申万/无渗透率时**静默跳过 LLM** ——
那是本仓最典型的失败模式（"没什么可分析的"其实只是"没匹配上"）。
本类把"申万截面按 `extra.industry_name` 命中"也算作关注指标，
正是为了让银行这类**只有截面、没有专属时序指标**的行业不再被跳过。
"""

from __future__ import annotations

import re
from typing import Any

from src.domain.agents.analysis.unlock_teaching import render_unlock_teaching
from src.domain.agents.industry.base import IndustryAgentBase

#: 不能当行业名的噪声（这些名字本身就是"综合/其他"的意思，用来分析没有信息量）
_NOISE_INDUSTRIES = frozenset({"", "综合", "其他", "综合行业"})


class GenericIndustryAgent(IndustryAgentBase):
    """兜底行业深度分析：行业名与关注指标**按输入内容解析**。

    继承 A13-A16 的骨架（本地景气信号 + 防幻觉 + 统一 prompt 契约），
    只覆盖"行业怎么确定 / 关注什么指标"这两个扩展点。
    """

    #: 类级默认（基类要求）。**实际值按请求解析**，见 `_industry_name_for`。
    industry_name = "综合"
    framework = "行业景气—估值—供需三维框架（无专属框架时用通用框架，并声明口径）"
    watch_keywords = ()   #: 类级默认空；按请求动态解析
    #: ★ 声明"我按请求解析行业与关注指标" —— 让护栏走动态分支而不是判我缺声明
    #: （见 `test_contract_consistency.py::test_industry_watch_keywords_...`）
    dynamic_industry = True
    #: 通用框架下不设行业特定警戒线，沿用基类默认（40 倍）
    pe_high_watermark = 40.0
    capabilities_names = ("generic_industry_analysis", "dynamic_industry_routing")

    system_prompt = (

        render_unlock_teaching("A20_generic_industry")

        +
        "资深行业分析师，负责**没有专属行业 Agent 覆盖**的行业"
        "（如银行/非银金融/公用事业/交通运输/建筑等）。"
        "你必须：①首句点名你在分析哪个行业以及**该行业是怎么确定的**"
        "（个股代码→本地名录 / 股票简称 / 问句直述）；"
        "②只依据给定数据点说话，**不得**用训练记忆补行业数据；"
        "③若本行业只有估值截面、没有专属产业时序指标，**必须明说这一边界**，"
        "并把结论限定在估值与已知宏观事实上；"
        "④禁脱离数据预测具体涨跌幅。"
    )

    def __init__(self, gateway, agent_id: str = "A20_generic_industry") -> None:
        super().__init__(agent_id, gateway)

    # ------------------------------------------------------------------
    # 行业解析（确定性三路 + 一路兜底）
    # ------------------------------------------------------------------

    @staticmethod
    def _text(payload) -> str:
        return f"{payload.focus or ''} {payload.user_query or ''}".strip()

    def resolve_industry(self, payload) -> tuple[str, str]:
        """返回 `(行业名, 依据说明)`；解析不出时行业名为空串。

        ⚠️ 真正的解析在 `catalog/industry_of.resolve_industry_from_text` ——
        **编排层（决定要不要挂本 Agent）与这里必须用同一份实现**，
        否则"编排层认为该挂兜底、Agent 自己解析出另一个行业"，
        结论里的行业名会与路由不一致，且不报错。
        """
        from src.infrastructure.catalog.industry_of import (
            resolve_industry_from_text,
        )

        return resolve_industry_from_text(self._text(payload))

    # ------------------------------------------------------------------
    # 覆盖基类的两个扩展点
    # ------------------------------------------------------------------

    def _industry_name_for(self, payload) -> str:
        name, _how = self.resolve_industry(payload)
        return name or self.industry_name  # 解析不出时退回"综合"

    def _framework_for(self, payload) -> str:
        name, how = self.resolve_industry(payload)
        if not name or name in _NOISE_INDUSTRIES:
            return self.framework
        return (f"{self.framework}；本行业={name}"
                f"（判定依据：{how or '未解析出'}）")

    def _watch_keywords_for(self, payload) -> tuple[str, ...]:
        """按解析出的行业给出关键词（用于**时序点**的子串匹配）。

        申万截面那类"行业名在 `extra` 里、不在 indicator 里"的点，
        由下面 `_watched_points` 单独处理 —— 两者的匹配面不同，必须分开。
        """
        name, _how = self.resolve_industry(payload)
        out: list[str] = []
        if name:
            out.append(name)
            # 行业名的核心词（"银行"→"银行"；"通信设备"→"通信"/"设备"）
            for token in re.findall(r"[\u4e00-\u9fff]{2,}", name):
                out.append(token)
        out.append("ind:sw_")          # 申万截面永远可支撑行业研判
        out.append("ind:penetration")  # 渗透率（若问句涉及成长赛道）
        return tuple(dict.fromkeys(out))

    def _watched_points(self, payload) -> list[dict[str, Any]]:
        """关注点 = 基类的子串匹配 **∪** 申万截面里 `extra.industry_name` 命中本行业的行。

        为什么要多这一路（实测）：银行这类行业**没有专属产业时序指标**，
        它在本地唯一的行业级数据就是申万截面里那一行
        （`ind:sw_first_pe_ttm:all` 且 `extra.industry_name == '银行'`）。
        只靠 `indicator` 子串匹配 ⇒ `watched_indicator_count == 0`
        ⇒ 基类 `_skip_reason()` **静默跳过 LLM** ⇒ 用户看到
        「无本行业关注指标…跳过LLM定性」——**那正是本次报障的形态**。
        """
        watched = list(super()._watched_points(payload))
        seen = {id(p) for p in watched}
        name, _how = self.resolve_industry(payload)
        if not name:
            return watched
        for p in payload.data_points:
            if id(p) in seen:
                continue
            extra = p.get("extra")
            if not isinstance(extra, dict):
                continue
            if str(extra.get("industry_name") or "").strip() == name:
                watched.append(p)
                seen.add(id(p))
        return watched

    def _enrich_result(self, payload, data: dict[str, Any]) -> dict[str, Any]:
        name, how = self.resolve_industry(payload)
        data = super()._enrich_result(payload, data)
        # 溯源：把"行业是怎么确定的"随结果下发（用户要能判这个结论准不准）
        data["generic_industry_resolution"] = {
            "industry": name,
            "basis": how,
            "resolved": bool(name),
            "watched_from_sw_section": sum(
                1 for p in self._watched_points(payload)
                if isinstance(p.get("extra"), dict)
                and str(p["extra"].get("industry_name") or "") == name),
        }
        return data
