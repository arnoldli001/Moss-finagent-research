#!/usr/bin/env python
"""ETF 份额监控 · 面试追问链演练（交互式）

## 这是什么

把 `docs/INTERVIEW_DEFENSE_ETF_FLOW.md` 里的三条「连续追问链」做成可交互演练：
脚本逐层提问 → 你输入答案 → 脚本比对**要点覆盖**、指出遗漏 → 需要时给参考答案与
「面试官在探什么」。

单问好答，**连续追问**才是面试的分水岭：第二层开始考「你有没有真的想过这个方案的
边界」，第三层开始就是「你的方案在哪里会失效」。

## 用法

    python scripts/interview_drill.py              # 三条链全跑
    python scripts/interview_drill.py --chain A    # 只练拆分识别
    python scripts/interview_drill.py --chain B C  # 练两条
    python scripts/interview_drill.py --list       # 只看题目列表（不交互）

交互命令（在答案输入行里直接打）：

    :a   看参考答案（看完仍要自己输入答案，否则记为未答）
    :k   只看要点提示（不给完整答案）
    :s   跳过本题
    :q   退出

## 判分口径

**召回优先**：每个要点给多个可接受说法，命中任一即算覆盖。
目的是帮你发现「哪一层完全没想过」，不是考措辞 —— 所以宁可少判你错。

## 为什么单独一个脚本、不并进 tests

它不测代码，测的是**人**。放进 `tests/` 会被 CI 跑起来挂在 `input()` 上。
"""

from __future__ import annotations

import argparse
import sys
import textwrap
from dataclasses import dataclass, field

# Windows 控制台默认可能是 GBK，中文会乱码或抛 UnicodeEncodeError
if hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        sys.stdin.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 老终端不支持就算了，不值得因此报错
        pass


@dataclass
class Point:
    """一个「要点」：命中任一 `match` 片段即算覆盖。"""

    label: str
    match: tuple[str, ...]

    def hit(self, answer: str) -> bool:
        return any(text in answer for text in self.match)


@dataclass
class Layer:
    """追问链的一层：问题 + 要点 + 参考答案 + 面试官在探什么。"""

    question: str
    points: list[Point]
    answer: str
    probe: str = ""


@dataclass
class Chain:
    key: str
    title: str
    why: str
    layers: list[Layer] = field(default_factory=list)


# ======================================================================
# 三条链的内容（与 docs/INTERVIEW_DEFENSE_ETF_FLOW.md 同步维护）
# ======================================================================

