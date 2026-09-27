"""情报内容过滤的回归测试。

## 用户口径（2026-09-25）

> "这知识星球爬取的数据，什么都没有也显示了，只有'#文字图片信息'，
> 这种就直接过滤掉不显示了，还有内容中出现 WD调研、礼物 等这些无关个股、
> 行业、政策的信息，都要过滤掉。用简易字符过滤或本地小模型快速过滤精简都可以。"

## 为什么用规则而不是本地模型（记录取舍）

用户给了两个选项，选规则的三个理由：

1. 这类噪音是**格式性**的（空条目、表情标记、运营话术），正则能百分之百
   拦住；模型会**偶尔漏**（同一份数据跑两次结果不同）。
2. 它在流水线**最前面**，每条都要过 —— 60 条 × 770ms = 46 秒，
   为一批注定要丢的垃圾付这个代价不值得。
3. **可解释可复算**：剥了什么、为什么剥，能逐条对上；模型只给一个
   "无关"的判断，用户没法质疑。

**语义判断留给模型**（"这条讲的是不是市场相关的事"），规则只做能确定的。
"""

from __future__ import annotations

from src.domain.intel.content_filter import (
    MIN_CONTENT_CHARS,
    clean,
    filter_items,
    looks_market_related,
    strip_noise,
)

# ======================================================================
# 无内容条目（用户点名的 "#文字图片信息"）
# ======================================================================

def test_placeholder_title_is_dropped() -> None:
    """★ 用户点名的那种：只有 `#文字图片信息`，点开什么都没有。"""
    for title in ("#文字图片信息", "文字图片信息", "＃图片信息",
                  "#图片", "#视频", "#分享", "#链接"):
        r = clean(title, "")
        assert not r.keep, f"{title!r} 没被丢掉"
        assert r.reason == "placeholder", f"{title!r} → {r.reason}"


def test_empty_and_tiny_content_is_dropped() -> None:
    assert not clean("", "").keep
    assert not clean("图片", "一张图").keep
    assert not clean("短", "太短").keep
    # 刚好到门槛的应当保留
    body = "字" * MIN_CONTENT_CHARS
    assert clean("标题", body).keep


def test_real_research_note_is_kept() -> None:
    """真研报必须留 —— 过滤过头的代价比漏过滤大。"""
    r = clean(
        "【中信机械】恒立液压美国双反调查分析",
        "双反事件整体影响非常有限。一方面，公司直接对美出口占比低；"
        "另一方面印尼与墨西哥出货已做好应对，北美大客户将照常推进。"
        "毛利率同比提升2个百分点，排产环比改善。")
    assert r.keep, f"真研报被误杀：{r.reason}"


# ======================================================================
# 平台运营话术（用户点名的 "礼物"）
# ======================================================================

def test_operational_content_is_dropped() -> None:
    """★ 用户点名的那种：星球福利、加群、打赏。"""
    cases = [
        "星球新人福利：加入星球领取福利资料，扫码进群，限时优惠！",
        "感谢大家的打赏与支持，点击链接阅读原文查看完整内容，后续继续分享",
        "限时优惠活动开始了，圈子续费八折，欢迎扫码加入",
    ]
    for text in cases:
        r = clean("标题", text)
        assert not r.keep, f"运营话术没被丢掉：{text[:20]}"
        assert r.reason == "operational"


def test_operational_check_covers_title_too() -> None:
    """运营词可能出现在标题里，不能只查正文。"""
    r = clean("星球福利大放送", "字" * 200)
    assert not r.keep and r.reason == "operational"


# ======================================================================
# 星球署名：**剥字样，不丢条目**
# ======================================================================

