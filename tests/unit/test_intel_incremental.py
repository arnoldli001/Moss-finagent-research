"""增量回填的回归测试。

## 为什么必须有这些测试

首版 `fetch_incremental` 有一个**静默丢数据**的 bug，而且没有任何测试
覆盖它 —— 表现是"采集任务每次都成功、日志无异常、界面正常"，但内容只
进来一小部分：

    水位线 13:15:06 → 第 1 页（无 end_time）30 条：13:15:41 … 11:43:09
    → 过滤 `ts > 水位线` 只剩 1 条（13:15:41）
    → 收 1 条就把水位线推到 13:15:41
    → 下一轮第 1 页没有更新的 → 空 → break
    → 剩下 29 条**永远不会被取**

根因是它把"一个时间戳"当成了两个边界（新鲜区下界 **和** 回填走上界）。
现在水位线是**已处理区间的最老边界**，取法永远只有一个方向：
`end_time=水位线` 连续往回取，取到什么就把水位线推到这批最老的那条。

这类 bug 靠人工看日志发现不了，只能靠"构造出那个边界"的测试锁住。
测试全部 mock `fetch_topics`，**不联网**。
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.infrastructure.connectors import zsxq_incremental as M
from src.infrastructure.connectors import zsxq_source as S


class FakeTopic:
    def __init__(self, created_at: str, hash_suffix: str) -> None:
        self.created_at = created_at
        self.content_hash = f"hash-{hash_suffix}"
        self.title = f"T{hash_suffix}"
        self.text = ""


def _ts(i: int) -> str:
    """第 i 条的时间戳（i 越小越新），步长 1 分钟，从 13:59 起往回。

    ⚠️ 必须**严格单调递减**：测试后端靠字符串比较做 `end_time` 过滤，
    除以取模拼小时会让跨小时边界错位（i=99 → 12:00 而 i=100 → 11:59，
    看着对，但 i=60 → 12:59 与 i=59 → 13:00 之间就断了）。
    这里直接用"从基准时刻起的分钟偏移"算，天然单调。
    """
    total = 13 * 60 + 59 - i          # 基准 13:59，每条早 1 分钟
    return f"2026-09-25T{total // 60:02d}:{total % 60:02d}:00.000+0800"


def _range(start: int, size: int) -> list[FakeTopic]:
    return [FakeTopic(_ts(i), str(i)) for i in range(start, start + size)]


def _fake_backend(monkeypatch: pytest.MonkeyPatch, newest: int = 0,
                  total: int = 200) -> dict[str, int]:
    """装一个**真正的**倒序分页后端（含边界，模拟 `end_time` 的 `<=` 语义）。

    `_ts` 随索引**递减**，所以"第一条 <= end_time"等价于"最大的 i
    使 `_ts(i) >= end_time`" —— 这里直接用后者，避免把方向写反。
    """
    calls = {"n": 0}

    def fake(*, limit: int = 30, end_time: str | None = None):
        calls["n"] += 1
        lo = newest
        if end_time:
            cands = [i for i in range(newest, total) if _ts(i) >= end_time]
            if not cands:
                return []          # 请求比最老内容还老的区间 → 空
            lo = max(cands)
        return _range(lo, min(limit, total - lo))

    monkeypatch.setattr(S, "fetch_topics", fake)
    return calls


@pytest.fixture()
def root(tmp_path: Path) -> Path:
    (tmp_path / "data" / "intel").mkdir(parents=True)
    return tmp_path


def _cursor_file(root: Path) -> Path:
    """水位线文件的**真实**位置（相对 `root`，不是相对项目 cwd）。

    ⚠️ 早期版本这里硬编码了 `data/intel/zsxq_cursor.json`，于是写进了
    项目目录、而 `fetch_incremental` 读的是 `tmp_path` —— 测试里"保存水位线"
    实际没生效，表现为**永远追不平**。路径必须由 `root` 推出来。
    """
    return root / "data" / "intel" / "zsxq_cursor.json"


def _set_wm(root: Path, wm: str) -> None:
    _cursor_file(root).write_text(
        json.dumps({"watermark": wm}), encoding="utf-8")


def test_backfill_drains_everything_below_the_watermark(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """核心回归：水位线之上只有 1 条，但下面还有一大堆 —— 必须都取回来。

    这正是首版丢掉 29 条的场景。
    """
    _fake_backend(monkeypatch)
    _set_wm(root, _ts(1))

    r = M.fetch_incremental(root=root, max_fetch=200, max_pages=5)

    got = {t.content_hash for t in r.topics}
    # 边界那条（= 水位线自己）**已处理过**，`end_time` 含边界会让它再回来，
    # 所以刻意排除它 —— 不能算"必须取到"。
    assert "hash-30" in got, "水位线之下的内容被丢掉了（首版 bug 复现）"
    assert "hash-100" in got, "回填没有一路追下去"


def test_cursor_advances_monotonically_backwards(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """水位线每轮都往**更老**推进，且最终追平到空。

    数据量要够大：若总量只有 200 条就会在循环上限之后才追平，
    那测的就不是"能追平"而是"跑得够多轮"。
    """
    _fake_backend(monkeypatch, total=500)
    _set_wm(root, _ts(5))

    seq = []
    for _ in range(40):
        r = M.fetch_incremental(root=root, max_fetch=200, max_pages=5)
        seq.append((r.new_count, r.watermark))
        if r.new_count == 0:
            break
        _set_wm(root, r.watermark)

    assert seq[-1][0] == 0, f"没有追平，共 {len(seq)} 轮: {seq[-4:]}"
    # 水位线索引单调不减（时间单调变老）
    def idx_of(w: str) -> int:
        return next((i for i in range(2000) if _ts(i) == w), -1)

    idx = [idx_of(w) for _, w in seq]
    assert -1 not in idx, f"出现无法映射的水位线: {idx}"
    assert idx == sorted(idx), f"水位线没有单调往老推进: {idx}"


def test_watermark_unchanged_when_nothing_fetched(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """空结果**不得**改动水位线。

    "只取到几条就把水位线推过去"正是首版丢数据的机制。
    """
    monkeypatch.setattr(S, "fetch_topics", lambda **kw: [])
    _set_wm(root, _ts(5))

    r = M.fetch_incremental(root=root)

    assert r.new_count == 0
    assert r.watermark == _ts(5), "空结果不应改动水位线"


def test_truncated_when_hitting_fetch_cap(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """装满上限时必须标 truncated（首版漏了这种最常见的情形）。"""
    _fake_backend(monkeypatch, total=500)
    _set_wm(root, _ts(0))

    r = M.fetch_incremental(root=root, max_fetch=30, max_pages=5)

    assert len(r.topics) == 30
    assert r.truncated is True, "装满上限却没标数据不完整"
    assert r.gap_note, "截断时必须给出可展示的缺口说明"


def test_truncated_when_hitting_page_cap(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """翻满页数也必须标 truncated。"""
    _fake_backend(monkeypatch, total=500)
    _set_wm(root, _ts(0))

    r = M.fetch_incremental(root=root, max_fetch=9999, max_pages=3)

    assert r.pages_used == 3
    assert r.truncated is True


def test_no_duplicates_within_one_run(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """`end_time` 含边界 → 边界那条会重复回来，必须去重。"""
    _fake_backend(monkeypatch, total=200)
    _set_wm(root, _ts(3))

    r = M.fetch_incremental(root=root, max_fetch=200, max_pages=5)

    hashes = [t.content_hash for t in r.topics]
    assert len(hashes) == len(set(hashes)), "同一批内出现重复条目"


def test_cross_run_has_no_overlap(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """两轮之间不得整页级重复。"""
    _fake_backend(monkeypatch, total=200)
    _set_wm(root, _ts(2))

    first = M.fetch_incremental(root=root, max_fetch=30, max_pages=5)
    _set_wm(root, first.watermark)
    second = M.fetch_incremental(root=root, max_fetch=30, max_pages=5)

    overlap = ({t.content_hash for t in first.topics}
               & {t.content_hash for t in second.topics})
    # 边界那条允许重复一次（end_time 含边界），但不能是整页级
    assert len(overlap) <= 1, f"两轮之间重复 {len(overlap)} 条"


def test_first_run_without_watermark_keeps_newest(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """首次运行（无水位线）：不带 end_time 取最新一页，水位线推到最老。"""
    _fake_backend(monkeypatch, total=200)

    r = M.fetch_incremental(root=root, max_fetch=30, max_pages=5)

    assert r.new_count == 30
    assert r.topics[0].created_at == _ts(0), "首轮应从最新一条开始"
    assert r.watermark == _ts(29), "水位线应指向本批最老那条"


def test_stops_when_backend_ignores_end_time(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """服务端忽略 `end_time`（每页都返回同一批）时必须停下，不能死循环。"""
    calls = {"n": 0}

    def fake(**kw):
        calls["n"] += 1
        return _range(0, 30)

    monkeypatch.setattr(S, "fetch_topics", fake)
    _set_wm(root, _ts(0))

    r = M.fetch_incremental(root=root, max_fetch=9999, max_pages=10)

    assert calls["n"] <= 3, f"未及时停止，调用了 {calls['n']} 次"
    hashes = [t.content_hash for t in r.topics]
    assert len(hashes) == len(set(hashes)), "死循环路径下出现重复"
