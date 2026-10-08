"""A12 合规爆雷Agent（合规风险与爆雷预警，PRD A12，P2）。

本地规则引擎（logic.py）计算合规旗标与爆雷等级，覆盖比率异常、存贷双高、
信息层诉讼/监管事件三类信号；LLM只负责综合定性，禁止自造风险事实。

## ★★★ 2026-09-27 第八轮：纯规则路径（−18,500 tokens_in/轮）

审计实证：A12 投到 `reasoning` 云端，每轮 ~18,500 tokens_in（占云端调用的 ~30%）。
但 A12 真正消耗的只是：
  · 财务比率阈值（关联交易/商誉/质押/担保 4 类，纯数字比较）；
  · 信息层 A06 抽取的诉讼/监管事件（已在 A06 阶段结构化）；
  · 存贷双高（货币资金 vs 有息负债两个数字）。

这三类信号 `evaluate_compliance()` 已经全部本地算好，LLM 的"综合定性"
**在"无信号"分支里是冗余的**（让 LLM 把"无"再写一遍 = 浪费 18.5k tokens）。

改造：当本地规则给出 `compliance_level_calc == "无"` **且**没有 events 时，
A12 直接以纯规则结果返回（跳过 LLM 调用），同时把 `model_used` 标记为
`"rule-only"` 方便审计追溯。

## ★★★ 2026-09-29：同一支路径里的两副面孔

上面那条优化的**前提**是「本地规则已经判定完了」。但 `level` 当时只有三个取值，
而 `else` 分支同时收下了两种**语义相反**的处境（详见 `logic.py` 模块 docstring）：

| 处境 | 该说的话 | 旧输出 |
|---|---|---|
| 6 个族都量到了、都没超阈值 | 「无」 | 「无」+「未见明显合规风险信号」+ confidence=high |
| **6 个族一条都没量到** | 「未量到」 | **同上**（一模一样） |

于是"没有输入"被渲染成一张**伪造的体检合格证**，而且因为走了纯规则路径，
连 LLM 都不会有异议。现在两条分支仍然都省 token，但输出严格分开：
`_rule_only_reason` 取值 `measured_clean` / `no_input`，后者
`compliance_level="未量到"`、`confidence="low"`、`burst_risk` 明写"无法判定"。

## ★★★ 2026-10-02：多标的（一次问句 ≥2 只票）不许共用一张合格证

`CHG-0216` 之后个股指标按 `resolved_codes` **逐只**排，所以 A12 的
`payload.data_points` 里同时躺着两只票的同名指标（`商誉占净资产比:601088`
与 `商誉占净资产比:002142`）。本地规则原先把整份输入当成**一只票**在扫，
于是能凭"首个命中"把 A 的 `货币资金` 与 B 的 `有息负债` 拼成一条
**两家合起来才有**的「存贷双高」，并把两只票各 3 个族并成"6/6 族"。

规则侧修法见 `logic.py`（按代码分组、逐只判定）。本文件的四处配合：

1. `_should_skip_llm()`：**多标的一律不跳 LLM**。纯规则路径的文案是
   **单数口径**的（"已量到 2/6 族"），多标的时填谁的数都是错的、填并集
   就是那张假合格证 —— 所以不是"并集看起来齐全就跳过"，
   而是多标的**根本没有单数结论可跳过**；
2. `_build_rule_only_result()`：加 `multi_target_guard` 兜底
   （正常到不了，绕过了那道门也不许输出统一等级）；
3. `_requirements()`：补逐只口径约束 —— 那句原话「LLM结论须与
   `compliance_level_calc` 自洽」在汇总值上**单独存在就是危险的**，
   会把"最坏的那只票"复制给所有标的；多标的时改为**逐只自洽**
   ＋追加 `compliance_by_code` 逐票 JSON 字段；
4. `_enrich_result()`：把 `compliance_per_code` 等 per-code 字段随结果下发
   （扁平三件套是**汇总**：族清单取交集，不是任何一只票的读数）。
"""

