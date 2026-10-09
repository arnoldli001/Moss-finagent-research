"""审计哈希链的**并发正确性**与**读放大**护栏（`CHG-0196` / PRD §45.3~§45.4）。

## 为什么要有这个文件

`docs/CONCURRENCY_CAPACITY_ASSESSMENT.md:36` 声称"实测 200 并发封存，链完整、seq 无冲突 ✅ 安全"。
本轮把那条口径复现了（`scripts/_probe_audit_chain_concurrency.py`，53.15 ms，与 55.6 ms 同量级），
却发现**它测的 writer 形态不是生产形态**，于是补测出两个真缺陷：

1. **`threading.Lock` 对生产路径是空转的。** 生产每次封存都 `AuditChainWriter(path).append(...)`
   （`domain/agents/audit/verifier/agent.py:259` 是全仓唯一构造点）⇒ 每请求一个**新实例**，
   而旧实现的锁与链头状态是**实例级**的 ⇒ 锁从不被争用。
   实测（旧实现）：生产形态 + 200 真线程 ⇒ `valid=False`、唯一 seq 只有 41/199、断在 `seq=1`。
2. **每次 append 全量重读整条链**（`_resume()` → `ChainVerifier.last_record()` → `records()`）
   ⇒ 每封一条 O(N)、累计 O(N²)；且因实例每次新建，`_resumed` 标志永远用不上。

**本文件的判据是"改完会红"的那两条**：`test_production_shape_...`（缺陷①）
与 `test_append_is_constant_read_amplification`（缺陷②）。
另配一条 `test_the_concurrency_judgement_can_fail` **自证**判据抓得住 ——
本项目纪律：只会报绿的检查等于没有检查（`test_v8_gate.py` 是同一范式）。
"""
from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from src.infrastructure.repositories import audit_chain
from src.infrastructure.repositories.audit_chain import (
    AuditChainLockTimeout,
    AuditChainWriter,
    ChainVerifier,
    _record_hash,
)

ROOT = Path(__file__).resolve().parents[2]


def _chain_file(tmp_path: Path) -> Path:
    return tmp_path / "audit_chain.jsonl"


def _summary(path: Path) -> tuple[bool, int, int]:
    """`(valid, 记录数, 唯一 seq 数)`。"""
    verdict = ChainVerifier(path).verify()
    seqs = [r["seq"] for r in ChainVerifier(path).records()]
    return bool(verdict["valid"]), int(verdict["count"]), len(set(seqs))


# ======================================================================
# ① 并发正确性：生产形态（每次 new 一个 writer）
# ======================================================================

def test_production_shape_concurrent_appends_keep_chain_valid(tmp_path: Path) -> None:
    """**生产形态** + 200 真线程 ⇒ 链必须完整、seq 必须全唯一。

    这条就是缺陷①的回归判据：修复前实测 `valid=False`、唯一 seq 41/199、断在 `seq=1`。
    "生产形态"不是修辞 —— 它逐字复刻 `agent.py:259`：
    `AuditChainWriter(path).append({...})`，**每个线程各建一个实例**。
    """
    path = _chain_file(tmp_path)
    n = 200
    errors: list[BaseException] = []

    def one(i: int) -> None:
        try:
            AuditChainWriter(str(path)).append({"kind": "research_audit", "i": i})
        except BaseException as exc:      # noqa: BLE001 线程里必须自己收，否则静默丢
            errors.append(exc)

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert not errors, f"并发封存抛异常：{errors[:3]}"
    valid, count, unique_seq = _summary(path)
    assert (valid, count, unique_seq) == (True, n, n), (
        f"链不完整：valid={valid} 记录={count} 唯一seq={unique_seq}（期望 {n}/{n}）")


def test_same_writer_concurrent_appends_keep_chain_valid(tmp_path: Path) -> None:
    """同一 writer + 200 真线程（**锁真争用**）也必须完整。

    这条是文档口径的加强版：文档只测了"单事件循环里的逻辑并发"，
    这里让 200 个线程抢同一把锁 —— 它才真的验证锁。
    """
    path = _chain_file(tmp_path)
    n = 200
    writer = AuditChainWriter(str(path))

    def one(i: int) -> None:
        writer.append({"kind": "research_audit", "i": i})

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert _summary(path) == (True, n, n)


