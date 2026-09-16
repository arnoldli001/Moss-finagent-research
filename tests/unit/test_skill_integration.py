"""Agent技能注入集成测试：分析层基类触发匹配 / A06/A07固定技能 / A17 ReAct工具。"""

from pathlib import Path

from src.domain.agents.analysis import MacroAnalysisAgent
from src.domain.agents.info.extractor import ExtractorAgent
from src.domain.agents.info.sentiment import SentimentAgent
from src.domain.skills.library import SkillLibrary
from tests.unit.test_analysis_agents import MACRO_REPLY, FakeGateway, _dp, _make_input


def _make_skill_root(tmp_path: Path) -> Path:
    root = tmp_path / "skills"
    # A08_macro 边界内：政策技能（触发短语：政策发布）
    skill_dir = root / "policy-analyst" / "policy-intensity-assessment"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: policy-intensity-assessment\n"
        "description: >\n"
        "  新政策文件发布后触发。判断政策力度与受益行业。\n"
        "  触发短语：政策发布、国务院文件、产业政策。\n"
        "tags: [policy]\n"
        "---\n"
        "# 政策力度评估\n## 执行步骤\nPhase 1：政策分类\n",
        encoding="utf-8",
    )
    return root


async def test_macro_agent_injects_matched_skill(tmp_path: Path) -> None:
    lib = SkillLibrary(_make_skill_root(tmp_path))
    gw = FakeGateway(MACRO_REPLY)
    agent = MacroAnalysisAgent(gw, skill_library=lib)
    out = await agent.execute(_make_input({
        "focus": "政策发布对半导体的影响", "data_points": [_dp("CPI", 2.1)],
    }))
    prompt = gw.calls[0]["prompt"]
    assert "专业技能指引" in prompt
    assert "policy-intensity-assessment" in prompt
    assert "Phase 1：政策分类" in prompt  # L1正文已注入
    assert any(s.step_type == "skill_injection" for s in out.reasoning_steps)


async def test_macro_agent_no_skill_without_trigger(tmp_path: Path) -> None:
    lib = SkillLibrary(_make_skill_root(tmp_path))
    gw = FakeGateway(MACRO_REPLY)
    agent = MacroAnalysisAgent(gw, skill_library=lib)
    await agent.execute(_make_input({
        "focus": "中国宏观", "data_points": [_dp("CPI", 2.1)],
    }))
    assert "专业技能指引" in gw.calls[0]["prompt"]
    assert "Phase 1" not in gw.calls[0]["prompt"]  # 未命中触发短语，不注入正文


async def test_macro_agent_works_without_library() -> None:
    gw = FakeGateway(MACRO_REPLY)
    await MacroAnalysisAgent(gw).execute(_make_input({
        "focus": "中国宏观", "data_points": [_dp("CPI", 2.1)],
    }))
    assert "Phase 1" not in gw.calls[0]["prompt"]


def _extract_reply() -> dict:
    return {"events": [{"item_id": "i1", "event_type": "earnings",
                        "subject": "测试公司", "direction": "positive",
                        "magnitude": "", "event_date": "",
                        "evidence_quote": "净利润增长", "confidence": 0.8}]}


async def test_extractor_injects_default_skill(tmp_path: Path) -> None:
    lib = SkillLibrary(_make_skill_root(tmp_path))
    # 给A06边界加news-analyst技能
    skill_dir = tmp_path / "skills" / "news-analyst" / "news-entity-extraction"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: news-entity-extraction\n"
        "description: 从新闻中抽取公司主体与事件。\n---\n"
        "# 新闻实体抽取\n### Phase 1：公司名称与股票代码提取\n",
        encoding="utf-8",
    )
    gw = FakeGateway(_extract_reply())
    agent = ExtractorAgent(gw, skill_library=lib)
    await agent.execute(_make_input({
        "info_items": [{"item_id": "i1", "title": "公告", "text": "净利润增长"}],
    }))
    prompt = gw.calls[0]["prompt"]
    assert "news-entity-extraction" in prompt
    assert "Phase 1：公司名称与股票代码提取" in prompt


async def test_sentiment_injects_default_skill(tmp_path: Path) -> None:
    skill_dir = tmp_path / "skills" / "sentiment-monitor" / "sentiment-heat-tracking"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: sentiment-heat-tracking\n"
        "description: 追踪舆情热度与演变。\n---\n"
        "# 舆情热度追踪\n## Phase 1\n热度量化\n",
        encoding="utf-8",
    )
    lib = SkillLibrary(tmp_path / "skills")
    gw = FakeGateway({"conclusion": "情绪中性", "confidence": "medium",
                      "sentiment_phase": "分歧", "narrative": "多空分歧"})
    agent = SentimentAgent(gw, skill_library=lib)
    await agent.execute(_make_input({
        "events": [{"subject": "X", "direction": "neutral", "event_type": "other",
                    "confidence": 0.5, "evidence_quote": "abc"}],
    }))
    assert "sentiment-heat-tracking" in gw.calls[0]["prompt"]
    assert "热度量化" in gw.calls[0]["prompt"]


async def test_skill_load_failure_does_not_block(tmp_path: Path) -> None:
    # default_skill不存在 → 静默降级，主流程照常
    gw = FakeGateway(_extract_reply())
    agent = ExtractorAgent(gw, skill_library=SkillLibrary(tmp_path / "skills"),
                           default_skill="nonexistent-skill")
    out = await agent.execute(_make_input({
        "info_items": [{"item_id": "i1", "title": "公告", "text": "净利润增长"}],
    }))
    assert out.result["stats"]["total"] == 1
