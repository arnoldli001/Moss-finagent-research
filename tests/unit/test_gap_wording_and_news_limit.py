"""三处「客户看到的错话」的判据（`CHG-0135`）。

用户 2026-09-30 报障后点名的三件事，本文件一条一条守：

| # | 缺陷 | 后果 | 判据 |
|---|---|---|---|
| ① | `liquidity_cycle` 往 `data_gaps` 写「当前环境**无法访问 CME/FRED**」 | **FRED 其实可达**（连接器自己都这么写）⇒ 客户读到"用着 FRED 的数据说 FRED 不可达" | `test_fedwatch_gap_text_no_longer_claims_fred_unreachable` |
| ② | 「不适用 / 未收录 / 真失败」三种情形在客户面板上**同一句话** | 客户把"这家公司没有质押公告"读成"系统取不到数" | `test_three_gap_kinds_have_three_distinct_wording` |
| ③ | `CachedNewsFetcher.fetch_news(limit=None)` 把 `None` 透传到 `max(1, limit)` | `TypeError: '>' not supported between NoneType and int` ⇒ **个股新闻整条取不到** | `test_none_limit_never_reaches_max` |
"""
from __future__ import annotations

import asyncio

import pytest

from src.core.exceptions import DataFetchError, NoApplicableData

# ─────────────────── ① 不再声称 FRED 不可达 ───────────────────


def test_fedwatch_gap_text_no_longer_claims_fred_unreachable() -> None:
    """`CME/FRED 不可达` 这句错话**不许**再进 `data_gaps`（给客户看的缺口文案）。

    事实：CME 主机确实不可达（TCP 预检跳过），**FRED 可达**
    （`fedwatch_connector` 自己的日志写着「政策利率请用 fed:policy_range
    （FRED 源，实测可达）」）。把两个源混成一句"都不可达"是**直接误导**，
    而且正面违反 `decision/capabilities.py` 的明令。

    ⚠️ **判据用 AST 取"真会进 `data_gaps` 的字符串"，不搜全文** ——
    我第一版就是搜全文，结果被**我自己解释这件事的注释**命中
    （注释里引用了那句被删掉的话）。搜字符串的判据会被"解释它的文字"绊倒，
    这正是本仓库反复记录的那类假红。
    """
    import ast
    from pathlib import Path

    path = Path("src/domain/skills/liquidity_cycle/analyzer.py")
    tree = ast.parse(path.read_text(encoding="utf-8"))

    appended: list[str] = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "append"
                and isinstance(node.func.value, ast.Subscript)):
            continue
        key = node.func.value.slice
        if isinstance(key, ast.Constant) and key.value == "data_gaps":
            appended += [a.value for a in node.args
                         if isinstance(a, ast.Constant) and isinstance(a.value, str)]

    # 自证：必须真的取到了目标（取不到 ⇒ 判据恒绿，什么也没守）
    assert appended, (
        "没解析出任何 `data_gaps.append(...)` 的字面量 —— 判据失去目标，"
        "此时它会静默地永远通过"
    )
    bad = [s for s in appended if "CME/FRED" in s or "无法访问 CME" in s]
    assert not bad, (
        f"analyzer 又在往 data_gaps 里写「无法访问 CME/FRED」了：{bad} —— "
        "FRED 是可达的，这句话是错的，且违反 capabilities 的明文禁令"
    )
    assert "fed:policy_range" in path.read_text(encoding="utf-8"), (
        "既然 CME 不可达，就必须指向**真正可用**的替代口径（FRED 的政策利率）"
    )
    # 而"禁止这种表述"的那条规则本身必须还在（提示词侧防线不能一起删掉）
    from src.domain.agents.decision.capabilities import render_capability_block

    assert "无法访问 CME/FRED" in render_capability_block(compact=False)


# ─────────────────── ② 三种缺口三种说法 ───────────────────


def test_noapplicable_data_carries_machine_readable_marker() -> None:
    """类型 + 字面标记两条都要有：类型给代码，标记跨层（路由器聚合后类型会丢）。"""
    na = NoApplicableData("银行没有流动比率", kind="not_applicable")
    nc = NoApplicableData("该专题表未收录该主体", kind="not_covered")
    assert isinstance(na, DataFetchError), "它仍是取数错误的一种，链路不许漏接"
    assert na.kind == "not_applicable" and nc.kind == "not_covered"
    assert NoApplicableData.MARKER_NOT_APPLICABLE in str(na)
    assert NoApplicableData.MARKER_NOT_COVERED in str(nc)
    with pytest.raises(ValueError):
        NoApplicableData("kind 写错必须报错", kind="whatever")


