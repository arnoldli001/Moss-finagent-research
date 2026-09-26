"""主线挖掘：成分股提纯（`relevance.py`）的单元测试。

对应需求：剔除与板块概念**走势不相关或低相关**的成分股，尤其是
"相关度低但大市值、在板块中权重占比高"的票；再叠加总市值门槛与 ST 剔除。

每条测试钉住一个具体的失效方式 —— 这些失效方式全部来自本次实测踩坑：

1. **题材归一化**：茅台同时属于 `白酒`/`白酒Ⅲ`/`白酒概念`，不去重会一个题材
   占掉多个前 3 名额。
2. **假板块排除**：`近期解禁` 靠巧合与三花智控相关 +0.567（排第 7），
   不排除就会挤掉真正的题材。
3. **保护名单**：`同花顺算力主题精选`、`重组蛋白` 是真题材，不能被规则误杀。
4. **共同日期对齐**：不同板块的数据末端差别很大（有的止于 2025-11），
   各自取"最后 240 个交易日"会得到两个不相交的窗口 —— 实测 14 万个组合里
   只有 1035 个能算出相关性。
5. **两个信号都要**：只看主营会砍掉"人形机器人"（三花的核心驱动），
   只看相关性会留下"近期解禁"。
6. **市值门槛按运行日取**：离线写死今天的市值会让回测自动选中后来才长大的票。
7. **保守性**：数据缺口（市值/名称/相关性缺失）不等于"不合格"。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from src.mainline.relevance import (
    CORR_WEIGHT,
    MIN_CORR_SAMPLES,
    MIN_CORR_SAMPLES_YOUNG,
    MIN_TOTAL_MV,
    RelevanceStore,
    ScoreTask,
    _common_tail,
    _log_returns,
    _norm_corr,
    _parse_llm_json,
    _pearson,
    _score_one,
    clean_member_map,
    is_fake_board,
    member_fundamentals,
    normalize_theme,
    rebuild_clean,
    resolve_basic_date,
)

# ==================================================================
# 一、题材归一化
# ==================================================================


@pytest.mark.parametrize("raw,expected", [
    ("白酒概念", "白酒"),
    ("白酒Ⅲ", "白酒"),
    ("白酒", "白酒"),
    ("白酒(A股)", "白酒"),
    ("机器人概念", "机器人"),
    ("PCB概念", "PCB"),
    ("家用电器指数", "家用电器"),
    # 不能把整个名字吃掉（否则题材名会变成空串）
    ("概念", "概念"),
    ("指数", "指数"),
    ("Ⅲ", "Ⅲ"),
])
def test_normalize_theme(raw: str, expected: str) -> None:
    """剥掉分类体系后缀，让同一个题材的不同体系名归一。"""
    assert normalize_theme(raw) == expected


def test_normalize_theme_collapses_maotai_variants() -> None:
    """茅台实测的三个体系名必须归一到同一个题材（否则占掉 3 个名额）。"""
    names = ["白酒", "白酒Ⅲ", "白酒概念", "酒饮料指数"]
    collapsed = {normalize_theme(name) for name in names}
    assert collapsed == {"白酒", "酒饮料"}


# ==================================================================
# 二、假板块与保护名单
# ==================================================================


@pytest.mark.parametrize("name,fake", [
    # 行情特征池
    ("昨日涨停表现", True), ("低价股", True), ("高贝塔值", True),
    ("昨日非ST连板", True), ("微盘股", True), ("次新股", True),
    # 事件/数据型 —— 实测靠巧合冲进相关性前列
    ("近期解禁", True), ("增持计划", True), ("高比例质押", True),
    ("近期强势", True), ("回购计划", True), ("摘帽", True),
    # 指数/样本
    ("同花顺A50", True), ("沪深300样本股", True), ("同花顺金仓100", True),
    # 真题材 —— 必须保留
    ("人形机器人", False), ("PCB概念", False), ("白酒Ⅲ", False),
    ("家用电器", False), ("特斯拉概念", False), ("汽车热管理", False),
])
def test_fake_board_detection(name: str, fake: bool) -> None:
    assert is_fake_board(name) is fake


@pytest.mark.parametrize("name", [
    "重组蛋白",                 # 真实行业（重组蛋白药物），不是"重组"事件
    "同花顺算力主题精选",        # 名字带指数前缀，内容是产业主题
    "同花顺果指数",             # 苹果产业链
    "同花顺中特估100",
])
def test_protected_real_themes_are_not_excluded(name: str) -> None:
    """保护名单优先于假板块规则：这几个实测都是真题材，被误杀就是数据损失。"""
    assert is_fake_board(name) is False


@pytest.mark.parametrize("name", [
    "杭州都市圈", "绍兴市指数", "佛山市指数", "珠三角城市群", "长三角",
    "股东户数增加", "同花顺出海50",   # 风格篮子，不做保护
])
def test_geography_and_style_boards_are_excluded(name: str) -> None:
    """地域板块与股东结构板块不是产业题材。

    实测三花智控与「杭州都市圈」+0.570、「绍兴市指数」+0.557 —— 同城共振
    纯属地缘，与产业主线无关；不排除就会挤掉真正的题材。
    """
    assert is_fake_board(name) is True


@pytest.mark.parametrize("name", [
    "酒、饮料和精制茶制造业",
    "酒饮料和精制茶制造业",
    "通用设备制造业",
    "电气机械和器材制造业",
    "专用设备制造业",
    # 宽泛行业/风格标签：能映射到板块，但对"该买哪只票"没有区分度
    # （半个市场都算"日常消费品"），实测会**成组**吃掉茅台的前 3
    "饮料",
    "食品、饮料与烟草",
    "酿酒商与葡萄酒商",
    "日常消费品",
    "超级品牌",
    "行业龙头",
    "茅",
])
def test_manufacturing_classifications_are_excluded(name: str) -> None:
    """国民经济行业分类的制造业大类 + 宽泛行业/风格标签。

    ⚠️ 实测这对**同义词变体**会吃掉整个前 3：贵州茅台的前 3 被判成
    「酒、饮料和精制茶制造业 / 酒饮料和精制茶制造业 / 饮料」—— 前两个是
    同一个东西的两种写法，于是「白酒」概念被挤出去，
    白酒板块反而把泸州老窖/五粮液/贵州茅台/洋河股份这些**最该留的**全剔了。
    """
    assert is_fake_board(name) is True


@pytest.mark.parametrize("name", ["白酒", "锂电池", "石油加工", "汽车热管理",
                                  "人形机器人", "集成电路制造", "钠离子电池"])
def test_real_themes_survive_the_manufacturing_rule(name: str) -> None:
    """`制造业`/宽泛行业规则不能误伤真题材（这些都得留下）。"""
    assert is_fake_board(name) is False


# ==================================================================
# 三、相关性基础件
# ==================================================================


def _series(closes: list[float], start: int = 20250101) -> list[tuple[str, float]]:
    """造一条日线序列（日期按天递增，格式 YYYYMMDD 由调用方保证有序）。"""
    return [(str(start + index), close) for index, close in enumerate(closes)]


def test_common_tail_takes_overlap_not_each_series_tail() -> None:
    """⚠️ 核心回归：取"共同日期的末尾 N 个"，而不是"各自末尾 N 个再求交"。

    实测场景：个股数据到 2026-09，板块数据止于 2025-11。两者各自取末尾 240 个
    交易日，得到的是两个不相交的窗口 —— 交集只有 12 天，相关性算不出来。
    """
    sret = {f"2025{i:04d}": 0.01 for i in range(1, 400)}        # 到 2026-01
    bret = {f"2024{i:04d}": 0.01 for i in range(1, 200)}        # 到 2024-12
    bret.update({f"2025{i:04d}": 0.02 for i in range(1, 150)})  # 重叠段
    common = _common_tail(sret, bret, 240)
    assert common, "必须能取到共同日期"
    assert len(common) == 149, f"应取重叠段全部（149 天），实际 {len(common)}"
    assert common[0] == "20250001" and common[-1] == "20250149"


def test_common_tail_respects_window() -> None:
    """重叠段比窗口长时，只取末尾 window 个。"""
    sret = {f"2025{i:04d}": 0.01 for i in range(1, 500)}
    bret = {f"2025{i:04d}": 0.01 for i in range(1, 500)}
    common = _common_tail(sret, bret, 240)
    assert len(common) == 240
    assert common[-1] == "20250499"


def test_log_returns_skips_invalid() -> None:
    """前收为 0 / 缺失时跳过该日，而不是产生 inf。"""
    series = [("20250101", 10.0), ("20250102", 0.0), ("20250103", 11.0),
              ("20250104", 12.1)]
    out = _log_returns(series)
    assert "20250102" not in out          # 当日收盘 0，不算收益率
    assert "20250103" not in out          # 前收 0，无法算
    assert out["20250104"] == pytest.approx(0.0953102, rel=1e-5)


def test_pearson_perfect_correlation() -> None:
    assert _pearson([1.0, 2.0, 3.0] * 30, [2.0, 4.0, 6.0] * 30) == pytest.approx(1.0)
    assert _pearson([1.0, 2.0, 3.0] * 30, [-2.0, -4.0, -6.0] * 30) == pytest.approx(-1.0)


def test_pearson_needs_enough_samples() -> None:
    """样本不足返回 None（而不是给一个不可信的数字）。"""
    assert _pearson([1.0] * (MIN_CORR_SAMPLES - 1),
                    [1.0] * (MIN_CORR_SAMPLES - 1)) is None
    assert _pearson([float(i) for i in range(MIN_CORR_SAMPLES)],
                    [float(i) for i in range(MIN_CORR_SAMPLES)]) is not None


def test_pearson_accepts_explicit_lower_threshold() -> None:
    """新板块可以显式放低门槛 —— 否则它的成分股永远算不出相关性。

    共同交易日不可能多于**板块自己的**行情长度，所以一个刚上市两个月的
    概念，它的每一只成分股都凑不够 60 天；不放低门槛就等于整块概念
    一次提纯都没做（实测 886111 玻璃基板 56/56 只 corr=NULL）。
    """
    count = MIN_CORR_SAMPLES_YOUNG
    xs = [float(i) for i in range(count)]
    ys = [2.0 * i for i in range(count)]
    assert _pearson(xs, ys) is None                       # 默认门槛仍是 60
    assert _pearson(xs, ys, min_samples=count) == pytest.approx(1.0)
    # 低于下限仍然拒绝（3~5 天的相关性是纯噪声）
    assert _pearson(xs[:MIN_CORR_SAMPLES_YOUNG - 1],
                    ys[:MIN_CORR_SAMPLES_YOUNG - 1],
                    min_samples=MIN_CORR_SAMPLES_YOUNG) is None


def test_young_board_threshold_is_lower_than_normal() -> None:
    assert MIN_CORR_SAMPLES_YOUNG < MIN_CORR_SAMPLES


def test_norm_corr_is_within_stock() -> None:
    """相关性归一化用**本股票内部**的极值 —— 不同股票的分布宽窄差别很大。"""
    values = [0.2, 0.5, 0.8]
    assert _norm_corr(0.2, values) == pytest.approx(0.0)
    assert _norm_corr(0.8, values) == pytest.approx(1.0)
    assert _norm_corr(0.5, values) == pytest.approx(0.5)
    # 全部相同时给中间值（不能除以 0）
    assert _norm_corr(0.5, [0.5, 0.5]) == pytest.approx(0.5)


# ==================================================================
# 四、LLM 输出解析与打分合成
# ==================================================================


def test_parse_llm_json_handles_fences_and_noise() -> None:
    assert _parse_llm_json('{"scores": []}') == {"scores": []}
    assert _parse_llm_json('```json\n{"scores": [1]}\n```') == {"scores": [1]}
    assert _parse_llm_json('前言 {"scores": [2]} 后记') == {"scores": [2]}
    assert _parse_llm_json("完全不是JSON") == {}


class _FakeGateway:
    """假网关：返回预设 JSON（不联网）。"""

    def __init__(self, body: str) -> None:
        self.body = body
        self.calls: list[str] = []

    async def complete(self, tier: str, system: str, prompt: str, **kwargs):
        self.calls.append(prompt)

        class _Resp:
            content = self.body
            model_used = "fake"

        return _Resp()


def _score(task: ScoreTask, body: str) -> list[dict]:
    gateway = _FakeGateway(body)
    return asyncio.run(_score_one(gateway, task, tier="decision", top=3,
                                  sem=asyncio.Semaphore(1)))


def test_score_blends_correlation_and_business() -> None:
    """最终分 = 走势相关性 60% + 主营业务 40%。"""
    task = ScoreTask(
        code="002050", name="三花智控", business="制冷部件", industry="家电",
        themes=["人形机器人", "家电零部件"],
        corr={"人形机器人": 0.68, "家电零部件": 0.51})
    body = json.dumps({"scores": [
        {"name": "人形机器人", "score": 40, "reason": "机器人业务占比低"},
        {"name": "家电零部件", "score": 95, "reason": "主营核心"},
    ]})
    picked = _score(task, body)
    by = {item["theme"]: item for item in picked}
    # 人形机器人：相关性归一化后 1.0，主营 40 → 0.6*100 + 0.4*40 = 76
    assert by["人形机器人"]["final_score"] == pytest.approx(76.0)
    # 家电零部件：相关性归一化后 0.0，主营 95 → 0.6*0 + 0.4*95 = 38
    assert by["家电零部件"]["final_score"] == pytest.approx(38.0)
    # 排序：人形机器人第一 —— 这正是"主营占比低但走势驱动"的正确结果
    assert picked[0]["theme"] == "人形机器人"
    assert CORR_WEIGHT == 0.6


def test_score_rejects_zero_business_as_coincidence() -> None:
    """主营分为 0（基本无关）时不进前 N —— 防"近期解禁"这类巧合。"""
    task = ScoreTask(
        code="002050", name="三花智控", business="制冷部件", industry="家电",
        themes=["近期解禁", "人形机器人"],
        corr={"近期解禁": 0.99, "人形机器人": 0.68})
    body = json.dumps({"scores": [
        {"name": "近期解禁", "score": 0, "reason": "纯事件"},
        {"name": "人形机器人", "score": 50, "reason": "机器人零部件"},
    ]})
    picked = _score(task, body)
    assert [item["theme"] for item in picked] == ["人形机器人"]


def test_score_dedupes_same_theme_variants() -> None:
    """同一题材的多个体系名只占一个名额。

    归一化发生在 **LLM 答案侧**：候选题材来自我们的题材表（本身已按归一化名
    去重），但 LLM 可能用板块原名（"白酒Ⅲ"）回答，所以要先归一化再落库 ——
    否则"白酒"和"白酒Ⅲ"会各占一个前 3 名额。
    """
    task = ScoreTask(
        code="600519", name="贵州茅台", business="白酒", industry="白酒",
        themes=["白酒", "机器人"],
        corr={"白酒": 0.9, "机器人": 0.2})
    body = json.dumps({"scores": [
        {"name": "白酒Ⅲ", "score": 95, "reason": "板块原名"},
        {"name": "白酒", "score": 95, "reason": "归一化名"},
        {"name": "机器人", "score": 5, "reason": "无关"},
    ]})
    picked = _score(task, body)
    assert [item["theme"] for item in picked] == ["白酒", "机器人"], (
        "白酒的两个体系名必须折叠成一个题材；机器人也应作为候选出现")
    assert len(picked) == 2


def test_build_tasks_orders_candidates_by_correlation(tmp_path: Path) -> None:
    """候选题材按相关性降序、截断到 max_candidates（省 token、避免长尾干扰）。"""
    from src.mainline.relevance import build_tasks

    store = RelevanceStore(tmp_path / "cache.db")
    conn = store.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS ml_board("
                 "code TEXT PRIMARY KEY, name TEXT, kind TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS ml_member("
                 "board_code TEXT, code TEXT, name TEXT, in_date TEXT,"
                 " out_date TEXT, source TEXT)")
    boards = [(f"C{i}.TI", f"题材{i}", "concept") for i in range(5)]
    conn.executemany("INSERT INTO ml_board(code,name,kind) VALUES(?,?,?)", boards)
    conn.executemany("INSERT INTO ml_member(board_code,code) VALUES(?,?)",
                     [(f"C{i}.TI", "600519") for i in range(5)])
    conn.executemany(
        "INSERT INTO ml_theme_board(theme,board_code,board_name,board_kind,members)"
        " VALUES(?,?,?,'concept',0)",
        [(f"题材{i}", f"C{i}.TI", f"题材{i}") for i in range(5)])
    conn.executemany(
        "INSERT INTO ml_member_corr(board_code,code,corr,samples,start_date,"
        "end_date,computed_at) VALUES(?,?,?,100,'','','')",
        [(f"C{i}.TI", "600519", 0.1 * i) for i in range(5)])
    conn.execute("INSERT INTO ml_company_business(code,name,business,fetched_at)"
                 " VALUES('600519','贵州茅台','白酒','')")
    conn.commit()
    conn.close()

    tasks = build_tasks(store=store, warehouse_path=None, max_candidates=3)
    assert len(tasks) == 1
    assert tasks[0].themes == ["题材4", "题材3", "题材2"], "必须相关性降序且截断"
    assert tasks[0].name == "贵州茅台"
    assert tasks[0].signature, "签名必须生成（缓存判定的依据）"


def test_build_tasks_drops_themes_that_map_to_no_board(tmp_path: Path) -> None:
    """**行业分类不能占掉前 3 名额**（实测踩过的结构性问题）。

    实测两个反例：中芯国际的前 3 全是行业分类（集成电路制造/半导体产品/…），
    结果"芯片概念"这个最该留的被挤出去、保留 0 个概念板块；三花智控的
    "通用设备制造业指数"占一席，把"机器人概念"挤掉。

    修法是在候选阶段就只保留**能映射到板块**的题材 —— 行业分类不在
    `ml_theme_board` 里，自然被排除。
    """
    from src.mainline.relevance import build_tasks

    store = RelevanceStore(tmp_path / "cache.db")
    conn = store.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS ml_board("
                 "code TEXT PRIMARY KEY, name TEXT, kind TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS ml_member("
                 "board_code TEXT, code TEXT, name TEXT, in_date TEXT,"
                 " out_date TEXT, source TEXT)")
    # C0 是能映射到板块的产业题材；C1 模拟"行业分类"（有成分股但没有板块映射）
    conn.executemany("INSERT INTO ml_board(code,name,kind) VALUES(?,?,?)",
                     [("C0.TI", "芯片概念", "concept"),
                      ("C1.TI", "半导体产品", "concept")])
    conn.executemany("INSERT INTO ml_member(board_code,code) VALUES(?,?)",
                     [("C0.TI", "688981"), ("C1.TI", "688981")])
    # ⚠️ 只给 C0 写题材映射 —— C1 代表不在概念目录里的行业分类
    conn.execute(
        "INSERT INTO ml_theme_board(theme,board_code,board_name,board_kind,members)"
        " VALUES('芯片','C0.TI','芯片概念','concept',0)")
    # 行业分类的相关性更高（+0.85 vs +0.60），若不过滤会挤掉概念题材
    conn.executemany(
        "INSERT INTO ml_member_corr(board_code,code,corr,samples,start_date,"
        "end_date,computed_at) VALUES(?,?,?,100,'','','')",
        [("C0.TI", "688981", 0.60), ("C1.TI", "688981", 0.85)])
    conn.execute("INSERT INTO ml_company_business(code,name,business,fetched_at)"
                 " VALUES('688981','中芯国际','集成电路晶圆代工','')")
    conn.commit()
    conn.close()

    tasks = build_tasks(store=store, warehouse_path=None)
    assert len(tasks) == 1
    assert tasks[0].themes == ["芯片"], (
        "只保留能映射到板块的题材；行业分类（相关性更高）必须被排除，"
        "否则它会占掉前 3 名额、让板块一个都留不下")
    assert "C1.TI" not in tasks[0].corr


def test_score_skips_themes_the_llm_did_not_answer() -> None:
    """LLM 漏答的题材不猜分，直接不作为候选。"""
    task = ScoreTask(
        code="X", name="X", business="b", industry="i",
        themes=["A", "B", "C"], corr={"A": 0.9, "B": 0.8, "C": 0.7})
    body = json.dumps({"scores": [{"name": "A", "score": 90, "reason": "r"}]})
    picked = _score(task, body)
    assert [item["theme"] for item in picked] == ["A"]


def test_score_writes_placeholder_when_nothing_is_relevant() -> None:
    """全部题材主营分 >0 才有前 N；全为 0 时写占位记录。

    占位是必需的：否则"确实都不相关"与"这次调用失败返回空"在库里
    都表现为"没有记录"，每次重跑都要重算这些不可能有结果的股票。
    """
    from src.mainline.relevance import MISSING_THEME

    task = ScoreTask(
        code="X", name="X", business="b", industry="i",
        themes=["A", "B"], corr={"A": 0.9, "B": 0.1})
    body = json.dumps({"scores": [{"name": "A", "score": 0, "reason": "无关"},
                                  {"name": "B", "score": 0, "reason": "无关"}]})
    picked = _score(task, body)
    assert len(picked) == 1
    assert picked[0]["theme"] == MISSING_THEME
    assert picked[0]["rank"] == 0
    assert picked[0]["business_score"] == 0.0


def test_empty_response_is_a_failure_not_a_result() -> None:
    """空响应必须当成失败（抛错），而不是"该股无相关题材"。

    实测踩过：LLM 偶发返回空响应（同一 prompt 重试就正常），当时被当成合法
    结果 —— 于是既没落库、也不会重试，那只股票静默丢失（茅台就是这么丢了记录的）。
    """
    from src.mainline.relevance import MISSING_THEME, _score_one_nowait

    task = ScoreTask(code="600519", name="贵州茅台", business="白酒", industry="白酒",
                     themes=["白酒"], corr={"白酒": 0.7})
    with pytest.raises(Exception, match="未返回可解析"):
        asyncio.run(_score_one_nowait(
            _FakeGateway(""), task, tier="reasoning", top=3))
    with pytest.raises(Exception, match="未返回可解析"):
        asyncio.run(_score_one_nowait(
            _FakeGateway("不是 JSON"), task, tier="reasoning", top=3))
    # 有 scores 但全是 0 → 合法结果，写占位
    ok = asyncio.run(_score_one_nowait(
        _FakeGateway(json.dumps({"scores": [{"name": "白酒", "score": 0}]})),
        task, tier="reasoning", top=3))
    assert ok[0]["theme"] == MISSING_THEME


def test_stall_guard_does_not_fire_when_failures_keep_the_heartbeat(
        tmp_path: Path) -> None:
    """卡死守卫**不能**把"连续失败"误判成卡死。

    实测踩过：某次重打分在跑到 ~3900 只时静默卡住（进程还在、数据库一个多小时
    没写入），没有守卫就只能干等。但守卫的判据必须是"**没有任何任务结束**"，
    而不是"没有成功" —— 否则一个持续报错的提供商会让守卫立刻中止整轮，
    把本可以继续推进的其余股票也一起丢掉。

    第一次实现就踩了这个坑（失败路径没有更新心跳 / 没判断"活已派完"），
    被这条测试抓住。
    """
    from src.mainline.relevance import score_stocks

    class _AlwaysFails:
        async def complete(self, *args, **kwargs):
            raise RuntimeError("提供商持续报错")

    store = RelevanceStore(tmp_path / "cache.db")
    store.connect().close()
    tasks = [ScoreTask(code=f"60000{i}", name="X", business="b", industry="i",
                       themes=["A"], corr={"A": 0.5}) for i in range(6)]
    stats = asyncio.run(score_stocks(
        store=store, gateway=_AlwaysFails(), tasks=tasks, tier="reasoning",
        concurrency=3, stall_seconds=2.0, retries=0))
    # 守卫没有中止整轮：6 只全部被计为失败，而不是抛 RelevanceError
    assert stats.stocks_failed == 6
    assert stats.stocks_scored == 0


def test_retry_recovers_a_transient_empty_response(tmp_path: Path) -> None:
    """重试能救回偶发空响应（必须重试，理由见 `score_stocks` 的 docstring）。

    ⚠️ 重试与守卫有交互：退避总时长（`retry_pause` 累加）若超过
    `stall_seconds`，守卫会在重试期间判定卡死。所以生产配置里
    `stall_seconds` 必须显著大于"重试次数 × 退避"，默认 300s vs 3×1s 是安全的。
    """
    from src.mainline.relevance import score_stocks

    class _FlakyThenOk:
        def __init__(self) -> None:
            self.calls: dict[str, int] = {}

        async def complete(self, tier, system, prompt, **kwargs):
            # 前两次返回空，第三次正常 —— 模拟提供商抖动
            key = "x"
            self.calls[key] = self.calls.get(key, 0) + 1
            body = ('{"scores": [{"name": "A", "score": 90, "reason": "r"}]}'
                    if self.calls[key] >= 3 else "")

            class _Resp:
                content = body
                model_used = "fake"

            return _Resp()

    store = RelevanceStore(tmp_path / "cache.db")
    store.connect().close()
    task = ScoreTask(code="600001", name="X", business="b", industry="i",
                     themes=["A"], corr={"A": 0.5})
    stats = asyncio.run(score_stocks(
        store=store, gateway=_FlakyThenOk(), tasks=[task], tier="reasoning",
        concurrency=1, stall_seconds=60, retries=3, retry_pause=0.01))
    assert stats.stocks_failed == 0, "重试后应当成功"
    assert stats.stocks_scored == 1


def test_stall_guard_fires_when_nothing_finishes(tmp_path: Path) -> None:
    """真正卡死（没有任何任务结束）时必须中止，而不是无限等待。"""
    from src.mainline.relevance import RelevanceError, score_stocks

    class _Hangs:
        async def complete(self, *args, **kwargs):
            await asyncio.sleep(3600)      # 永远不会返回

    store = RelevanceStore(tmp_path / "cache.db")
    store.connect().close()
    tasks = [ScoreTask(code="600001", name="X", business="b", industry="i",
                       themes=["A"], corr={"A": 0.5})]
    with pytest.raises(RelevanceError, match="卡死"):
        asyncio.run(score_stocks(
            store=store, gateway=_Hangs(), tasks=tasks, tier="reasoning",
            concurrency=1, stall_seconds=1.0))


# ==================================================================
# 五、运行期过滤（临时库，不碰真实数据）
# ==================================================================


def _make_warehouse(path: Path, rows: list[tuple[str, str, float | None, str]]) -> None:
    """造一个最小行情仓：`(code, trade_date, total_mv, name)`。"""
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE quant_daily_basic"
                 "(code TEXT, trade_date TEXT, total_mv REAL, circ_mv REAL)")
    conn.execute("CREATE TABLE quant_stock_basic"
                 "(code TEXT PRIMARY KEY, name TEXT, industry TEXT)")
    seen: set[str] = set()
    for code, date, mv, name in rows:
        conn.execute("INSERT INTO quant_daily_basic VALUES(?,?,?,?)",
                     (code, date, mv, mv))
        if code not in seen:
            conn.execute("INSERT INTO quant_stock_basic VALUES(?,?,?)",
                         (code, name, ""))
            seen.add(code)
    conn.commit()
    conn.close()


def _make_cache(path: Path, *, boards: list[tuple[str, str, str]],
                members: list[tuple[str, str]],
                rel: list[tuple[str, str, int, str]]) -> RelevanceStore:
    """造最小主线仓：`ml_board` / `ml_member` / `ml_member_clean`。

    `ml_board` / `ml_member` 属于 `datastore` 的建表范围（不是 `relevance` 的），
    所以这里按生产 schema 的最小列自建。
    """
    store = RelevanceStore(path)
    conn = store.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS ml_board("
                 "code TEXT PRIMARY KEY, name TEXT, kind TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS ml_member("
                 "board_code TEXT, code TEXT, name TEXT, in_date TEXT,"
                 " out_date TEXT, source TEXT)")
    conn.executemany("INSERT INTO ml_board(code,name,kind) VALUES(?,?,?)", boards)
    conn.executemany("INSERT INTO ml_member(board_code,code) VALUES(?,?)", members)
    # `ml_theme_board` 的题材名来自**板块名归一化**（与 rebuild_theme_boards 同口径），
    # 不是来自相关性判定 —— 后者对不相关的成员是空主题名，会让多个板块撞到
    # 同一个 (theme, board_code) 主键上。
    conn.executemany(
        "INSERT OR IGNORE INTO ml_theme_board(theme,board_code,board_name,"
        "board_kind,members) VALUES(?,?,?,'concept',0)",
        [(normalize_theme(name), code, name) for code, name, _kind in boards])
    conn.executemany(
        "INSERT INTO ml_member_clean(board_code,code,relevant,rank_in_stock,theme,"
        "refreshed_at) VALUES(?,?,?,?,?,'')",
        [(b, c, 1 if r else 0, r or None, "") for b, c, r, _t in rel])
    conn.commit()
    conn.close()
    return store


BOARDS = [("C1.TI", "人形机器人", "concept"),
          ("C2.TI", "家电零部件", "concept"),
          ("S1.SI", "家用电器", "sw_l1")]
MEMBERS = [("C1.TI", "002050"), ("C1.TI", "600001"),
           ("C2.TI", "002050"), ("C2.TI", "600002"),
           ("S1.SI", "002050"), ("S1.SI", "600003")]
# 002050 在人形机器人里相关（rank 1），在家电零部件里不相关
REL = [("C1.TI", "002050", 1, "人形机器人"),
       ("C1.TI", "600001", 0, ""),
       ("C2.TI", "002050", 0, ""),
       ("C2.TI", "600002", 1, "家电零部件")]


def test_clean_drops_low_relevance_and_keeps_multiple_concepts(
        tmp_path: Path) -> None:
    """一只股票可以留在**多个**它真正相关的概念里。"""
    wh = tmp_path / "wh.db"
    _make_warehouse(wh, [("002050", "20260917", 1.0e11, "三花智控"),
                         ("600001", "20260917", 1.0e11, "大票A"),
                         ("600002", "20260917", 1.0e11, "大票B"),
                         ("600003", "20260917", 1.0e11, "大票C")])
    store = _make_cache(tmp_path / "cache.db", boards=BOARDS, members=MEMBERS,
                        rel=REL)
    out = clean_member_map({"C1.TI": ["002050", "600001"],
                            "C2.TI": ["002050", "600002"],
                            "S1.SI": ["002050", "600003"]},
                           store=store, warehouse_path=wh,
                           trade_date="20260917")
    assert out.members["C1.TI"] == ["002050"]          # 相关 → 留
    assert out.members["C2.TI"] == ["600002"]          # 不相关 → 剔
    # 申万一级不在过滤范围（是行业分类不是题材）→ 原样保留
    assert out.members["S1.SI"] == ["002050", "600003"]
    assert out.dropped["irrelevant"] == 2               # 600001 + 002050@C2


def test_clean_drops_small_caps_but_keeps_large_irrelevant_visible(
        tmp_path: Path) -> None:
    """市值门槛按**总市值**；大市值但低相关的票靠相关性剔（不是靠市值）。"""
    wh = tmp_path / "wh.db"
    _make_warehouse(wh, [
        ("002050", "20260917", 1.0e11, "三花智控"),
        ("600001", "20260917", 1.0e10, "小票"),        # 100 亿 → 留市值
        ("600002", "20260917", 2.0e9, "微票"),         # 20 亿 → 市值剔
    ])
    store = _make_cache(tmp_path / "cache.db", boards=BOARDS, members=MEMBERS,
                        rel=REL)
    out = clean_member_map({"C1.TI": ["002050", "600001"],
                            "C2.TI": ["600002"]},
                           store=store, warehouse_path=wh,
                           trade_date="20260917")
    assert out.members["C1.TI"] == ["002050"]
    assert out.dropped["mv"] == 1


def test_clean_drops_st_members(tmp_path: Path) -> None:
    """ST / 退市股剔除（涨跌幅限制不同，资金动作与板块主线无关）。"""
    wh = tmp_path / "wh.db"
    _make_warehouse(wh, [("002050", "20260917", 1.0e11, "三花智控"),
                         ("600001", "20260917", 1.0e11, "ST诺泰")])
    rel = [("C1.TI", "002050", 1, "人形机器人"),
           ("C1.TI", "600001", 1, "人形机器人")]
    store = _make_cache(tmp_path / "cache.db", boards=BOARDS,
                        members=[("C1.TI", "002050"), ("C1.TI", "600001")],
                        rel=rel)
    out = clean_member_map({"C1.TI": ["002050", "600001"]}, store=store,
                           warehouse_path=wh, trade_date="20260917")
    assert out.members["C1.TI"] == ["002050"]
    assert out.dropped["st"] == 1
    # 关掉开关就保留
    out2 = clean_member_map({"C1.TI": ["002050", "600001"]}, store=store,
                            warehouse_path=wh, trade_date="20260917",
                            exclude_st=False)
    assert out2.members["C1.TI"] == ["002050", "600001"]


def test_clean_is_conservative_on_missing_data(tmp_path: Path) -> None:
    """保守性：市值缺失 / 名称缺失 → 保留（数据缺口 ≠ 不合格）。"""
    wh = tmp_path / "wh.db"
    _make_warehouse(wh, [("002050", "20260917", 1.0e11, "三花智控")])
    rel = [("C1.TI", "002050", 1, "人形机器人"),
           ("C1.TI", "600001", 1, "人形机器人")]   # 600001 无行情数据
    store = _make_cache(tmp_path / "cache.db", boards=BOARDS,
                        members=[("C1.TI", "002050"), ("C1.TI", "600001")],
                        rel=rel)
    out = clean_member_map({"C1.TI": ["002050", "600001"]}, store=store,
                           warehouse_path=wh, trade_date="20260917")
    assert set(out.members["C1.TI"]) == {"002050", "600001"}


def test_clean_keeps_board_without_relevance_data(tmp_path: Path) -> None:
    """某板块完全没有相关性数据 → 原样保留并在 gaps 里如实标注。"""
    wh = tmp_path / "wh.db"
    _make_warehouse(wh, [("600009", "20260917", 1.0e11, "未打分股")])
    store = _make_cache(tmp_path / "cache.db",
                        boards=[("C9.TI", "未打分板块", "concept")],
                        members=[("C9.TI", "600009")], rel=[])
    out = clean_member_map({"C9.TI": ["600009"]}, store=store,
                           warehouse_path=wh, trade_date="20260917")
    assert out.members["C9.TI"] == ["600009"]
    assert any("没有相关性数据" in note for note in out.gaps)


def test_member_fundamentals_returns_none_for_missing_cap(tmp_path: Path) -> None:
    wh = tmp_path / "wh.db"
    _make_warehouse(wh, [("002050", "20260917", 1.0e11, "三花智控"),
                         ("600001", "20260917", None, "无市值股")])
    facts = member_fundamentals(wh, ["002050", "600001", "999999"], "20260917")
    assert facts["002050"] == (1.0e11, "三花智控")
    assert facts["600001"][0] is None
    assert facts["600001"][1] == "无市值股"
    assert facts["999999"] == (None, "")     # 缺失也在返回值里


def test_resolve_basic_date_falls_back_to_latest_available(tmp_path: Path) -> None:
    """请求日没有市值数据时回退到最近交易日，并明确报告"回退了"。"""
    wh = tmp_path / "wh.db"
    _make_warehouse(wh, [("002050", "20260916", 1.0e11, "三花智控"),
                         ("002050", "20260917", 1.0e11, "三花智控")])
    # 命中当天 → 不回退
    assert resolve_basic_date(wh, "20260916") == ("20260916", False)
    # 仓库只到 09-17，请求 09-18（真实场景：quant_daily_basic 比行情主表晚一天）
    assert resolve_basic_date(wh, "20260918") == ("20260917", True)
    # 请求日早于仓库起始 → 无可用数据，原样返回且不算回退
    assert resolve_basic_date(wh, "20200101") == ("20200101", False)


def test_clean_does_not_silently_skip_mv_gate_on_a_date_off_the_warehouse(
        tmp_path: Path) -> None:
    """🩸 回归：请求日超出仓库覆盖时，市值门槛**不能**静默失效。

    修复前 `mv_ok = cap is None or ...` 会把「整批没有市值数据」当成
    「整批都合格」，小票全部漏进股池且不报错。修复后应回退到最近交易日
    的市值口径，并在 `gaps` 里写清回退事实。
    """
    wh = tmp_path / "wh.db"
    _make_warehouse(wh, [("002050", "20260917", 1.0e11, "三花智控"),
                         ("600002", "20260917", 2.0e9, "微票")])
    # 两只票在 C1.TI 里**都判定为相关**，所以唯一能剔掉微票的理由就是市值
    rel = [("C1.TI", "002050", 1, "人形机器人"),
           ("C1.TI", "600002", 1, "人形机器人")]
    store = _make_cache(tmp_path / "cache.db", boards=BOARDS,
                        members=[("C1.TI", "002050"), ("C1.TI", "600002")],
                        rel=rel)
    out = clean_member_map({"C1.TI": ["002050", "600002"]}, store=store,
                           warehouse_path=wh, trade_date="20260918")
    assert out.members["C1.TI"] == ["002050"], "20 亿小票必须被市值门槛剔掉"
    assert out.dropped["mv"] == 1
    assert any("回退到最近交易日 20260917" in note for note in out.gaps)


# ==================================================================
# 六、题材 → 板块映射与落库
# ==================================================================


def test_rebuild_clean_maps_theme_to_all_taxonomies(tmp_path: Path) -> None:
    """一个题材映射回它在**各分类体系**下的全部板块（茅台的白酒三兄弟）。"""
    store = RelevanceStore(tmp_path / "cache.db")
    conn = store.connect()
    conn.execute("CREATE TABLE IF NOT EXISTS ml_board("
                 "code TEXT PRIMARY KEY, name TEXT, kind TEXT)")
    conn.execute("CREATE TABLE IF NOT EXISTS ml_member("
                 "board_code TEXT, code TEXT, name TEXT, in_date TEXT,"
                 " out_date TEXT, source TEXT)")
    conn.executemany("INSERT INTO ml_board(code,name,kind) VALUES(?,?,?)", [
        ("881273.TI", "白酒", "concept"),
        ("884188.TI", "白酒Ⅲ", "concept"),
        ("885525.TI", "白酒概念", "concept"),
        ("883920.TI", "近期解禁", "concept"),      # 假板块
    ])
    conn.executemany("INSERT INTO ml_member(board_code,code) VALUES(?,?)", [
        ("881273.TI", "600519"), ("884188.TI", "600519"),
        ("885525.TI", "600519"), ("883920.TI", "600519"),
    ])
    conn.execute(
        "INSERT INTO ml_stock_theme(code,rank,theme,raw_name,scored_at)"
        " VALUES('600519',1,'白酒','白酒','')")
    conn.commit()
    conn.close()

    stats = rebuild_clean(store=store)
    assert stats.boards_fake_excluded == 1, "近期解禁必须被排除"
    conn = store.connect()
    try:
        kept = {str(r["board_code"]) for r in conn.execute(
            "SELECT board_code FROM ml_member_clean WHERE relevant = 1")}
    finally:
        conn.close()
    # 三个白酒板块全部保留（同一题材名额只占 1 个）
    assert kept == {"881273.TI", "884188.TI", "885525.TI"}


def test_config_defaults_match_module_constants() -> None:
    """配置默认值必须与模块常量一致，否则文档与行为会漂移。"""
    from src.mainline.config import RelevanceConfig

    cfg = RelevanceConfig()
    assert cfg.min_total_mv == MIN_TOTAL_MV == 3.0e9
    assert cfg.corr_weight == CORR_WEIGHT == 0.6
    assert cfg.top_themes == 3
    assert cfg.exclude_st is True
    # `light` 层实测不可用（每个题材都给 85 分、漏答严重），默认必须是云层之一
    assert cfg.llm_tier in ("reasoning", "decision"), (
        "llm_tier 默认值不能是 light —— 本地小模型的打分会失去区分度")
