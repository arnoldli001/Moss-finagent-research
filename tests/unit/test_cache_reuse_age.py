"""**复用年龄**的守护测试（`CHG-0209`）—— 它是 TTL 的唯一依据。

## 这个文件为一次"我自己的提议被自己否掉"而写

上一轮我说"往审计里加 anchor 就能给 L2 定 TTL"。**那是错的**：
审计**只记录出网的调用**，而语义复用发生在**命中路径**上（命中不调 LLM
⇒ 不落审计）⇒ 审计里只有在"两次都漏掉"之间的距离，**没有复用**。

⇒ 正确的量在缓存里，而且是被复用那条的**年龄**：`age = now − entry.created_at`。
存量的 5,007 条条目**没有 `created_at`** ⇒ 那部分必须记成"未知"，不许猜 0。

## 这个文件守的四件事

1. ★★ **精确命中与语义命中分开记** —— 两者该有不同的 TTL
   （"同一句话再问一次"是秒级，"换了个说法"可能跨小时）。
   合成一个数就分不出该给哪一层调 TTL。
2. ★ **记录点只有一个**（`_mark`）：它同时拿得到「命中类型」与「被命中的 entry」。
   放到 `_finish` 里会漏掉精确命中，放到调用方会漏掉语义命中。
3. ★ **缺 `created_at` ⇒ 记未知，不记 0**（「没量到 ≠ 量到 0」）。
   静默塞进 `<1min` 桶会让"复用都发生在 1 分钟内"这个**假结论**看着很有依据。
4. **分档顺序与空桶**：输出按刻度顺序、空桶显式补 0
   （缺一档 vs 那一档是 0 次，必须长得不一样）。

跑法：
    uv run python -m pytest tests/unit/test_cache_reuse_age.py -q
"""
from __future__ import annotations

import json
import time

import pytest

from src.infrastructure.llm.cache import (
    REUSE_AGE_BUCKETS,
    LLMCache,
    cache_key,
    reuse_age_label,
)
from src.infrastructure.llm.models import LLMResponse

SYS = "你是资深分析师。" * 8
P = "今天A股市场怎么样？"
_VEC = [0.1, 0.2, 0.3, 0.4]

LANE_LABELS = [label for label, _u in REUSE_AGE_BUCKETS]


def _resp() -> LLMResponse:
    return LLMResponse(content="答案", model_used="m", provider="p",
                       prompt_hash="ph", response_hash="rh")


class _ConstEmbed:
    """对任何文本返回同一向量 ⇒ 语义命中的发生是确定的。"""

    configured = True

    async def embed(self, _text: str) -> list[float]:
        return list(_VEC)

    def stats(self) -> dict[str, object]:
        return {}


def _cache(tmp_path, embed=None) -> LLMCache:
    return LLMCache(cache_dir=str(tmp_path), ttl_hours=1.0,
                    semantic_threshold=0.85,
                    embed_client=embed, embed_threshold=0.80, recall_k=12)


def _age(entry_path, *, seconds: float) -> None:
    """把盘上那条的 `created_at` **回拨** `seconds` 秒（模拟一条老条目）。"""
    raw = json.loads(entry_path.read_text(encoding="utf-8"))
    raw["created_at"] = time.time() - seconds
    entry_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")


def test_age_label_boundaries() -> None:
    """分档边界要钉住 —— 它们是 **TTL 候选刻度**，差一档就读错结论。"""
    assert reuse_age_label(0.0) == "<1min"
    assert reuse_age_label(59.9) == "<1min"
    assert reuse_age_label(60.0) == "<1min"          # 上界是 `<=`
    assert reuse_age_label(60.1) == "1-10min"
    assert reuse_age_label(3600.0) == "10min-1h"
    assert reuse_age_label(3601.0) == "1-6h"
    assert reuse_age_label(86400.0) == "6-24h"
    assert reuse_age_label(86401.0) == "1-3d"
    assert reuse_age_label(1e12) == ">7d"


def test_write_stamps_created_at(tmp_path) -> None:
    """`put` 必须写 `created_at` —— 没有它，年龄永远算不出来。"""
    c = _cache(tmp_path)
    before = time.time()
    c.put(SYS, P, _resp(), agent_id="A08_macro", scope="s")
    after = time.time()
    files = list(tmp_path.glob("*.json"))
    assert len(files) == 1
    raw = json.loads(files[0].read_text(encoding="utf-8"))
    assert "created_at" in raw, "条目里没有 `created_at` ⇒ 年龄算不出来"
    assert before <= float(raw["created_at"]) <= after


def test_exact_hit_age_lands_in_the_right_bucket(tmp_path) -> None:
    """★ 精确命中按**被命中那条的年龄**归档（不是按"刚写的"）。"""
    c = _cache(tmp_path)
    c.put(SYS, P, _resp(), agent_id="A08_macro", scope="s")
    # 回拨 5 小时 ⇒ 应落在 `1-6h`
    _age(tmp_path / f"{cache_key(SYS, P, 's')}.json", seconds=5 * 3600)
    c._memory.clear()                       # noqa: SLF001 逼它从盘上读
    assert c.get(SYS, P, agent_id="A08_macro", scope="s") is not None
    st = c.stats()
    assert st["reuse_age_exact"]["1-6h"] == 1, st["reuse_age_exact"]
    assert st["reuse_age_semantic"][">7d"] == 0
    assert sum(st["reuse_age_exact"].values()) == 1