def test_group_byline_is_stripped_but_item_kept() -> None:
    """★ 署名（群名）要**剥掉字样**而不是丢掉整条。

    ## 为什么这条重要

    知识星球的帖子常以群名开头做署名，而那一条本身可能是有效研报。
    为署名丢掉整条会把真内容一起杀了 —— 那是"过滤过头"，
    比漏过滤更难发现（用户只会觉得"怎么没内容了"）。

    ⚠️ 判据里**不写死群名**：群名是可变配置，写死等于换个星球就失效，
    而且把真实群名明文写进代码本身就不该做（那正是要保护的资产）。
    用"XX调研/XX纪要/XX投研"的**形态**认。
    """
    for text, byline in (
        ("WD调研：半导体设备再强调，存储扩产超预期", "WD调研"),
        ("群纪要 今日复盘：市场成交平稳", "群纪要"),
        ("星球纪要：某行业景气度回升，订单同比增长", "星球纪要"),
        ("圈投研 显示某板块景气回升，排产环比提升", "圈投研"),
    ):
        r = clean("标题占位" * 10, text)
        assert r.keep, f"{byline} 那条被整条丢掉了"
        assert byline not in r.text, f"{byline} 署名没剥掉：{r.text[:30]}"


def test_section_headings_are_not_treated_as_byline() -> None:
    """**章节名**不是署名，不能剥。

    纯中文且无平台前缀的 `市场调研` / `行业纪要` 是正文的一部分，
    剥掉会让句子读不通（"：某行业景气度回升"开头很怪）。

    区分方式：署名**含字母数字**或**带平台前缀**（群/星球/圈）。
    """
    for text in ("市场调研：该行业需求回暖，多家厂商排产提升",
                 "行业纪要 显示板块轮动加快，成交额下降",
                 "市场调研显示需求回暖，多家厂商排产提升"):
        out = strip_noise(text)
        assert out.startswith(text[:4]), f"章节名被误剥：{out[:20]}"


def test_real_broker_byline_is_preserved() -> None:
    """真实机构署名（研报归属）**必须保留** —— 那是溯源信息。

    `【广发机械】` / `某某证券研究所` 告诉用户"这是哪家出的"，
    剥掉等于删掉溯源。所以署名识别不能用通用的"研究"。
    """
    for text in ("【广发机械】半导体设备再强调：存储扩产超预期",
                 "某证券研究所：该股业绩超预期，营收同比增长30%"):
        out = strip_noise(text)
        assert out.startswith(text[:2]), f"真实署名被误剥：{out[:20]}"


def test_stock_name_with_group_char_is_preserved() -> None:
    """股票名里的"群"（如"群兴玩具"）不能被当成署名前缀。"""
    text = "群兴玩具今日涨停，公司主营玩具制造，订单同比增长明显"
    assert strip_noise(text).startswith("群兴玩具")


# ======================================================================
# 格式噪音清理
# ======================================================================

def test_emoji_markers_are_stripped() -> None:
    """实测来源用表情做项目符号：`[玫瑰]1、…` / `[红包]煤价…`。"""
    assert strip_noise("[玫瑰]1、国产化推动增长").startswith("1、")
    assert "玫瑰" not in strip_noise("[玫瑰]测试内容")
    assert "红包" not in strip_noise("[红包]煤价秋季仍在较高水平")


def test_hash_and_tail_noise_are_stripped() -> None:
    assert strip_noise("#坚定推荐华新建材").startswith("坚定推荐")
    assert not strip_noise("正文内容…展开全文").endswith("展开全文")
    assert not strip_noise("正文内容 全文").endswith("全文")


def test_whitespace_is_normalised() -> None:
    out = strip_noise("第一行\n\n\n\n第二行　 　第三行")
    assert "\n\n\n" not in out and "　" not in out


# ======================================================================
# 信号词（**只作旁证，不作丢弃依据**）
# ======================================================================

def test_market_signal_is_advisory_only() -> None:
    """`looks_market_related` 不参与丢弃决策。

    一条没命中信号词的内容也可能是有效信息（例如"某公司实控人变更"），
    所以它只给调用方一个旁证，不能当过滤器用。
    """
    assert looks_market_related("营收同比增长30%，订单排到明年")
    assert not looks_market_related("今天天气不错")
    # 即便没有信号词，长内容也照样保留
    assert clean("标题", "这条内容没有任何市场信号词但它足够长" * 4).keep


# ======================================================================
# 批量过滤
# ======================================================================

