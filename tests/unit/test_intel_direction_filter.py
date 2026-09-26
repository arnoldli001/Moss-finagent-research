"""多/空（原文倾向）筛选的回归测试（用户口径 2026-09-26）。

## 用户口径

> "intel-controls 表头要加一个 多/空 的过滤选择器（多还是空 是模型分析
>   出来的 每条信息第一个字【空】【多】）"

## 钉住的四条不变量

1. **筛出来的与看到的标记必须是同一判据**。列表标题前那个【多】/【空】
   由前端 `directionMarker` 渲染，而筛选在服务端 —— 两边判据分叉的表现是
   "筛了【多】却漏掉几条明显带【多】的"，且**不会有任何报错**。
2. **判据只有一份**：`service.direction_of` / `service._has_direction` 都
   委托 `alert_bridge`，不允许各自实现一遍（2026-09-26 之前就是三份）。
3. **角标统计在筛选之前**：选中【多】之后【空】的角标不能变成 0，
   否则用户没法切回去。
4. **收容组不参与多空筛选**：它装的是"给不出方向"的条目，
   筛【多】时整组都不该出现（放它在筛选之后要额外踢，容易漏）。
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

from src.domain.intel import service as S

BULL = "偏多"
BEAR = "偏空"
NEUTRAL = "中性"


def _item(idx: int, *, tone: dict | None = None,
          days_ago: float = 0.0) -> dict:
    ts = (datetime.now(timezone.utc).astimezone()
          - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    # 正文必须超过 `content_filter.MIN_CONTENT_CHARS`（50），否则样本会在
    # 内容过滤那一步被清空，失败信息看起来像"筛选太狠"。
    body = (f"这是第 {idx} 条公开快讯的正文内容，用于验证多空筛选的判据"
            f"与角标统计，长度需要超过内容过滤的最小字数门槛。")
    return {
        "kind": "newswire", "kind_label": "财经快讯",
        "title": f"标题 {idx}", "summary": body, "published_at": ts,
        "source_alias": "src-abc12345", "platform": "东方财富",
        "codes": [], "industry": "", "rating_origin": "", "agency": "",
        "content_hash": f"hash{idx}", "extra": {},
        "credibility": {"score": 74, "source_base": 74, "content_base": 74,
                        "source_reason": "权威财经媒体",
                        "content_reason": "含事实要素"},
        "tone": tone,
    }


def _bull(idx: int, **kw) -> dict:
    return _item(idx, tone={"tone": BULL, "has_tone": True, "source": "model"},
                 **kw)


def _bear(idx: int, **kw) -> dict:
    return _item(idx, tone={"tone": BEAR, "has_tone": True, "source": "model"},
                 **kw)


def _undetermined(idx: int, **kw) -> dict:
    return _item(idx, tone={"tone": "未定", "has_tone": False, "source": "model"},
                 **kw)


class _StubItem:
    def __init__(self, payload: dict) -> None:
        self._p = payload

    def to_public(self) -> dict:
        return dict(self._p)


def _patch_pipeline(monkeypatch, items: list[dict]) -> None:
    async def _fake_fetch_all(**_kw):
        return [_StubItem(x) for x in items], {}

    monkeypatch.setattr(
        "src.infrastructure.connectors.intel_sources.fetch_all", _fake_fetch_all)
    monkeypatch.setattr(
        "src.infrastructure.connectors.zsxq_incremental.fetch_incremental",
        lambda: (_ for _ in ()).throw(RuntimeError("测试不取知识星球")))
    monkeypatch.setattr(
        "src.infrastructure.connectors.zsxq_incremental.save_watermark",
        lambda *a, **k: None)


def _run(**kw):
    return asyncio.run(S.build_feed(**kw))


def _mixed() -> list[dict]:
    return [_bull(1), _bull(2), _bear(3), _undetermined(4), _undetermined(5)]


# ======================================================================
# 筛选本身
# ======================================================================

def test_direction_bull_keeps_only_bullish(monkeypatch) -> None:
    _patch_pipeline(monkeypatch, _mixed())
    feed = _run(limit=50, direction="bull", group_undetermined=False)
    titles = {x["title"] for x in feed.items}
    assert titles == {"标题 1", "标题 2"}, titles


def test_direction_bear_keeps_only_bearish(monkeypatch) -> None:
    _patch_pipeline(monkeypatch, _mixed())
    feed = _run(limit=50, direction="bear", group_undetermined=False)
    assert {x["title"] for x in feed.items} == {"标题 3"}


def test_direction_all_is_no_filter(monkeypatch) -> None:
    """`all`（默认）不能改变任何行为 —— 老调用方与预热都依赖这一点。"""
    _patch_pipeline(monkeypatch, _mixed())
    feed = _run(limit=50, group_undetermined=False)
    assert len(feed.items) == 5
    assert feed.direction_hidden == 0


def test_direction_filter_hides_undetermined_items(monkeypatch) -> None:
    """★ 未定条目没有方向，所以筛【多】时它们必须一条都不留。

    这条看着显然，但它是"筛出来的与标记一致"的**必要条件**：
    留下一条没有【多】标记的，用户立刻会发现筛选在骗他。
    """
    _patch_pipeline(monkeypatch, _mixed())
    feed = _run(limit=50, direction="bull", group_undetermined=False)
    assert all(S.direction_of(x) == "bull" for x in feed.items), (
        "筛【多】之后不该出现任何非偏多的条目")


# ======================================================================
# 角标：统计在筛选**之前**
# ======================================================================

def test_direction_dist_counts_before_filtering(monkeypatch) -> None:
    """★ 选中【多】之后，`direction_dist` 里的 bull/bear 都**不能变**。

    否则用户选中【多】→【空】的角标变 0 → 看起来"没有偏空的信息"，
    而且切不回去（按钮还在，但数字是 0 会让人以为点了也没东西）。
    """
    _patch_pipeline(monkeypatch, _mixed())
    feed_all = _run(limit=50, direction="all", group_undetermined=False)
    feed_bull = _run(limit=50, direction="bull", group_undetermined=False)

    assert feed_all.direction_dist == {"bull": 2, "bear": 1}
    assert feed_bull.direction_dist == feed_all.direction_dist, (
        "角标必须与当前选中的方向档无关")


def test_direction_hidden_reports_dropped_count(monkeypatch) -> None:
    """被筛掉的条数要报出来（"为什么只剩这么几条"要可解释）。"""
    _patch_pipeline(monkeypatch, _mixed())
    feed = _run(limit=50, direction="bull", group_undetermined=False)
    assert feed.direction_hidden == 3, "5 条里留下 2 条偏多 → 挡掉 3 条"


# ======================================================================
# 判据本身：三份实现必须收敛成一份
# ======================================================================

def test_neutral_flag_beats_literal_tone() -> None:
    """★ `neutral=True` 压过字面值。

    这是 `alert_bridge._has_direction` 的既有行为，而 `service` 原来那份
    实现**漏了它** —— 于是"模型说偏多、但结论是中性"的行会被筛进【多】，
    而列表上它没有【多】标记。委托之后两边一致。
    """
    item = {"tone": {"tone": BULL, "has_tone": True, "neutral": True}}
    assert S.direction_of(item) == ""
    assert S._has_direction(item) is False


def test_legacy_row_without_has_tone_key_still_counts() -> None:
    """★ 老存储行没有 `has_tone` 键 → 按字面值判，不能被静默丢掉。

    第二轮之前落库的行就是这种形状。原来的 `service._has_direction`
    只看 `has_tone`，会把这些行整批判成"没方向" —— 表现为"偏多筛选下
    老内容全部消失"，而那批内容本来是有标记的。
    """
    item = {"tone": {"tone": BULL}}
    assert S.direction_of(item) == "bull"


def test_service_judgement_is_the_canonical_one() -> None:
    """★ `service` 与 `alert_bridge` 必须**同进同出**（委托，不是各写一份）。

    对每一种输入形状两边的结论都要一样。分叉的表现是"筛选与标记不一致"，
    而那不会有任何报错。
    """
    from src.domain.intel import alert_bridge as ab

    shapes = [
        {"tone": {"tone": BULL, "has_tone": True}},
        {"tone": {"tone": BEAR, "has_tone": True}},
        {"tone": {"tone": NEUTRAL, "has_tone": True}},
        {"tone": {"tone": BULL, "has_tone": True, "neutral": True}},
        {"tone": {"tone": BULL}},                       # 老行
        {"tone": {"tone": "未定", "has_tone": False}},
        {"tone": {}},
        {},
    ]
    for shape in shapes:
        canon = ab.direction_of(shape)
        want = {"偏多": "bull", "偏空": "bear"}.get(canon, "")
        assert S.direction_of(shape) == want, shape
        assert S._has_direction(shape) is ab._has_direction(
            shape.get("tone")), shape


def test_unknown_direction_value_is_rejected_by_route() -> None:
    """路由层对非法 `direction` 报 422，**不静默按 all 处理**。

    静默的话用户以为筛过了，实际看到的是全部 —— "筛选没生效"里最难查的一种。
    """
    import pathlib

    src = pathlib.Path("src/api/routes/intel.py").read_text(encoding="utf-8")
    assert "_DIRECTIONS" in src
    body = src[src.index("def intel_feed"):]
    assert "bad_direction" in body[:6000], "端点里少了非法 direction 的 422"


def test_cache_key_includes_direction() -> None:
    """★ 缓存键必须含 `direction`，否则换方向会拿到上一档的结果。

    漏掉的表现极其隐蔽：切到【空】看到的还是【多】的列表，界面完全正常。
    """
    from src.api.routes.intel import feed_cache_key

    base = dict(limit=60, watch=[], sort="credibility", filter="all")
    key_all = feed_cache_key(**base, direction="all")
    key_bull = feed_cache_key(**base, direction="bull")
    key_bear = feed_cache_key(**base, direction="bear")
    assert len({key_all, key_bull, key_bear}) == 3, "三个方向档必须三个键"


def test_cache_key_default_matches_explicit_all() -> None:
    """★ 不传 `direction` 与显式传 `all` 必须**逐字同键**。

    预热那条路径不传该参数（走默认值），端点不传时也不写进 query。
    两边键不同的话，预热写进一份、请求读另一份 —— 用户照样吃冷启动，
    而日志里写着"已从落盘缓存热加载"。
    """
    from src.api.routes.intel import feed_cache_key

    base = dict(limit=60, watch=[], sort="credibility", filter="all")
    assert feed_cache_key(**base) == feed_cache_key(**base, direction="all")
