"""Agent 展示元数据单测：中文名映射与置信度中文化。"""

from src.core.agent_meta import (
    CONFIDENCE_ZH,
    agent_meta_table,
    agent_name,
    confidence_zh,
    load_agent_meta,
)

# configs/agents.yaml 必须覆盖的全部运行时 agent_id
RUNTIME_IDS = [
    "A01_data_collector", "A02_data_cleaner", "A03_data_validator",
    "A04_data_storage", "A05_verifier", "A06_extractor", "A07_sentiment",
    "A08_macro", "A09_meso", "A10_micro", "A11_fin_risk", "A12_compliance",
    "A13_tech", "A14_consumer", "A15_cyclical", "A16_pharma",
    "A17_recommend", "A18_audit",
]


def test_every_runtime_agent_has_chinese_name() -> None:
    load_agent_meta.cache_clear()
    meta = agent_meta_table()
    for agent_id in RUNTIME_IDS:
        assert agent_id in meta, f"缺少元数据: {agent_id}"
        name = meta[agent_id]["name"]
        assert name and name != agent_id, f"{agent_id} 未配置中文名"
        assert any("一" <= ch <= "鿿" for ch in name), f"{agent_id} name 非中文: {name}"


def test_known_agent_names() -> None:
    assert agent_name("A17_recommend") == "投研建议Agent"
    assert agent_name("A09_meso") == "中观分析Agent"
    assert agent_name("A18_audit") == "逻辑审计Agent"


def test_unknown_id_returned_as_is() -> None:
    assert agent_name("A99_unknown") == "A99_unknown"
    assert agent_name(None) == "未知Agent"


def test_confidence_zh() -> None:
    assert confidence_zh("high") == "高"
    assert confidence_zh("medium") == "中"
    assert confidence_zh("low") == "低"
    assert confidence_zh(None) == "未知"
    # 大小写不敏感、未知值原样返回
    assert confidence_zh("HIGH") == "高"
    assert confidence_zh("weird") == "weird"
    assert set(CONFIDENCE_ZH.values()) == {"高", "中", "低"}


def test_layer_field_present() -> None:
    meta = agent_meta_table()
    assert meta["A17_recommend"]["layer"] == "decision"
    assert meta["A13_tech"]["layer"] == "industry"
