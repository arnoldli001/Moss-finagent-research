"""分段（多次调用）抽取 + 确定性合并的回归测试（第四轮）。

## 这一轮存在的唯一理由（实测，不是假设）

本地 8B（qwen3:8b-q4_K_M）跑一条真实的 3452 字《碳化硅材料专题会议》：
抽到 591 字的"两头"入参，实体字段**全空**。而 `天岳先进`（688234）与
`第三代半导体` 就在被切掉的中间 ~2500 字里 —— **模型没失败，它没看见**。
于是"找出利好了哪个行业、哪只票"这件事被"两头取文"卡死在 0 个实体。

所以本文件按"一个失败模式 → 一条用例"组织：

    中间段被丢掉        → 只出现在第 2 段的实体必须被抽到（核心回归）
    跨段校验         → 第 3 段才有的词组**不许**当作第 2 段的输出被接受
    段数失控         → 上限生效、且**跳过了哪一段要说得出来**
    某段失败         → 其余段的结论照常保留（不整条丢）
    平票             → `未定`（不发明加权方案）
    成本             → ≤100 字**一次调用都不发**；≤600 字仍是一次调用

⚠️ 假网关是**按段排队**的：第 n 次调用拿第 n 个回复。这样"模型看到的到底是
哪一段"是可断言的（`gateway.prompts`），而"每段用哪段原文校验"才是可验的。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from src.domain.intel import tone_job, tone_store, vocab
from src.domain.intel.tone import (
    EXTRACT_MAX_CHARS,
    MAX_EVENTS,
    MAX_EXTRACT_SEGMENTS,
    MAX_SUMMARY_CHARS,
    MIN_CHARS_FOR_EXTRACTION,
    TONE_BEAR,
    TONE_BULL,
    TONE_NEUTRAL,
    TONE_UNKNOWN,
    ToneResult,
    merge_tone_results,
    segment_text,
)

# ======================================================================
# 受控词表（同 `test_intel_tone.py`：不读真实数据仓，断言与真库解耦）
# ======================================================================

_FAKE_ENTRIES = [
    vocab.VocabEntry("第三代半导体", vocab.KIND_BOARD, board_code="886063.TI"),
    vocab.VocabEntry("存储芯片", vocab.KIND_BOARD, board_code="886042.TI"),
    vocab.VocabEntry("军工", vocab.KIND_BOARD, board_code="885847.TI"),
    vocab.VocabEntry("中芯国际", vocab.KIND_STOCK, code="688981"),
    vocab.VocabEntry("隆基绿能", vocab.KIND_STOCK, code="601012"),
    vocab.VocabEntry("天岳先进", vocab.KIND_STOCK, code="688234"),
]


@pytest.fixture(autouse=True)
def _fake_vocabulary(monkeypatch: pytest.MonkeyPatch) -> None:
    """把实体词表换成受控小表（**不读**数据缸/行情仓）。"""
    monkeypatch.setattr(vocab, "_TABLE", vocab.build_table(_FAKE_ENTRIES))


# ======================================================================
# 造文本：**每句 240 字**，保证一段只装得下 2 句（600 字上限）
# ======================================================================

#: 一句的长度。选 300 是为了让"**一句一段**"成为**算出来的**结果（不是碰巧）：
#: 两句就是 602 字 > 600，装不进同一段，所以每句话自成一段。
#: 同时每句都远超 300 字 —— "两头各 300 字"的旧窗口里连一句完整的话都放不下，
#: 这正是当初实体全空的原因，用例可以拿它当反面对照。
_SENT_CHARS = 300

#: 造句子用的**变化**词块（重复同一个词块会让每个 300 字窗口里都出现同样的字，
#: 那样"中间段独有的实体"就断言不出来了）。
_WORDS = ("半导体", "行业", "景气度", "跟踪", "与", "产能", "释放", "节奏",
          "观察", "以及", "下游", "需求", "变化", "趋势", "判断", "结论")


def _sent(marker: str) -> str:
    """一个带标记的长句子（**以 `。` 收尾**，所以是合法切点）。

    `marker` 在开头，词块在中后段循环补齐，末尾补 `。`。
    词块**循环且变化**，任何 300 字窗口都不会碰巧包含另一个标记。
    """
    body = marker
    i = 0
    while len(body) < _SENT_CHARS:
        body += _WORDS[i % len(_WORDS)]
        i += 1
    out = body[:_SENT_CHARS] + "。"
    assert len(out) == _SENT_CHARS + 1, len(out)
    return out


def _note(*markers: str) -> str:
    """按标记造一条笔记：**每个标记一句话，每句话自成一段**。"""
    return "".join(_sent(m) for m in markers)


#: 核心回归用的四段笔记的标记。实体只在**第 3 段**（= 被两头窗口切掉的那一段）。
_SEG1_PHRASE = "机构上调盈利预测"
_SEG2_ENTITY = "第三代半导体"
_SEG2_STOCK = "天岳先进"
_SEG3_PHRASE = "价格下滑承压"


def _si_note() -> str:
    """四段式碳化硅笔记（**段数正好等于上限 4，不触发裁剪**）：

        第 1 段  结论（偏多词）
        第 2 段  个股（`天岳先进`）
        第 3 段  ★ 实体（`第三代半导体`）—— **中间段**，两头窗口切掉的那一段
        第 4 段  结尾的风险句

    ⚠️ 每句 301 字（两句 602 > `EXTRACT_MAX_CHARS`），所以**一句一段**；
    第 2、3 段在"两头各 300 字"的旧窗口里一句话都放不下 ——
    这正是当初实体字段全空的原因，核心用例可以直接拿它当反面对照。
    """
    return _note(_SEG1_PHRASE, f"{_SEG2_STOCK}主导衬底环节",
                 f"{_SEG2_ENTITY}进入成长期", f"隆基绿能{_SEG3_PHRASE}")


# ======================================================================
# 分段本身（纯函数，先锁住"段数与边界"这个地基）
# ======================================================================

def test_short_text_is_one_segment() -> None:
    """≤600 字 = 一段 = **第三轮的单次调用行为**（这条不许回归）。"""
    plan = segment_text("存储芯片涨价，中芯国际扩产。")
    assert plan.count == 1
    assert plan.single is True
    assert plan.total == 1
    assert plan.skipped == ""


def test_segments_are_bounded_and_contiguous() -> None:
    """★ 每段 ≤600 字，且**首尾相接、不重不漏**（漏一段就是又丢一截原文）。"""
    text = _note(*[f"标记{i}" for i in range(4)])
    plan = segment_text(text)
    assert plan.count == 4, f"4 句 ×301 字应切 4 段，实得 {plan.count}"
    for seg in plan.segments:
        assert 0 < len(seg) <= EXTRACT_MAX_CHARS, f"段超长：{len(seg)}"
    # 段与段真的接得上（拼回去就是原文，一字不差）
    joined = "".join(plan.segments)
    assert joined == text, "切段把原文改了（漏字或多字）"


def test_sentence_boundary_is_respected() -> None:
    """段只在**句末标点之后**断开 —— 半句话会让模型顺手补全它。"""
    plan = segment_text(_note(*[f"标记{i}" for i in range(4)]))
    assert plan.count == 4
    for seg in plan.segments:
        assert seg.endswith("。"), f"段没有停在句末：…{seg[-12:]!r}"


def test_segment_cap_keeps_the_last_segment_and_reports_the_gap() -> None:
    """★★ 段数上限：取**前 N-1 段 + 最后一段**，并把跳过的区间**说出来**。

    为什么必须保留最后一段：研报结构固定为"开头给结论、**结尾给标的**"
    （"综上，推荐 XX，目标价…"）。只取前 N 段等于砍掉这次抽取最想要的标的。

    为什么必须**记下**跳过的那段：静默丢中间一段的表现只是"这段怎么没抽到"，
    与"模型没抽到"在存储里长得一模一样，排障时没有任何线索。
    """
    text = _note(*[f"标记{i}" for i in range(6)])
    plan = segment_text(text, max_segments=3)
    assert plan.total == 6, "total 必须报**不设上限**时的段数"
    assert plan.count == 3, "上限没有生效"
    assert plan.skipped, "跳过了中间段却没有记录"
    assert plan.skipped == "3~5 段 / 903 字", plan.skipped
    # 保留的是第 1、2 段与**第 6 段**（最后一段），不是第 1~3 段
    assert plan.segments[0].startswith("标记0")
    assert plan.segments[1].startswith("标记1")
    assert "标记2" not in plan.segments[1]
    assert plan.segments[-1].startswith("标记5"), "最后一段没有保留"
    assert plan.segments[-1] == segment_text(text).segments[-1]


# ======================================================================
# 假网关（**按段排队**）
# ======================================================================

class _QueuedGateway:
    """假网关：第 n 次调用返回 `replies[n]`；`None` 表示**抛异常**。

    `prompts` 记住每次调用**实际送出去的文本** —— 这是"模型到底看没看见
    中间那段原文"唯一可断言的地方。
    """

    def __init__(self, replies: list[str | None]) -> None:
        self.replies = list(replies)
        self.calls = 0
        self.prompts: list[str] = []
        self.kwargs: list[dict[str, object]] = []

    async def complete(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        self.prompts.append(str(args[2]) if len(args) > 2 else "")
        self.kwargs.append(dict(kwargs))
        idx = self.calls - 1
        reply = self.replies[idx] if idx < len(self.replies) else None
        if reply is None:
            raise RuntimeError("ollama 不可用（本条用例故意让它挂）")
        return type("R", (), {"content": reply})()


def _reply(**kw: object) -> str:
    """一段模型回复（**九个键齐全**，与受约束解码的输出结构一致）。"""
    base: dict[str, object] = {
        "summary": "", "events": [], "tone": TONE_NEUTRAL,
        "phrases": [], "codes": [],
        "bull_industries": [], "bull_stocks": [],
        "bear_industries": [], "bear_stocks": [],
    }
    base.update(kw)
    return json.dumps(base, ensure_ascii=False)


def _fresh_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """把 `tone_store` 换到临时目录（**不碰真实存储**，清掉进程内缓存）。"""
    monkeypatch.setattr(tone_store, "store_path",
                        lambda root=None: tmp_path / "tone_results.jsonl")
    monkeypatch.setattr(tone_store, "_CACHE", {})
    monkeypatch.setattr(tone_store, "_LOADED", True)


def _item(text: str, content_hash: str = "h1") -> dict:
    """一条送抽取的条目（`extract_text` = 清洗后的全文，正是线上那条路）。"""
    return {
        "content_hash": content_hash,
        "title": "【天风电子】碳化硅材料专题会议",
        "extract_text": text,
        "credibility": {"score": 70},
    }


async def _run(gateway: object, text: str, tmp_path: Path,
               content_hash: str = "h1") -> dict:
    return await tone_job.run_once(gateway=gateway,
                                   items=[_item(text, content_hash)],
                                   root=tmp_path)


# ======================================================================
# ★★ 核心回归：只出现在**中间段**的实体现在必须被抽到
# ======================================================================

def test_entity_only_in_middle_segment_is_now_extracted(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ 这条就是本次改动的**存在理由**。

    实测现象：8B 跑《碳化硅材料专题会议》，591 字的"两头"入参让实体字段全空，
    而 `天岳先进` / `第三代半导体` 在被切掉的中间 ~2500 字里。
    分段之后 —— 中间那段**也被送进模型了**（`gateway.prompts` 可断言），
    它给出的实体按**它自己那段原文**校验后进入合并结果。

    ⚠️ 反面对照写在同一用例里：旧的"两头各 300 字"窗口里，
    中间那句 301 字的原话**一个字都放不下** —— 用例本身证明了回归点。
    """
    text = _si_note()
    assert len(segment_text(text).segments) == 4, "用例构造失败：应当 4 段"
    gateway = _QueuedGateway([
        _reply(summary="机构上调盈利预测", tone=TONE_BULL,
               phrases=[_SEG1_PHRASE]),
        _reply(summary="天岳先进主导衬底环节", tone=TONE_BULL,
               phrases=[_SEG2_STOCK], bull_stocks=[_SEG2_STOCK]),
        _reply(summary="第三代半导体成长期", tone=TONE_BULL,
               phrases=[_SEG2_ENTITY], bull_industries=[_SEG2_ENTITY],
               events=[_SEG2_ENTITY]),
        _reply(summary="碳化硅价格下滑", tone=TONE_BEAR,
               phrases=[_SEG3_PHRASE], bear_stocks=["隆基绿能"]),
    ])

    _fresh_store(monkeypatch, tmp_path)
    stats = asyncio.run(_run(gateway, text, tmp_path))

    # ── 四次调用，每次一段，且**中间那段真的被送出过** ──
    assert gateway.calls == 4, f"应当按段调用 4 次，实得 {gateway.calls}"
    assert stats["calls"] == 4
    assert stats["multi_segment"] == 1
    assert stats["segment_capped"] == 0
    joined_prompts = "\n".join(gateway.prompts)
    assert _SEG2_ENTITY in joined_prompts, "中间那段原文从未送给模型"
    assert _SEG2_ENTITY not in gateway.prompts[0], \
        "第 1 段的调用里出现了第 3 段的内容（说明送的还是全文）"
    # 反面对照：旧的"两头各 300 字"窗口里没有它（这正是当初实体全空的原因）
    assert _SEG2_ENTITY not in text[:300] and _SEG2_ENTITY not in text[-300:]

    # ── 合并结果里必须能看到中间段的实体 ──
    hit = tone_store.get("h1", root=tmp_path)
    assert hit is not None
    bull_names = [s["name"] for s in hit["bullish"]["stocks"]]
    assert _SEG2_STOCK in bull_names, f"中间段前后的个股丢了：{bull_names}"
    assert _SEG2_ENTITY in hit["bullish"]["industries"], hit["bullish"]
    assert {"name": _SEG2_ENTITY, "code": "886063.TI"} in \
        hit["bullish"]["boards"], "板块代码没有从词表取"
    # 代码只从词表来（模型一个代码都没给）
    codes = {s["name"]: s["code"] for s in hit["bullish"]["stocks"]}
    assert codes[_SEG2_STOCK] == "688234", codes
    # 其余段照常合并（别为了修中间把两头弄丢）
    assert hit["bearish"]["industries"] == [], hit["bearish"]
    assert [s["name"] for s in hit["bearish"]["stocks"]] == ["隆基绿能"]
    assert _SEG1_PHRASE in hit["phrases"], hit["phrases"]
    assert hit["tone"] == TONE_BULL, "三段偏多一段偏空 → 多数取胜"