def test_markers_agree_with_supervisor() -> None:
    """★ 跨模块契约：异常里的标记与 supervisor 认的标记**必须逐字一致**。

    两处各写一份字面量，改一处忘另一处 ⇒ 分类静默失效
    （这正是本仓库反复记录的"同一样东西写在多处"的形状）。
    """
    from src.orchestration.supervisor import (
        NOT_APPLICABLE_MARKERS,
        NOT_COVERED_MARKERS,
    )

    assert NoApplicableData.MARKER_NOT_APPLICABLE in NOT_APPLICABLE_MARKERS
    assert NoApplicableData.MARKER_NOT_COVERED in NOT_COVERED_MARKERS
    assert not set(NOT_APPLICABLE_MARKERS) & set(NOT_COVERED_MARKERS), (
        "两个标记集合不许重叠，否则判定顺序会决定分类结果"
    )


def test_three_gap_kinds_have_three_distinct_wording() -> None:
    """★ 三种情形必须给出**三句不同的话**（这正是用户要的"分开显示"）。"""
    from src.orchestration.supervisor import collection_gap_note

    na = collection_gap_note("流动比率:600036", "该口径对本主体不适用",
                             stage="not_applicable")
    nc = collection_gap_note("对外担保占净资产比:600036",
                             "该专题只收录有担保公告的公司", stage="not_covered")
    failed = collection_gap_note("主线告警:600036", "所有源都没返回", stage="local")
    texts = {na, nc, failed}
    assert len(texts) == 3, f"三种情形的话术没有区分开：{texts}"
    assert "不适用" in na and "未收录" in nc and "未取到" in failed
    assert "非缺陷" in na and "非缺陷" in nc, "后两者必须明说不是故障"
    assert "≠ 取值为 0" in nc, (
        "『未收录』必须点明不等于 0 —— 否则客户会把『表里没有这家公司』"
        "读成『这家公司没有担保』（正好读反）"
    )


def test_real_failure_is_not_misclassified() -> None:
    """★ 反向判据：**真的取数失败**不许被归类成"不适用/未收录"。

    没有这条，把标记写得宽一点就能让所有失败都"看起来很健康"。
    """
    from src.orchestration.supervisor import (
        NOT_APPLICABLE_MARKERS,
        NOT_COVERED_MARKERS,
    )

    real = [
        "DataFetchError: 所有数据源获取 主线告警:600036 均失败",
        "TimeoutError: 防撞钟 10s",
        "ConnectionError: 源站 502",
    ]
    for text in real:
        assert not any(m in text for m in NOT_APPLICABLE_MARKERS), text
        assert not any(m in text for m in NOT_COVERED_MARKERS), text


def test_compliance_connector_raises_not_covered_for_missing_entity() -> None:
    """★ **行为**判据（不是搜源码）：两处「不在表内」必须**抛**带标记的异常。

    这是本轮修复的关键：原来只 `logger.info` + `return []`，上层拿到的
    只是一个空列表 ⇒ 与"真的取不到"在客户面板上无法区分。

    做法：把两张专题表换成**空表**（表结构在、但没有这只票），
    直接调连接器的两个点位方法，断言抛 `NoApplicableData` 且 kind 正确。
    """
    from src.infrastructure.connectors.compliance_fin_connector import (
        ComplianceFinConnector,
    )

    conn = ComplianceFinConnector()
    # 表在、但 by_code 里没有 600036（= "不在表内"）
    conn._pledge_holder_index = lambda: {"rows": 126863, "by_code": {}}  # type: ignore[method-assign]
    conn._guarantee_snapshot = lambda window: {}                        # type: ignore[method-assign]

    for call, label in (
        (lambda: conn._pledge_points("600036", None), "大股东质押比例"),
        (lambda: conn._guarantee_points("600036", None), "对外担保占净资产比"),
    ):
        with pytest.raises(NoApplicableData) as ei:
            call()
        assert ei.value.kind == "not_covered", f"{label} 的 kind 错了：{ei.value.kind}"
        assert NoApplicableData.MARKER_NOT_COVERED in str(ei.value), label
        assert "0%" in str(ei.value), (
            f"{label} 的文案必须点明『不在表内』≠『取值为 0』"
        )


