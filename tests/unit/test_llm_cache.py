"""LLM语义缓存测试。"""

import pytest

from src.infrastructure.llm.cache import (
    LLMCache,
    cache_key,
    cosine_similarity,
    normalize_text,
    _ngram_vector,
)
from src.infrastructure.llm.models import LLMResponse


def _resp(content: str = "答案") -> LLMResponse:
    return LLMResponse(
        content=content, model_used="deepseek-v4-flash", provider="deepseek",
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
    cache.put("系统", "请基于宏观流动性与行业景气度，分析贵州茅台的投资价值", _resp("结论A"))

    hit = cache.get("系统", "请基于宏观流动性与行业景气度，分析五粮液的投资价值")
    assert hit is not None
    assert hit.cache_kind == "semantic"


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
