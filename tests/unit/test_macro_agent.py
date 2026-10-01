"""A08 宏观分析Agent 测试（含纯模板路径）。"""

import json

from src.core.models import AgentInput
from src.domain.agents.analysis.macro.agent import MacroAnalysisAgent
from src.domain.agents.analysis.macro.agent import _latest_numeric


class FakeGateway:
    def __init__(self, reply: dict) -> None:
        self._reply = reply
        self.calls: list[dict] = []

    async def complete(self, task_tier, system, prompt, **kwargs):
        self.calls.append({"task_tier": task_tier, "system": system,
                           "prompt": prompt, **kwargs})
        from src.infrastructure.llm.models import LLMResponse
        return LLMResponse(
            content=json.dumps(self._reply, ensure_ascii=False),
            model_used="fake-model", provider="fake",
            prompt_hash="ph", response_hash="rh",
        )


def _make_input(payload: dict) -> AgentInput:
    return AgentInput(task_id="t08", tenant_id="tenant_001", payload=payload)


def _dp(indicator: str, value: float, period: str = "2026-08") -> dict:
    return {"indicator": indicator, "value": value, "period_date": period,
            "source_name": "AkShare", "data_id": f"d_{indicator}"}


# ---------- 内部工具函数 ----------


def test_latest_numeric_basic():
    pts = [
        _dp("CPI", 2.0, "2026-08"),
        _dp("CPI", 2.5, "2026-07"),
        _dp("PPI", -1.0, "2026-08"),
    ]
    assert _latest_numeric(pts, "CPI") == 2.0  # 取最新期
    assert _latest_numeric(pts, "PPI") == -1.0
    assert _latest_numeric(pts, "missing") is None


def test_latest_numeric_handles_bad_value():
    pts = [_dp("CPI", "bad")]
    assert _latest_numeric(pts, "CPI") is None


# ---------- 纯模板路径（第十轮）----------


