"""实体词表（`vocab`）+ 短文本**不走模型** + 命中词高亮（第三轮）。

## 这个文件为什么存在

第二轮的做法是"让模型抽实体 + 逐字校验"。实测下来逐字校验**不够**：

  · 一条**连数字都没有**的笔记，模型输出了 `"codes":["603919"]`。
    逐字校验能挡住那个代码，但挡不住"名字逐字在原文里、代码是它配上去的"
    那一种 —— 而那一种更危险：界面上看起来完全合理，用户核不出来。
  · 行业名会飘 —— "新能源车""光模块龙头"这种**听起来就该在这段里**的词，
    在原文里确实逐字存在，但它不是本项目跟踪的板块。

第三轮把实体来源换成**项目自己的目录**（`vocab` 一张表，四个来源），
并且这张表同时服务三件事：

    抽取   行业/个股只从表里出，代码只从表里取
    折叠   "涉不涉及个股/板块/外部标的/AI"（不涉及才可以折叠）
    高亮   把命中的词交给前端标绿（**判定只有一份**，前端不重新匹配）

## 测试怎么做到不碰真实数据库

词表是进程内缓存，这里直接换成受控小表（`_fake_table`）——
断言与 `data/mainline_cache.db` / `data/quant/warehouse.db` 的内容**解耦**：
真库明天多一个板块，这些用例的期望值不会跟着变。真表另有两条 `test_real_*`。
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.domain.intel import service as S
from src.domain.intel import tone_job, tone_store, vocab
from src.domain.intel.tone import (
    MIN_CHARS_FOR_EXTRACTION,
    TONE_UNKNOWN,
    extract_tone,
    validate_entities,
)

#: 受控词表：名字/代码都取自真实来源（`ml_board` / 行情仓 / 用户清单）。
_FAKE_ENTRIES = [
    vocab.VocabEntry("存储芯片", vocab.KIND_BOARD, board_code="886042.TI"),
    vocab.VocabEntry("第三代半导体", vocab.KIND_BOARD, board_code="885908.TI"),
    vocab.VocabEntry("军工", vocab.KIND_BOARD, board_code="885847.TI"),
    vocab.VocabEntry("白酒概念", vocab.KIND_BOARD, board_code="885525.TI"),
    vocab.VocabEntry("中芯国际", vocab.KIND_STOCK, code="688981"),
    vocab.VocabEntry("隆基绿能", vocab.KIND_STOCK, code="601012"),
    vocab.VocabEntry("金徽酒", vocab.KIND_STOCK, code="603919"),
    vocab.VocabEntry("英伟达", vocab.KIND_OVERSEAS, note="用户手工清单"),
    vocab.VocabEntry("NVIDIA", vocab.KIND_OVERSEAS, note="用户手工清单"),
    vocab.VocabEntry("加息", vocab.KIND_OVERSEAS, note="用户手工清单（宏观词）"),
    vocab.VocabEntry("台积电", vocab.KIND_OVERSEAS, note="用户手工清单"),
    vocab.VocabEntry("AI", vocab.KIND_AI, note="用户手工清单"),
    vocab.VocabEntry("大模型", vocab.KIND_AI, note="用户手工清单"),
    vocab.VocabEntry("算力", vocab.KIND_AI, note="用户手工清单"),
]


@pytest.fixture(autouse=True)
def _fake_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """把词表换成受控小表（**不读**数据仓/行情仓）。"""
    monkeypatch.setattr(vocab, "_TABLE", vocab.build_table(_FAKE_ENTRIES))


# ======================================================================
# 表本身：最长优先 / 大小写 / 位置
# ======================================================================

def test_scan_reports_positions() -> None:
    """每个命中都带**位置**（高亮要用它），位置能对上原文切片。"""
    text = "中芯国际扩产"
    hits = vocab.scan(text)
    assert [h.term for h in hits] == ["中芯国际"]
    hit = hits[0]
    assert text[hit.start:hit.end] == "中芯国际"
    assert (hit.kind, hit.code) == (vocab.KIND_STOCK, "688981")


def test_longer_term_wins_at_the_same_offset(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """同一位置**最长优先**：`存储芯片` 不该被更短的 `芯片` 在**同一起点**抢走。

    ⚠️ 不同起点的重叠命中**都要报**（父需求明确：overlapping matches at
    different positions are fine）—— 所以 `芯片` 会在偏移 2 处再命中一次。
    高亮侧不受影响：前端按长度降序替换，`存储芯片` 先被吃掉，
    里面的 `芯片` 就不会被单独标出来。
    """
    monkeypatch.setattr(vocab, "_TABLE", vocab.build_table([
        vocab.VocabEntry("存储芯片", vocab.KIND_BOARD, board_code="886042.TI"),
        vocab.VocabEntry("芯片", vocab.KIND_BOARD, board_code="000000.TI"),
    ]))
    got = vocab.scan("存储芯片涨价")
    at_zero = [h.term for h in got if h.start == 0]
    assert at_zero == ["存储芯片"], f"同一起点出了短词：{at_zero}"
    assert {h.term for h in got} == {"存储芯片", "芯片"}


def test_overlapping_hits_at_different_offsets_are_both_reported(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """不同起点的重叠命中**都要报**（只禁"同起点的短子串"）。"""
    monkeypatch.setattr(vocab, "_TABLE", vocab.build_table([
        vocab.VocabEntry("存储芯片", vocab.KIND_BOARD, board_code="886042.TI"),
        vocab.VocabEntry("芯片概念", vocab.KIND_BOARD, board_code="000002.TI"),
    ]))
    got = vocab.scan("存储芯片概念股")
    assert [h.term for h in got] == ["存储芯片", "芯片概念"], [h.term for h in got]


def test_latin_terms_are_case_insensitive_but_keep_original_spelling() -> None:
    """英文名大小写混写都要命中，且**标出来的词就是原文那一段**。

    查表键做 ASCII 小写（否则 `nvidia` 认不出来）；返回的 `term` 是**原文切片**
    —— 高亮必须逐字能在界面上找到，不能把读者看到的 `nvidia` 换成表里的
    `NVIDIA`（那样前端在原文里根本找不到这个词）。规范拼写在 `entry.term` 里。
    """
    hits = vocab.scan("nvidia 与 NVIDIA 都在讲")
    assert [h.term for h in hits] == ["nvidia", "NVIDIA"]
    assert {h.entry.term for h in hits} == {"NVIDIA"}
    for hit in hits:
        assert "nvidia 与 NVIDIA".find(hit.term) >= 0


def test_one_char_terms_are_not_in_the_table() -> None:
    """单字词不进表（在任何中文文本里都能撞上，没有核对价值）。"""
    table = vocab.build_table([vocab.VocabEntry("中", vocab.KIND_STOCK)])
    assert table.entries == () and table.max_len == 0


def test_hits_are_deduped_with_counts() -> None:
    """同一词出现多次要计数（`count` 供界面/抽取使用）。"""
    got = vocab.stocks_in_text("中芯国际扩产，中芯国际获上调")
    assert got == [{"name": "中芯国际", "code": "688981", "count": 2}], got


# ======================================================================
# 个股代码：只能来自词表 / 原文配对
# ======================================================================

def test_model_invented_code_cannot_reach_output() -> None:
    """★ 实测缺陷：原文**一个数字都没有**，模型照样给了 6 位代码。

    这是第二轮留下的洞 —— 逐字校验只在"代码本身"上拦，而模型更常见的
    形态是"名字逐字在原文里 + 一个它自己配的代码"。现在的判据是
    **代码只从词表/原文配对来**，模型给的那个直接不看。

    用 `603919`（金徽酒）当例子是因为它正是实测里被模型编出来的那个号。
    """
    text = "某白酒公司三季报业绩承压，渠道库存高企"      # 全文没有 6 位数字
    _, bear, _ = validate_entities(
        {"bear_stocks": [{"name": "金徽酒", "code": "603919"}],
         "bear_industries": ["白酒概念"]}, text)
    # 名字不在原文里 → 整条丢（不是"留个名字"）
    assert bear["stocks"] == [], "原文里没有的名字必须丢"
    blob = json.dumps(bear, ensure_ascii=False)
    assert "603919" not in blob, "模型编的代码上了结果"


def test_name_in_text_gets_code_from_vocabulary() -> None:
    """★ 名字逐字在原文里 → 代码**从词表取**（不是从模型取）。"""
    bull, _, _ = validate_entities({"bull_stocks": ["中芯国际"]},
                                  "中芯国际扩产，订单饱满")
    assert bull["stocks"] == [{"name": "中芯国际", "code": "688981", "count": 1}], \
        bull["stocks"]


def test_vocabulary_code_wins_over_model_code() -> None:
    """模型给的代码与词表冲突时，**词表赢**，且模型那个值进 rejected 供排障。"""
    bull, _, rejected = validate_entities(
        {"bull_stocks": [{"name": "中芯国际", "code": "600519"}]},
        "中芯国际（688981）受益于扩产")
    assert bull["stocks"][0]["code"] == "688981"
    assert "600519" not in json.dumps(bull, ensure_ascii=False)
    assert rejected["bull_stocks_model_code"] == ["中芯国际:600519"]


def test_name_not_in_vocabulary_keeps_name_without_code() -> None:
    """名字逐字在原文里、但词表里没有 → **留名字、留空代码**（不编代码）。"""
    bull, _, _ = validate_entities({"bull_stocks": ["某未上市标的"]},
                                  "某未上市标的签约")
    assert bull["stocks"] == [{"name": "某未上市标的", "code": "", "count": 1}]


def test_bare_code_from_text_resolves_name_from_vocabulary() -> None:
    """只给了代码（没给名字）：代码必须在**原文里真出现**，名字从词表反查。"""
    bull, _, _ = validate_entities({"bull_stocks": ["688981"]}, "688981 扩产公告")
    assert bull["stocks"] == [{"name": "中芯国际", "code": "688981", "count": 1}]
    _, _, rejected = validate_entities({"bull_stocks": ["603919"]}, "扩产公告")
    assert rejected["bull_stocks"] == ["603919:代码不在原文(603919)"]


# ======================================================================
# 概念板块：只认主线挖掘跟踪的那份目录
# ======================================================================

def test_industry_must_be_a_tracked_concept_board() -> None:
    """★ 行业闸门：不是主线挖掘跟踪的板块 → 丢，哪怕它逐字在原文里。"""
    text = "半导体设备景气上行，新能源车销量下滑，存储芯片涨价"
    bull, bear, rejected = validate_entities(
        {"bull_industries": ["半导体设备", "存储芯片"],
         "bear_industries": ["新能源车"]}, text)
    assert bull["industries"] == ["存储芯片"], bull["industries"]
    assert "存储芯片" in text, "被留下的行业必须逐字在原文里"
    assert bull["boards"] == [{"name": "存储芯片", "code": "886042.TI"}]
    assert "半导体设备" in rejected["bull_industries"], "未跟踪的板块被放行了"
    assert "新能源车" in rejected["bear_industries"]


def test_industry_must_also_be_literal_in_text() -> None:
    """两条判据缺一不可：是跟踪的板块 **且** 逐字在原文里。"""
    bull, _, rejected = validate_entities(
        {"bull_industries": ["存储芯片"]}, "半导体设备景气上行")
    assert bull["industries"] == []
    assert rejected["bull_industries"] == ["存储芯片"]


def test_empty_board_vocabulary_falls_back_to_literal_rule(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 板块词表读不到时**退回旧的逐字判据**，而不是把行业整块清空。

    读不到就"一律丢弃"的表现是：界面上行业**全部消失**，而且没有任何
    报错线索 —— 那正是本项目最忌讳的静默功能消失
    （与 `zsxq_incremental.fresh_floor` 那次事故同类）。
    """
    monkeypatch.setattr(
        vocab, "_TABLE",
        vocab.build_table([], gaps=("concept_board 词表不可用（测试）",)))
    assert not vocab.boards_available()
    bull, _, _ = validate_entities({"bull_industries": ["半导体设备"]},
                                  "半导体设备景气上行")
    assert bull["industries"] == ["半导体设备"], "词表为空时行业被静默清空了"
    assert bull["boards"] == [{"name": "半导体设备", "code": ""}]


