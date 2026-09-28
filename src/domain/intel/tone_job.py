"""原文倾向的**抽取编排**（定时任务调这个）。

## 职责边界

    tone.py        单条的判定与拦截（纯函数，可单测）
    tone_store.py  结果存储（JSONL）
    本模块         取哪些条、限多少量、调模型、落库

## 用户口径（2026-09-25）

> "原则上有幻觉风险的可以不显示数值，可信度低的也不做倾向分析。"

两条都在这里落地：

  · **可信度低的直接跳过**，连模型都不调（省算力，也避免"抽错了没人发现"）
  · **有幻觉风险的项不显示数值** —— 由 `tone.py` 的交叉验证保证：
    两层不一致时给 `未定`，`confidence` 为 `None`

## 为什么必须限流

本地模型单条 ~770ms。一次抽 200 条就是 2.5 分钟 ——
定时任务可以慢，但**不能占着模型不放**（其它任务与用户请求都要用）。
所以 `MAX_PER_RUN` 默认 40 条，2 小时一批在情报量上是够的；
不够时下一批继续（水位线就是"哪些还没抽过"，天然去重）。

## ⚠️ 第四轮起：**一次调用** → **一note 一～四次调用**（分段）

第三轮之前一条情报只调一次模型，输入是"两头各 300 字"。
实测的硬天花板：一条 3452 字的《碳化硅材料专题会议》取两头得到 591 字，
而 `天岳先进`（688234）与 `第三代半导体` 就在被切掉的中间 ~2500 字里 ——
实体字段**全空**。不是模型失败，是它**没看见那段原文**。

所以本模块现在：把清洗后的**全文**按句边界切成 ≤600 字的段
（`tone.segment_text`，最多 `MAX_EXTRACT_SEGMENTS` 段），**逐段一次调用**，
每段用它**自己那段原文**校验，最后 `tone.merge_tone_results` 确定性合成一条。
≤600 字的原文仍然只有一段 —— 也就是"没有回归"的那条路。

⚠️ 成本：本地 8B 实测 **19.7~31.1 秒/次调用**（均值 26.5s，这台机器，
受约束解码 + 思维链；`use_cache=False` 冷跑）。一次调用变成一~六次，
实测**平均 1.9~2.0 段/条**（最近 30 条样本）→ 单条 ~50 秒、
单批 40 条约 35 分钟，仍在 2 小时的调度间隔内。
**调用次数不能失控** —— 段数上限就是为此存在的（见
`tone.MAX_EXTRACT_SEGMENTS` 的说明：6 段是实测定的，不是拍的）。

## ⚠️ 模型调用**只能**发生在这里（不许挪到请求路径）

`build_feed` 是**每次请求现拼**、不落库的。本地模型单条 ~770ms，
一页 60 条现算就是 **+46 秒** —— 接口从"秒回"退化成"超时"。

所以链路是固定的两段，不能合并：

    抽取（慢，本模块，2 小时一次）→ `tone_store`（按 `content_hash`）
    读取（快，`service.build_feed`）→ 只查存储，O(1)

⚠️ 这条本周已经踩过一次同类事故：一个"跳过已采集内容"的优化让内容
**永久不可见**（见 `zsxq_incremental` 里 `fresh_floor` 的注释）。
任何"顺手在读取侧补一次抽取"的改动都属于同一类错误。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Final

from src.domain.intel import summarize, tone, tone_store
from src.domain.intel.tone import (
    MIN_CHARS_FOR_EXTRACTION,
    TONE_UNKNOWN,
    extract_tone,
)

logger = logging.getLogger(__name__)

#: 单次抽取条数上限（防占着模型不放，见模块 docstring）
MAX_PER_RUN: Final = 40

#: 并发度。**刻意是 1**：本地 Ollama 单实例，并发只会互相排队，
#: 而且把 GPU/CPU 打满会拖慢用户请求。
#:
#: ⚠️ 第四轮起单条的耗时不只取决于条数，还取决于**段数**：
#: 8B 实测 ~26.5 秒/次调用（冷跑），一条 3000+ 字的笔记最多
#: `tone.MAX_EXTRACT_SEGMENTS`（6）次。**实测平均只有 1.9~2.0 段/条**，
#: 所以串行 40 条约 35 分钟 —— 仍在 2 小时的调度间隔内，**不需要提高并发**。
#: （若将来笔记普遍变长到 6 段/条，40 条 ≈ 106 分钟，仍够；再长才需要动它。）
CONCURRENCY: Final = 1

#: 送模型的文本长度上限
_HEAD: Final = 500

#: 单条调用的输出预算。
#:
#: ⚠️ 第二轮从 320 提到 700：一次调用现在要出**九个字段**
#: （摘要 + 事件 + 语气 + 词组 + 代码 + 两个方向的行业与个股）。
#: 320 时实测输出会被截断在 `"bear_stocks":["...` 中间 ——
#: 语气字段还在（排在前面），但后面的行业/个股**全丢**，
#: 而调用方看起来只是"这次没抽到标的"。截断的 JSON 有修复回退
#: （`tone.parse_json`），但能一次说全就别赌修复。
#:
#: ⚠️⚠️ 第三轮换成 **8B（qwen3）之后必须再抬高**：qwen3 是**思维链模型**，
#: 而 Ollama 的 `options.num_predict` 把思维链 token **一起算进去**
#: （与网关里记的 DeepSeek 那条同源）。实测同一条笔记：
#:
#:     num_predict=700   → tokens_out=700, content 长度 **0**  ← 全被想完了
#:     num_predict=1500  → tokens_out=1130（思维 ~570 + 正文 559 字），解析成功
#:
#: 700 那次的后果最阴险：**不是报错，而是静默退回规则层** ——
#: 存储里那条 summary 为空、events 为空、两侧标的为空，
#: 界面上看起来只是"这条没什么内容"，而真实原因是模型一个字都没吐出来。
#: 2048 是在 1500 的基础上留的余量（一条笔记可能有 8 条事件）。
_MAX_TOKENS: Final = 2048

#: 这条链路走的**任务层级** —— 决定用哪个模型，必须是 `medium`。
#:
#: 单列成常量（而不是把 `"medium"` 写死在调用里）是为了让"哪一层 = 哪个模型"
#: 可被**测试直接钉住**（见 `tests/unit/test_intel_vocab.py` 的
#: `test_extraction_tier_resolves_to_a_capable_local_model`）。
#:
#: ⚠️ 不能用 `light`：它本地是 `qwen2.5:1.5b`，而本模块开头记着实测 ——
#: 1.5B 在**这个任务**上 5 条样本出 4 类错（把 JSON 模板抄回来、标点被改写、
#: 判定相反），并且实测会编出原文里根本没有的股票代码。这条链路要读 ~600 字
#: 再吐 9 个字段、还要求实体精确，小模型扛不住；它出的错**看起来完全合理**
#: （用户核不出来），在本项目里那比"抽不到"严重得多。
#:
#: ## ★ 2026-09-28 第二十三轮：地板从 `qwen3:8b` 换成 `qwen3.5:4b`
#:
#: 带标注的字段级测评（9 条真实语料 × 2 轮 × `think=false`）：
#:
#:     指标            qwen3.5:4b    qwen3:8b
#:     完全正确率         89%          89%
#:     p50 / p95       4.44/5.58s   5.40/22.49s
#:     显存驻留         2983MB       5578MB
#:     codes/brokers/analysts/bull_stocks 命中率：两者均 100%
#:
#: 唯一差异是**歧义样本**（减持+问询 与 分析师维持增持 同时出现）的 `tone`：
#: 4B 读成"偏多/中性"，8B 读成"偏空"。该样本 prompt 口径本身两可，且 `tone`
#: 有词表口径的交叉核对；换来的是 p95 从 22.5s 降到 5.6s（**长尾才是超时型
#: 掷硬币的来源**）以及能与 1.5B 同时常驻。详见 `configs/models.yaml` 第二节。
#:
#: ## 为什么"慢一点没关系"（仍然成立，只是基线从 2~3s 变成 ~1.5s/条）
#:
#: 这是**定时任务**：2 小时一批、单批上限 40 条，慢一点只花电。
#: 短文本那条路已经不调模型，所以真正落到地板的条目比原来更少。
EXTRACT_TIER: Final = "medium"


def build_prompt(title: str, summary: str, part: str = "") -> str:
    """**一次调用同时出摘要、倾向、利好/利空行业与个股、关键事件。**

    ## 为什么合并（而不是每样字段调一次）

    本地模型一次前向就能出全部字段。分开调就是 N 倍延迟，
    而两者读的是同一段文本、走同一次前向。合并后 token 只多几十个。

    ## 为什么现在**委托**给 `tone.build_prompt`

    字段（摘要/事件/两个方向的行业与个股）与语气必须一起校验
    （逐字、券商名单），而校验的判据就写在 `tone.build_prompt` 的提示里。
    两处各写一份提示 = 改了校验忘了改提示 = 模型被要求输出
    "校验一定会拦掉"的东西，看起来像"模型总是抽不到"。
    所以提示与校验同源，放在 `tone` 里，这里只负责把 title / 这一段拼起来。

    ## `part`：第四轮的**段**。缺省 = 用 `summary`（旧调用点照旧）

    ⚠️ 传进来的必须是**已经切好的那一段**，不是全文：
    送模型的文本与校验用的文本必须是同一个字符串（逐段校验的要求）。
    段本身 ≤ `tone.EXTRACT_MAX_CHARS`，加上标题后由 `tone.build_prompt`
    的 `MAX_TEXT_CHARS` 截断兜底。
    """
    body = f"{title}\n{part or summary}".strip()[:1500]
    return tone.build_prompt(body)


#: ⚠️ 与 `tone.SYSTEM_PROMPT` **同源**：那份里已经写清了券商名单、
#: 逐字约束、越线措辞禁令。这里再抄一份会出现"两份提示说法不一致"
#: （改了一处忘了另一处），而模型只会看到其中一份。
_SYSTEM = tone.SYSTEM_PROMPT


def _input_parts(item: dict[str, Any]) -> tuple[str, str, str]:
    """一条情报 → `(标题, 正文, 送模型/判长度的全文)`。

    ## 抽取入参优先用 `extract_text`（**清洗后的全文**），其次才是展示用的 `summary`

    `summary` 是**展示**字段：`IntelItem.to_public()` 把它截到 260 字
    （移动端一条 3400 字占满十屏）。拿它当抽取入参，模型只看得到开头 ——
    而研报的个股、评级、目标价几乎都在**结尾**（"综上，推荐 XX，目标价…"），
    于是"找利好/利空个股"这件事会漏掉大半。

    `extract_text` 由 `build_feed` 从**全文**算出（`service.extraction_input`：
    先剥富文本标签，再交给 `tone.extraction_text`）。⚠️ **第四轮起它是全文，
    不再是"两头各 300 字"** —— 压缩改由 `tone.segment_text` 切段完成，
    因为取两头会把中间整段丢掉，而实体恰好常在那里（实测《碳化硅材料专题会议》）。
    它只在进程内传递（`IntelFeed.to_public()` 会剥掉），
    所以这里读不到也不算错 —— 老数据、或别的调用方直接喂 item 时，
    退回 `summary` 仍能工作（只是输入短、通常只切出一段）。

    ⚠️ 抽出来单独一个函数是为了让**长度判定**与**送模型的内容**用同一个值：
    短文本阈值如果在一处按 `extract_text` 判、另一处按 `summary` 判，
    就会出现"这条算短的所以没调模型，但摘要写的是另一个字符串"。
    """
    title = str(item.get("title") or "")
    body = str(item.get("extract_text") or item.get("summary") or "")
    return title, body, f"{title} {body}".strip()


async def _ask_segment(gateway: Any, title: str, seg: str,
                       text: str) -> dict[str, Any] | None:
    """对**一段**原文调一次模型，返回解析后的 dict（拿不到返回 `None`）。

    ## ⚠️ 为什么是"一段一次调用"而不是"整篇一次"

    实测（8B / qwen3:8b-q4_K_M）一条 3452 字的《碳化硅材料专题会议》：
    取两头（591 字）后实体字段**全空**，而 `天岳先进` / `第三代半导体`
    就在被切掉的中间 ~2500 字里。模型没失败，是**它没看见**。

    ## ⚠️ 送进去的 `seg` 与校验用的 `text` 必须是**同一个字符串**

    调用方传的 `text` 就是 `seg`（不是全文）。这是**正确性要求**：
    若拿整篇原文校验第 2 段的输出，第 3 段里的词就能"通过"第 2 段 ——
    等于接受一句模型不可能读到的引文，而界面上它显示为"原文逐字可核对"。

    模型失败 / JSON 解析不出来都**不抛**，返回 `None`（调用方按规则层兜那一段）。
    一段失败不能牵连其它段 —— 这是"模型挂了只降精度、不丢功能"的同一条纪律。
    """
    try:
        resp = await gateway.complete(
            # ★ **medium** 层，不是 light（见 `EXTRACT_TIER` 的说明）：
            #   这一层的本地主模型是 8B 规模，而 light 层本地是 1.5B ——
            #   1.5B 在"读 600 字吐 9 个字段"这个任务上实测 5 条出 4 类错。
            EXTRACT_TIER, _SYSTEM, build_prompt(title, seg),
            agent_id="intel_extract", json_mode=True, max_tokens=_MAX_TOKENS,
            # ★ 语法级约束（Ollama 走受约束解码）。只给 json_mode 是不够的：
            # 实测 qwen2.5:1.5b 会返回"是 JSON 但不是这个 JSON"的东西
            # （字段名自创），解析失败后整条静默退回规则层 ——
            # 表现成"模型没抽到"，实际是"抽到了但结构不对"。
            json_schema=tone.extraction_schema(),
            # ★ **只走本地模型**：`medium` 层的 fallback 同样配的是
            #   `deepseek-flash`（云端、按 token 计费）—— 本地 Ollama 一挂
            #   它就会真的去调云端。用户口径（2026-09-25）：
            #   "本地模型推理不费钱……只要不用云端tokens就行"，
            #   所以这里显式裁掉计费提供商（`PAID_PROVIDERS`）。
            #   裁完为空会**抛错**（而不是悄悄用云端）—— 调用方本来就
            #   按"模型不可用就退回规则层"设计，报错比花钱好。
            #
            # ⚠️ **每一段都必须带 `local_only=True`**：分段把调用次数
            #   从 1 变成最多 4，漏掉一处就是 4 倍的云端账单，
            #   而且只会在"本地模型刚好挂了"的那一段上才暴露出来。
            local_only=True)
        raw = str(getattr(resp, "content", "") or "")
    except Exception as exc:  # noqa: BLE001 模型不可用不该让任务失败
        logger.info("抽取模型失败（该段退回规则层）：%s", type(exc).__name__)
        return None
    obj = summarize.parse_json(raw)
    if obj is None:
        # summarize.parse_json 只认"干净 JSON"；模型偶尔会写一句前言再给
        # JSON，或有截断。这里用 tone 侧更强的回退链（剥围栏 → 抓第一个
        # {...} → 修截断）再试一次，仍拿不到就返回 None（由 extract_tone
        # 走那一段的规则层）。
        obj = tone.parse_json(raw)
    return obj


async def _extract_one(gateway: Any, item: dict[str, Any]) -> dict[str, Any]:
    """抽一条：**按段调用**（≤600 字时就是一次），再确定性合并成一条。

    模型在某一段失败或 JSON 解析失败时**不放弃那一段，也不放弃其它段** ——
    那一段退回规则层（`extract_tone(llm_obj=None)`），其余段照常合并。
    **全部**段都失败时整条退回 `rule_tone`。任何一条路上都不抛异常 ——
    这样"模型挂了"不会让功能消失，只是精度下降，且界面能看出来。

    ## ★ 短文本**一次模型调用都不发生**（用户口径 2026-09-25）

    > "若这个内容长度本身就100字，长度不长久不需要走模型抽取了，
    >  文字多的才需要抽取，然后输出到前端。"

    `≤ tone.MIN_CHARS_FOR_EXTRACTION` 的原文本身就是摘要：模型对它唯一能做的
    就是**改写**，而模型是这条链路上最不可靠的一环（实测会把 JSON 模板抄回来、
    会编出原文没有的代码、会把语气判反）。

    ⚠️ 短文本**照样落库**（`tone_store` 里有一行、前端就看得到）：
    摘要 = 原文本身（不是模型改写的）、倾向 = `rule_tone`、
    实体 = 词表扫描（`extract_tone` 的规则层，零模型成本）。
    跳过模型**不等于**跳过这条内容 —— "没有数据"是本项目明令禁止的静默消失。

    ## ★ 段数：≤600 字一段（= 第三轮的行为，不回归），更长按句边界切

    切段与上限都在 `tone.segment_text` 里（含"超上限取前 N-1 段 + 最后一段"）。
    这里只负责：**逐段调用 → 逐段校验 → 合并 → 落库**。
    """
    title, body, text = _input_parts(item)
    cred = item.get("credibility") or {}
    try:
        score = int(cred.get("score"))
    except (TypeError, ValueError):
        score = 0

    # 短文本：不调模型（省算力、也避开"改写"这个唯一风险）
    short = len(text) <= MIN_CHARS_FOR_EXTRACTION

    plan = tone.segment_text(text)
    if short:
        # 免模型那条路：一段（原文本体），且**一次调用也不发起**。
        # 不在这里 return，是为了让短文本走与长文本**同一条**落库路径 ——
        # 两条路径一旦分叉，就是"短文本少了几个字段"这类静默差异的温床。
        plan = tone.SegmentPlan(segments=[text] if text else [], total=1)

    parts: list[tone.ToneResult] = []
    #: 逐段的**模型原始摘要**（未过 `summarize` 那关）。与 `parts` 一一对应 ——
    #: `ToneResult.summary` 里那份是 `tone` 侧已截到 ≤40 字的核心摘要，
    #: 拿它去过更严的 `summarize.validate_summary` 会丢掉"字数"这个判据。
    summaries: list[str] = []
    calls = 0
    answered = 0
    for seg in plan.segments:
        if short or score < 50:
            # 短文本（用户口径）与低可信（`run_once` 已挡一层，这里再挡一层）
            # 都不调模型 —— 但那一段仍然要产出一条 `ToneResult`，
            # 否则合并侧会以为"这段没抽到"。
            obj = None
        else:
            calls += 1
            obj = await _ask_segment(gateway, title, seg, seg)
            if obj is not None:
                answered += 1
        summaries.append(str((obj or {}).get("summary") or "").strip())
        # ★ 逐段校验：`text=seg` —— 用**模型真正看过的那段原文**校验它自己的
        #   输出。拿整篇原文校验会让第 3 段里的词"通过"第 2 段，
        #   等于接受一句模型不可能读到的引文（见 `_ask_segment`）。
        parts.append(extract_tone(
            text=seg, credibility_score=score, llm_obj=obj,
            # ★ 短文本才让规则层去扫实体：那条路本来就不该有模型，
            #   词表扫描是纯规则、零模型成本，前端照样有东西可看。
            #   长文本传 False —— 模型没给实体就是没给，
            #   按整条语气给每个标的派方向会张冠李戴。
            rule_entities=short))

    # ── 确定性合并（各段的字段早已按**各自那段原文**校验过）──
    res = tone.merge_tone_results(parts, segments=plan.count, calls=calls,
                                  skipped=plan.skipped)
    # `source` 由这里决定，判据是**有没有一段真的答了**（`answered`），
    # ⚠️ 不是"发起了几次调用"（`calls`）：模型挂了但仍在计数时给
    # `rules+llm`，界面就会把纯词表的结果标成"模型也参与了"——
    # 界面靠这个字段区分"模型给的"与"词表给的"，标错等于抹掉唯一的可观测线索。
    # （实测踩到：`_FakeGateway(None)` 抛异常那条用例因此从 `rules` 变成
    #  `rules+llm`。旧行为是"异常就没调用过"，所以判据必须落在"答了没有"。）
    res.source = "rules+llm" if answered else "rules"
    if plan.skipped:
        # 缺口必须写进 `explain`（面向用户的字段）：只落库不显示的话，
        # 用户看到"这条只抽到前两段和结尾"时没有任何线索知道为什么。
        res.explain = f"{res.explain}；{plan.skipped}未处理" if res.explain \
            else f"{plan.skipped}未处理"
    # 依据兜底：有倾向却没有逐字依据时，用**规则层命中的词**兜底，
    # 与 `extract_tone` 同一条纪律（界面不能显示一个无从核对的标签）。
    if res.tone in (tone.TONE_BULL, tone.TONE_BEAR) and not res.phrases:
        hits = tone.rule_tone(text)
        res.phrases = list(hits.bull_hits if res.tone == tone.TONE_BULL
                           else hits.bear_hits)
    pub = res.to_public()

    # ── 摘要：**逐段校验、再按规则收敛**（合并已在 `merge_tone_results` 里做完）──
    #
    # ⚠️ 每段的摘要都必须在**它自己那段原文**上过一遍更严的那一关
    # （`summarize.validate_summary`：它还校验"数字不能编"与"不得出现买卖建议"），
    # 而校验用的 `seg` 必须是**那一段** —— 与其余字段同理：拿整篇原文校验第 2 段的
    # 摘要，"第 3 段才有的数字"就能通过第 2 段。
    #
    # ⚠️ 顺序：先试 `summarize`（上限 90 字、更严），不行才用 `tone` 侧
    # ≤40 字的核心摘要兜底（它过的是 `_has_common_run`）。两份都不过就留空 ——
    # **不编**。合并规则本身在 `tone._merge_summary`（连接；超 `MAX_SUMMARY_CHARS`
    # 只留第 1 段），这里不重新实现一遍。
    out_summary = ""
    for seg, raw_summary in zip(plan.segments, summaries, strict=False):
        if not raw_summary:
            continue
        sv = summarize.validate_summary(raw_summary, seg)
        if sv.ok:
            out_summary = sv.text          # 首段过了就用首段（导语优先）
            break
    if not out_summary:
        out_summary = str(pub.get("summary") or "")

    # ── 短文本：摘要就是**原文本身** ──
    #
    # 不能用 `pub["summary"]`（那是 tone 侧的 ≤40 字核心摘要，短文本下必然是
    # 空串），也不截断：它本来就 ≤ MIN_CHARS_FOR_EXTRACTION，
    # 比展示上限（研报 200 / 笔记 260）还短。原文即摘要，一个字都不用改。
    if short:
        out_summary = body or text

    # ── 中性条目：**落库但不展示**（用户口径 2026-09-25）──
    #
    # > "中性的新闻不要统计，也不要记录在前端"
    #
    # ## ⚠️ 这里改过一次，原因是原实现让"不显示中性"变成了**死代码**
    #
    # 原实现是 `return {}`（中性条目根本不落库），思路是"存储里没有，
    # 任何统计都不可能算进去"。但它漏了一件事：**读取侧也要靠这份存储
    # 才能知道某条是中性**。`build_feed` 里"丢掉中性"的分支判的是
    # `tone.has_tone and tone.tone == '中性'` —— 而存储里没有这条记录，
    # 于是它被判成"尚未抽取"，**照样显示在前端**。
    #
    # 实测：`tone_store` 26 条里只有 `未定 24 / 偏多 2`，一条中性都没有，
    # 而用户看到的中性快讯一条没少。规则写了、测试也过了，
    # 但**它从未生效** —— 因为它是从"写入侧"去实现一个"读取侧"的需求。
    #
    # 现在的做法：中性**落库并打 `neutral=True`**。统计防线改由
    # **结构**保证 —— `tone_dist` 只累加 `has_tone=True` 的条目，
    # 而中性的 `has_tone` 恒为 False，所以任何按 `has_tone` 过滤的统计
    # 都不可能把它算进去（这条性质有单测锁住）。
    neutral = bool(pub["has_tone"] and pub["tone"] == "中性")
    return {
        "content_hash": str(item.get("content_hash") or ""),
        "tone": pub["tone"],
        "has_tone": pub["has_tone"],
        #: **已判定为中性** —— 读取侧据此隐藏。与"未定/未抽取"是两种状态：
        #: 中性是"确定了没有倾向"，未定是"我们不知道"。
        "neutral": neutral,
        "phrases": pub["phrases"],
        "codes": pub["codes"],
        "confidence": pub["confidence"],
        "source": pub["source"],
        "explain": pub["explain"],
        # 空串 = 这次没生成/校验不过 —— 前端据此退回原文截断
        "summary": out_summary,
        # ── 第二轮：原文里明写的利好/利空行业与个股 + 关键事件 ──
        #
        # ⚠️ 这几个字段**显式列出来**（不是 `**pub`）：整份透传会把
        # `to_public()` 将来新增的任何字段一起写进持久层，
        # 而落库的东西是"用户能看到的"，必须逐个决定，不能默认放行。
        # 落库前还会再过一次 `tone_store.build_row` 白名单（双保险）。
        "events": pub["events"],
        "bullish": pub["bullish"],
        "bearish": pub["bearish"],
        # ── 第四轮：分段抽取的**来源说明**（落库，供排障与界面解释）──
        #
        # 为什么这三项必须落库而不是只写日志：
        #   · 用户看到"摘要只覆盖了开头"或"实体明显少了"时，唯一的线索
        #     就是"这条切了几段、有没有跳过一段"。日志是 2 小时一批的滚动文本，
        #     而这条数据要能随时按 `content_hash` 查回来。
        #   · `calls=0` 是"短文本免模型"这件事**唯一的观测点** ——
        #     没有它，"这批怎么这么快"与"这条怎么没有模型摘要"都查不出原因。
        "segments": int(res.segments),
        "calls": int(res.calls),
        #: 空串 = 全处理了。**必须显式落库**：静默跳一段的表现只是
        #: "这段怎么没抽到"，与"模型没抽到"在存储里长得一模一样。
        "skipped_segments": str(res.skipped_segments or ""),
        # ── 第五轮：券商名 / 分析师名（模型给的，**只作审计**）──
        #
        # ⚠️ 显式列出来（与上面几个字段同一个理由）：不列 = 模型认出了
        # 名单之外的机构/分析师，而它永远进不了存储 —— 表现是
        # "提示词要了、模型给了、审计时查不到"，没有任何报错。
        #
        # ⚠️ 这两个字段**不上屏**：界面上"机构：X / 分析师：X"由
        # `alert_rules` 的确定性扫描保证（见 `tone.validate_people`）。
        "brokers": [str(x) for x in (pub.get("brokers") or [])],
        "analysts": [str(x) for x in (pub.get("analysts") or [])],
    }


def _interleave_by_kind(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """按**类型轮流**重排候选：每轮从每个类型各取一条，类型内部保持传入顺序。

    调用方已把 `items` 按时间倒序排好，所以"类型内部"仍是"最新优先"。

    为什么要这么排（而不是纯时间序取前 N），见 `run_once` 里那段长注释：
    快讯一分钟一条、券商作文一天几条，纯时间序 = 按产量分配预算，
    低产品种**结构上永远轮不到**（实测 24 条笔记在 168 条快讯后面，
    40 个额度永远被快讯占满）。
    """
    buckets: dict[str, list[dict[str, Any]]] = {}
    for it in items:
        buckets.setdefault(str(it.get("kind") or "other"), []).append(it)
    out: list[dict[str, Any]] = []
    while buckets:
        # 对键的**快照**迭代：循环体里会删掉取空的桶。
        for key in list(buckets):
            out.append(buckets[key].pop(0))
            if not buckets[key]:
                del buckets[key]
    return out


async def run_once(*, gateway: Any, items: list[dict[str, Any]],
                   max_items: int = MAX_PER_RUN,
                   root: Any = None) -> dict[str, Any]:
    """抽一批并落库。返回统计。

    `items` 是**已经带 `credibility` 的情报条目**（即 `build_feed` 的产物）。
    没有 `content_hash` 的条目跳过 —— 没有指纹就查不回来，抽了也没用。
    """
    known = tone_store.load(root=root)
    todo: list[dict[str, Any]] = []
    skipped_low = 0
    skipped_done = 0
    skipped_no_hash = 0
    for it in items:
        h = str(it.get("content_hash") or "")
        if not h:
            skipped_no_hash += 1
            continue
        if h in known:
            skipped_done += 1
            continue
        cred = it.get("credibility") or {}
        try:
            score = int(cred.get("score"))
        except (TypeError, ValueError):
            score = 0
        if score < 50:
            # 低可信：**不做倾向分析**（用户口径）。这里就跳过，
            # 连模型都不调 —— 省算力，也避免"抽错了没人发现"。
            skipped_low += 1
            continue
        todo.append(it)

    # ★ **按类型轮流**取候选，而不是纯按时间倒序取前 N。
    #
    # ## 纯时间序为什么会把"券商作文"饿死（实测 2026-09-26）
    #
    # 待抽取队列当时的构成：`newswire` 168 条、`research_note` **24 条**，
    # 而单轮上限 40。快讯一分钟来一条，按时间倒序取前 40 —— **永远全是快讯**，
    # 那 24 条券商作文一条都排不进去。表现在界面上是：
    #
    #   · 它们的摘要永远是"原文截断"（`summary_text.kind = "excerpt"`），
    #     用户要的"本地模型压出的一句话摘要"从来没有过；
    #   · `events` / `bullish` / `bearish` 全空 → **多空分析那一整块是空的**；
    #   · 方向只有请求路径的词表兜底（`source = "rules"`）。
    #
    # 而用户要看的恰恰是小作文 —— 所以这不是"慢"，是**结构上永远轮不到**。
    #
    # 这与 `service._balanced_take` 是同一个问题（那里管**展示**取样，这里管
    # **抽取预算**）：数量级差异下，"按产量分配"必然把低产品种挤出去。
    # 解法也照抄那一个：类型之间轮流，类型内部仍按时间倒序。
    #
    # ## 代价（如实写出来）
    #
    # 券商作文是长文（实测 2000~3500 字 → 最多 6 段），一条约 2 分钟。
    # 每类各分 1/k 的额度意味着模型时间不再全给快讯，单轮会变长 ——
    # 这是把"永远抽不到"换成"两小时内抽完"的必要代价。
    from src.infrastructure.connectors.intel_sources import sort_key

    todo.sort(key=lambda x: sort_key(x.get("published_at")), reverse=True)
    todo = _interleave_by_kind(todo)[:max(1, max_items)]

    results: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(CONCURRENCY)

    async def _guarded(item: dict[str, Any]) -> dict[str, Any]:
        async with sem:
            return await _extract_one(gateway, item)

    if todo:
        results = await asyncio.gather(*(_guarded(i) for i in todo))

    # _extract_one 对中性条目返回空 dict（不落库）—— 这里过滤掉，
    # 否则会被 save_many 的"没有 hash 就跳过"兜住，但统计会算错
    results = [r for r in results if r]
    written = tone_store.save_many(results, root=root)
    pruned = tone_store.prune(root=root)
    tones: dict[str, int] = {}
    for r in results:
        t = str(r.get("tone") or TONE_UNKNOWN)
        tones[t] = tones.get(t, 0) + 1
    # 短文本（免模型）**单独报数**：它现在也是"落库了但没调模型"的一条，
    # 混在 `extracted` 里看不出来。少了这个数，用户问"这批怎么这么快"
    # 或者"这条怎么没有模型摘要"时，日志里什么都查不到。
    skipped_short = sum(1 for i in todo
                        if len(_input_parts(i)[2]) <= MIN_CHARS_FOR_EXTRACTION)
    # ── 分段统计（第四轮）──
    #
    # 单批的**实际调用次数**是这条链路的成本，而它现在由"段数"决定：
    # 全部短文本时 0 次，全是长文时最多 `MAX_PER_RUN × MAX_EXTRACT_SEGMENTS`。
    # 只报条数的话，"这批怎么跑了 2 小时"在日志里看不出来 ——
    # 而调用次数恰恰是唯一需要盯着调的量（8B 实测 ~49s/次）。
    calls = sum(int(r.get("calls") or 0) for r in results)
    multi = sum(1 for r in results if int(r.get("segments") or 0) > 1)
    truncated = sum(1 for r in results if r.get("skipped_segments"))
    return {
        "considered": len(items),
        "extracted": len(results),
        "written": written,
        "skipped_low_credibility": skipped_low,
        "skipped_already_done": skipped_done,
        "skipped_no_hash": skipped_no_hash,
        "skipped_short_text": skipped_short,
        #: 本批**实际发起的模型调用次数**（= 各条段数之和，短文本不计）
        "calls": calls,
        #: 走了多段（>1）的条数
        "multi_segment": multi,
        #: 因段数上限**跳过了中间段**的条数（>0 时界面上那些条只覆盖了前后两截）
        "segment_capped": truncated,
        "pruned": pruned,
        "tones": tones,
    }


__all__ = ["CONCURRENCY", "MAX_PER_RUN", "run_once"]