def test_the_concurrency_judgement_can_fail(tmp_path: Path, monkeypatch) -> None:
    """**自证**：把修复的两层保护**都**拆掉（= 缺陷①的原语义），同一负载必须**报红**。

    为什么要它：并发用例最危险的失败模式是**假绿** ——
    线程没真的重叠、或保护根本没被走到，于是"过了"什么也没证明。

    ★ 本轮实测得到的意外结论（值得记住）：**只拆掉进程内那一层，链依然是完整的**
      —— 因为跨进程文件锁把线程也一起串行化了。这说明两层保护是**独立**的
      （进程内：免锁开销；跨进程：兜住多实例/多进程）。所以自证必须**两层都拆**，
      否则它测的是"另一层还在不在"，而不是"判据抓不抓得住竞态"。
    """
    real_state = audit_chain._ChainState

    monkeypatch.setattr(audit_chain, "_state_for",
                        lambda path: real_state())        # 拆第 1 层：每次都是新状态（旧语义）

    @contextlib.contextmanager
    def no_file_lock(path, state):                        # 拆第 2 层：文件锁变空操作
        yield

    monkeypatch.setattr(audit_chain, "_cross_process_lock", no_file_lock)
    real_hash = audit_chain._record_hash

    def slow_hash(*args, **kwargs):
        time.sleep(0.005)                                 # 撑开竞态窗口
        return real_hash(*args, **kwargs)

    monkeypatch.setattr(audit_chain, "_record_hash", slow_hash)

    path = _chain_file(tmp_path)
    n = 60

    def one(i: int) -> None:
        AuditChainWriter(str(path)).append({"kind": "research_audit", "i": i})

    threads = [threading.Thread(target=one, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    valid, count, unique_seq = _summary(path)
    assert not valid or unique_seq < count, (
        f"判据抓不住竞态（valid={valid} 记录={count} 唯一seq={unique_seq}）—— "
        "上面的并发用例是假绿，先修判据再谈结论")


# ======================================================================
# ② 读放大：稳态每次封存不得重读整链
# ======================================================================

def _append_raw_record(path: Path, payload: dict) -> None:
    """**模拟另一个进程**追加一条合法记录（不用 writer，避免走它的缓存）。"""
    verdict = ChainVerifier(path).verify()
    last = ChainVerifier(path).last_record()
    seq = (int(last["seq"]) + 1) if last else 1
    prev = (str(last["record_hash"]) if last else audit_chain._GENESIS)
    assert verdict["valid"]
    ts = "2026-10-07T00:00:00+00:00"
    rec = {"seq": seq, "ts": ts, "prev_hash": prev, "entry": payload,
           "record_hash": _record_hash(seq, prev, payload, ts)}
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(rec, ensure_ascii=False) + "\n")


def test_append_is_constant_read_amplification(tmp_path: Path) -> None:
    """**缺陷②的回归判据**：300 次封存的**读取字节数**必须与链长无关。

    旧实现每条都全量重读整链 ⇒ 读取量随 N 线性增长（O(N²) 累计）。
    这里用**字节计数**而不是耗时 —— 耗时换台机器就不成立，字节数是可 review、可断言的。
    两半都要断：
      · **正例**：连续 300 次 append，`bytes_read` 增量为 **0**（指纹没变 ⇒ 一次读都不做）；
      · **反例**：**外部**（模拟另一个进程）改了链之后，下一次 append 必须**真的去读**
        —— 否则缓存就成了"看不见别人写入"的瞎子，那是更危险的假绿。
    """
    path = _chain_file(tmp_path)
    AuditChainWriter(str(path)).append({"kind": "warmup"})   # 冷启动：建文件

    before = audit_chain.stats()["bytes_read"]
    writer = AuditChainWriter(str(path))
    for i in range(300):
        writer.append({"kind": "seal", "i": i})
    steady = audit_chain.stats()["bytes_read"] - before
    assert steady == 0, f"稳态封存还在读盘：300 次共读 {steady} 字节（期望 0）"

    # 反例：外部写入后必须重新读链尾（且读取量有界 ≤ 尾部窗口）
    _append_raw_record(path, {"kind": "external"})
    before_ext = audit_chain.stats()["bytes_read"]
    rec = AuditChainWriter(str(path)).append({"kind": "seal", "after_external": True})
    ext_bytes = audit_chain.stats()["bytes_read"] - before_ext
    assert ext_bytes > 0, "外部改链后仍不重读 —— 缓存会算错 seq（假绿）"
    assert ext_bytes <= audit_chain._TAIL_BYTES + 1, (
        f"重新读取了 {ext_bytes} 字节，超过尾部窗口 —— 又退化成全量重读了")
    assert rec["seq"] == 303, f"外部写入后 seq 没接上：{rec['seq']}（期望 303）"
    assert ChainVerifier(path).verify()["valid"] is True


def test_resume_from_file_when_state_is_cold(tmp_path: Path) -> None:
    """缓存不是唯一事实源：**丢掉进程内缓存**（= 换一个进程）后，新实例必须从文件续链。"""
    path = _chain_file(tmp_path)
    writer = AuditChainWriter(str(path))
    for i in range(3):
        writer.append({"kind": "seal", "i": i})

    audit_chain._reset_state_for_tests(path)          # 模拟"另一个进程刚接手"
    rec = AuditChainWriter(str(path)).append({"kind": "seal", "cold": True})
    assert rec["seq"] == 4
    assert ChainVerifier(path).verify() == {
        "valid": True, "count": 4,
        "head": ChainVerifier(path).last_record()["record_hash"], "broken_at": None}


def test_multi_process_appends_keep_chain_valid(tmp_path: Path) -> None:
    """**跨进程**并发：3 个进程各封 40 条 ⇒ 链完整、seq 全唯一（`msvcrt`/`fcntl` 两条路都覆盖）。

    为什么必须有它：只做进程内锁解决不了本仓库真实发生过的
    "同端口 2 个并发实例"（`manage.py:2169`）—— 那两个实例是两个进程。
    """
    path = _chain_file(tmp_path)
    child = (
        "import sys; sys.path.insert(0, sys.argv[1]);"
        "from src.infrastructure.repositories.audit_chain import AuditChainWriter;"
        "p = sys.argv[2];"
        "[AuditChainWriter(p).append({'kind': 'child', 'i': i}) for i in range(40)]"
    )
    procs = [
        subprocess.Popen([sys.executable, "-c", child, str(ROOT), str(path)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        for _ in range(3)
    ]
    for proc in procs:
        _, err = proc.communicate(timeout=120)
        assert proc.returncode == 0, f"子进程失败：{err.decode('utf-8', 'replace')[:400]}"

    valid, count, unique_seq = _summary(path)
    assert (valid, count, unique_seq) == (True, 120, 120), (
        f"跨进程写坏链：valid={valid} 记录={count} 唯一seq={unique_seq}")


# ======================================================================
# 锁拿不到时必须**显式失败**，不许静默写坏链
# ======================================================================

def test_lock_timeout_is_loud_and_writes_nothing(tmp_path: Path, monkeypatch) -> None:
    """拿不到锁 ⇒ 抛 `AuditChainLockTimeout`，且**一条都不写**（不许降级、不许静默）。"""
    path = _chain_file(tmp_path)
    AuditChainWriter(str(path)).append({"kind": "seal"})
    size_before = path.stat().st_size

    monkeypatch.setenv(audit_chain._ENV_LOCK_TIMEOUT, "0.05")
    monkeypatch.setattr(audit_chain, "_try_lock", lambda fh: False)   # 永远抢不到

    with pytest.raises(AuditChainLockTimeout):
        AuditChainWriter(str(path)).append({"kind": "seal", "must_not_land": True})

    assert path.stat().st_size == size_before, "拿不到锁却写进去了 —— 会产出重复 seq 的坏链"
    assert ChainVerifier(path).verify()["count"] == 1


def test_lock_timeout_default_is_declared() -> None:
    """超时上限必须有**单一默认值**且可被环境变量覆盖（`AGENTS.md`「上限写进代码」）。"""
    assert audit_chain.DEFAULT_LOCK_TIMEOUT_SEC > 0
    assert audit_chain._ENV_LOCK_TIMEOUT == "MOSS_AUDIT_CHAIN_LOCK_TIMEOUT"
    if os.environ.get(audit_chain._ENV_LOCK_TIMEOUT):
        pytest.skip("本机用环境变量覆盖了超时，默认值判据不适用")
    assert audit_chain._lock_timeout() == audit_chain.DEFAULT_LOCK_TIMEOUT_SEC


def test_lock_wait_is_bounded_for_the_event_loop() -> None:
    """等待上限必须**小**：A18 是在**事件循环**上同步调 `append()` 的。

    临界区实测 <1 ms（探针 F：一次链尾读 + 一次追加），所以 1 s 已经是 1000× 余量；
    把它调到几十秒，等于把"审计链锁争用"直接变成"全站卡住"
    （本项目 2026-09-27 那次阻塞事件循环事故的同一形状）。
    这条不是风格检查 —— 它是**给未来改这个数的人**留的刹车。
    """
    assert audit_chain.DEFAULT_LOCK_TIMEOUT_SEC <= 1.0, (
        f"锁等待上限被调大到 {audit_chain.DEFAULT_LOCK_TIMEOUT_SEC}s —— "
        "A18 在事件循环上同步调用，先确认它已挪进线程（PRD §45.6）再改这个数")
