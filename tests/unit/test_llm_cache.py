"""LLM语义缓存测试。"""

import pytest

from src.infrastructure.llm.cache import (
    LLMCache,
    _ngram_vector,
    cache_key,
    cosine_similarity,
    normalize_text,
)
from src.infrastructure.llm.models import LLMResponse


def _resp(content: str = "答案") -> LLMResponse:
    return LLMResponse(
        content=content, model_used="deepseek-flash", provider="deepseek",
        prompt_hash="ph", response_hash="rh",
    )


def test_normalize_strips_punct_and_case():
    assert normalize_text("Hello, 世界！ GDP ") == "hello世界gdp"


def test_cosine_identical_and_disjoint():
    assert cosine_similarity(_ngram_vector("abcabc"), _ngram_vector("abcabc")) == pytest.approx(1.0)
    assert cosine_similarity(_ngram_vector("aaaa"), _ngram_vector("zzzz")) == 0.0


def test_exact_hit_roundtrip(tmp_dir):
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1)
    assert cache.get("系统", "贵州茅台2026年估值分析") is None

    cache.put("系统", "贵州茅台2026年估值分析", _resp("PE=18"))
    hit = cache.get("系统", "贵州茅台2026年估值分析")
    assert hit is not None and hit.content == "PE=18"
    assert hit.cache_hit and hit.cache_kind == "exact"


def test_semantic_hit_above_threshold(tmp_dir):
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.6)
    cache.put("系统", "请基于宏观流动性与行业景气度，分析贵州茅台的投资价值",
              _resp("结论A"), agent_id="A08_macro")

    hit = cache.get("系统", "请基于宏观流动性与行业景气度，分析五粮液的投资价值",
                    agent_id="A08_macro")
    assert hit is not None
    assert hit.cache_kind == "semantic"


def test_semantic_cross_agent_isolation(tmp_dir):
    """同一Agent内语义复用允许；跨Agent（如A05核验→A06抽取）禁止语义命中。"""
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.6)
    cache.put("核验系统", "请基于宏观流动性与行业景气度，分析贵州茅台的投资价值",
              _resp("结论A"), agent_id="A05_verifier")
    # 高度相似的输入但来自不同Agent → 不得命中（防止结论串台）
    assert cache.get("核验系统", "请基于宏观流动性与行业景气度，分析五粮液的投资价值",
                     agent_id="A06_extractor") is None
    # 调用方不带agent_id（旧用法）同样不做语义复用
    assert cache.get("核验系统", "请基于宏观流动性与行业景气度，分析五粮液的投资价值") is None


def test_miss_below_threshold(tmp_dir):
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.99)
    cache.put("系统", "分析贵州茅台的投资价值", _resp("结论A"))
    assert cache.get("系统", "完全不同主题的问题今天天气如何") is None


def test_ttl_expiry(tmp_dir):
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1e-6)
    cache.put("系统", "问题Q", _resp())
    import time

    time.sleep(0.05)
    assert cache.get("系统", "问题Q") is None


def test_key_ignores_whitespace_and_punct():
    assert cache_key("系统", "问题 一！") == cache_key("系统", "问题一")


# ======================================================================
# 缓存作用域（模型层级 / 输出格式）
# ======================================================================

def test_same_prompt_at_different_tier_does_not_cross_hit(tmp_dir):
    """**作用域回归**：同一 system/prompt 在不同模型层级下**不可互相复用**。

    `light` 层跑的是本地 qwen2.5:1.5B，`decision` 层跑的是云端 pro；
    两者对精度要求差一个量级，但 system/prompt 可能一字不差。
    若缓存键只由 (system, prompt) 决定，同一次任务里先跑的 1.5B 结果
    会被后面的决策层直接复用 —— 省钱省到了错误的地方。
    """
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.99)
    cache.put("系统提示", "分析贵州茅台", _resp("1.5B的粗略结论"), agent_id="A17",
              scope="light|json=0")
    # 换层级 → 必须未命中
    assert cache.get("系统提示", "分析贵州茅台", "A17", scope="decision|json=0") is None
    # 同层级 → 正常命中
    hit = cache.get("系统提示", "分析贵州茅台", "A17", scope="light|json=0")
    assert hit is not None and hit.content == "1.5B的粗略结论"


def test_json_mode_is_part_of_scope(tmp_dir):
    """要求结构化输出与自由文本，是两种不同的请求，不能共用缓存。"""
    cache = LLMCache(cache_dir=tmp_dir, ttl_hours=1, semantic_threshold=0.99)
    cache.put("系统", "抽取字段", _resp("{}"), agent_id="A06", scope="medium|json=1")
    assert cache.get("系统", "抽取字段", "A06", scope="medium|json=0") is None
    assert cache.get("系统", "抽取字段", "A06", scope="medium|json=1") is not None


def test_scope_defaults_to_legacy_behaviour_for_audit_hash():
    """不传 scope 时退化为只看 (system, prompt) —— 审计用的 prompt_hash 依赖这点。

    `prompt_hash` 要标识 **prompt 本身**，与调用条件无关，不能带 scope。
    """
    assert cache_key("系统", "问题") == cache_key("系统", "问题", scope="")
    assert cache_key("系统", "问题") != cache_key("系统", "问题", scope="light")