@pytest.mark.asyncio
async def test_semantic_hit_age_is_counted_separately(tmp_path) -> None:
    """★★ **精确与语义分开记** —— 它们该有不同的 TTL。

    合成一个数的话，"同一句话 10 秒后再问"与"换个说法 5 小时后问"
    会落进同一个桶 ⇒ 分不出该给 L1 还是 L2 调 TTL。
    """
    c = _cache(tmp_path, embed=_ConstEmbed())
    c.put(SYS, P, _resp(), agent_id="A08_macro", scope="s",
          anchor="问题甲", embedding=list(_VEC))
    _age(tmp_path / f"{cache_key(SYS, P, 's')}.json", seconds=5 * 3600)
    c._memory.clear()                       # noqa: SLF001

    other = "麻烦看下今日大盘行情"           # L1 不中 ⇒ 走语义
    assert await c.aget(SYS, other, agent_id="A08_macro", scope="s",
                        anchor=other) is not None
    st = c.stats()
    assert st["reuse_age_semantic"]["1-6h"] == 1, st["reuse_age_semantic"]
    assert st["reuse_age_exact"]["1-6h"] == 0, (
        "语义命中被记进了精确命中 —— 两层的 TTL 就分不开了")


def test_missing_created_at_is_unknown_not_zero(tmp_path) -> None:
    """★★ **没量到 ≠ 量到 0**：存量条目没有 `created_at` ⇒ 记未知。

    静默塞进 `<1min` 桶的后果很具体：面板会显示"复用全部发生在 1 分钟内"
    ⇒ 结论是"TTL 取 1 分钟就够" ⇒ **把缓存砍废**。
    而真相是"这些条目是加字段之前写的，年龄不知道"。
    """
    c = _cache(tmp_path)
    c.put(SYS, P, _resp(), agent_id="A08_macro", scope="s")
    path = tmp_path / f"{cache_key(SYS, P, 's')}.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    del raw["created_at"]                   # 模拟 `CHG-0209` 之前的存量条目
    path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    c._memory.clear()                       # noqa: SLF001

    assert c.get(SYS, P, agent_id="A08_macro", scope="s") is not None
    st = c.stats()
    assert st["reuse_age_unknown"] == 1, "缺 `created_at` 的命中没被记成未知"
    assert sum(st["reuse_age_exact"].values()) == 0, (
        "缺 `created_at` 的命中被塞进了年龄直方图 ⇒ 会被读成'复用发生在 1 分钟内'")


def test_histogram_is_ordered_and_pads_empty_buckets(tmp_path) -> None:
    """★ 输出按**刻度顺序**、空桶显式补 0（缺一档 ≠ 那一档是 0 次）。"""
    c = _cache(tmp_path)
    st = c.stats()
    assert list(st["reuse_age_exact"]) == LANE_LABELS, st["reuse_age_exact"]
    assert list(st["reuse_age_semantic"]) == LANE_LABELS
    assert all(v == 0 for v in st["reuse_age_exact"].values())


def test_histogram_supports_the_ttl_question_directly(tmp_path) -> None:
    """★★ 累积读法：**"TTL 取 X 能吃到多少次复用"** 直接从直方图读出来。

    这是这个直方图存在的理由 —— 它必须能直接回答 `CHG-0209` 的那个问题，
    而不是让人再写一个脚本去聚合。
    """
    c = _cache(tmp_path)
    ages = [30.0, 300.0, 1800.0, 7200.0, 12 * 3600]
    for i, sec in enumerate(ages):
        prompt = f"{P}（第{i}次）"
        c.put(SYS, prompt, _resp(), agent_id="A08_macro", scope="s")
        _age(tmp_path / f"{cache_key(SYS, prompt, 's')}.json", seconds=sec)
    c._memory.clear()                       # noqa: SLF001
    for i in range(len(ages)):
        assert c.get(SYS, f"{P}（第{i}次）", agent_id="A08_macro",
                     scope="s") is not None

    hist = c.stats()["reuse_age_exact"]
    assert hist == {"<1min": 1, "1-10min": 1, "10min-1h": 1, "1-6h": 1,
                    "6-24h": 1, "1-3d": 0, "3-7d": 0, ">7d": 0}, hist

    def covered(ttl_sec: float) -> int:
        """TTL 取 `ttl_sec` ⇒ 能吃到多少次复用（累积到该档）。"""
        return sum(n for label, n in hist.items()
                   if REUSE_AGE_BUCKETS[LANE_LABELS.index(label)][1] <= ttl_sec)

    assert covered(60.0) == 1          # TTL=60s 只吃到 1/5
    assert covered(3600.0) == 3
    assert covered(86400.0) == 5       # TTL=24h 全吃到
    assert covered(604800.0) == 5      # 7d 相对 24h **零增益**（`CHG-0209` 同结论）