CHAINS: list[Chain] = [
    Chain(
        key="A",
        title="拆分识别 —— 考的是「你的方案在哪里会失效」",
        why="这是全场唯一需要**推导**而非记忆的部分，也最容易被追问到边界。",
        layers=[
            Layer(
                question="你怎么发现那 4 条「行业反转警示」是假的？",
                points=[
                    Point("份额大幅跳变（+96%）",
                          ("96", "份额跳", "份额增", "份额翻")),
                    Point("价格反向大幅跳变（-49%）",
                          ("49", "48.6", "价格跌", "腰斩", "价格腰")),
                    Point("总市值基本不变（差 0.74%）",
                          ("市值", "0.74", "不变", "1.0074")),
                ],
                answer=(
                    "份额 +96.29%（1,200,859 → 2,357,118）而价格 -48.67%"
                    "（1.660 → 0.852），份额×价格只差 0.74% —— **总市值一分没变**。"
                    "这是 1 拆 2，机械换股被读成了史上最大的一次申购。"),
                probe="面试官在探：你有没有先看数据再下结论，还是照着现象猜原因。",
            ),
            Layer(
                question="市值不变就一定是拆分吗？不能是巧合？",
                points=[
                    Point("这关系是结构性/必然的，不是巧合",
                          ("结构性", "必然", "数学", "同一笔资产", "换份数", "定义")),
                    Point("全历史扫描只匹配到 3 次，且都对应已知事件",
                          ("3 次", "三次", "全历史", "扫描", "9 只")),
                    Point("巧合要凑到 1% 以内极难",
                          ("极难", "概率", "凑", "罕见", "不可能")),
                ],
                answer=(
                    "1 拆 2 时「份额比 × 价格比 ≈ 1」是**结构性**的：同一笔资产换了份数，"
                    "总市值按定义守恒。巧合要同时凑出「份额跳 96%」+「价格跌 49%」+"
                    "「乘起来落在 1±15%」三个条件，概率极低。"
                    "而且我扫了观测清单 9 只 ETF 的 2018-2026 全历史，"
                    "**只匹配到 3 次，都对应已知的份额变更事件**。"),
                probe="面试官在探：你的判据是「看着像」还是「有必然性」？有没有做全样本复核？",
            ),
            Layer(
                question="你的第三个条件（市值比≈1）会不会漏掉真实的大额申购？",
                points=[
                    Point("真实申购会改变市值，比值会明显偏离 1",
                          ("改变市值", "偏离", "不守恒", "资金净流入")),
                    Point("第一个条件（份额跳变≥15%）先挡掉了小额",
                          ("第一个条件", "份额跳变", "15%", "门槛")),
                    Point("举出实测反例：0403 那天份额仅 +1.91%、价格 -0.25%",
                          ("1.91", "0403", "4/3", "0.25", "不满足")),
                ],
                answer=(
                    "不会。真实申购**改变**总市值，比值会明显偏离 1。"
                    "三重条件叠加后能通过的必须是「份额大幅跳变 + 价格大幅反向 + 市值不变」。"
                    "实测反例：20260403 那天份额 +1.91%、价格 -0.25%，"
                    "比值 1.0165 虽然落在容忍区间内，但**份额跳变只有 1.91%，"
                    "第一个条件就不满足**。"),
                probe="面试官在探：你有没有用假阳性/假阴性两个方向检验过自己的判据？",
            ),
            Layer(
                question="如果一次拆分同时伴随真实大额申购呢？（关键层）",
                points=[
                    Point("真实申购会被吸收进份额比",
                          ("吸收", "混入", "算进", "并入", "包含")),
                    Point("结果是被归一化掉 / 真实申购被抹平",
                          ("归一化", "抹平", "抵消", "消失", "忽略", "掩盖")),
                    Point("承认这是方案的已知局限（假设拆分当天无大额申赎）",
                          ("局限", "边界", "假设", "不完美", "失效")),
                    Point("要彻底解决需要外部权威数据（基金公告的拆分比例）",
                          ("公告", "权威", "基金公司", "官方", "外部数据")),
                ],
                answer=(
                    "那我的判据会把真实申购**吸收进份额比** —— 观测到的比例会是 1.0074"
                    "而不是 1.0000，于是真实申购被当作拆分的一部分**归一化掉了**。"
                    "这是我的方案的**已知局限**：它假设拆分当天没有大额申赎。"
                    "要彻底解决需要**外部权威数据**（基金公告里的拆分比例），"
                    "而不是从价格/份额反推。"),
                probe=("面试官在探（**这层答出来很加分**）：你会不会主动说出自己方案的失效边界？"
                       "大多数人只会讲「我的方案能处理什么」，讲不出「它在哪里会错」。"),
            ),
            Layer(
                question="你考虑过分红、份额折算、扩募这些其他公司行为吗？",
                points=[
                    Point("分红不改变份额（改的是净值），不触发判据",
                          ("分红", "净值")),
                    Point("扩募是真实资金流入，不该被归一化",
                          ("扩募", "申购", "真实流入", "真实资金")),
                    Point("因为价格不会同比例反向跳变，所以不会误伤",
                          ("价格不会", "同比例", "反向", "不会误伤", "不触发")),
                    Point("结论：检测器作用域是恰当窄的，只抓按比例换份数",
                          ("恰当", "只抓", "作用域", "范围窄", "精确")),
                ],
                answer=(
                    "分红**不会改变份额**（ETF 分红改的是净值），所以不会触发也不需要处理；"
                    "**扩募是真实的资金流入，本来就该触发信号**，不该被归一化 —— "
                    "我的判据不会误伤它，因为价格不会同比例反向跳变。"
                    "所以这个检测器的**作用域是恰当窄的**：只抓「按比例换份数」这一类，"
                    "不碰资金流。"),
                probe=("面试官在探：你有没有主动界定方案的作用域？"
                       "一个「什么都没误伤」的过滤器，通常也什么都没抓到。"),
            ),
        ],
    ),
    Chain(
        key="B",
        title="伪重复 —— 考的是「统计正确性」与「仓位纪律」是否连得起来",
        why="判据用存量指标的累加值，是本项目最隐蔽的一个统计缺陷。",
        layers=[
            Layer(
                question="为什么同一个信号会连续报 21 天？",
                points=[
                    Point("判据用的是**存量**指标的累加值（份额 5 日累计）",
                          ("存量", "5 日累计", "五日累计", "累计")),
                    Point("一次流入后累计值连续多日都超标",
                          ("连续", "天天", "多日", "一直")),
                    Point("给出量级：最长 21 天 / 300 行只对应 63 事件",
                          ("21", "63", "12 天", "18 天")),
                ],
                answer=(
                    "判据是「份额 **5 日累计** > +10%」，而份额是**存量** —— "
                    "一次资金流入发生后，接下来 5 天的累计值**天天都超标**。"
                    "实测：159516 的 41 条行业反转只对应 **4 个独立事件，最长连报 21 天**；"
                    "最近 300 行明细只对应 **63 个独立事件**。"),
                probe="面试官在探：你知不知道「存量 vs 流量」会直接影响信号定义？",
            ),
            Layer(
                question="那你为什么在展示层去重，而不是在信号层？",
                points=[
                    Point("信号层逐日触发是**有意**的（持续性确认 / alert_id 逐日）",
                          ("有意", "故意", "设计", "持续性", "确认", "提醒", "alert_id")),
                    Point("展示层要的是独立事件数（一次事件重复计入会虚增样本）",
                          ("独立事件", "虚增", "样本", "展示", "统计")),
                    Point("保留 repeat_days，连报天数这个信息不丢失",
                          ("repeat_days", "保留", "不丢", "标注")),
                ],
                answer=(
                    "**两者目的不同**。信号层逐日触发是**有意**的"
                    "（`alert_id = 日期+代码+类型`）：资金还在流入就该每天提醒，"
                    "这是「持续性确认」而不是重复。而展示层要的是**独立事件数** —— "
                    "一次事件被当成 N 个独立样本计入会虚增样本量。"
                    "所以我在展示层合并，同时保留 `repeat_days`，"
                    "让「连报了多少天」这个信息不丢失。"),
                probe="面试官在探：你会不会区分「业务语义上的重复」和「统计上的重复」？",
            ),
            Layer(
                question="合并之后样本量从 300 变成 63，胜率会不会变？",
                points=[
                    Point("展示层的统计表没变（by_kind/by_etf 仍按日计样本）",
                          ("没变", "不变", "按日", "by_kind", "仍按")),
                    Point("事件级统计需要重新聚合，那是另一个口径",
                          ("重新聚合", "另一个口径", "事件级", "重新统计")),
                    Point("没擅自改，因为改了就没法跟历史报告对比",
                          ("对比", "历史报告", "没法比", "口径一致")),
                ],
                answer=(
                    "**展示层的胜率表没变** —— `by_kind` / `by_etf` 那些统计仍按日计样本，"
                    "我只改了明细的显示。如果要做**事件级**统计，需要重新聚合，"
                    "那会是一个新的口径；**我没有擅自改**，因为改了就没法和历史报告对比了。"),
                probe=("面试官在探：**诚实度**。这题的标准答案是「没变」—— "
                       "如果答成「胜率提高了」，说明你并不清楚自己改了什么。"),
            ),
            Layer(
                question="如果把判据从「5 日累计」改成「当日份额环比」会怎样？",
                points=[
                    Point("那是另一个信号，不是同一个信号的优化",
                          ("另一个信号", "不同信号", "变成另一个", "换了定义")),
                    Point("逐渐式建仓会漏报（5 日累计 +13% 但单日都不到 3%）",
                          ("逐渐", "漏报", "单日不到", "温和", "分步")),
                    Point("改判据必须重新回测，不能凭直觉",
                          ("重新回测", "回测", "不能凭直觉", "验证")),
                ],
                answer=(
                    "会变成**另一个信号**。项目文档里已记录了这个权衡："
                    "**逐渐式建仓会漏报** —— 5 日累计 +13% 但单日都不到 3% 就不触发。"
                    "改判据必须**重新回测**，不能凭直觉。"),
                probe="面试官在探：你会不会随手改阈值/口径，而不重新验证？",
            ),
        ],
    ),
    Chain(
        key="C",
        title="门控 —— 考的是「统计显著性」与「规则的生命周期」",
        why="66 条样本撑起的规则，最容易被追问「你怎么知道这不是拟合出来的」。",
        layers=[
            Layer(
                question="环境门控是怎么来的？",
                points=[
                    Point("原始规则 582 条混在一起是 -0.04% / 49.48%（像噪声）",
                          ("582", "-0.04", "49.4", "49.5", "抛硬币", "噪声")),
                    Point("按环境拆开后符号相反",
                          ("符号相反", "方向相反", "分层", "拆开", "分组")),
                    Point("熊市 +8.78% / 76.6%",
                          ("8.78", "76.6", "熊市")),
                ],
                answer=(
                    "582 条机会信号混在一起是 **-0.04% / 49.48%**，等于抛硬币。"
                    "按市场环境拆开后**符号相反**：熊市 77 条 **+8.78% / 76.6%**、"
                    "牛市 60 条 -1.53% / 36.7%、震荡市 445 条 -0.70% / 46.5% —— "
                    "**这是辛普森悖论**，不是噪声。"),
                probe=("面试官在探：你知不知道「整体无效 ≠ 分层无效」？"
                       "这是量化面试的高频考点。"),
            ),
            Layer(
                question="66 个样本够吗？83.3% 胜率可信吗？",
                points=[
                    Point("主动说出样本量：8 年多只有 66 条，年均约 8 个",
                          ("66", "年均", "8 个", "八年")),
                    Point("给出置信区间（95% CI 约 [72%, 91%]）",
                          ("置信", "区间", "72", "91", "标准误")),
                    Point("定位为环境过滤器而不是独立策略",
                          ("过滤器", "辅助", "不构成", "不是独立", "只当")),
                ],
                answer=(
                    "8 年多只有 **66 条**，年均约 **8 个**。83.3% 的 95% 置信区间大约"
                    "**[72%, 91%]**（n=66 时标准误约 4.6%）。**绝对够用但不算充分**，"
                    "所以我不把它当独立策略，只当**大盘环境过滤器** —— "
                    "它大部分时间不出信号，这是它的性质，不是缺陷。"),
                probe=("面试官在探：**你会不会主动暴露样本量的弱点**。"
                       "只说「胜率 83.3%」就等着被追问「多少个样本」。"),
            ),
            Layer(
                question="你怎么排除这是数据挖掘出来的规则？",
                points=[
                    Point("门控来自机制解释（国家队托底行为），不是参数搜索",
                          ("机制", "国家队", "托底", "逆势", "解释", "不是搜索")),
                    Point("保留了被门控的对照组（223 条 +0.52% / 52.9%）",
                          ("对照", "223", "0.52", "52.9", "降级")),
                    Point("T+1 口径不衰减，排除信号日涨完的回测假象",
                          ("T+1", "8.90", "不衰减", "次日")),
                ],
                answer=(
                    "三层防线：① 门控规则来自**机制解释**不是参数搜索 —— "
                    "宽基 ETF 大额逆势申购主要来自国家队托底，最激烈的时点就是熊市底部；"
                    "② 保留了**被门控的对照组**（223 条 +0.52% / 52.9%）—— "
                    "如果门控只是过拟合，对照组不该这么接近抛硬币，两桶的差距才是门控的价值；"
                    "③ **T+1 口径不衰减**（+8.90% vs +9.18%），排除「信号日已经涨完」"
                    "的回测假象。"),
                probe="面试官在探：你有没有做过反面验证（negative control）？",
            ),
            Layer(
                question="如果明年市场环境变了，这个规则还成立吗？",
                points=[
                    Point("可能不成立，并给出证据：风险排除熊市那格仅 13 样本、8 年只拦下 1 个",
                          ("13", "1 个", "风险", "熊市", "样本少")),
                    Point("项目文档自己标注不要当已验证规则",
                          ("文档", "标注", "不要当", "未验证", "局限")),
                    Point("需要样本量下限 + 定期复核机制",
                          ("样本量下限", "下限", "复核", "定期", "门槛", "监控")),
                ],
                answer=(
                    "**可能不成立，而且我有证据说它不牢**：门控里「风险信号排除熊市」"
                    "那一格**只有 13 个样本、8 年只拦下 1 个信号** —— "
                    "项目文档自己标注「**不要当已验证规则**」。"
                    "我的态度是：这类规则必须有**样本量下限**和**定期复核机制**，"
                    "66 条撑不起一个长期承诺。"),
                probe=("面试官在探：你是不是把统计结论当成了永恒真理？"
                       "架构师和风控最在意的就是「规则的失效条件」。"),
            ),
        ],
    ),
]