async def test_macro_template_skips_llm_when_trigger_and_data_present():
    """命中'美联储/加息'且有 ≥2 条宏观数据 → 走纯模板，不调 LLM。

    审计实证：macro 任务里 A08 一次云端调用 ≈ 10s（reasoning 8B
    本地或 flash），纯模板省下这一刀。
    """
    gw = FakeGateway({})  # 不应被调
    payload = {
        "focus": "宏观",
        "user_query": "美联储年内还会加息吗",
        "data_points": [
            _dp("us_fed_rate", 5.25, "2026-09"),
            _dp("us_cpi_yoy", 3.2, "2026-08"),
            _dp("us_unemployment", 3.8, "2026-08"),  # 配合 CPI 定 cycle
            _dp("CPI", 0.5, "2026-08"),
        ],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert gw.calls == []  # ★ 关键：纯模板不调 LLM
    assert out.result["model_used"] == "rule-only"
    assert out.result["cycle_position"] == "过热"  # CPI 3.2 ≥3 且 失业率 3.8 ≤4.5
    assert "鹰派偏紧" in out.conclusion
    assert out.result["liquidity"] == "收紧"


async def test_macro_template_does_not_skip_when_no_trigger():
    """未命中模板触发词 → 必须调 LLM（不省这一刀，但保持行为正确）。"""
    gw = FakeGateway({
        "conclusion": "结构性通胀分析", "confidence": "medium",
        "cycle_position": "不明确", "liquidity": "中性",
        "key_points": ["x"], "risks": ["y"],
    })
    payload = {
        "focus": "宏观",
        "user_query": "为什么最近猪肉价格波动这么大",  # ← 不在模板触发词
        "data_points": [_dp("CPI", 0.5), _dp("us_fed_rate", 5.25)],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert len(gw.calls) == 1  # 调了 LLM


async def test_macro_template_does_not_skip_when_too_few_macro_points():
    """命中触发词但宏观数据 < 2 条 → 仍走 LLM，避免幻觉硬出模板。

    例：collect 完全失败时（只拉回 1 条数据）走模板会硬编结论。
    """
    gw = FakeGateway({
        "conclusion": "数据不足声明缺口", "confidence": "low",
        "cycle_position": "不明确", "liquidity": "不明确",
        "key_points": [], "risks": [],
    })
    payload = {
        "focus": "宏观",
        "user_query": "美联储加息对A股影响",  # 触发词命中
        "data_points": [_dp("us_fed_rate", 5.25)],  # 仅 1 条宏观点
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert len(gw.calls) == 1  # 仍走 LLM


async def test_macro_template_handles_missing_us_data():
    """缺美国数据时降级为'不明确'，不报错。

    ⚠️ 第二十三轮后这条的语义**收窄了**：用户问的是"美联储政策"（**没有**
    问利率方向），所以闸门 3 不触发，模板照走 —— 此时"缺少利率数据"
    是**如实登记**，不是答非所问。
    见 `test_macro_template_falls_back_to_llm_when_rate_data_missing`：
    用户**明确问利率**而利率数据缺失时，行为**相反**（退回 LLM）。
    """
    gw = FakeGateway({})
    payload = {
        "focus": "宏观",
        "user_query": "美联储政策",
        "data_points": [_dp("CPI", 0.5), _dp("PPI", -1.0)],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert gw.calls == []
    assert "数据缺失" in out.conclusion or "缺少" in out.conclusion


# ---------- ★ 第二十三轮：闸门 3（必答口径齐备）回归 ----------


async def test_macro_template_falls_back_to_llm_when_rate_data_missing():
    """★★★ 报障现场回归：**问利率却没利率数据** → 必须退回 LLM，不许模板硬答。

    ## 真实报障（2026-09-28）

    用户问：「当前宏观环境如何，**预测下一年美国的加息、降息节奏**，
    对A股的影响，以及AI应用加速失业率增加对消费的影响节奏时间节点分析，
    未来半年能否持有高股息的招商银行？」

    前端显示：`model=rule-only … 缺少联邦基金利率数据，无法判定方向`

    ## 为什么旧的闸门放它过去了

        闸门 1：命中"加息"/"降息"                        ✅
        闸门 2：宏观类数据点 ≥ 2                          ✅（48 条）
                ↑ 全是 CPI/PPI/us_cpi_yoy/us_core_cpi —— **没有一条利率数据**

    闸门 2 数的是"宏观数据**条数**"，不是"这道题**需要**的数据"。
    于是系统用 CPI 的条数证明了"可以跳过 LLM"，然后输出"无法判定方向"。
    **省了成本，没回答问题。**

    这条测试锁死新行为：明确问利率 + 无利率数据 → `_should_skip_llm` 为假
    → 走 LLM 完整路径（带真实缺口、给方向）。
    """
    gw = FakeGateway({
        "conclusion": "利率数据缺失，按通胀回落给方向：降息概率上升",
        "confidence": "low", "cycle_position": "不明确", "liquidity": "不明确",
        "key_points": ["缺联邦基金利率"], "risks": ["数据缺口"],
    })
    payload = {
        "focus": "宏观",
        "user_query": "预测下一年美国的加息、降息节奏",
        "data_points": [  # 只有 CPI 类，**一条利率数据都没有**（复刻报障现场）
            _dp("CPI", 0.8, "2026-09"),
            _dp("PPI", 3.8, "2026-09"),
            _dp("us_cpi_yoy", 3.4, "2026-08"),
            _dp("us_core_cpi", 0.3, "2025-08"),
        ],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert len(gw.calls) == 1, (
        "问利率却没有利率数据时仍走了纯模板 —— 用户会继续看到"
        "「缺少联邦基金利率数据，无法判定方向」这句没回答问题的话"
    )
    assert out.result["model_used"] != "rule-only"


async def test_macro_template_still_used_when_rate_data_present():
    """反向：**有**利率数据时仍走模板（不能把省成本的那条路堵死）。"""
    gw = FakeGateway({})  # 不应被调
    payload = {
        "focus": "宏观",
        "user_query": "预测下一年美国的加息、降息节奏",
        "data_points": [
            _dp("fed:effr", 3.88, "2026-09-24"),
            _dp("fed:target_upper", 4.00, "2026-09-27"),
            _dp("fed:target_lower", 3.75, "2026-09-27"),
            _dp("us_cpi_yoy", 3.2, "2026-08"),
            _dp("us_unemployment", 3.8, "2026-08"),
            _dp("CPI", 0.5, "2026-08"),
        ],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert gw.calls == [], "有齐全利率数据时不该白花一次云端调用"
    assert out.result["model_used"] == "rule-only"
    assert "3.75%~4.00%" in out.conclusion, "目标区间必须**成对**报出"


async def test_macro_template_accepts_legacy_policy_range_as_rate_data():
    """旧混合口径 `fed:policy_range` 存在时也算"有利率数据"（历史库兼容）。

    为什么不能要求只有新口径：库里已有 3 条旧口径数据，
    判成"缺失"会把**可用信息一起丢掉**。
    """
    gw = FakeGateway({})  # 不应被调
    payload = {
        "focus": "宏观",
        "user_query": "美联储还会降息吗",
        "data_points": [
            _dp("fed:policy_range", 3.88, "2026-09-24"),
            _dp("us_cpi_yoy", 3.2, "2026-08"),
            _dp("CPI", 0.5, "2026-08"),
        ],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert gw.calls == []
    assert out.result["model_used"] == "rule-only"


async def test_macro_template_never_reports_range_bound_as_the_rate():
    """★★ 回归：**禁止把区间的一边当成政策利率**报出去。

    ## 这个 bug 比"缺数据"更危险

    旧 `fed:policy_range` 把 FRED 的三个序列（DFEDTARU 上限 4.00 /
    DFEDTARL 下限 3.75 / DFF 有效利率 3.88）**写成同一个 indicator**，
    同一天三条。而旧模板用 `_latest_numeric` 取"最新一条" →
    取到的是**上限或下限之一**，输出
    「目标区间 3.75%（FRED 口径）」——
    数字看着有据，**语义是错的**（3.75 是区间下限，不是利率）。

    判据：不能出现"目标区间 <单个数字>%"这种把区间写成标量的形状；
    旧口径必须**成对**读出（`3.75%~4.00%`）。
    """
    gw = FakeGateway({})  # 不应被调
    payload = {
        "focus": "宏观",
        "user_query": "美联储加息节奏",
        "data_points": [
            _dp("fed:policy_range", 4.00, "2026-09-27"),   # DFEDTARU 上限
            _dp("fed:policy_range", 3.75, "2026-09-27"),   # DFEDTARL 下限
            _dp("us_cpi_yoy", 3.2, "2026-08"),
            _dp("CPI", 0.5, "2026-08"),
        ],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert gw.calls == []
    assert "3.75%~4.00%" in out.conclusion, (
        f"旧口径没有成对读出区间，结论是：{out.conclusion}"
    )
    # 关键反向断言：不许把单边值当区间
    assert "目标区间 3.75%（" not in out.conclusion
    assert "目标区间 4.00%（" not in out.conclusion


async def test_macro_template_prefers_effr_over_target_bounds():
    """方向判断优先用**有效利率**（可判鹰鸽），而不是目标区间的某一边。

    有效利率 3.88 落在 [3.5, 4.5) → "中性偏紧"；
    若错用上限 4.00 也是同一档，所以这条额外用 5.25 的有效利率验证
    鹰派判断确实来自 effr 而不是 bounds。
    """
    gw = FakeGateway({})  # 不应被调
    payload = {
        "focus": "宏观",
        "user_query": "美联储还会加息吗",
        "data_points": [
            _dp("fed:effr", 5.25, "2026-09-27"),
            _dp("fed:target_upper", 5.50, "2026-09-27"),
            _dp("fed:target_lower", 5.25, "2026-09-27"),
            _dp("us_cpi_yoy", 3.2, "2026-08"),
            _dp("us_unemployment", 3.8, "2026-08"),
            _dp("CPI", 0.5, "2026-08"),
        ],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert "鹰派偏紧" in out.conclusion
    assert "5.50%" in out.conclusion and "5.25%" in out.conclusion  # 区间成对


# ---------- ★ 第二十三轮：规则路径的审计留痕（消除审计黑洞）----------


class _RecordingAudit:
    """假审计：只记下 record() 的入参。"""

    def __init__(self) -> None:
        self.entries: list[dict] = []

    def record(self, **kwargs) -> dict:
        self.entries.append(kwargs)
        return kwargs


class RecordingGateway(FakeGateway):
    """带 `audit_log` 的假网关（生产网关通过 `audit_log` 属性暴露审计）。"""

    def __init__(self, reply: dict) -> None:
        super().__init__(reply)
        self.audit_log = _RecordingAudit()


async def test_rule_only_path_writes_audit_entry():
    """★★★ 走纯模板时**必须**在 LLM 审计里留痕（tokens=0、provider=rule）。

    ## 为什么这条是硬约束（AGENTS.md）

    > **「没量到」与「量到 0」必须分开显示**；读不到数据时显示"未量到/
    > 无法统计"，绝不用 0 糊过去。

    修复前：规则路径直接 return，**一个字节都不写审计**
    （`LLMAuditLog.record()` 只在网关里被调用）。实测现场 ——
    用户问宏观问题时审计里只有 planning + A17 两条，A08-A12 全部无痕。
    排查时第一反应是"分析层没执行"，真相是"走了规则路径"。
    **两种完全不同的原因，在审计里长得一模一样。**

    这条测试锁死：规则判定也要落一条（tokens 真为 0，不是"未量到"）。
    """
    gw = RecordingGateway({})  # LLM 不应被调
    payload = {
        "focus": "宏观",
        "user_query": "美联储年内还会加息吗",
        "data_points": [
            _dp("fed:effr", 3.88, "2026-09-24"),
            _dp("us_cpi_yoy", 3.2, "2026-08"),
            _dp("us_unemployment", 3.8, "2026-08"),
        ],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert gw.calls == []                       # 确实没调 LLM
    assert out.result["model_used"] == "rule-only"
    # ★ 但审计里**必须有一条**
    assert len(gw.audit_log.entries) == 1, (
        "规则路径没有落审计 —— 『走了模板』与『压根没跑』又变成不可区分了"
    )
    entry = gw.audit_log.entries[0]
    resp = entry["response"]
    assert resp.provider == "rule"
    assert resp.tokens_in == 0 and resp.tokens_out == 0
    assert resp.model_used == "rule-only"
    assert entry["cached"] is False
    assert "rule_only" in (entry["error"] or "")
    # 原因要能读懂（含宏观点数与利率口径有无）
    assert "macro_points=" in entry["error"]
    assert "rate_data=" in entry["error"]


async def test_rule_only_audit_keeps_cost_at_zero():
    """★ 留痕**不能污染账单**：这条记录必须计价为 0。

    `tokens_by_tenant()` 会把审计里所有行的 tokens_in/out 累加；
    `call_cost_cny()` 对未定价模型返回 0。
    谁把 tokens 写成真实值，谁就让"规则判定"开始花钱记账。
    """
    from src.core.budget import call_cost_cny

    gw = RecordingGateway({})
    payload = {
        "focus": "宏观",
        "user_query": "美联储还会加息吗",
        "data_points": [
            _dp("fed:target_upper", 4.00, "2026-09-27"),
            _dp("fed:target_lower", 3.75, "2026-09-27"),
            _dp("CPI", 0.5, "2026-08"),
        ],
    }
    await MacroAnalysisAgent(gw).execute(_make_input(payload))
    resp = gw.audit_log.entries[0]["response"]
    assert resp.tokens_in == 0 and resp.tokens_out == 0
    cost = call_cost_cny(provider=resp.provider, model=resp.model_used,
                         tokens_in=resp.tokens_in, tokens_out=resp.tokens_out,
                         provider_cache_hit=False)
    assert cost == 0, f"规则判定的计价应为 0，实际 {cost}"


async def test_rule_only_audit_failure_does_not_break_fallback():
    """★ 审计写失败**绝不能**把兜底路径也弄坏。

    规则路径的存在意义就是"LLM 不可用时还有结果"。若审计异常能穿透，
    就等于给兜底加了一个新的失败点（本项目对"审计不能因环境问题丢记录"
    的纪律是同一取向：审计是旁路，不是主路）。
    """

    class ExplodingAudit:
        def record(self, **kwargs):
            raise OSError("disk full")

    gw = RecordingGateway({})
    gw.audit_log = ExplodingAudit()
    payload = {
        "focus": "宏观",
        "user_query": "美联储还会加息吗",
        "data_points": [
            _dp("fed:effr", 3.88, "2026-09-24"),
            _dp("us_cpi_yoy", 3.2, "2026-08"),
        ],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert out.result["model_used"] == "rule-only"
    assert out.conclusion  # 仍然给出了结论


async def test_rule_only_works_with_gateway_without_audit_log():
    """★ 假网关/测试替身**不需要**实现审计接口（否则是把替身当生产依赖）。

    大量既有测试用只有 `complete()` 的 FakeGateway；
    要求它们都加 `audit_log` 会把"审计留痕"变成一次全仓库测试改造。
    """
    gw = FakeGateway({})  # 没有 audit_log 属性
    payload = {
        "focus": "宏观",
        "user_query": "美联储还会加息吗",
        "data_points": [
            _dp("fed:effr", 3.88, "2026-09-24"),
            _dp("us_cpi_yoy", 3.2, "2026-08"),
        ],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert out.result["model_used"] == "rule-only"


# ---------- ★ 陈旧值标注（修完白名单后暴露的第二层缺陷）----------


async def test_stale_rate_value_is_annotated_not_silently_used():
    """★★★ 陈旧利率必须**标注"可能已过时"**，不能当成今天的判断。

    ## 这个缺陷是在修白名单之后才暴露出来的（实测链条）

    修好白名单后 A08 拿得到 `us_fed_rate = 4.5`，而它的期间是
    **2025-07-31**（AkShare 那条源停更 14 个月）。模板据此输出
    「降息空间有限，年内再加息概率不低」—— **与一年前数据对应的结论**，
    读起来却像今天的判断；而同样在库里的 FRED 口径（更近）
    指向另一档。

    ## 判据

    · 陈旧值**仍然报**（它是真实历史值，丢掉等于让"源停更"不可见）；
    · 但结论里必须出现"过时/距今"这类**可核对**的标注；
    · `key_points` 必须带**期间**（用户要能自己看出这是哪一期的数）。
    """
    gw = FakeGateway({})  # 不应被调
    payload = {
        "focus": "宏观",
        "user_query": "预测下一年美国的加息、降息节奏",
        "data_points": [
            # 陈旧：距今一年以上（AkShare 实际停更位置）
            _dp("us_fed_rate", 4.50, "2025-07-31"),
            _dp("us_cpi_yoy", 3.2, "2026-08"),
            _dp("us_unemployment", 3.8, "2026-08"),
            _dp("CPI", 0.5, "2026-08"),
        ],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert gw.calls == []
    assert "过时" in out.conclusion, (
        f"陈旧利率没有被标注，结论是：{out.conclusion}"
    )
    kps = " ".join(out.result.get("key_points", []))
    assert "2025-07-31" in kps, (
        f"key_points 没带期间，用户无法判断这个数是不是本期的：{kps}"
    )


async def test_fresh_rate_value_is_not_annotated():
    """反向：**新鲜**利率值不该被误标"过时"（否则标注会变成噪音、没人看）。"""
    gw = FakeGateway({})  # 不应被调
    from datetime import date, timedelta

    fresh = (date.today() - timedelta(days=3)).isoformat()
    payload = {
        "focus": "宏观",
        "user_query": "美联储还会加息吗",
        "data_points": [
            _dp("fed:effr", 3.88, fresh),
            _dp("fed:target_upper", 4.00, fresh),
            _dp("fed:target_lower", 3.75, fresh),
            _dp("us_cpi_yoy", 3.2, "2026-08"),
        ],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert "过时" not in out.conclusion, (
        f"新鲜值被误标成过时，结论是：{out.conclusion}"
    )


async def test_fresh_series_wins_over_stale_series_for_direction():
    """★★ 新旧两套口径同时存在时，方向必须按**新鲜的那套**判。

    实测现场：`us_fed_rate = 4.5`（2025-07，陈旧）与
    `fed:effr = 3.88`（2026-09，新鲜）同时在 payload 里 ——
    两者落在**不同**的鹰鸽档（4.5 → 鹰派偏紧 / 3.88 → 中性偏紧）。
    用错哪一个，结论方向就相反。
    """
    gw = FakeGateway({})  # 不应被调
    payload = {
        "focus": "宏观",
        "user_query": "预测下一年美国的加息、降息节奏",
        "data_points": [
            _dp("us_fed_rate", 4.50, "2025-07-31"),   # 陈旧、偏鹰
            _dp("fed:effr", 3.88, "2026-09-24"),      # 新鲜、中性偏紧
            _dp("fed:target_upper", 4.00, "2026-09-27"),
            _dp("fed:target_lower", 3.75, "2026-09-27"),
            _dp("us_cpi_yoy", 3.2, "2026-08"),
            _dp("us_unemployment", 3.8, "2026-08"),
            _dp("CPI", 0.5, "2026-08"),
        ],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert "中性偏紧" in out.conclusion, (
        f"方向没有按新鲜的 fed:effr(3.88) 判，结论：{out.conclusion}"
    )
    assert "3.88" in " ".join(out.result.get("key_points", []))


# ---------- 常规路径（未触发模板）----------


async def test_macro_normal_path_calls_llm():
    """未命中模板触发词 → 完整 LLM 路径，reasoning tier。"""
    gw = FakeGateway({
        "conclusion": "宏观结论", "confidence": "medium",
        "cycle_position": "复苏", "liquidity": "宽松",
        "key_points": ["CPI 2.1%"], "risks": ["外需"],
    })
    payload = {
        "focus": "宏观",
        "user_query": "中国 PMI 怎么看",
        "data_points": [_dp("PMI", 50.5), _dp("CPI", 2.0)],
    }
    out = await MacroAnalysisAgent(gw).execute(_make_input(payload))
    assert len(gw.calls) == 1
    assert gw.calls[0]["task_tier"] == "reasoning"