# ======================================================================
# ★★ 逐段校验：第 3 段才有的词组不许"通过"第 2 段
# ======================================================================

def test_phrase_from_another_segment_is_rejected(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ 校验必须跑在**模型真正看过的那段原文**上。

    构造：`价格下滑承压` **只出现在第 3 段**，而模型在第 1、2 段的回复里
    都声称自己看到了它（模拟"模型跨段串味"）。

    正确行为：第 1、2 段各自的 `validate_extraction` 拿**自己那段**跑子串校验
    → 两处都拦掉 → 合并结果里没有它。

    ⚠️ 若把校验挪到合并之后（拿整篇原文校验合并结果），它就会通过 ——
    那等于接受一句**模型不可能读到**的引文，而界面上它显示为"原文逐字可核对"。
    本项目的全部意义就是让用户能拿它去原文里核对；放宽这一条 = 取消这条纪律。

    ⚠️ 第 3 段故意不给回复（假网关抛异常）：它的内容**不会**进入合并结果，
    所以 `价格下滑承压` 不可能从第 3 段自己"合法地"进来 ——
    这条断言才真的在验"跨段拦得住"，而不是"碰巧第 3 段也拦了"。
    """
    text = _si_note()
    assert _SEG3_PHRASE in text, "用例构造失败：这个词组应当在最后一段"
    segs = segment_text(text).segments
    assert _SEG3_PHRASE not in segs[0] and _SEG3_PHRASE not in segs[1]
    assert _SEG3_PHRASE in segs[-1]

    gateway = _QueuedGateway([
        _reply(tone=TONE_BULL, phrases=[_SEG3_PHRASE]),      # 越界的词组
        _reply(tone=TONE_BULL, phrases=[_SEG3_PHRASE]),      # 同上
        None, None,                                          # 其余段：模型挂了
    ])
    _fresh_store(monkeypatch, tmp_path)
    asyncio.run(_run(gateway, text, tmp_path))
    hit = tone_store.get("h1", root=tmp_path)
    assert hit is not None
    assert _SEG3_PHRASE not in hit["phrases"], \
        f"最后一段才有的词组被当成了前两段的输出：{hit['phrases']}"


# ======================================================================
# 合并规则（逐条锁住，全部可复算）
# ======================================================================

def test_tone_tie_yields_unknown() -> None:
    """★ 平票 → `未定`。**不发明加权方案**（段长/位置都是编出来的先验）。"""
    merged = merge_tone_results([
        ToneResult(tone=TONE_BULL, confidence=0.6),
        ToneResult(tone=TONE_BEAR, confidence=0.6),
    ])
    assert merged.tone == TONE_UNKNOWN
    assert merged.has_tone is False
    assert "票数相等" in merged.explain, merged.explain
    # 未定时不留依据（否则界面会出现"未定 + 一串像证据的词组"）
    assert merged.phrases == []


def test_tone_majority_wins() -> None:
    """多数取胜；`未定` 段**不参与计票**（它是"没判出"，不是"判了未定"）。"""
    merged = merge_tone_results([
        ToneResult(tone=TONE_BULL),
        ToneResult(tone=TONE_BULL),
        ToneResult(tone=TONE_UNKNOWN),
    ])
    assert merged.tone == TONE_BULL
    assert "多数段一致" in merged.explain


def test_confidence_takes_the_minimum() -> None:
    """置信度取**最小值**（保守）：`None` 是"不给数值"，不是 0，不参与。"""
    merged = merge_tone_results([
        ToneResult(tone=TONE_BULL, confidence=0.8),
        ToneResult(tone=TONE_BULL, confidence=0.5),
        ToneResult(tone=TONE_BULL, confidence=None),
        ToneResult(tone=TONE_BULL, confidence=0.9),
    ])
    assert merged.confidence == 0.5
    # 全都没有数值 → 不给数值（不是 0.0）
    assert merge_tone_results([ToneResult(tone=TONE_BULL)]).confidence is None


def test_summary_joins_then_falls_back_to_the_lede() -> None:
    """★ 摘要：`；` 连接；**超过上限只留第 1 段**（导语，必然读得通）。

    绝不无界拼接：摘要的全部价值就是"短到能一眼扫完"，
    四段拼起来就是一段 160 字的新正文，而界面上的位置没变。
    """
    merged = merge_tone_results([
        ToneResult(tone=TONE_BULL, summary="存储芯片涨价"),
        ToneResult(tone=TONE_BULL, summary="军工订单下调"),
    ])
    assert merged.summary == "存储芯片涨价；军工订单下调"
    assert len(merged.summary) <= MAX_SUMMARY_CHARS

    # 连接后**超过上限** → 只留第 1 段（导语），绝不截出一句拼接的假摘要
    merged2 = merge_tone_results([
        ToneResult(tone=TONE_BULL,
                   summary="存储芯片景气上行带动设备与材料环节订单饱满并持续提价"),
        ToneResult(tone=TONE_BULL, summary="军工订单下调导致相关标的存在较大压力"),
    ])
    assert merged2.summary == \
        "存储芯片景气上行带动设备与材料环节订单饱满并持续提价", \
        "超长时应当只留第 1 段（而不是两段拼起来截前半句）"
    assert "；" not in merged2.summary
    assert len(merged2.summary) <= MAX_SUMMARY_CHARS


def test_events_and_phrases_are_unioned_with_caps() -> None:
    """并集：逐字去重、首现顺序、**截到既有上限**（长度不能由段数决定）。"""
    merged = merge_tone_results([
        ToneResult(tone=TONE_BULL, events=["A事件", "B事件"],
                   phrases=["甲词组", "乙词组"]),
        ToneResult(tone=TONE_BULL, events=["B事件", "C事件"],
                   phrases=["乙词组", "丙词组"]),
    ])
    assert merged.events == ["A事件", "B事件", "C事件"]
    assert merged.phrases == ["甲词组", "乙词组", "丙词组"]
    # 上限：每段都给满 8 条时，合并后仍是 8 条（不能变成 16 条）
    many = [ToneResult(tone=TONE_BULL, events=[f"事件{i}" for i in range(8)])]
    many.append(ToneResult(tone=TONE_BULL,
                           events=[f"事件{i}" for i in range(8, 16)]))
    assert len(merge_tone_results(many).events) == MAX_EVENTS


def test_sides_are_merged_by_name_and_code() -> None:
    """行业/个股按 `(名字, 代码)` 去重，`count` **重算**（不累加）。"""
    merged = merge_tone_results([
        ToneResult(tone=TONE_BULL, bullish={
            "industries": ["存储芯片"], "count": 2,
            "stocks": [{"name": "中芯国际", "code": "688981", "count": 3}]}),
        ToneResult(tone=TONE_BULL, bullish={
            "industries": ["存储芯片", "军工"], "count": 3,
            # 同一只票出现两次（两段都抽到了）→ 去重成一条
            "stocks": [{"name": "中芯国际", "code": "688981", "count": 3},
                       {"name": "天岳先进", "code": "688234", "count": 1}]}),
    ])
    assert merged.bullish["industries"] == ["存储芯片", "军工"]
    names = [s["name"] for s in merged.bullish["stocks"]]
    assert names == ["中芯国际", "天岳先进"]
    assert merged.bullish["count"] == 4, "count 必须重算，不能把重复的票数两遍"
    assert merged.bullish["boards"] == [
        {"name": "存储芯片", "code": "886042.TI"},
        {"name": "军工", "code": "885847.TI"}]


def test_merge_of_empty_parts_is_conservative() -> None:
    """全部段都失败 → 保守的空结果（**不抛异常**，由调用方走规则层）。"""
    merged = merge_tone_results([], segments=2, calls=2, skipped="")
    assert merged.tone == TONE_UNKNOWN
    assert merged.summary == "" and merged.events == []
    assert merged.segments == 2 and merged.calls == 2


# ======================================================================
# 编排层：失败隔离 / 上限落库 / 成本
# ======================================================================

def test_one_failed_segment_keeps_the_others(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 中间那段挂了，**其余段的结论照常保留**。

    "模型挂了"是必然会发生的事（Ollama 被关机、被别的任务占满）。
    正确行为是"那一段降级"，而不是"整条丢"——
    按段隔离之后，"整条退回词表"这种大范围降级就不再发生了。
    """
    text = _si_note()
    assert len(segment_text(text).segments) == 4, "用例构造失败：应当 4 段"
    gateway = _QueuedGateway([
        _reply(summary="机构上调盈利预测", tone=TONE_BULL,
               phrases=[_SEG1_PHRASE]),
        None,                                                      # ★ 第 2 段失败
        _reply(summary="第三代半导体成长期", tone=TONE_BULL,
               phrases=[_SEG2_ENTITY], bull_industries=[_SEG2_ENTITY]),
        _reply(summary="碳化硅价格下滑", tone=TONE_BEAR,
               bear_stocks=["隆基绿能"]),
    ])
    _fresh_store(monkeypatch, tmp_path)
    stats = asyncio.run(_run(gateway, text, tmp_path))
    assert stats["written"] == 1, "一段失败不该让整条不落库"
    # 四次调用都发起了（失败的那次也算调用 —— 它确实占了模型时间）
    assert gateway.calls == 4
    hit = tone_store.get("h1", root=tmp_path)
    assert hit is not None
    # 成功段的实体都在（**包括失败段之后**的那两段）
    assert hit["bullish"]["industries"] == [_SEG2_ENTITY], hit["bullish"]
    # 依据词组：成功段给的逐字词组都在（失败段那条只留规则层命中词）
    assert _SEG1_PHRASE in hit["phrases"], hit["phrases"]
    assert _SEG2_ENTITY in hit["phrases"], hit["phrases"]
    assert [s["name"] for s in hit["bearish"]["stocks"]] == ["隆基绿能"]
    assert hit["source"] == "rules+llm"
    # 三段偏多一段偏空 → 多数取胜（失败那段不参与计票）
    assert hit["tone"] == TONE_BULL
    # 摘要按"连接后超上限只留首段"收敛（失败的那段没给摘要）
    assert hit["summary"], "成功段的摘要必须留下"


def test_all_segments_failing_falls_back_to_rules_without_raising(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 每一段都挂 → 整条退回 `rule_tone`，**不抛异常**、旧字段照常落库。"""
    text = _si_note()
    gateway = _QueuedGateway([None] * 4)
    _fresh_store(monkeypatch, tmp_path)
    stats = asyncio.run(_run(gateway, text, tmp_path))
    assert gateway.calls == 4, "每一段都必须试过（失败也要如实计数）"
    assert stats["written"] == 1
    hit = tone_store.get("h1", root=tmp_path)
    assert hit is not None
    assert hit["source"] == "rules", "全挂时必须退回纯规则层"
    assert hit["tone"], "旧字段必须照常落库"
    # 没抽到就是空值，**不许编**
    assert hit["events"] == []
    assert hit["bullish"]["stocks"] == []


def test_segment_cap_is_respected_and_the_gap_is_persisted(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ 段数上限生效、**跳过的区间落库**、最后一段被保留。

    为什么落库而不是只写日志：用户看到"这条只抽到开头和结尾"时，
    唯一的线索就是这个字段 —— 而日志是 2 小时一批的滚动文本，
    这条数据要能随时按 `content_hash` 查回来。
    """
    text = _note(*[f"标记{i}" for i in range(4)])       # 4 句 → 4 段
    plan = segment_text(text, max_segments=4)           # **显式给上限**（不依赖常量）
    assert plan.count == 4 and plan.skipped == ""
    gateway = _QueuedGateway([_reply(tone=TONE_BULL)] * 4)
    _fresh_store(monkeypatch, tmp_path)
    # 走真实编排：上限用默认值（= `MAX_EXTRACT_SEGMENTS`），文本必须**不超过**它
    assert len(segment_text(text).segments) == 4
    stats = asyncio.run(_run(gateway, text, tmp_path))
    assert gateway.calls == 4, f"调用次数必须被上限钉住，实得 {gateway.calls}"
    assert stats["calls"] == 4
    hit = tone_store.get("h1", root=tmp_path)
    assert hit is not None
    assert hit["segments"] == 4
    assert hit["skipped_segments"] == "", "没超上限时不该报缺口"
    # 最后一段（标记3）确实被送进过模型 —— 结论与标的都在结尾
    assert "标记3" in gateway.prompts[-1]


def test_capped_note_reports_the_gap_in_the_stored_row(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ 真的超上限时：**调用次数被钉住、缺口落库、最后一段保留**。

    为什么落库而不是只写日志：用户看到"这条只抽到开头和结尾"时，
    唯一的线索就是这个字段 —— 而日志是 2 小时一批的滚动文本，
    这条数据要能随时按 `content_hash` 查回来。
    """
    cap = MAX_EXTRACT_SEGMENTS
    total = cap + 1                                     # 一定超上限，且只多一段
    text = _note(*[f"标记{i}" for i in range(total)])
    assert segment_text(text).total == total
    assert segment_text(text).count == cap
    gateway = _QueuedGateway([_reply(tone=TONE_BULL)] * cap)
    _fresh_store(monkeypatch, tmp_path)
    stats = asyncio.run(_run(gateway, text, tmp_path))
    assert gateway.calls == cap, f"调用次数必须被上限钉住，实得 {gateway.calls}"
    assert stats["calls"] == cap
    assert stats["segment_capped"] == 1, "跳过了段却没有计数"
    hit = tone_store.get("h1", root=tmp_path)
    assert hit is not None
    assert hit["segments"] == cap
    # 被跳过的正是**最后一段之前**的那一段：段号 cap ~ total-1
    assert hit["skipped_segments"] == \
        f"{cap}~{total - 1} 段 / {_SENT_CHARS + 1} 字", hit["skipped_segments"]
    assert "未处理" in hit["explain"], "缺口没有写进面向用户的 explain"
    # 前 cap-1 段 + **最后一段**（结论与标的都在结尾，不能砍）
    assert "标记0" in gateway.prompts[0]
    assert f"标记{cap - 2}" in gateway.prompts[cap - 2]
    assert f"标记{total - 1}" in gateway.prompts[-1], "最后一段没有保留"


def test_short_text_makes_zero_calls(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ ≤100 字：**一次调用都不发**（用户口径），但仍然落库。

    跳过模型**不等于**跳过这条内容 —— "没有数据"是本项目明令禁止的静默消失。
    """
    text = "存储芯片涨价，中芯国际扩产。"
    assert len(text) <= MIN_CHARS_FOR_EXTRACTION
    gateway = _QueuedGateway([_reply(tone=TONE_BULL)])
    _fresh_store(monkeypatch, tmp_path)
    stats = asyncio.run(_run(gateway, text, tmp_path))
    assert gateway.calls == 0, "短文本调了模型（会白花算力，还可能改写原文）"
    assert stats["calls"] == 0
    assert stats["skipped_short_text"] == 1
    hit = tone_store.get("h1", root=tmp_path)
    assert hit is not None, "短文本也必须落库"
    assert hit["calls"] == 0 and hit["segments"] == 1
    assert hit["summary"] == text, "短文本的摘要就是**原文本身**"
    assert hit["source"] == "rules"


def test_single_segment_text_still_makes_one_call(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """≤600 字但 >100 字：**仍然只调一次**（第三轮的行为，不回归）。"""
    text = _note(_SEG1_PHRASE)            # 1 句 = 1 段
    assert len(text) > MIN_CHARS_FOR_EXTRACTION
    assert len(text) <= EXTRACT_MAX_CHARS
    gateway = _QueuedGateway([
        _reply(summary="机构上调盈利预测", tone=TONE_BULL,
               phrases=[_SEG1_PHRASE], codes=[],
               bull_industries=[], bull_stocks=[],
               events=[_SEG1_PHRASE]),
    ])
    _fresh_store(monkeypatch, tmp_path)
    asyncio.run(_run(gateway, text, tmp_path))
    assert gateway.calls == 1, f"单段文本应当只调一次，实得 {gateway.calls}"
    hit = tone_store.get("h1", root=tmp_path)
    assert hit is not None
    assert hit["segments"] == 1 and hit["calls"] == 1
    assert hit["skipped_segments"] == ""
    assert hit["tone"] == TONE_BULL
    assert hit["phrases"] == [_SEG1_PHRASE]


def test_local_only_on_every_segment(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ **每一段**都必须是 `local_only=True`。

    这条路径必须**结构上不可能**花云端 token：`medium` 层的 fallback 配的是
    `deepseek-flash`。分段把调用次数从 1 变成最多 4 ——
    漏掉任何一段的 `local_only` 就是多倍的云端账单，
    而且只在"本地模型刚好挂了"的那一段才暴露。
    """
    gateway = _QueuedGateway([_reply(tone=TONE_BULL)] * 4)
    _fresh_store(monkeypatch, tmp_path)
    asyncio.run(_run(gateway, _si_note(), tmp_path))
    assert gateway.calls == 4
    for i, kwargs in enumerate(gateway.kwargs):
        assert kwargs.get("local_only") is True, f"第 {i + 1} 段没有裁掉云端模型"
        assert kwargs.get("json_schema"), f"第 {i + 1} 段没有下发布局约束"
    # 层级仍然是 medium（= 8B 本地模型，见 `EXTRACT_TIER` 的说明）
    assert tone_job.EXTRACT_TIER == "medium"


def test_provenance_survives_the_store_round_trip(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ 分段来源说明必须**读得回来**（白名单漏一个键 = 落库了但永远看不到）。

    `tone_store` 的写入（`build_row`）与读取（`view`/`_side_view`）都是白名单/投影，
    漏一个键的表现是"算了、写了、但接口永远看不到" —— **不会有任何报错**。
    """
    _fresh_store(monkeypatch, tmp_path)
    row = tone_store.build_row({
        "tone": TONE_BULL, "segments": 3, "calls": 3,
        "skipped_segments": "4~5 段 / 440 字",
    })
    assert row["segments"] == 3 and row["calls"] == 3
    assert row["skipped_segments"] == "4~5 段 / 440 字"
    view = tone_store.view({
        "tone": TONE_BULL, "has_tone": True, "segments": 3, "calls": 3,
        "skipped_segments": "4~5 段 / 440 字",
    })
    assert view["segments"] == 3 and view["calls"] == 3
    assert view["skipped_segments"] == "4~5 段 / 440 字"


def test_legacy_row_provenance_defaults_are_not_misleading() -> None:
    """★ 老行（第一~三轮）没有这三个键 → 给**不误导人**的默认值。

    `segments=0` 会被读成"这条根本没抽过"，而事实是"老格式是单次调用"。
    默认值必须选那个不会被误读的：至少一段、0 次调用（老行确实没记调用数）、
    没跳过任何段。
    """
    view = tone_store.view({"tone": TONE_BULL, "has_tone": True})
    assert view["segments"] == 1, "老行的段数默认值会把'抽过了'读成'没抽过'"
    assert view["calls"] == 0
    assert view["skipped_segments"] == ""


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
