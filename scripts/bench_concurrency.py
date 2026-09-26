"""并发改造效果基准（before/after 对照）。

衡量三个改造点的实际效果：
  1. LLM 语义缓存：目录扫描 → 内存索引（事件循环阻塞时长）
  2. TaskStore：无淘汰 → TTL+容量双阈值（N 请求后的残留对象）
  3. 准入控制：无闸门 → 信号量（并发下的排队与拒绝行为）

用法：
    python scripts/bench_concurrency.py            # 只测当前（after）
    python scripts/bench_concurrency.py --compare   # 同时测改造前（before）
"""

from __future__ import annotations

import argparse
import asyncio
import importlib
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

CACHE_DIR = str(ROOT / "data" / "llm_cache")


# ---------------------------------------------------------------- 事件循环阻塞

class LagMeter:
    """后台协程测事件循环滞后：同步阻塞会直接体现为滞后尖峰。"""

    def __init__(self, interval: float = 0.005) -> None:
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._last = 0.0
        self.samples: list[float] = []

    async def _loop(self) -> None:
        self._last = time.perf_counter()
        while True:
            await asyncio.sleep(self._interval)
            now = time.perf_counter()
            self.samples.append((now - self._last) * 1000)
            self._last = now

    async def __aenter__(self) -> LagMeter:
        self._task = asyncio.create_task(self._loop())
        await asyncio.sleep(self._interval * 2)
        return self

    async def __aexit__(self, *exc: object) -> None:
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    @property
    def worst(self) -> float:
        return max(self.samples) if self.samples else 0.0

    @property
    def p95(self) -> float:
        if not self.samples:
            return 0.0
        s = sorted(self.samples)
        return s[min(len(s) - 1, int(len(s) * 0.95))]

    @property
    def median(self) -> float:
        return statistics.median(self.samples) if self.samples else 0.0


# ---------------------------------------------------------------- 1. 语义缓存

async def bench_cache(concurrency: int) -> dict:
    """N 个并发协程同时做语义查找（各自不同 prompt → 全部未命中）。"""
    from src.infrastructure.llm.cache import LLMCache

    cache = LLMCache(cache_dir=CACHE_DIR)
    n_files = len(list(Path(CACHE_DIR).glob("*.json")))

    # 冷启（未建索引）下的首轮并发
    async with LagMeter() as meter:
        t0 = time.perf_counter()
        await asyncio.gather(*[
            cache.aget(f"sys{i}" * 40, f"prompt{i}" * 60,
                       agent_id=f"A{8 + i % 5:02d}_x", scope="reasoning|json=1|eff=high")
            for i in range(concurrency)
        ])
        cold_ms = (time.perf_counter() - t0) * 1000

    # 热态（索引已建）下的又一轮并发
    async with LagMeter() as meter2:
        t0 = time.perf_counter()
        await asyncio.gather(*[
            cache.aget(f"hot{i}" * 40, f"query{i}" * 60,
                       agent_id=f"A{8 + i % 5:02d}_x", scope="reasoning|json=1|eff=high")
            for i in range(concurrency)
        ])
        hot_ms = (time.perf_counter() - t0) * 1000

    return {
        "files": n_files,
        "concurrency": concurrency,
        "cold_total_ms": cold_ms,
        "hot_total_ms": hot_ms,
        "cold_lag_worst_ms": meter.worst,
        "hot_lag_worst_ms": meter2.worst,
        "hot_lag_p95_ms": meter2.p95,
        "index_entries": cache.stats()["index_entries"],
    }


# ---------------------------------------------------------------- 2. TaskStore

def bench_taskstore(n_requests: int) -> dict:
    """N 个任务走完整生命周期（含终结），看残留对象数。"""
    from src.api.tasks import TaskStore, new_task_id
    from src.core.cancel import CancellationToken

    store = TaskStore()
    for _ in range(n_requests):
        tid = new_task_id()
        store.create(tid, trace_id=tid, tenant_id="t1", query="q",
                     analysis_type="full", target="x")
        store.register_token(tid, CancellationToken(tid))
        store.set_live_state(tid, {"progress": [], "agent_messages": []})
        store.update(tid, status="running")
        # 模拟产出：报告正文（真实场景是几十 KB 的 Markdown）
        store.update(tid, status="completed",
                     final_report="# 报告\n" + ("正文" * 2000),
                     agent_outputs=[{"agent_id": "A17_recommend",
                                     "conclusion": "结论" * 100}])
        store.clear_live_state(tid)
        store.evict()
    return store.stats()


# ---------------------------------------------------------------- 3. 准入控制