# ─────────────────── ③ `limit=None` 不许炸 ───────────────────


def test_none_limit_never_reaches_max() -> None:
    """★ 复现真凶：`None` 传到 `max(1, limit)` 会抛 TypeError。

    先证明"裸 max 会炸"（否则判据可能在守一个不存在的问题），
    再证明归一化之后不炸。

    ⚠️ `src/quant/stock_news.py` 是**故意不入公开仓库**的私有模块
    （`.gitignore:82` 显式忽略，`news_fetcher` 里也写着"公开仓库无此模块 →
    返回空"）⇒ 本文件**入库**、那个模块**不入库**，克隆里必须**跳过**而不是红。
    """
    news_mod = pytest.importorskip(
        "src.quant.stock_news",
        reason="私有模块（.gitignore:82 显式忽略）—— 公开仓库里没有它，只有本机部署有")
    DEFAULT_LIMIT = news_mod.DEFAULT_LIMIT
    _build_url, _norm_limit = news_mod._build_url, news_mod._norm_limit

    with pytest.raises(TypeError):
        max(1, None)                      # noqa: F821 —— 这就是线上那一抛

    assert _norm_limit(None) == DEFAULT_LIMIT
    assert _norm_limit(0) == DEFAULT_LIMIT and _norm_limit(-3) == DEFAULT_LIMIT
    assert _norm_limit("5") == 5
    url = _build_url("600036", None, sort="time")     # 修复前这里就抛
    #: URL 是**整体 percent-encode** 的（直接塞原始 JSON 会被接口判非法参数），
    #: 所以断言要**解回来做结构比对**，不能搜字符串 —— 搜字符串第一版就假红了。
    import json
    from urllib.parse import parse_qs, urlparse

    payload = json.loads(parse_qs(urlparse(url).query)["param"][0])
    assert payload["param"]["cmsArticleWebOld"]["pageSize"] == DEFAULT_LIMIT, payload


def test_parse_news_payload_tolerates_none_limit() -> None:
    """解析入口同样归一化（切片不再依赖调用方传对）。"""
    news_mod = pytest.importorskip(
        "src.quant.stock_news",
        reason="私有模块（.gitignore:82 显式忽略）—— 公开仓库里没有它")
    DEFAULT_LIMIT = news_mod.DEFAULT_LIMIT
    parse_news_payload = news_mod.parse_news_payload

    payload = {"result": {"cmsArticleWebOld": [
        {"title": f"标题{i}", "date": "2026-09-30", "url": "u", "content": "c"}
        for i in range(10)
    ]}}
    assert len(parse_news_payload(payload, None)) == DEFAULT_LIMIT


def test_cached_fetcher_does_not_forward_none_limit() -> None:
    """★ 根因所在层：`None` **不许**被透传给被包装者（改为不传这个 kwarg）。"""
    from src.infrastructure.connectors.cached_news_fetcher import CachedNewsFetcher

    seen: list[dict] = []

    class _Stub:
        async def fetch_news(self, code, **kw):        # noqa: ANN001, ANN003
            seen.append(kw)
            return []

        async def fetch_topic_news(self, keywords, **kw):  # noqa: ANN001, ANN003
            seen.append(kw)
            return []

    fetcher = CachedNewsFetcher(_Stub())
    fetcher._store = _noop_store            # type: ignore[assignment]
    asyncio.run(fetcher.fetch_news("600036"))
    asyncio.run(fetcher.fetch_news("600036", limit=7))
    asyncio.run(fetcher.fetch_topic_news(["银行"]))
    assert seen[0] == {}, f"None 被透传了：{seen[0]}（这正是线上那个 TypeError 的入口）"
    assert seen[1] == {"limit": 7}, "给了具体值就必须传下去"
    assert seen[2] == {}, "topic 那条同样不许透传 None"


async def _noop_store(*_args, **_kwargs) -> None:  # noqa: ANN002, ANN003
    """替掉缓存写入（判据只关心"透传了什么"）。"""
    return None
