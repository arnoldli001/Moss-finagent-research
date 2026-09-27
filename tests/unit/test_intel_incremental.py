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
    total = 839 - i                   # 基准 13:59，每条早 1 分钟
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


def test_watermark_only_ever_moves_older(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """水位线**只能往更老走，绝不前移** —— 这是丢数据事故的直接防线。

    ⚠️ 本用例替换了原来的 `test_cursor_advances_monotonically_backwards`。
    老断言要求"每轮都取到更老的内容、最终**追平到空**"，那是纯回填时代的行为。

    现在每轮都会**重新取一遍「现在 → 水位线」这一段**（`fresh_floor = watermark`），
    所以 `new_count` 不会再掉到 0 —— "追平到空"这个性质**本身已经不成立**了。
    为什么必须重取：情报流是**每次请求现拼、不落库**的，靠水位线跳过已取内容
    会让那段内容**永远不会出现在页面上**（2026-09-25 实测 `counts` 里
    `research_note` 直接归零，用户报障"前端看不到 1 条知识星球信息"）。

    仍然必须成立、而且更要命的不变式是：**水位线只能变老**。
    它一旦前移，被跨过的那段就被标成"已处理"而实际从没取过 —— 永久静默丢内容。
    """
    _fake_backend(monkeypatch, total=500)
    _set_wm(root, _ts(5))

    seen: list[str] = []
    for _ in range(12):
        r = M.fetch_incremental(root=root, max_fetch=200, max_pages=5)
        seen.append(r.watermark)
        _set_wm(root, r.watermark)

    # `_ts(i)`：i 越大越**老**（见 test_first_run_without_watermark_keeps_newest），
    # 所以"只往更老走"等价于时间戳字符串**单调不增**。
    assert seen == sorted(seen, reverse=True), f"水位线出现了前移: {seen}"
    # ⚠️ 这里**刻意不**断言"水位线必须推进"。水位线只在**新鲜段被完整覆盖**
    # 之后才会往更老挪（`caught_up` 为真才跑回填）—— 本用例的合成数据里
    # "现在 → 水位线"这一段超过单轮 200 条的上限，所以它**应当原地不动**：
    # 宁可不回填，也不把没取到的那段标成"已处理"。这正是要守的不变式。


def test_empty_middle_page_is_retried_instead_of_stopping_paging(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 翻页途中某一页为空 → **重试**，而不是本轮提前收工。

    ## 为什么这条必须单独钉（2026-09-26，单轮上限提到 100 之后）

    一轮要翻 4 页（`MAX_FETCH_PER_RUN` 100 / `PAGE_SIZE` 30），而上游空页
    约 1/6 —— 于是"一轮里**至少撞到一次**空页"的概率约 1-(5/6)^4 ≈ **52%**。

    撞上时原实现的处理是 `caught_up = True`（把空页当成"翻到底了"），于是：

      · 这一轮**只取到前几页**，剩下的新鲜内容要等下一轮才出现；
      · 多出来的额度会被阶段 B 用**16 天前的回填内容**填满
        —— 那些内容落在 3 天时效窗口之外，白取一趟（实测：一轮的 100 条里
        混进了 09-08 的条目，时间跨度被拉成 17 天）。
    """
    calls = {"n": 0}
    _fake_backend(monkeypatch, newest=0, total=400)
    real = S.fetch_topics

    def flaky(*, limit: int = 30, end_time: str | None = None):
        calls["n"] += 1
        # 第 2 次调用（第 2 页）返回一次空页，其余正常。
        if calls["n"] == 2:
            return []
        return real(limit=limit, end_time=end_time)

    monkeypatch.setattr(S, "fetch_topics", flaky)
    _set_wm(root, _ts(0))

    r = M.fetch_incremental(root=root, max_fetch=100, max_pages=8)

    assert len(r.topics) >= 90, (
        f"只取到 {len(r.topics)} 条 —— 中途空页让本轮提前收工了（没有重试）")
    # 而且不该混进"16 天前"那种回填内容：这批的时间跨度应落在最新那一段内
    oldest = min(t.created_at for t in r.topics)
    assert oldest >= _ts(140), f"混进了更早的回填内容：{oldest}"


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


def test_empty_newest_page_is_retried_not_treated_as_caught_up(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """**最新页偶发为空时，不能当成"已追平"**（2026-09-26 用户报障）。

    ## 这一支漏掉时的表现（用户原话："券商作文 原来的信息丢那里去了"）

        第一页（无 end_time）返回空 → 原实现置 `caught_up = True`
        → 立刻转去回填水位线那一段（当时水位线在 **16 天前**）
        → 那 30 条全部落在 3 天时效窗口之外被丢弃
        → 页面上「券商作文」**整块消失**，缺口文案还写着
          "券商作文最近 3 天无更新（已归档 N 条过期内容）"—— 与事实相反

    数据源空页是**偶尔**发生的（阶段 B 里记过实测：同一请求连调 6 次，
    第 5 次返回 0 条、第 6 次又 30 条），所以用户看到的是
    "刷几次，这块内容时有时无"。这里锁住三件事：**要重试**、
    **最终拿到的必须是最新那一页**、**不能拿 16 天前的内容冒充**。
    """
    # 第一次调用（最新页）返回空，之后正常 —— 模拟一次抖动。
    state = {"first": True}
    real = S.fetch_topics
    _fake_backend(monkeypatch, newest=0, total=200)
    fake = S.fetch_topics

    def flaky(*, limit: int = 30, end_time: str | None = None):
        if end_time is None and state["first"]:
            state["first"] = False
            return []                   # 抖动：最新页空
        return fake(limit=limit, end_time=end_time)

    monkeypatch.setattr(S, "fetch_topics", flaky)
    # 水位线取"比最新旧 40 分钟"：这样阶段 A 的第一页不会一上来就撞到
    # 水位线（撞到就 `caught_up`，测不出"重试后拿到的是最新页"）。
    _set_wm(root, _ts(40))

    r = M.fetch_incremental(root=root, max_fetch=30, max_pages=5)

    assert r.topics, "抖动一次就把整块内容丢了（没有重试）"
    assert r.upstream_empty is False
    # 修复后：重试拿到最新页 → 这 30 条是 `_ts(0)` 起往回的 30 条（最早 `_ts(29)`）。
    # 修复前：空页被当成追平 → 转去 `end_time=_ts(40)` 回填 → 最早会是 `_ts(69)`，
    #         即界面上少了最近 40 分钟的全部内容。
    assert min(t.created_at for t in r.topics) >= _ts(29), \
        "重试后拿到的必须是最新那一段，不能退去回填更早的内容"
    assert real is not None             # 保留导入，避免 lint 误报未使用


def test_upstream_empty_is_reported_and_does_not_backfill(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """重试后仍为空：**如实报缺口**，且**不去回填**（回填必然落在窗口外）。

    ⚠️ 不能退化成"券商作文最近 3 天无更新"——那句的意思是
    "看过了、确实没有新的"，与"这一趟没拿到"是两件事。
    """
    monkeypatch.setattr(S, "fetch_topics", lambda **kw: [])
    _set_wm(root, "2026-09-09T00:00:00.000+0800")

    r = M.fetch_incremental(root=root, max_fetch=30, max_pages=5)

    assert r.topics == []
    assert r.upstream_empty is True
    assert r.truncated is True, "没取到必须标数据不完整，否则界面静默少一块"
    assert "未取到" in r.gap_note or "返回空" in r.gap_note, \
        f"缺口文案没说真话：{r.gap_note!r}"
    assert "最近 3 天无更新" not in r.gap_note, \
        "把『没拿到』说成『没有新的』—— 用户会以为这个来源停了"
    assert r.watermark == "2026-09-09T00:00:00.000+0800", \
        "空结果不得改动水位线"


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


def test_cross_run_repeats_only_the_fresh_segment(
        root: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """两轮之间**会**重复"最新那一段" —— 这是刻意的，不是 bug。

    ⚠️ 本用例替换了原来的 `test_cross_run_has_no_overlap`。老断言要求
    "两轮之间整页级不重复"，前提是"取过的内容已经落库了"。**这条链路不落库**：
    情报流每次请求现拼，上一轮取到的内容没有存在任何地方，跳过它 = 让它
    永远不出现在页面上（2026-09-25 实测 `counts` 里 `research_note` 归零）。

    所以这里断言的是**水位线不前移**（真正的不变式），而不是零重复；
    重复只允许发生在"新鲜段"，且由 `content_hash` 在批内去重。
    """
    _fake_backend(monkeypatch, total=200)
    _set_wm(root, _ts(2))

    first = M.fetch_incremental(root=root, max_fetch=30, max_pages=5)
    _set_wm(root, first.watermark)
    second = M.fetch_incremental(root=root, max_fetch=30, max_pages=5)

    assert second.watermark <= first.watermark, \
        f"第二轮把水位线前移了：{first.watermark} → {second.watermark}"
    # 批内仍然不许有重复（那是真 bug）
    for r in (first, second):
        hashes = [t.content_hash for t in r.topics]
        assert len(hashes) == len(set(hashes)), "同一批内出现重复条目"


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