def test_missing_table_reports_a_gap_instead_of_silence(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 数据源读不到时：手工清单照旧可用，**缺口要报出来**。

    不报缺口的后果是"整页不再有高亮、所有条目都变成可折叠"，而没有任何
    线索 —— 那是本项目最忌讳的静默失效（同 `zsxq_incremental.fresh_floor`）。
    """
    def _boom(_gaps: list[str]) -> list[vocab.VocabEntry]:
        raise RuntimeError("模拟数据仓损坏")

    monkeypatch.setattr(vocab, "_load_board_entries", _boom)
    monkeypatch.setattr(vocab, "_load_stock_entries", _boom)
    entries, gaps = vocab._load_entries()
    kinds = {e.kind for e in entries}
    assert kinds == {vocab.KIND_OVERSEAS, vocab.KIND_AI}, kinds
    assert len(gaps) == 2, gaps
    # 手工清单照旧能命中
    table = vocab.build_table(entries, tuple(gaps))
    assert "AI" in {e.term for e in table.by_kind[vocab.KIND_AI]}
    assert table.gaps


# ======================================================================
# 短文本：不调模型，但照样落库
# ======================================================================

class _CountingGateway:
    """假网关：`reply=None` 时抛异常，同时**数调用次数**并记 tier 与 kwargs。"""

    def __init__(self, reply: str | None = None) -> None:
        self.reply = reply
        self.calls = 0
        self.tier = ""
        self.kwargs: dict[str, object] = {}

    async def complete(self, *args: object, **kwargs: object) -> object:
        self.calls += 1
        self.tier = str(args[0]) if args else ""
        self.kwargs = kwargs
        if self.reply is None:
            raise RuntimeError("ollama 不可用")
        return type("R", (), {"content": self.reply})()


def _fresh_store(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(tone_store, "store_path",
                        lambda root=None: tmp_path / "tone_results.jsonl")
    monkeypatch.setattr(tone_store, "_CACHE", {})
    monkeypatch.setattr(tone_store, "_LOADED", True)


def _item(content_hash: str, text: str, title: str = "") -> dict:
    return {
        "content_hash": content_hash,
        "title": title,
        "summary": text,
        "credibility": {"score": 70},
    }


#: 短文本（≤ 阈值）：一条真会出现的快讯形态
_SHORT = "中芯国际扩产，存储芯片订单饱满"
#: 长文本（> 阈值，必须走模型）。刻意写成研报那种"结论在后半段"的形状
_LONG = (
    "半导体设备板块景气上行，存储芯片涨价，中芯国际受益于扩产，"
    "机构上调盈利预测；光伏组件价格下滑，隆基绿能承压。"
    "就当前时点看，下游需求回暖的持续性仍需观察，但订单能见度已明显改善，"
    "我们维持对板块的正面看法，并提示关注后续产能释放节奏与价格传导情况。"
    "综上，推荐关注设备与材料两个环节的龙头公司。"
)


def test_threshold_boundary_is_documented() -> None:
    """阈值常量与用户口径绑死（改了要有人知道）。"""
    assert MIN_CHARS_FOR_EXTRACTION == 100
    assert len(_SHORT) <= MIN_CHARS_FOR_EXTRACTION < len(_LONG)


def test_short_text_never_calls_model(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ 用户口径：`≤100` 字的原文**不走模型**（断言 gateway 一次都没被调）。

    同时锁住三件事：
      · 短文本**照样落库**（否则前端显示"这条没有数据"）
      · 摘要 = **原文本身**（不是模型改写的）
      · 实体照旧由词表解析（纯规则、零模型成本）
    """
    _fresh_store(monkeypatch, tmp_path)
    gateway = _CountingGateway()
    stats = asyncio.run(tone_job.run_once(
        gateway=gateway, items=[_item("short1", _SHORT)], root=tmp_path))

    assert gateway.calls == 0, "短文本调了模型"
    assert stats["written"] == 1, "短文本没有落库 —— 前端会显示成没有数据"
    assert stats["skipped_short_text"] == 1

    hit = tone_store.get("short1", root=tmp_path)
    assert hit is not None
    assert hit["summary"] == _SHORT, "短文本的摘要必须是原文本身"
    assert hit["source"] == "rules"
    assert hit["bullish"]["industries"] == ["存储芯片"]
    assert hit["bullish"]["stocks"] == [
        {"name": "中芯国际", "code": "688981", "count": 1}]
    assert hit["bullish"]["boards"] == [
        {"name": "存储芯片", "code": "886042.TI"}]


def test_long_text_still_calls_model(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★ 另一侧：长文本**必须**照旧走模型，而且要满足两条硬要求：

      · **`medium` 层** —— 它的本地地板是 `configs/models.yaml` 的
        `local_medium`（2026-09-28 起是 `qwen3.5:4b`；**名字只写在配置里**，
        代码里不再有 `LOCAL_MODEL` 这类常量 —— 见
        `tests/unit/test_local_model_single_source.py`）。
        `light` 层的本地模型是 `local_light`（qwen2.5:1.5b 级），而本模块开头记着实测：
        1.5B 在这个任务上 5 条样本出 4 类错（模板抄回、标点改写、判定相反）。
        这条链路要读 ~600 字吐 9 个字段，小模型扛不住。
      · **`local_only=True`** —— `medium` 层的 fallback 是 `deepseek-flash`
        （云端、按 token 计费）。用户口径："只要不用云端tokens就行"。
    """
    _fresh_store(monkeypatch, tmp_path)
    reply = json.dumps({
        "summary": "半导体设备景气上行，存储芯片涨价",
        "events": ["机构上调盈利预测"],
        "tone": "偏多", "phrases": ["机构上调盈利预测"], "codes": ["688981"],
        "bull_industries": ["存储芯片"], "bull_stocks": ["中芯国际"],
        "bear_industries": [], "bear_stocks": [],
    }, ensure_ascii=False)
    gateway = _CountingGateway(reply)
    stats = asyncio.run(tone_job.run_once(
        gateway=gateway, items=[_item("long1", _LONG)], root=tmp_path))

    assert gateway.calls == 1, "长文本没有走模型"
    assert gateway.tier == "medium", f"抽取走的是 {gateway.tier} 层，不是 8B 那一层"
    assert gateway.kwargs.get("local_only") is True, "抽取调用没有限制为本地模型"
    assert stats["skipped_short_text"] == 0
    hit = tone_store.get("long1", root=tmp_path)
    assert hit is not None and hit["source"] == "rules+llm"


def test_extraction_tier_resolves_to_a_capable_local_model() -> None:
    """★★ 真配置下把这条链路**实际会用的模型**钉死：`medium` → 有**够用的**本地兜底。

    这条用例直接读 `configs/models.yaml`（与运行期同一个文件）——
    "哪一层 = 哪个模型"是配置决定的，光断言常量名证明不了它真的会被调用。
    只读配置、不建网关（不联网）。

    ⚠️ 2026-09-28 三处修正：
    ① 改为读**解析后的完整链**（`_load_model_config`）：原实现手工拼
       `primary` + 单数 `fallback`，遇到三跳链（`fallbacks` 列表）会**漏掉
       后续跳** → 误报"medium 层没有本地模型"。
    ② "本地"判据改用 `provider == "ollama"`，**不用** `not in PAID_PROVIDERS`：
       后者是"**不花钱**"语义，而 `zhipu`（glm，免费）不在 PAID_PROVIDERS 里
       → 会被误判成本地模型。本例要的是**本机**，不是"免费"。
    ③ **第二十三轮：判据从"必须是 8B 这个名字"改成"能力类别够用"**。

       原因：本地地板从 `qwen3:8b-q4_K_M` 换成了 `qwen3.5:4b`（带标注的
        9 条语料 × 2 轮实测：两者**完全正确率同为 89%**，而 4B 的
        `p95 5.58s` 远好于 8B 的 `22.49s`、显存 2983MB 远小于 5578MB，
        且能与 1.5B 同时常驻不换出）。

       原判据 `"8b" in model_name` 把"**这条链路需要多大的本地模型**"这个
       真问题，写成了"当时正好选了哪个模型名"—— 换模型时会误报，而它想防的
       （被裁到 1.5B）却不一定拦得住。现在改成两条**与模型名无关**的判据：
        · 不能是 light 层那个 1.5B（1.5B 在本任务"5 条样本出 4 类错"）
        · 显存档位必须 ≥ 3000MB（= 4B 级），1.5B 级（<1500MB）不算数
    """
    from src.infrastructure.llm.gateway import _load_model_config

    tier = tone_job.EXTRACT_TIER
    specs, chains, _lo = _load_model_config("configs/models.yaml")
    chain = chains[tier]
    local = [m for m in chain if specs[m].provider == "ollama"]
    assert local, f"{tier} 层没有**本机**模型兜底（链={chain}）"

    spec = specs[local[0]]
    light_specs = [specs[m] for m in chains.get("light", [])
                   if specs[m].provider == "ollama"]
    _assert_capable_local_floor(spec, light_specs)


def _assert_capable_local_floor(spec, light_specs) -> None:
    """抽取层本地地板的**能力判据**（抽成函数是为了能自证它真的会报错）。

    两条都与"模型叫什么名字"无关 —— 因为名字会变，能力要求不会：
      ① 不能与 light 层那个轻量模型是同一个（它在"读 600 字吐 9 个字段"上
         实测 5 条样本出 4 类错），名字里也不能出现 `1.5b`
      ② 显存档位 ≥ 3000MB（`vram_mb` 是网关判"拉不拉得起来"用的**实测值**，
         4B 实测 2983MB）—— 1.5B 级（<1500MB）不算数
    """
    name = spec.model_name.lower()
    for ls in light_specs:
        assert spec.model_name != ls.model_name, (
            f"抽取层的地板被裁成了 light 的轻量模型 {ls.model_name} —— "
            f"那条实测过 5 条样本出 4 类错")
    assert "1.5b" not in name, f"抽取层地板是 1.5B 级模型：{spec.model_name}"
    assert spec.vram_mb >= 3000, (
        f"抽取层地板的显存档位只有 {spec.vram_mb}MB（4B 级实测 2983MB）—— "
        f"疑似被裁到更小的模型：{spec.model_name}")


def test_capable_floor_criterion_actually_rejects_a_downgrade() -> None:
    """★ 自证：把地板裁成 1.5B / 更小模型时，上面那条判据**必须报错**。

    没有这条自证，`_assert_capable_local_floor` 就只是一个"看起来在守"的
    断言 —— 它恒真与它有效在测试结果上长得一模一样（AGENTS.md：
    「自己的检查脚本必须先自证」/「护栏保真」）。
    """
    from src.infrastructure.llm.models import ModelSpec

    def spec(name: str, vram: int) -> ModelSpec:
        return ModelSpec(name="local_medium", provider="ollama",
                         model_name=name, base_url="http://localhost:11434",
                         vram_mb=vram)

    light = [spec("qwen2.5:1.5b-instruct-q4_K_M", 1000)]
    # 合法：4B（现状）与 8B（回退路径）都要放行
    _assert_capable_local_floor(spec("qwen3.5:4b", 3000), light)
    _assert_capable_local_floor(spec("qwen3:8b-q4_K_M", 5400), light)

    # 非法三种：同 light 模型 / 名字带 1.5b / 显存档位不足
    with pytest.raises(AssertionError, match="裁成了 light"):
        _assert_capable_local_floor(spec("qwen2.5:1.5b-instruct-q4_K_M", 1000), light)
    with pytest.raises(AssertionError, match="1.5B 级"):
        _assert_capable_local_floor(spec("qwen2.5:1.5b", 1400), [])
    with pytest.raises(AssertionError, match="显存档位"):
        _assert_capable_local_floor(spec("qwen3:1b-q4_K_M", 900), [])


def test_rule_entities_only_on_short_text() -> None:
    """规则层扫实体**只对短文本**，而且只在方向明确时给。

    长文本用整条语气给每个标的派方向会张冠李戴（"光伏承压"与"半导体涨价"
    常常同时出现，整条语气是它们相抵后的结果）。
    """
    short = extract_tone(text=_SHORT, credibility_score=70, llm_obj=None,
                         rule_entities=True)
    assert short.tone != TONE_UNKNOWN
    assert short.bullish["industries"] == ["存储芯片"]
    assert short.bearish == {}, "没有利空依据却给出了利空侧"

    long = extract_tone(text=_LONG, credibility_score=70, llm_obj=None,
                        rule_entities=True)
    assert long.bullish == {} and long.bearish == {}

    default = extract_tone(text=_SHORT, credibility_score=70, llm_obj=None)
    assert default.bullish == {} and default.bearish == {}


def test_no_direction_keeps_sides_empty_but_records_hits() -> None:
    """规则层给不出方向（未定）时**两侧都不放** —— 不猜。"""
    res = extract_tone(text="中芯国际投资者交流会", credibility_score=70,
                       llm_obj=None, rule_entities=True)
    assert res.tone == TONE_UNKNOWN
    assert res.bullish == {} and res.bearish == {}
    assert res.rejected["entities_no_direction"] == ["中芯国际"]


def test_low_credibility_is_not_scanned_for_entities() -> None:
    """低可信连规则层实体也不给（用户口径："可信度低的也不做倾向分析"）。"""
    res = extract_tone(text=_SHORT, credibility_score=30, llm_obj=None,
                       rule_entities=True)
    assert res.source == "skipped"
    assert res.bullish == {} and res.bearish == {}


# ======================================================================
# 高亮词：四类都要，且逐字在标题/摘要里
# ======================================================================

def test_highlights_cover_all_four_kinds() -> None:
    """★ 概念板块 / 个股 / 外部关键标的 / AI 四类都要出高亮。"""
    title = "英伟达算力需求旺盛"
    summary = "存储芯片涨价，中芯国际扩产，加息预期扰动，AI 大模型投入加大"
    got = vocab.highlights(title, summary)
    for want in ("英伟达", "算力", "存储芯片", "中芯国际", "加息", "AI", "大模型"):
        assert want in got, f"{want} 没被标出来：{got}"
    # **每个词都必须逐字出现在标题或摘要里**（前端要拿它去替换）
    both = f"{title} {summary}"
    for term in got:
        assert term in both, f"高亮词 {term!r} 在文本里找不到"
    # 长词在前（前端按顺序替换，短词不能把长词切碎）
    assert got.index("存储芯片") < got.index("AI")


def test_highlights_respect_the_cap() -> None:
    """高亮词有上限（一份研报可能命中十几个词，全标绿等于没重点）。"""
    every_term = "、".join(e.term for e in _FAKE_ENTRIES)
    assert len(vocab.highlights(every_term)) <= vocab.MAX_HIGHLIGHTS


def test_highlights_ignore_markup_fragments() -> None:
    """★★ 匹配跑在**清洗后**的文本上，而不是原始富文本片段上。

    实测的原始正文长这样（`<e>` 是知识星球的富文本标签）：

        …存储芯片涨价<e type="hashtag" hid="5112" title="%E6%96%87%E5%AD%97" />

    标签**连同内容**会被剥掉（`intel_sources._strip_rich_tags`）。剥完还在
    原始文本上匹配的话，标出来的绿字用户在自己界面上根本找不到 ——
    那正是"核不到的证据"。
    """
    from src.infrastructure.connectors.intel_sources import IntelItem

    pub = IntelItem(
        kind="research_note",
        title="<e type=\"web\" href=\"x\">英伟达</e> 盘后点评",
        summary="存储芯片涨价 <e type=\"hashtag\" title=\"%E6%96%87%E5%AD%97\" />",
        published_at="2026-09-25T10:00:00+0800").to_public()
    got = vocab.highlights(str(pub["title"]), str(pub["summary"]))
    assert "存储芯片" in got, "清洗后的正文没被匹配到"
    assert "英伟达" not in got, "标签里的内容被当成了正文"


# ======================================================================
# 折叠规则（内容驱动）+ 馈送窗口
# ======================================================================

def _feed_item(idx: int, *, kind: str, title: str, summary: str,
               days_ago: float = 0.04) -> dict:
    ts = (datetime.now(timezone.utc).astimezone()
          - timedelta(days=days_ago)).strftime("%Y-%m-%d %H:%M:%S")
    return {
        "kind": kind,
        "kind_label": {"newswire": "财经快讯",
                       "research_note": "券商作文"}.get(kind, kind),
        "title": title,
        "summary": summary,
        "published_at": ts,
        "source_alias": "src-abc12345",
        "platform": "东方财富",
        "codes": [],
        "industry": "",
        "rating_origin": "",
        "agency": "",
        "content_hash": f"hash{idx}",
        "extra": {},
        "credibility": {"score": 74, "source_base": 74, "content_base": 74,
                        "source_reason": "权威财经媒体",
                        "content_reason": "含事实要素"},
        "tone": None,
    }


class _StubItem:
    def __init__(self, payload: dict) -> None:
        self._p = payload

    def to_public(self) -> dict:
        return dict(self._p)


def _patch_pipeline(monkeypatch: pytest.MonkeyPatch,
                    items: list[dict]) -> None:
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


#: 一条"什么都不涉及"的研究笔记（宏观数据，正是应该被折叠的那类）
#:
#: ⚠️ 正文必须 > `content_filter.MIN_CONTENT_CHARS`（50 字），否则整条在
#: 内容过滤那一步就被丢了 —— 那时用例失败的原因看起来像"折叠逻辑错了"。
_QUIET_NOTE = ("统计局公布上月社会消费品零售总额数据，同比增速较前值小幅回落，"
               "市场对此反应平稳，后续仍需观察消费复苏的持续性。")
#: 一条"涉及概念板块"的研究笔记
_BOARD_NOTE = ("存储芯片价格连续上涨，产业链库存回补，相关环节景气度上行，"
               "下游需求能见度改善，机构对后续价格弹性看法偏积极。")
#: 一条"涉及外部标的"的研究笔记
_OVERSEAS_NOTE = ("英伟达公布最新季度指引，数据中心业务收入超市场预期，"
                  "供应链订单能见度提升，市场关注其后续产能爬坡节奏。")
#: 一条"涉及 AI 关键词"的研究笔记
_AI_NOTE = ("大模型推理需求快速增长，算力租赁价格上行，"
            "多家厂商上调资本开支计划，产业链景气度持续改善。")


def test_content_based_folding_keeps_market_notes(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 用户口径：涉及个股/概念板块/外部标的/AI 的**不折叠**；
    什么都不涉及的（例行的宏观数据）**可以折叠**。

    快讯照旧折叠（"这里面绝大多数新闻都无异议"）—— 所以这里放 6 条快讯
    把组撑到阈值以上，这样"笔记有没有被折进去"才看得出来。
    """
    items = [_feed_item(i, kind="newswire", title=f"快讯 {i}", summary=_QUIET_NOTE)
             for i in range(1, 7)]
    items.append(_feed_item(11, kind="research_note", title="板块笔记",
                            summary=_BOARD_NOTE))
    items.append(_feed_item(12, kind="research_note", title="外部标笔记",
                            summary=_OVERSEAS_NOTE))
    items.append(_feed_item(13, kind="research_note", title="AI 笔记",
                            summary=_AI_NOTE))
    items.append(_feed_item(14, kind="research_note", title="宏观笔记",
                            summary=_QUIET_NOTE))
    _patch_pipeline(monkeypatch, items)
    feed = _run(limit=50)

    groups = [x for x in feed.items if x.get("is_group")]
    assert len(groups) == 1, "没有形成收容组"
    folded = [r["title"] for r in groups[0]["group_items"]]
    normal = [x["title"] for x in feed.items if not x.get("is_group")]
    # 涉及任一类的笔记**必须留在普通条目里**
    for want in ("板块笔记", "外部标笔记", "AI 笔记"):
        assert want in normal, f"{want} 被折叠了：normal={normal}"
        assert want not in folded
    # 什么都不涉及的笔记**可以折叠**
    assert "宏观笔记" in folded, f"无关笔记没被折叠：folded={folded}"
    # 快讯照旧折叠
    assert any(t.startswith("快讯") for t in folded)


def test_stock_mention_also_prevents_folding(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """个股同理（四类里最容易漏掉的一类：正文只写了个股名）。"""
    items = [_feed_item(i, kind="newswire", title=f"快讯 {i}", summary=_QUIET_NOTE)
             for i in range(1, 7)]
    items.append(_feed_item(21, kind="research_note", title="个股笔记",
                            summary="中芯国际公布业绩快报，产能利用率环比改善，"
                                    "市场关注后续扩产节奏与资本开支安排，"
                                    "以及先进制程产能爬坡对毛利率的拉动幅度。"))
    _patch_pipeline(monkeypatch, items)
    feed = _run(limit=50)
    assert any(x["title"] == "个股笔记" for x in feed.items
               if not x.get("is_group")), "提到个股的笔记被折叠了"


def test_fold_decision_uses_the_full_text_not_just_the_summary(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★★ 折叠判据看**全文**，不是展示摘要 —— 实测踩到的那个例子。

    一条《碳化硅材料专题会议》的真实笔记（3452 字）：正文里有 `天岳先进`
    （688234）与 `第三代半导体`，而模型摘要只写"碳化硅行业处成长期…" ——
    摘要里**一个板块/个股名都没有**。只看摘要的话这条会被折叠，
    而它恰恰是用户最想看的那类内容（"涉及股票、概念板块…的不折叠"）。

    ⚠️ 同时锁住"两个问法分开"：展示高亮可以**为空**（读者看得见的那段文字里
    确实没有可标的词），但这条内容仍然不折叠。
    """
    items = [_feed_item(i, kind="newswire", title=f"快讯 {i}", summary=_QUIET_NOTE)
             for i in range(1, 7)]
    note = _feed_item(21, kind="research_note", title="碳化硅材料专题会议",
                      summary="行业正处在成长曲线斜率最陡峭的中段，"
                              "中国衬底厂商已跻身第一梯队，格局出清利好龙头。")
    # 全文（抽取入参的来路）里有市场词；`market_terms` 就是 `build_feed`
    # 在拿到**全文**时算出来的那一份
    note["market_terms"] = ["第三代半导体", "天岳先进"]
    items.append(note)
    _patch_pipeline(monkeypatch, items)
    feed = _run(limit=50)

    normal = [x["title"] for x in feed.items if not x.get("is_group")]
    assert "碳化硅材料专题会议" in normal, "正文里提到板块/个股的笔记被折叠了"
    rows = [x for x in feed.items if x.get("is_group")]
    assert rows and "碳化硅材料专题会议" not in [
        r["title"] for r in rows[0]["group_items"]]


def test_internal_keys_never_reach_the_browser() -> None:
    """`extract_text` 与 `market_terms` 都是**内部键**，一层都不许出接口。

    `market_terms` 尤其容易被顺手发出去（它看着像"给前端标绿用的"）——
    但它是**全文**扫出来的，词未必出现在展示摘要里，前端在那段文字里
    根本找不到它。给前端的只有 `highlights`（逐字在展示文本里）。
    """
    feed = S.IntelFeed(items=[
        {"title": "普通条目", "summary": "正文", "highlights": ["存储芯片"],
         "extract_text": "内部全文", "market_terms": ["第三代半导体"]},
        {"title": "收容组", "is_group": True, "group_items": [
            {"title": "组内条目", "summary": "正文", "highlights": [],
             "extract_text": "组内全文", "market_terms": ["天岳先进"]},
        ]},
    ])
    blob = json.dumps(feed.to_public(), ensure_ascii=False)
    for leak in ("extract_text", "market_terms", "内部全文", "组内全文",
                 "第三代半导体", "天岳先进"):
        assert leak not in blob, f"内部内容泄漏到接口：{leak!r}"
    # 该给前端的照旧给
    pub = feed.to_public()
    assert pub["items"][0]["highlights"] == ["存储芯片"]


def test_highlights_are_emitted_for_display_and_survive_to_public(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 涉及任一类的条目要带 `highlights`（前端据此标绿），并**能出接口**。"""
    items = [_feed_item(11, kind="research_note", title="英伟达算力需求旺盛",
                        summary=_BOARD_NOTE)]
    _patch_pipeline(monkeypatch, items)
    feed = _run(limit=50, group_undetermined=False)

    assert len(feed.items) == 1, feed.items
    got = feed.items[0]["highlights"]
    for want in ("英伟达", "算力", "存储芯片"):
        assert want in got, f"{want} 没进 highlights：{got}"
    # 出接口（`to_public` 只剥内部键，highlights 是要给前端的）
    pub = feed.to_public()["items"][0]
    assert pub["highlights"] == got
    text = f"{pub['title']} {pub['summary']}"
    for term in pub["highlights"]:
        assert term in text, f"高亮词 {term!r} 在展示文本里找不到"


def test_group_rows_also_carry_highlights(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """收容组**展开后的子条目**也要带高亮词（快讯照样可能提到个股）。

    ⚠️ 这条快讯的文案**刻意不含方向词**（2026-10-01 第六轮）：
    `build_feed` 现在会用**规则层**给没有抽取结果的条目兜底判方向
    （用户报障："一条明显偏多的笔记没有【多】标记"），而被判出方向的条目
    不再进收容组。原来那句"多家云厂商上调资本开支计划…订单能见度提升"
    命中 3 个弱档词 → 判成偏多 → 它自己从组里走了，于是这条用例
    以 `KeyError` 失败，看起来像"高亮词丢了"。
    用例要验的是**高亮词透传**，不是方向判定，所以文案改成中性表述。
    """
    items = [_feed_item(i, kind="newswire", title=f"快讯 {i}",
                        summary=_QUIET_NOTE) for i in range(1, 7)]
    items.append(_feed_item(99, kind="newswire", title="快讯 99",
                            summary="英伟达盘中发布最新指引，市场关注其数据中心业务"
                                    "与后续算力需求的节奏变化，多家机构电话会"
                                    "纪要显示产业链上下游仍在观察。"))
    _patch_pipeline(monkeypatch, items)
    feed = _run(limit=50)
    groups = [x for x in feed.items if x.get("is_group")]
    assert groups, feed.items
    rows = {r["title"]: r for r in groups[0]["group_items"]}
    assert "英伟达" in rows["快讯 99"]["highlights"], rows["快讯 99"]


def test_feed_window_is_three_days(monkeypatch: pytest.MonkeyPatch) -> None:
    """★ 窗口 = **3 天**（用户口径："抓取的信息只保留3天，超过日期的直接溢出丢弃"）。

    常量与行为两侧都锁：常量改了要有人知道，而"窗口外条目真的被丢"
    由行为断言验（不靠读代码）。
    """
    assert S.FEED_WINDOW_DAYS == 3
    items = [_feed_item(1, kind="newswire", title="窗口内", summary=_QUIET_NOTE),
             _feed_item(2, kind="newswire", title="窗口外", summary=_QUIET_NOTE,
                        days_ago=4)]
    _patch_pipeline(monkeypatch, items)
    feed = _run(limit=50, group_undetermined=False)
    titles = {x["title"] for x in feed.items}
    assert titles == {"窗口内"}, titles


# ======================================================================
# 落库 / 读取：`boards` 是新的嵌套字段，老行没有它
# ======================================================================

def test_boards_survive_store_roundtrip(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ 方向桶里的 `boards` 必须能落库并**读回来**。

    `build_row` 整份放行 `bullish`/`bearish`，但**读取侧 `_side_view` 是投影**
    —— 漏一个键的表现是"抽到了、落库了、接口永远看不到"，不会有任何报错。
    """
    _fresh_store(monkeypatch, tmp_path)
    asyncio.run(tone_job.run_once(
        gateway=_CountingGateway(), items=[_item("s1", _SHORT)], root=tmp_path))
    tone_store.load(root=tmp_path, force=True)
    view = tone_store.view(tone_store.get("s1", root=tmp_path))
    assert view["bullish"]["boards"] == [
        {"name": "存储芯片", "code": "886042.TI"}]
    assert view["bullish"]["industries"] == ["存储芯片"]


def test_old_row_without_boards_still_reads(
        monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """★★ 老行（第二轮格式，方向桶里**没有** `boards`）必须照常可读。"""
    _fresh_store(monkeypatch, tmp_path)
    legacy = {
        "content_hash": "old1", "at": "2026-09-25T16:27:11+08:00",
        "tone": "偏多", "has_tone": True, "neutral": False,
        "phrases": ["增长"], "codes": [], "confidence": 0.6,
        "source": "rules+llm", "explain": "词表计数",
        "summary": "中秋假期跨区域人员流动量增长",
        "events": ["机构上调盈利预测"],
        "bullish": {"industries": ["半导体设备"],
                    "stocks": [{"name": "中芯国际", "code": "688981",
                                "count": 2}],
                    "count": 2},
    }
    p = tone_store.store_path(tmp_path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(legacy, ensure_ascii=False) + "\n",
                 encoding="utf-8")
    tone_store.load(root=tmp_path, force=True)

    view = tone_store.view(tone_store.get("old1", root=tmp_path))
    assert view["bullish"]["boards"] == [], "老行缺 boards 应当是空列表"
    assert view["bullish"]["industries"] == ["半导体设备"]
    assert view["bullish"]["stocks"][0]["code"] == "688981"


def test_public_side_exposes_boards() -> None:
    """`to_public()` 是白名单 —— 嵌套的 `boards` 也要显式放行。"""
    pub = extract_tone(text=_SHORT, credibility_score=70, llm_obj=None,
                       rule_entities=True).to_public()
    assert pub["bullish"]["boards"] == [
        {"name": "存储芯片", "code": "886042.TI"}]
    assert pub["bearish"]["boards"] == []


# ======================================================================
# 真实表（需本地库，缺库则跳过）
# ======================================================================

def test_real_table_matches_the_documented_sources() -> None:
    """真实词表：概念板块来自 `ml_board`，个股来自行情仓的 A 股名录。

    这条用例是唯一碰真实库的地方 —— 它锁的是"某一类没有突然塌掉"
    （塌掉的表现是识别与高亮整块失效，而代码层面看不出任何异常）。
    """
    if not Path("data/mainline_cache.db").exists():
        pytest.skip("本地主线数据仓不存在")
    if not Path("data/quant/warehouse.db").exists():
        pytest.skip("本地行情仓不存在")
    vocab.reset_cache()
    try:
        st = vocab.stats()
        # 库文件在但**源数据为空**（CI/公开快照只有骨架库）也按缺库处理：
        # 这条用例锁的是"真实规模不塌"，空库没有可锁的东西。
        if st["kind_concept_board"] == 0 or st["kind_a_stock"] == 0:
            pytest.skip(f"本地数据仓缺板块/A股名录数据：{st['gaps']}")
        # 2026-09-27：主线概念池冻结后 `ml_board` 的 concept 目录 = 122
        # （与 `configs/mainline_frozen_pool.yaml` 逐字相等）。原值是 138。
        assert st["kind_concept_board"] == 122, st
        assert st["kind_a_stock"] == 5562, st
        assert st["kind_overseas"] == len(vocab.OVERSEAS_TERMS), st
        # `人工智能` 同时是概念板块与 AI 关键词 —— 一张表里按 `KINDS` 顺序
        # 去重（板块在前），所以 AI 这一类少一条。这是"先到先得"的可见证据，
        # 也是"一张表"这个设计的直接后果（分两张表就不会有这次去重）。
        assert st["kind_ai"] == len(vocab.AI_TERMS) - 1, st
        assert vocab.table().index["人工智能"].kind == vocab.KIND_BOARD
        assert st["total"] == (122 + 5562 + len(vocab.OVERSEAS_TERMS)
                              + len(vocab.AI_TERMS) - 1), st
        assert st["max_len"] >= 8, st            # "仿制药一致性评价"这类长名
        assert st["gaps"] == [], st              # 两类数据源都必须读到了
        boards = {e.term: e.board_code
                  for e in vocab.entries_by_kind(vocab.KIND_BOARD)}
        assert boards.get("存储芯片") == "886042.TI"
        assert boards.get("第三代半导体") == "885908.TI"
        # 别名表（`ml_theme_board`）刻意不用：185/322 行的板块已不在 `ml_board`
        assert "苹果概念" not in boards
        stocks = {e.term: e.code
                  for e in vocab.entries_by_kind(vocab.KIND_STOCK)}
        assert stocks.get("平安银行") == "000001"
        assert stocks.get("中芯国际") == "688981"
        # 港股名不该出现在 A 股词表里（实测 `ml_member` 里混着这些）
        for hk_name in ("亮晴控股", "世大控股", "裕兴科技"):
            assert hk_name not in stocks
    finally:
        vocab.reset_cache()


def test_real_scan_on_a_market_note_is_fast() -> None:
    """真实表 + 一条真实长度的笔记：一次扫描必须**毫秒级**。

    它跑在**请求路径**上（每条都要标绿、还要据此决定折不折叠），
    所以这里量单条成本，防止将来有人往里塞几万条词。
    """
    if not Path("data/mainline_cache.db").exists():
        pytest.skip("本地主线数据仓不存在")
    vocab.reset_cache()
    try:
        note = ("半导体设备板块景气上行，存储芯片涨价，中芯国际（688981）受益于扩产，"
                "机构上调盈利预测；英伟达数据中心业务超预期，AI 算力需求旺盛，"
                "特斯拉 Optimus 进展披露；光伏组件价格下滑，隆基绿能承压。"
                "就当前时点看，下游需求回暖的持续性仍需观察。")
        rounds = 100
        started = time.perf_counter()
        for _ in range(rounds):
            vocab.highlights(note)
        per_item_ms = (time.perf_counter() - started) / rounds * 1000
        assert per_item_ms < 5.0, f"单条扫描 {per_item_ms:.2f}ms，太慢（请求路径）"
    finally:
        vocab.reset_cache()


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