from __future__ import annotations

import logging
from typing import Any

from src.domain.agents.analysis.base import (
    AnalysisAgentBase,
    AnalysisPayload,
    emit_per_item_rows,
    enforce_per_item_rows,
)
from src.domain.agents.analysis.compliance.logic import (
    FLAG_NO_SIGNAL,
    LEVEL_UNMEASURED,
    evaluate_compliance,
)
from src.domain.agents.analysis.unlock_teaching import render_unlock_teaching

logger = logging.getLogger(__name__)


def _families_coverage(row: dict[str, Any]) -> str:
    """`3/6 族` —— **逐只**的族覆盖率（多标的 prompt 里必须逐只报，不许报并集）。"""
    measured = list(row.get("families_measured") or [])
    unmeasured = list(row.get("families_unmeasured") or [])
    return f"{len(measured)}/{len(measured) + len(unmeasured)} 族"


class ComplianceAnalysisAgent(AnalysisAgentBase):
    """合规风险、爆雷风险预警（PRD A12，P2）。"""

    system_prompt = (

        render_unlock_teaching("A12_compliance")

        +
        "严谨合规风控专员，负责上市公司合规排雷与爆雷预警。依据财务比率、本地规则旗标"
        "与诉讼/监管事件研判；未提及的违规/诉讼不断言。爆雷等级须与旗标数量、"
        "严重程度自洽，不得弱化严重旗标。"
        # ★★★ 2026-10-01 CHG-0155（用户口径）：**预期差**驱动股价。
        + (
            "\n\n★★★ 投研分析核心原则：**概念板块与个股的股价上涨动力来自「预期差」，"
            "不是预期本身**。\n"
            "- 预期 = 市场已有共识 ⇒ 已被定价 = 中位线（**不构成涨跌动力**）；\n"
            "- **预期差 = 市场已有预期 vs 数据/事件推断的实际预期** 之差 ⇒ 涨跌的**真正动力**；\n"
            "- 正向预期差（实际 > 市场）⇒ 资金流入 / 估值上修；\n"
            "- 负向预期差（实际 < 市场）⇒ 资金撤离 / 估值下修；\n"
            "- 合规侧应用：判断合规风险 vs 市场未充分定价的潜在处罚；\n"
            "- **禁止**只罗列「预期数据」就给出方向结论；必须先定位预期差方向与幅度，再判方向；\n"
            "- 当上下文无市场预期数据时，显式声明「市场预期不可得」，输出「无法判方向」。"
        )
    )

    def __init__(self, gateway, agent_id: str = "A12_compliance",
                 skill_library=None) -> None:
        super().__init__(agent_id, gateway, skill_library)

    def get_capabilities(self) -> dict:
        return {
            "agent_id": self.agent_id,
            "capabilities": ["compliance_risk", "fraud_burst_warning",
                             "litigation_monitoring"],
            "task_tier": self.task_tier,
        }

    def health_check(self) -> bool:
        return True

    def _prepare(self, payload: AnalysisPayload) -> None:
        payload.hint["compliance_calc"] = evaluate_compliance(
            payload.data_points, payload.events
        )

    # ★ 2026-09-27：是否走纯规则路径
    #
    # ★ 2026-09-29 修正：`level` 现在有四个取值，**跳过 LLM 的那一支要分两种输出**
    #   ——「量到了、都没超阈值」与「一个族都没量到」处境相反，旧实现共用
    #   `level == "无"` 一次判断 + 一段文案，于是"没有输入"被写成
    #   「未见明显合规风险信号」+ confidence=high（伪造的体检合格证）。
    #   现在两支都跳过 LLM（都省 18.5k tokens/轮），但**结果文案完全不同**，
    #   见 `_build_rule_only_result()`。
    #
    # ★★ 2026-10-02：**多标的一律不跳 LLM**。
    #   纯规则路径的整套文案（`_build_rule_only_result`）是**单数口径**的：
    #   "本地合规规则扫描完成：…已量到 2/6 族…" —— 它描述的是"这一家公司"。
    #   多标的时这句话无论填哪只票的数都是错的，而填并集就是那张
    #   "A 量到 3 族 + B 量到 3 族 = 6/6"的假合格证（见 `logic.py` 的
    #   2026-10-02 一节）。所以这里不是"并集看起来齐全就跳过"，
    #   而是**多标的根本没有单数结论可跳过**：宁可花一次 reasoning tokens，
    #   也不许省出一张无归属的合格证。
    def _should_skip_llm(self, payload: AnalysisPayload) -> bool:
        calc = payload.hint.get("compliance_calc", {}) or {}
        level = str(calc.get("compliance_level_calc", ""))
        events = payload.events or []
        if events:
            return False
        if self._is_multi_target(calc):
            return False
        # 「量到了且没超阈值」→ 纯规则；
        # 「未量到」→ 也纯规则（LLM 同样没有输入，调它只会得到一段无据的定性）。
        return level in ("无", LEVEL_UNMEASURED)

    @staticmethod
    def _is_multi_target(calc: dict[str, Any]) -> bool:
        """本次判定是不是**多标的**（≥2 只票各有自己的合规结论）。

        判据取 `compliance_multi_target`（logic 侧的权威值）；
        `compliance_per_code` 只是交叉校验 —— 两个字段漂移时按"多"处理（保守）。
        """
        if calc.get("compliance_multi_target"):
            return True
        return len(calc.get("compliance_per_code") or []) > 1

    def _build_rule_only_result(self, payload: AnalysisPayload) -> dict[str, Any]:
        """纯规则结果：跳过 LLM 调用，直接输出。

        ## 三种 `_rule_only_reason`（审计里必须能分开）

        · `measured_clean`：真的量过了，都没超阈值 → 可以报「无」。
        · `no_input`：6 个族一条都没量到、也没有诉讼/监管事件 →
          **只能报「未量到」**，且 `confidence=low`、`burst_risk` 不许写
          "未见明确爆雷路径"（那也是一句无据的结论）。
        · `multi_target_guard`：**多标的**（★ 2026-10-02）。这条分支是**兜底**，
          正常路径到不了 —— `_should_skip_llm()` 对多标的直接返回 False。
          留着它是为了"有人绕过那道门"时**仍然不许**输出一张收下全部标的的
          合格证：上面两支的文案都是**单数口径**的（"这一家公司已量到 2/6 族"），
          套到多标的上，无论填哪只票的数都是错的，填并集就是那张
          "A 3 族 + B 3 族 = 6/6"的假合格证。

        为什么把 reason 也落进结果：没有它，"走了纯规则"与"压根没跑"
        在审计里长得一模一样，排查方向会被带偏（本项目 2026-09-28 实测过
        同款：规则式路径原先不写审计）。
        """
        calc = payload.hint.get("compliance_calc", {}) or {}
        level = str(calc.get("compliance_level_calc", LEVEL_UNMEASURED))
        measured = list(calc.get("compliance_families_measured") or [])
        unmeasured = list(calc.get("compliance_families_unmeasured") or [])
        flags = list(calc.get("compliance_flags") or [])

        if self._is_multi_target(calc):
            return self._build_multi_target_guard_result(calc, flags)

        if level == LEVEL_UNMEASURED:
            conclusion = (
                "本地合规规则**未获得输入**，无法判定合规风险：6 类比率族"
                f"（{'/'.join(unmeasured) or '全部'}）一条都没量到，且无诉讼/监管事件。"
                "此结果**不等于「无风险」**，属数据缺口。"
            )
            return {
                "conclusion": conclusion[:200],
                "confidence": "low",
                "compliance_level": LEVEL_UNMEASURED,
                "burst_risk": "无法判定（本地规则未获得输入，非「无爆雷路径」）",
                "red_flags": [],
                "key_points": [
                    f"规则族未量到 {len(unmeasured)}/{len(unmeasured) + len(measured)}",
                    "数据缺口，不是「无风险」结论",
                    "纯规则判定，未调 LLM",
                ],
                "_rule_only": True,
                "_rule_only_reason": "no_input",
                "model_used": "rule-only",
            }

        # ★ 2026-09-29：**「无」结论必须自带覆盖率**。
        #
        # 现场：只量到 2/6 族时，结论文本原本只写「未见明显合规风险信号」——
        # 覆盖率藏在 `key_points` 里（"已量到规则族 2/6"），而**结论才是用户看的那一行**。
        # 这与 CHG-0073 修掉的"伪造合格证"是**同一条纪律的更细一层**：
        # 六族全没量到时报「无」是伪造（已修）；**两族量到就报「无」而不说清
        # 另外四族没量到**，仍然是一张**残缺的**合格证 —— 数字上没错，
        # 但用户会读成"这家公司合规方面查过了、没问题"。
        #
        # 四种「没量到」的原因**各不相同**，禁止合并成一句：
        #   · 该实体口径不适用（银行的「货币资金」）
        #   · 接口通了但这只票不在该专题表内（担保 / 质押）
        #   · 免费源没有这个比率口径（关联交易）
        #   · 本次没去取（计划里没排）——**最危险的一种，因为它长得像前三种**
        total_families = len(measured) + len(unmeasured)
        coverage = (
            f"已量到 {len(measured)}/{total_families} 族（{'/'.join(measured)}）"
            + (f"，未量到 {'/'.join(unmeasured)} —— "
               "未量到**不等于**这几种风险为零"
               if unmeasured else "")
        )
        conclusion = (
            f"本地合规规则扫描完成：{'; '.join(flags[:3])}。{coverage}。"
            "评级由本地规则计算（model_used=rule-only，跳过 LLM 调用）。"
        )
        key_points = [
            f"合规旗标 {len(flags)} 条",
            f"严重旗标 {calc.get('severe_flag_count', 0)} 条",
            f"已量到规则族 {len(measured)}/{total_families}",
        ]
        if unmeasured:
            key_points.append(
                "未量到族：" + "/".join(unmeasured)
                + "（各自原因不同：口径不适用 / 该票不在专题表内 / 无免费源 / 本次未取）")
        key_points.append("纯规则判定，无需 LLM")
        return {
            "conclusion": conclusion[:200],
            "confidence": "high" if not unmeasured else "medium",
            "compliance_level": level,
            "burst_risk": "未见明确爆雷路径",
            # 占位旗标不是风险明细，不许放进 red_flags
            "red_flags": [f for f in flags if f != FLAG_NO_SIGNAL],
            "key_points": key_points,
            "_rule_only": True,
            "_rule_only_reason": "measured_clean",
            "model_used": "rule-only",
        }

    @staticmethod
    def _build_multi_target_guard_result(
        calc: dict[str, Any], flags: list[str]
    ) -> dict[str, Any]:
        """★ 2026-10-02：多标的的**兜底**输出（正常到不了，见 `_should_skip_llm`）。

        它不许做的事很清楚：**不许给出一个收下全部标的的合规等级**。
        所以 `compliance_level` 固定填「未量到」（= 本次没有可用的统一结论），
        逐只结果放在 `conclusion` 里并原样随 `compliance_per_code` 下发。
        """
        rows = list(calc.get("compliance_per_code") or [])
        summary = "；".join(
            f"{row.get('label') or row.get('code') or '?'}={row.get('level')}"
            for row in rows
        ) or "无逐只结果"
        return {
            "conclusion": (
                f"本地合规规则按标的**分别**判定：{summary}。"
                "多标的**不得**合成一个合规等级，本结论只是一个占位，"
                "逐只明细见 compliance_per_code。"
            )[:200],
            "confidence": "low",
            "compliance_level": LEVEL_UNMEASURED,
            "burst_risk": "无法判定（多标的未逐只复核，非「无爆雷路径」）",
            # 占位旗标不是风险明细，不许放进 red_flags
            "red_flags": [f for f in flags if f != FLAG_NO_SIGNAL],
            "key_points": [
                f"标的数 {len(rows)}（逐只判定，禁止合并）",
                f"合规旗标 {len(flags)} 条",
                "多标的兜底：未输出统一等级",
                "纯规则判定，未调 LLM",
            ],
            "_rule_only": True,
            "_rule_only_reason": "multi_target_guard",
            "model_used": "rule-only",
        }

    def _enrich_result(self, payload: AnalysisPayload, data: dict[str, Any]) -> dict[str, Any]:
        calc = payload.hint.get("compliance_calc", {})
        data["compliance_flags_calc"] = calc.get("compliance_flags")
        data["severe_flag_count"] = calc.get("severe_flag_count")
        data["compliance_level_calc"] = calc.get("compliance_level_calc")
        # 溯源三件套随结果下发：界面/审计能判"这个结论准不准"，
        # 而不是把「无」与「未量到」渲染成同一个绿点。
        data["compliance_measured"] = calc.get("compliance_measured")
        data["compliance_families_measured"] = calc.get("compliance_families_measured")
        data["compliance_families_unmeasured"] = calc.get("compliance_families_unmeasured")
        # ★ 2026-10-02：per-code 维度随结果下发 —— 多标的时**唯一诚实**的表达方式。
        #   扁平三件套是逐只结果的汇总（族清单取交集），没有它，
        #   汇总字段会被读成"这家公司量到了 6/6 族"。
        #
        # ★★ `CHG-0241`（2026-10-08）：**单标的时这个键不再出现** ——
        #   发射判据改用共用实现 `emit_per_item_rows()`，与 A10 的
        #   `valuation_calc_by_code` 同一条规则（「≥2 条才出现」）。
        #   修复前 A12 单标的会多留 1 条，而 A10 单标的不出现 ⇒
        #   同一个消费方（前端 / A17）要处理**两种形状**。
        #   `compliance_multi_target` 是**布尔性质**（不是逐只明细）⇒ 仍然始终下发。
        if emit_per_item_rows(data, "compliance_per_code",
                              calc.get("compliance_per_code")):
            data["compliance_codes"] = calc.get("compliance_codes")
        data["compliance_multi_target"] = calc.get("compliance_multi_target")
        data["compliance_unattributed_event_flags"] = calc.get(
            "compliance_unattributed_event_flags")
        # ★★ `CHG-0236`：多标的**运行时**等长校验（与 A17 的 `per_subject`
        #   共用 `enforce_per_item_rows` —— 「同一判断只允许一份实现」）。
        #
        # 为什么挂在 `_enrich_result` 而不是 `execute`：基类的 LLM 解析在
        # `AnalysisAgent.execute()` 里，子类没有那个钩子；而 `_enrich_result`
        # 是**两条路径（LLM / 纯规则）都要过**的唯一收口 ⇒ 挂这里才不会漏。
        #
        # ⚠️ **纯规则路径显式跳过**：那条分支（`multi_target_guard`）
        #    **压根没调 LLM**，要求它给 `compliance_by_code` 是**无中生有**，
        #    补出来的占位行会把"规则逐只判定过"误标成"模型没返回"。
        #    它是否安全由 `_build_multi_target_guard_result()` 保证
        #    （固定不许输出统一等级），不靠这一层。
        if self._is_multi_target(calc) and not data.get("_rule_only"):
            enforce_per_item_rows(
                data, "compliance_by_code",
                [str(c) for c in (calc.get("compliance_codes") or [])],
                where="A12_compliance/_enrich_result",
                label="标的",
                placeholder_fields={
                    "level": LEVEL_UNMEASURED,
                    "burst_risk": "该标的未返回逐只结论（非「无爆雷路径」）",
                    "red_flags": [],
                    "_placeholder": "A12 未返回该标的的逐只合规结论",
                },
                warning_prefix="A12 未按标的逐只返回合规结论",
            )
        return data

    def _requirements(self, payload: AnalysisPayload) -> str:
        calc = payload.hint.get("compliance_calc", {}) or {}
        calc_level = calc.get("compliance_level_calc", LEVEL_UNMEASURED)
        unmeasured = list(calc.get("compliance_families_unmeasured") or [])
        gap_note = ""
        if calc_level == LEVEL_UNMEASURED:
            gap_note = (
                "\n★★ 本地规则本次**未获得输入**（未量到的族："
                f"{'/'.join(unmeasured) or '全部'}）。"
                "你**不得**输出「未见合规风险」「无风险」这类结论；"
                "必须把这是**数据缺口**写明，并把 compliance_level 填 `未量到`。\n"
            )
        return (
            f"本地规则爆雷等级为「{calc_level}」，LLM结论须与此自洽。\n"
            f"{gap_note}"
            f"{self._multi_target_note(calc)}"
            "输出JSON：\n"
            '- "conclusion": 合规与爆雷可能性评估，120字内，须引用具体旗标/事件\n'
            '- "confidence": high|medium|low\n'
            '- "compliance_level": 高|中|无|未量到（须与本地规则一致）\n'
            '- "burst_risk": 爆雷路径简述（质押平仓/商誉减值/立案处罚；无则填"未见明确爆雷路径"）\n'
            '- "red_flags": 风险明细数组（与本地旗标呼应）\n'
            '- "key_points": 2-4条'
            f"{self._multi_target_json_note(calc)}"
        )

    @staticmethod
    def _multi_target_note(calc: dict[str, Any]) -> str:
        """★ 2026-10-02：多标的时给 LLM 的**逐票口径**约束（单标的不加，逐字不变）。

        prompt 里还有一句原话是「LLM结论须与此自洽」——它单独存在时是**危险**的：
        多标的的 `compliance_level_calc` 是**汇总**（取最坏），一只票的读数
        与它对齐就等于把另一个标的的处境也算到自己头上。所以这里必须补一句
        "自洽"是**逐只**自洽，不是让所有票共用一个等级。
        """
        rows = list(calc.get("compliance_per_code") or [])
        if len(rows) < 2:
            return ""
        detail = "；".join(
            f"{row.get('label') or row.get('code') or '?'}={row.get('level')}"
            f"（量到 {_families_coverage(row)}）"
            for row in rows
        )
        return (
            "\n★★★ 本次是**多标的**（同一份上下文里有 "
            f"{len(rows)} 个标的）：{detail}。\n"
            "硬性要求：\n"
            "1. **逐只给结论**，每只票的等级/旗标只能引用**它自己**的数据"
            "（旗标已带 `[代码]` 前缀标明归属，禁止把 A 的旗标写到 B 头上）；\n"
            "2. 上面的「本地规则爆雷等级」是**汇总值（取最坏）**，"
            "**不是**每只票的读数 —— 禁止把汇总值复制给所有标的；\n"
            "3. 某只票「未量到」时，对该票只能说**数据缺口**，"
            "不许说它「未见合规风险」；\n"
            "4. 汇总层计数（`compliance_families_measured`）是**交集**"
            "（每只票都量到的族），**不许**读成「这家公司量到了 6/6 族」。\n"
        )

    @staticmethod
    def _multi_target_json_note(calc: dict[str, Any]) -> str:
        """多标的时追加一个**逐票**JSON 字段（单标的不加，逐字不变）。"""
        if len(calc.get("compliance_per_code") or []) < 2:
            return ""
        return (
            '\n- "compliance_by_code": 逐只结论数组（多标的**必填**），'
            '每项 {"code": "6位代码", "level": "高|中|无|未量到", '
            '"burst_risk": "该票的爆雷路径", "red_flags": ["该票自己的旗标"]}'
        )

