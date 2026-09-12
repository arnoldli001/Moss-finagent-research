"""LLM语义缓存（L1内存 + L2本地文件，两级查找）。

精确命中：规范化prompt的SHA256键；
语义命中：字符3-gram余弦相似度≥阈值（无依赖轻量向量，Demo级）。
持久化为单键单JSON文件，TTL惰性过期，命中不产生LLM调用费用。
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import time
from collections import Counter
from pathlib import Path
from typing import Any

from src.infrastructure.llm.models import LLMResponse

_NORMALIZE_RE = re.compile(r"[^\w\u4e00-\u9fff]+", re.UNICODE)


def normalize_text(text: str) -> str:
    """小写化并剔除空白/标点，保留字母数字与中日韩字符。"""
    return _NORMALIZE_RE.sub("", text.lower())


def cache_key(system: str, prompt: str) -> str:
    return hashlib.sha256(
        (normalize_text(system) + "\x00" + normalize_text(prompt)).encode("utf-8")
    ).hexdigest()


def _ngram_vector(text: str, n: int = 3) -> Counter[str]:
    if len(text) < n:
        return Counter([text]) if text else Counter()
    return Counter(text[i : i + n] for i in range(len(text) - n + 1))


def cosine_similarity(a: Counter[str], b: Counter[str]) -> float:
    if not a or not b:
        return 0.0
    dot = sum(cnt * b.get(g, 0) for g, cnt in a.items())
    norm_a = math.sqrt(sum(c * c for c in a.values()))
    norm_b = math.sqrt(sum(c * c for c in b.values()))
    return dot / (norm_a * norm_b) if norm_a and norm_b else 0.0


class LLMCache:
    """两级LLM响应缓存（进程内存 → 本地JSON文件）。"""

    def __init__(
        self,
        cache_dir: str = "data/llm_cache",
        ttl_hours: float = 24.0,
        semantic_threshold: float = 0.85,
    ) -> None:
        self._dir = Path(cache_dir)
        self._ttl_seconds = ttl_hours * 3600
        self._threshold = semantic_threshold
        self._memory: dict[str, tuple[float, dict[str, Any]]] = {}

    def _file_path(self, key: str) -> Path:
        self._dir.mkdir(parents=True, exist_ok=True)
        return self._dir / f"{key}.json"

    def get(self, system: str, prompt: str) -> LLMResponse | None:
        """先精确后语义。返回的response已带cache_hit/cache_kind标记。"""
        key = cache_key(system, prompt)
        hit = self._get_exact(key)
        if hit is not None:
            return self._mark(hit, "exact")
        return self._get_semantic(system, prompt, exclude=key)

    def put(self, system: str, prompt: str, response: LLMResponse) -> None:
        expires_at = time.time() + self._ttl_seconds
        entry = {
            **response.model_dump(mode="json"),
            "vector_text": normalize_text(system + prompt),
        }
        key = cache_key(system, prompt)
        self._memory[key] = (expires_at, entry)
        self._file_path(key).write_text(
            json.dumps({"expires_at": expires_at, **entry}, ensure_ascii=False),
            encoding="utf-8",
        )

    def _load(self, key: str) -> dict[str, Any] | None:
        now = time.time()
        mem = self._memory.get(key)
        if mem:
            expires_at, entry = mem
            if expires_at > now:
                return entry
            self._memory.pop(key, None)
        path = self._file_path(key)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        if data.get("expires_at", 0) <= now:
            path.unlink(missing_ok=True)
            return None
        self._memory[key] = (data["expires_at"], data)
        return data

    def _get_exact(self, key: str) -> dict[str, Any] | None:
        return self._load(key)

    def _get_semantic(
        self, system: str, prompt: str, *, exclude: str
    ) -> dict[str, Any] | None:
        """扫描L2文件构建向量索引，取相似度最高且≥阈值的条目。"""
        if not self._dir.exists():
            return None
        query_vec = _ngram_vector(normalize_text(system + prompt))
        best_key, best_score = None, self._threshold
        for path in self._dir.glob("*.json"):
            key = path.stem
            if key == exclude:
                continue
            entry = self._load(key)
            if entry is None:
                continue
            score = cosine_similarity(query_vec, _ngram_vector(entry.get("vector_text", "")))
            if score >= best_score:
                best_key, best_score = key, score
        return self._mark(entry, "semantic") if (best_key and (entry := self._load(best_key))) else None

    @staticmethod
    def _mark(entry: dict[str, Any], kind: str) -> LLMResponse:
        data = {k: v for k, v in entry.items() if k in LLMResponse.model_fields}
        resp = LLMResponse.model_validate(data)
        resp.cache_hit = True
        resp.cache_kind = kind  # type: ignore[assignment]
        return resp