async def bench_admission(concurrency: int, limit: int) -> dict:
    """N 个并发"分析任务"抢 limit 个名额，测排队与峰值。"""
    sem = asyncio.Semaphore(limit)
    inflight = 0
    peak = 0
    served = 0

    async def one() -> None:
        nonlocal inflight, peak, served
        async with sem:
            inflight += 1
            peak = max(peak, inflight)
            served += 1
            await asyncio.sleep(0.02)   # 模拟管线工作
            inflight -= 1

    t0 = time.perf_counter()
    await asyncio.gather(*[one() for _ in range(concurrency)])
    return {
        "submitted": concurrency, "limit": limit, "served": served,
        "peak_inflight": peak, "wall_ms": (time.perf_counter() - t0) * 1000,
    }


# ---------------------------------------------------------------- main

async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--compare", action="store_true",
                    help="同时基准改造前的实现（从当前源码动态替换为旧逻辑）")
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--requests", type=int, default=3000)
    ap.add_argument("--limit", type=int, default=4)
    args = ap.parse_args()

    print("=" * 74)
    print("投研链路并发改造基准")
    print("=" * 74)

    print("\n【1】LLM 语义缓存：事件循环阻塞")
    r = await bench_cache(args.concurrency)
    print(f"  缓存文件数           : {r['files']}")
    print(f"  并发查找数           : {r['concurrency']}")
    print(f"  冷启(未建索引) 总耗时: {r['cold_total_ms']:8.1f} ms"
          f"   事件循环最大滞后 {r['cold_lag_worst_ms']:6.1f} ms")
    print(f"  热态(索引已建) 总耗时: {r['hot_total_ms']:8.1f} ms"
          f"   事件循环最大滞后 {r['hot_lag_worst_ms']:6.1f} ms"
          f"   p95 {r['hot_lag_p95_ms']:5.1f} ms")
    print(f"  索引条目数           : {r['index_entries']}")
    if args.compare:
        before = await bench_cache_legacy(args.concurrency)
        print(f"  -- 改造前（同步全目录扫描，协程内直接调用）")
        print(f"     并发查找总耗时   : {before['total_ms']:8.1f} ms"
              f"   阻塞事件循环 {before['block_ms']:8.1f} ms")
        print(f"     => 事件循环阻塞改善: "
              f"{before['block_ms'] / max(r['hot_total_ms'], 0.01):.0f}x")
        print(f"     => 并发总耗时改善  : "
              f"{before['total_ms'] / max(r['hot_total_ms'], 0.01):.0f}x")

    print(f"\n【2】TaskStore 残留（{args.requests} 个完整生命周期后）")
    st = bench_taskstore(args.requests)
    print(f"  {st}")
    print(f"  => 任务数 {st['tasks']} <= max_tasks {st['max_tasks']}；"
          f"tokens {st['tokens']} / handles {st['handles']} 同步回收")

    print(f"\n【3】准入控制（提交 {args.concurrency * 4} 个，闸门 {args.limit}）")
    a = await bench_admission(args.concurrency * 4, args.limit)
    print(f"  {a}")
    print("  => 峰值在飞数被闸门钳住，不再无限膨胀")


async def bench_cache_legacy(concurrency: int) -> dict:
    """复现改造前的语义查找：在协程里同步遍历全目录（不卸载线程）。

    ⚠️ 测法是"单次未命中的阻塞时长"而不是 LagMeter：
    旧实现是**纯同步、内部无 await**，整段扫描期间事件循环根本没有
    机会切到测滞后协程上 —— LagMeter 会显示 0ms（测不到），
    这正是"同步阻塞"的典型误测。所以直接量每轮扫描的墙上时间，
    它就等于**事件循环被冻结的时长**。
    """
    import json

    from src.infrastructure.llm.cache import (
        _ngram_vector,
        cache_key,
        cosine_similarity,
        normalize_text,
    )

    d = Path(CACHE_DIR)

    def one_pass(system: str, prompt: str, agent_id: str, scope: str) -> None:
        exclude = cache_key(system, prompt, scope)
        qv = _ngram_vector(normalize_text(system + prompt))
        best_key, best_score = None, 0.85
        # ↓↓↓ 旧实现：解析每个文件才能读到 agent_id/scope 做过滤
        for path in d.glob("*.json"):
            if path.stem == exclude:
                continue
            try:
                entry = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            if str(entry.get("agent_id") or "") != agent_id:
                continue
            if str(entry.get("scope") or "") != scope:
                continue
            score = cosine_similarity(
                qv, _ngram_vector(entry.get("vector_text", "")))
            if score >= best_score:
                best_key, best_score = path.stem, score

    t0 = time.perf_counter()
    for i in range(concurrency):
        one_pass(f"sys{i}" * 40, f"prompt{i}" * 60,
                 f"A{8 + i % 5:02d}_x", "reasoning|json=1|eff=high")
    total = (time.perf_counter() - t0) * 1000
    return {"total_ms": total, "block_ms": total}


if __name__ == "__main__":
    importlib.invalidate_caches()
    asyncio.run(main())