def test_filter_items_reports_stats_and_log() -> None:
    items = [
        {"kind": "research_note", "title": "#文字图片信息", "summary": ""},
        {"kind": "research_note", "title": "标题", "summary": "星球福利：扫码加群"},
        {"kind": "research_note", "title": "【广发机械】半导体设备",
         "summary": "存储扩产超预期，年内迎来新一轮订单上修，"
                    "CX新的扩产框架订单正在逐步落地，多家厂商排产提升"},
    ]
    log: list[dict] = []
    kept, stats = filter_items(items, log=log)

    assert len(kept) == 1, f"应只留 1 条，实际 {len(kept)}"
    assert stats.get("placeholder") == 1
    assert stats.get("operational") == 1
    assert len(log) == 2
    # 日志**不含原文**（原文可能夹带上游标识），只有长度与原因
    for row in log:
        assert "title" not in row and "summary" not in row
        assert row.get("reason") and "title_len" in row


def test_filter_items_cleans_kept_items_in_place() -> None:
    """保留下来的条目要**就地清洗**（表情/井号/署名剥掉）。"""
    items = [{
        "kind": "research_note",
        "title": "#坚定推荐华新建材",
        "summary": "[玫瑰]海外内生性增长EBIT同比+24%，[红包]毛利率61%还在环比提升，"
                   "26Q1/Q2分别为61%/63%，产能利用率维持高位，订单能见度延续到明年。",
    }]
    kept, _ = filter_items(items)
    assert len(kept) == 1
    assert not kept[0]["title"].startswith("#")
    assert "[玫瑰]" not in kept[0]["summary"]
    assert "玫瑰" not in kept[0]["summary"]


# ======================================================================
# ★ 分析师名 / 券商名**不能**让条目被丢掉（用户 2026-09-25）
# ======================================================================

#: 用户点名要盯的分析师（`alert_rules.ANALYST_WATCHLIST`）与真实券商名。
#: ⚠️ 这里**故意不 import** 那份名单：本用例要断言的是"这几个**具体字面词**
#: 不会触发任何过滤规则"，从被测模块拿名单会让用例跟着名单一起变，
#: 失去"名单里的人不能被过滤掉"这层意思。
_WATCHED_NAMES = ("孙潇雅", "赵宇阳", "武超则", "陈果", "刘晨明", "洪灏",
                  "中泰证券", "天风证券", "国金证券")


def test_analyst_and_broker_names_survive_cleaning() -> None:
    """★ 用户口径："这些名字**不能被本地大模型过滤掉**"。

    模型那一路由抽取侧的逐字校验管；**规则这一路也要钉住** ——
    内容过滤在流水线最前面，它若因为"人/机构名"丢掉整条，
    后面的名字识别再准也拿不到东西，而表现只是"今天没弹窗"。

    这里同时断言"清洗**不改动**这些名字"：名字被剥字（哪怕少一个字）
    会让逐字校验失效，而那种失败看起来像"模型没抽到"。
    """
    for name in _WATCHED_NAMES:
        body = f"{name}认为该环节供需格局改善，建议关注龙头企业。" * 3
        r = clean(f"【{name}】最新观点", body)
        assert r.keep, f"含 {name} 的条目被丢掉了（reason={r.reason}）"
        assert name in r.title or name in r.text
        assert name in f"{r.title} {r.text}"


def test_analyst_name_alone_does_not_invalidate_an_item() -> None:
    """只有一个人名、没有任何市场信号词的长条目**照样留下**（不误杀）。"""
    body = ("孙潇雅在路演中提到该公司的产能布局与客户结构，"
            "并回答了投资者关于交付节奏的问题。" * 3)
    r = clean("路演纪要", body)
    assert r.keep
    assert "孙潇雅" in r.text


def test_broker_byline_is_not_stripped_by_the_byline_rule() -> None:
    """`【中泰证券】` **不是**星球署名，不能被 `_BYLINE` 剥掉。

    `_BYLINE` 只认 `调研 / 纪要 / 投研` 结尾的群名（`WD调研`、`群纪要`…）。
    券商名的结尾是"证券"，两者本来不冲突 —— 这条用例把边界钉住：
    哪天有人"顺手"把 `证券` 加进署名形态，机构名就会在清洗阶段消失，
    而前端那句"机构：中泰证券"会静默变空。
    """
    r = strip_noise("【中泰证券】汽车行业周报：销量超预期")
    assert "中泰证券" in r
    # 对照：真正的星球署名仍然要被剥掉（这条规则本身没坏）
    assert "WD调研" not in strip_noise("WD调研：半导体设备再强调")