# ======================================================================
# 交互
# ======================================================================

HELP = """\
  输入答案后回车 → 脚本比对要点覆盖情况
  :a  看参考答案（看完仍可继续作答）      :k  只看要点提示
  :s  跳过本题                          :q  退出
"""


def _wrap(text: str, indent: str = "    ") -> str:
    return textwrap.fill(text, width=88,
                         initial_indent=indent, subsequent_indent=indent)


def _plain(text: str) -> str:
    """去掉加粗标记 —— 这些文案在终端里显示，`**` 只会变成视觉噪声。"""
    return str(text).replace("**", "")


def ask(layer: Layer, index: int, total: int) -> tuple[float, bool]:
    """问一层；返回 `(覆盖率 0~1, 是否作答)`。"""
    print()
    print("-" * 90)
    print(f"【第 {index}/{total} 层】{layer.question}")
    print(HELP)
    while True:
        try:
            raw = input("你的回答 > ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            raise
        if raw == ":q":
            raise KeyboardInterrupt
        if raw == ":s":
            print("（跳过；要点如下）")
            for point in layer.points:
                print(f"    [ ] {_plain(point.label)}")
            print(_wrap("参考：" + _plain(layer.answer)))
            return 0.0, False
        if raw == ":k":
            print("要点提示（不显示答案）：")
            for point in layer.points:
                print(f"    * {_plain(point.label)}")
            continue
        if raw == ":a":
            print(_wrap("参考：" + _plain(layer.answer)))
            continue
        if not raw:
            print("（空输入：直接回车会记为未答，可用 :a 看答案 / :s 跳过）")
            continue
        break

    hits = [point for point in layer.points if point.hit(raw)]
    missed = [point for point in layer.points if not point.hit(raw)]
    print()
    print(f"  要点覆盖：{len(hits)}/{len(layer.points)}")
    for point in hits:
        print(f"    [v] {_plain(point.label)}")
    for point in missed:
        print(f"    [x] {_plain(point.label)}   <-- 遗漏")
    if missed:
        print()
        print(_wrap("参考答案：" + _plain(layer.answer)))
    if layer.probe:
        print()
        # `probe` 本身已以「面试官在探」开头，这里不再加前缀（否则读起来会重复）
        print(_wrap(_plain(layer.probe), indent="  > "))
    return len(hits) / max(len(layer.points), 1), True


def run(chains: list[Chain]) -> int:
    total_layers = sum(len(chain.layers) for chain in chains)
    print("=" * 90)
    print("ETF 份额监控 · 面试追问链演练")
    print(f"共 {len(chains)} 条链 / {total_layers} 层。连续追问才是分水岭，别只看单题。")
    print("=" * 90)

    scores: dict[str, list[float]] = {}
    skipped: list[str] = []
    answered = 0
    for chain in chains:
        print()
        print("=" * 90)
        print(f"链 {chain.key}：{chain.title}")
        print(_wrap("为什么要练　" + chain.why))
        print("=" * 90)
        scores[chain.key] = []
        for index, layer in enumerate(chain.layers, start=1):
            try:
                ratio, done = ask(layer, index, len(chain.layers))
            except KeyboardInterrupt:
                print()
                print("已退出。下面是本次成绩。")
                return _summary(scores, skipped, answered, total_layers)
            scores[chain.key].append(ratio)
            if done:
                answered += 1
            else:
                skipped.append(f"{chain.key}{index}")
        got = sum(scores[chain.key]) / len(chain.layers)
        print()
        print(f"-- 链 {chain.key} 完成，要点覆盖率 {got * 100:.0f}%")
    return _summary(scores, skipped, answered, total_layers)


def _summary(scores: dict[str, list[float]], skipped: list[str],
             answered: int, total: int) -> int:
    print()
    print("=" * 90)
    print("成绩单（按要点覆盖率，不是按措辞）")
    print("=" * 90)
    weak: list[str] = []
    for key, items in scores.items():
        if not items:
            continue
        avg = sum(items) / len(items)
        filled = int(avg * 20)
        bar = "#" * filled + "." * (20 - filled)
        print(f"  链 {key}  [{bar}]  {avg * 100:>5.0f}%")
        for index, ratio in enumerate(items, start=1):
            if ratio < 0.5:
                weak.append(f"{key}{index}")
    print()
    print(f"  作答 {answered}/{total} 层"
          + (f"；跳过 {', '.join(skipped)}" if skipped else ""))
    if weak:
        print(f"  [!] 覆盖率低于 50% 的层：{', '.join(weak)}")
        print("      -> 回 docs/INTERVIEW_DEFENSE_ETF_FLOW.md 对应小节，"
              "并把参考答案**默写一遍**（写比读有效得多）")
    else:
        print("  [v] 各层要点覆盖良好。下一步：不看题面，把三条链口述一遍（限时 3 分钟）。")
    print()
    print("  提醒：覆盖率只反映「有没有想到」，不反映「说得好不好」。")
    print("  面试真正被扣分的是**语速慢 + 数字含糊**，建议再演练一次：")
    print("  每题先报数字、再解释，控制在 60 秒内。")
    print("=" * 90)
    return 0


def list_only() -> int:
    for chain in CHAINS:
        print(f"\n链 {chain.key}：{chain.title}")
        for index, layer in enumerate(chain.layers, start=1):
            print(f"  {index}. {layer.question}")
            labels = "；".join(_plain(p.label) for p in layer.points)
            print(f"     要点：{labels}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ETF 份额监控 · 面试追问链演练（交互式）")
    parser.add_argument("--chain", nargs="*", default=None,
                        help="只练指定链，如 --chain A 或 --chain A B")
    parser.add_argument("--list", action="store_true",
                        help="只列出题目与要点，不进入交互")
    args = parser.parse_args(argv)

    if args.list:
        return list_only()

    wanted = CHAINS
    if args.chain:
        keys = {item.upper() for item in args.chain}
        wanted = [chain for chain in CHAINS if chain.key in keys]
        if not wanted:
            print(f"没有匹配的链：{', '.join(sorted(keys))}"
                  f"（可选 {', '.join(c.key for c in CHAINS)}）")
            return 2

    if not sys.stdin.isatty():
        print("[!] 当前 stdin 不是终端（管道/重定向）。交互演练需要真实终端输入。")
        print("    想非交互查看内容请用：python scripts/interview_drill.py --list")
        return 2
    return run(wanted)


if __name__ == "__main__":
    raise SystemExit(main())
