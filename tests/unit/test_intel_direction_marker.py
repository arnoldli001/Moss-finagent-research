"""方向标记【多】/【空】：映射、**专用配色**、以及"五条渲染路径都要有"。

> 用户口径（2026-10-01）：
>   "偏多（利多）→【多】显示绿色；偏空（利空）→【空】显示红色。"

## ⚠️ 这个绿红与本页其余地方**正好相反**，这是用户点名要的

本页（以及全站）其余地方用 A 股约定 **红涨绿跌**（`.up` 红 / `.down` 绿，
`intel.css` 的 `--intel-up` / `--intel-down`）。方向标记反过来：绿=利多、红=利空。
两套语义同时存在于同一页，所以必须：

    · 独立 class（`.intel-dir-bull` / `.intel-dir-bear`）
    · 独立 token（`--intel-dir-bull` / `--intel-dir-bear`）
    · 绝不复用 `.up` / `.down` / `--intel-up` / `--intel-down` / `.intel-hl`

复用会怎样：有人调涨跌配色（一个很正常的改动）时，方向标记跟着变 ——
而**颜色不会报错**，只会在某天被人发现"利多怎么是红的"。

## 为什么这些断言是**源码级**的

前端没有 JS 测试运行器（`web/package.json` 只有 dev/build/preview），
所以这里用"读源码 + 解析结构化片段"的方式钉住契约：

    `DIRECTION_MARKERS`   是**合法 JSON**（键值都是带引号的字符串）→ 直接解析比对
    CSS token             正则取十六进制值 → 按 RGB 判"绿"还是"红"
    TSX                   数一数标记与 chip 出现在几条渲染路径里

这不是最理想的测试形态，但它能抓住真实缺陷（改了映射、改反了颜色、
漏了一条渲染路径），而这三件事都不会有任何运行时错误。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

_WEB = Path(__file__).resolve().parents[2] / "web" / "src"
_API_TS = _WEB / "intelApi.ts"
_FEED_TSX = _WEB / "components" / "intel" / "IntelFeedTab.tsx"
_CSS = _WEB / "intel.css"

#: 本页"红涨绿跌"那一套（方向标记**不许**复用它们）
_A_SHARE_CLASSES = ("intel-up", "intel-down", "intel-hl")


def _markers() -> dict[str, dict[str, str]]:
    """解析 `intelApi.ts` 里的 `DIRECTION_MARKERS`。

    ⚠️ TS 对象字面量里的**内层键没有引号**（`{ text: "【多】", … }`），
    那不是合法 JSON。所以先把裸键补上引号再解析 —— 比在 TS 里写
    `{"text": …}` 这种别扭的键名要好（生产代码不该为了测试变形）。
    """
    src = _API_TS.read_text(encoding="utf-8")
    m = re.search(r"DIRECTION_MARKERS[^=]*=\s*\{(?P<body>.*?)\n\};", src, re.S)
    assert m, "找不到 DIRECTION_MARKERS 表"
    body = m.group("body").rstrip().rstrip(",")     # JSON 不允许尾随逗号
    body = re.sub(r"([{,]\s*)([A-Za-z_]\w*)\s*:", r'\1"\2":', body)
    return json.loads("{" + body + "}")


def _hex_token(css: str, name: str) -> tuple[int, int, int]:
    m = re.search(rf"--{name}:\s*#([0-9a-fA-F]{{6}})", css)
    assert m, f"CSS 里找不到 --{name} 的十六进制值"
    v = m.group(1)
    return int(v[0:2], 16), int(v[2:4], 16), int(v[4:6], 16)


def _rule(css: str, selector: str) -> str:
    """取一条简单规则的声明块（够用即可：这些规则里没有嵌套花括号）。"""
    m = re.search(rf"{re.escape(selector)}\s*\{{(?P<body>[^}}]*)\}}", css)
    assert m, f"CSS 里找不到规则 {selector}"
    return m.group("body")


# ======================================================================
# ① 映射本身
# ======================================================================

def test_marker_mapping_is_exactly_what_the_user_asked() -> None:
    """偏多→【多】、偏空→【空】，且各自绑到**专用** class 上。"""
    markers = _markers()
    assert markers["偏多"]["text"] == "【多】"
    assert markers["偏空"]["text"] == "【空】"
    assert markers["偏多"]["cls"] == "intel-dir-bull"
    assert markers["偏空"]["cls"] == "intel-dir-bear"
    # 只有这两态：中性/未定/未抽取一律**不在表里**（= 什么都不加）
    assert set(markers) == {"偏多", "偏空"}, markers


def test_no_direction_means_nothing_is_rendered() -> None:
    """`has_tone` 为假（未抽取 / 未定 / 中性）→ 返回 `null`，**不是空串**。

    这是源码级断言（没有 JS 运行器），但它钉的是那个唯一的判据：
    少了 `has_tone` 这一关，未抽取的条目会带着一个"未定"去查表 ——
    查不到同样返回 null，所以**单看结果发现不了**；而一旦有人往表里加了
    一个兜底值，未抽取的条目就会全部冒出一个标记。守在这里最省事。
    """
    src = _API_TS.read_text(encoding="utf-8")
    fn = re.search(r"export function directionMarker\((?P<body>.*?)\n\}",
                   src, re.S)
    assert fn, "找不到 directionMarker"
    body = fn.group("body")
    assert "!tone.has_tone" in body, "判据里少了 has_tone（未抽取的条目会漏出标记）"
    assert "return null" in body
    # 表里查不到也必须给 null（`?? null` 或等价的空值合并）
    assert "?? null" in body, "表里查不到时的兜底变了 —— 可能返回 undefined 让调用方渲染出空块"


def test_marker_is_not_a_placeholder_when_absent() -> None:
    """没有方向时**不留占位**：组件里只有一处渲染标记（模板拼接那处）。

    留一个空 `<span className="intel-dir" />` 会在标题前留一格空白，
    用户会以为"这里本来有个标记没显示出来"（用户口径是"什么都不加"）。
    """
    tsx = _FEED_TSX.read_text(encoding="utf-8")
    assert tsx.count("intel-dir ${mark.cls}") == 1, "标记的渲染点不止一处"
    for placeholder in ('className="intel-dir"', "className={`intel-dir`}"):
        assert placeholder not in tsx, f"出现空占位：{placeholder}"
    assert "if (!mark) return null" in tsx


# ======================================================================
# ② 配色：绿=利多、红=利空，且**专用**
# ======================================================================

def test_bull_marker_is_green_and_bear_marker_is_red() -> None:
    """★ 按 RGB 判色：利多绿（G 明显大于 R）、利空红（R 明显大于 G）。"""
    css = _CSS.read_text(encoding="utf-8")
    r, g, b = _hex_token(css, "intel-dir-bull")
    assert g > r, f"--intel-dir-bull 不是绿色：{r,g,b}"
    r, g, b = _hex_token(css, "intel-dir-bear")
    assert r > g, f"--intel-dir-bear 不是红色：{r,g,b}"


def test_marker_uses_its_own_tokens_not_the_a_share_up_down_ones() -> None:
    """★★ 方向标记**绝不复用** A 股红涨绿跌那一套 token。

    这是本次改动最容易悄悄退化的一处：`--intel-up`（红）与
    `--intel-dir-bear`（红）**色值恰好相同**，所以"复用"在界面上看不出差别 ——
    而它把两套相反的语义焊死在了一起：下次有人把 `--intel-up` 调成绿色，
    利多标记会跟着变绿（看起来"没错"），利空标记却变成绿色（大错）。
    """
    css = _CSS.read_text(encoding="utf-8")
    for cls, token in ((".intel-dir-bull", "--intel-dir-bull"),
                       (".intel-dir-bear", "--intel-dir-bear")):
        rule = _rule(css, cls)
        assert f"var({token})" in rule, (cls, rule)
        for banned in ("--intel-up", "--intel-down", "--high", "--low"):
            assert banned not in rule, f"{cls} 复用了 {banned}：{rule}"
    # 反向也要钉：A 股涨跌那两个 class/token 仍然只服务涨跌
    for banned in _A_SHARE_CLASSES:
        assert f".intel-dir-bull .{banned}" not in css
        assert f".intel-dir-bear .{banned}" not in css


def test_marker_class_is_distinct_from_highlight_and_up_down() -> None:
    """三个 class 名互不重叠（同一个元素不可能既是"命中词"又是"方向"）。"""
    markers = _markers()
    classes = {m["cls"] for m in markers.values()}
    assert classes.isdisjoint(set(_A_SHARE_CLASSES))
    assert classes == {"intel-dir-bull", "intel-dir-bear"}


# ======================================================================
# ③ 每条渲染路径都要有（标记 / 机构名 / 分析师名）
# ======================================================================

def _fn_bodies(tsx: str) -> dict[str, str]:
    """把 `function X(` 到下一个 `function Y(` 之间的源码切成一段。

    ## 为什么按函数切、而不是全局数出现次数

    全局 `tsx.count("<DirectionTag") == N` 在**路径改名或合并**时会给出
    误导性的结果：这一页 2026-10-01 改成主从结构后，标记总数**恰好还是 5**，
    但其中两个函数已经不叫 `GroupTableRow` / `GroupCard` 了。只数总数的断言
    会"看起来通过"，而它钉住的那份清单已经跟代码对不上了。

    按函数切段之后，断言可以说清"**是哪几条路径**"。
    """
    marks = list(re.finditer(r"\n(?:export )?function ([A-Za-z_]\w*)\(", tsx))
    out: dict[str, str] = {}
    for i, m in enumerate(marks):
        end = marks[i + 1].start() if i + 1 < len(marks) else len(tsx)
        out[m.group(1)] = tsx[m.start():end]
    return out


#: 会渲染**条目标题**的渲染路径 —— 每一条都必须带上方向标记【多】/【空】。
#:
#: 2026-10-01 主从结构改版后的清单（原来是 5 条各自独立的 DOM 分支：
#: 桌面表格行 / 手机卡片 / 组容器两种形态 / 组内条目）：
#:
#:     IntelListRow            桌面左栏列表行
#:     IntelGroupRow           桌面左栏 · 收容组容器行
#:     IntelCard               手机卡片（收起态的标题）
#:     IntelDirectionAnalysis  详情面板的「判定」那一格
#:     GroupList               展开后的组内子条目（另一份投影）
_MARKER_PATHS = ("IntelListRow", "IntelGroupRow", "IntelCard",
                 "IntelDirectionAnalysis", "GroupList")

#: 会**展示条目内容**因而必须带机构名/分析师名 chip 的路径。
#:
#: 改版后 chips 不再出现在列表行上（那会把行高翻倍），而是搬进详情面板；
#: 而详情面板是**一份实现三个位置**（桌面右栏 / 就地展开 / 手机卡片），
#: 所以"漏一处"的风险点从 5 条收敛成 2 条。第二条才是真正要守的：
#: 组内子条目不走 `IntelItemDetail`，它走服务端另一份投影（`_group_row()`），
#: 字段要单独透传 —— 漏了的表现是"展开收容组后看不到机构/分析师"。
_CHIP_PATHS = ("IntelItemDetail", "GroupList")


def test_marker_is_present_in_every_render_path() -> None:
    """**每一条会渲染条目标题的路径**都要有方向标记。

    清单写死是**故意**的：加一条渲染路径时这里会失败，逼人做一次
    "这条新路径要不要标记"的决定 —— 漏一条的表现只是"某些条目没标记"，
    没有任何报错。
    """
    bodies = _fn_bodies(_FEED_TSX.read_text(encoding="utf-8"))
    missing = [n for n in _MARKER_PATHS if n not in bodies]
    assert not missing, (
        f"渲染路径改名或消失了：{missing} —— 改这份清单之前先确认标记还在")
    for name in _MARKER_PATHS:
        assert "<DirectionTag" in bodies[name], f"{name} 漏了方向标记"
    total = sum(b.count("<DirectionTag") for b in bodies.values())
    assert total == len(_MARKER_PATHS), (
        f"标记点共 {total} 处，清单里只有 {len(_MARKER_PATHS)} 条 —— "
        "多出来的那一处要么补进清单，要么在注释里说明它为什么不需要标记")


@pytest.mark.parametrize("component", ["InstChips", "AnalystChips"])
def test_name_chips_are_rendered_wherever_content_is_shown(
        component: str) -> None:
    """机构名与分析师名要出现在**展示条目内容**的每一处。

    ⚠️ 这两行是"用户点名要看到的东西"（"一定要前端输出信息" /
    "推送到前端展示"），漏一条路径的表现是"展开收容组后看不到了"，
    而顶层还正常 —— 看起来像渲染 bug 而不是字段没透传。
    """
    bodies = _fn_bodies(_FEED_TSX.read_text(encoding="utf-8"))
    for name in _CHIP_PATHS:
        assert name in bodies, f"{name} 不见了（渲染路径变了）"
        assert f"<{component}" in bodies[name], f"{name} 漏了 {component}"


def test_marker_tooltip_shows_the_evidence_words() -> None:
    """★★ 方向标记的 tooltip 必须带上**命中的词**（`phrases`）。

    词表判定有**已知的出错形态**，而且调词表解决不了：一句
    "多家云厂商上调资本开支计划，产业链订单能见度提升" 会同时命中
    `订单` + `上调` + `提升` 三个弱档词，在"有市场语境"那一支上被判成偏多
    —— 而它其实是行业观察。`src/domain/intel/tone.py` 的 `_BULL_STRONG`
    注释里记着这条实测残余。

    要把它与"业绩下滑 + 减持"（同样两个弱档，确实该判偏空）分开需要读懂语义，
    而请求路径**不许**调模型。所以唯一的兜底是让判定**可核对**：
    tooltip 里逐字印出命中的词，用户一眼就能看到"它是靠订单/上调/提升判的"，
    从而自己打折。少了这一段，界面就只有一句"词表规则判定"，
    用户**没有任何办法**判断这个标签该不该信。

    ⚠️ `phrases` 在收容组那一行取不到（`_tone_marker` 只投最小三键），
    所以实现必须是**可选**拼接 —— 缺了就不写，不编。
    """
    src = _API_TS.read_text(encoding="utf-8")
    m = re.search(r"export function directionTitle\((?P<body>.*?)\n\}", src, re.S)
    assert m, "找不到 directionTitle"
    body = m.group("body")
    assert "phrases" in body, "tooltip 没有带判定依据词（用户无法核对）"
    assert "evidence" in body and "words.length" in body, \
        "依据词必须**可选**拼接（收容组那一路拿不到 phrases）"


def test_analyst_chips_have_their_own_prefix_and_class() -> None:
    """分析师必须有**自己的前缀与 class**（不能与机构名混成一片）。"""
    tsx = _FEED_TSX.read_text(encoding="utf-8")
    assert '<NameChips names={names} label="分析师" tone="analyst" />' in tsx
    assert '<NameChips names={names} label="机构" tone="inst" />' in tsx
    css = _CSS.read_text(encoding="utf-8")
    assert ".intel-analyst" in css, "分析师 chip 没有独立样式（会与机构名同色）"


if __name__ == "__main__":       # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
