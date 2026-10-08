"""`scripts/cache_health.py` 的判据 —— 钉住**两件事**（`CHG-0208`）。

## 1. ★★ 跨实现一致性：「过期」这个判断被写了两遍，不许漂移

`cache_health.scan()` **刻意自己读盘**（不经过 `LLMCache`），因为
"索引看到的"与"盘上真有的"必须是**两次独立测量** —— 用同一份实现验自己
等于没验。

但代价很具体：**"什么算过期"被实现了两遍**

    `cache_health.scan()`      →  `if exp <= now: continue`
    `LLMCache._build_index()`  →  `if expires_at <= now: continue`

两份实现漂移**不会有任何报错**，症状只是"健康度面板说 3000 条，
而语义索引里只有 2000 条"，然后没人知道该信哪个。
⇒ 本文件断言：**对同一批文件，`scan()["alive"]` 必须等于
`LLMCache.warm_up()` 的索引条目数**。这条断言是唯一能抓住漂移的东西。

## 2. 「存活」的定义本身要钉住（边界值）

`expires_at == now` 算**过期**（`<=`，不是 `<`）。边界写反了会多算一条，
而它只在"恰好同一秒"出现 —— 属于**测不出来但会真实发生**的那一类，
所以用注入的 `now` 把边界钉死。
"""
from __future__ import annotations

import json
import pathlib
import sys
import time

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from scripts.cache_health import scan  # noqa: E402
from src.infrastructure.llm.cache import LLMCache, cache_key  # noqa: E402
from src.infrastructure.llm.embedding import encode_vector  # noqa: E402

_VEC = [0.1, 0.2, 0.3, 0.4]


def _write(cache_dir: pathlib.Path, *, system: str, prompt: str, scope: str,
           expires_in: float, agent_id: str = "A08_macro",
           anchor: str = "", with_vec: bool = True) -> None:
    """直接落一个**原始条目文件**（不走 `LLMCache.put`）。

    刻意手写：`put()` 会顺带建索引、算 key，用它就测不出"盘上真有什么"。
    """
    cache_dir.mkdir(parents=True, exist_ok=True)
    entry: dict = {
        "content": "答案", "model_used": "m", "provider": "p",
        "prompt_hash": "ph", "response_hash": "rh",
        "vector_text": f"{system}{prompt}", "agent_id": agent_id, "scope": scope,
        "expires_at": time.time() + expires_in,
    }
    if anchor:
        entry["anchor_text"] = anchor
    if with_vec:
        entry["embedding"] = encode_vector(_VEC)
    (cache_dir / f"{cache_key(system, prompt, scope)}.json").write_text(
        json.dumps(entry, ensure_ascii=False), encoding="utf-8")


def test_scan_and_index_agree_on_what_is_alive(tmp_path) -> None:
    """★★ **跨实现一致性**：`scan()['alive'] == LLMCache.warm_up()`。

    这是本文件存在的理由。"什么算过期"有两份实现，漂移不报错 ——
    只有这条断言能抓住它。
    """
    d = tmp_path / "c"
    for i in range(4):                                    # 4 条活
        _write(d, system="S", prompt=f"活{i}", scope="s", expires_in=3600)
    for i in range(3):                                    # 3 条死
        _write(d, system="S", prompt=f"死{i}", scope="s", expires_in=-60)

    r = scan(d)
    assert (r["total"], r["alive"], r["expired"]) == (7, 4, 3), r

    idx = LLMCache(cache_dir=str(d), ttl_hours=1.0).warm_up()
    assert idx == r["alive"], (
        f"`scan()` 说活 {r['alive']} 条，`LLMCache` 索引里 {idx} 条 —— "
        f"「过期」的两份实现漂移了（这类漂移不会报错，只会让两个面板对不上）")


def test_expiry_boundary_is_inclusive(tmp_path) -> None:
    """★ 边界：`expires_at == now` 算**过期**（`<=`）。

    写反成 `<` 只在"恰好同一秒"多算一条 —— 真实会发生、平时测不出来。
    """
    d = tmp_path / "c"
    now = time.time()
    _write(d, system="S", prompt="刚好到期", scope="s", expires_in=0.0)
    r = scan(d, now=time.time())
    assert r["total"] == 1
    assert r["alive"] == 0, "恰好到期的条目被算成了存活"
    assert r["expired"] == 1

    # 反向对照：只要多活一点点就必须算活（否则是"宁可错杀"）
    _write(d, system="S", prompt="多活一点", scope="s", expires_in=0.5)
    r2 = scan(d, now=now)
    assert r2["alive"] == 1 and r2["expired"] == 1, r2


def test_buckets_split_by_mode_and_only_count_the_alive(tmp_path) -> None:
    """桶口径必须与 `_SemanticIndex._bucket_key` 一致：`(agent, scope, mode)`。

    ★ 且**只数存活的** —— 这正是那个异常的来源：42 个桶里 40 个全死了，
    把死条目也算进来的话，"桶数"看着很健康而实际上一片空白。
    """
    d = tmp_path / "c"
    _write(d, system="S", prompt="A", scope="s1", expires_in=3600,
           agent_id="agent_x", anchor="问题A")                 # anchor 模式 · 活
    _write(d, system="S", prompt="B", scope="s1", expires_in=3600,
           agent_id="agent_x")                                  # prompt 模式 · 活
    _write(d, system="S", prompt="C", scope="s2", expires_in=-1,
           agent_id="agent_y")                                  # prompt 模式 · 死

    r = scan(d)
    assert r["buckets"][("agent_x", "s1", "anchor")] == 1
    assert r["buckets"][("agent_x", "s1", "prompt")] == 1
    assert len(r["buckets"]) == 2, r["buckets"]
    # 含死条目的口径要多出那一个桶 —— 两个口径必须都能报出来
    assert len(r["buckets_all"]) == 3, r["buckets_all"]
    assert r["buckets_all"][("agent_y", "s2", "prompt")] == 1
    assert ("agent_y", "s2") in r["pairs_all"]


def test_broken_file_is_counted_not_silently_skipped(tmp_path) -> None:
    """解析失败的文件要**计数**，不能静默跳过。

    静默跳过的症状是"总数对不上但没人知道为什么"，而"几个文件坏了"
    恰恰是磁盘/写入中断的早期信号。
    """
    d = tmp_path / "c"
    d.mkdir(parents=True, exist_ok=True)
    (d / "broken.json").write_text("{ 这不是 JSON", encoding="utf-8")
    _write(d, system="S", prompt="好", scope="s", expires_in=3600)
    r = scan(d)
    assert r["total"] == 2 and r["broken"] == 1
    assert r["alive"] == 1
    # 坏文件既不算活也不算过期（它根本没有 expires_at 可读）
    assert r["alive"] + r["expired"] + r["broken"] == r["total"]
