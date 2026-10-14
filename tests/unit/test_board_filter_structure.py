"""高拥挤度告警面板「板块」列筛选的回归守卫（2026-10-14 用户要求）。

## 用户原话

> 「给高度拥挤告警的数据面板中，"板块"这列加一个检索过滤功能，当前板块太多了，
>   想找其中一个板块要翻好久，支持联想或模糊搜索」

## 为什么需要守卫

"筛选"这件事**写错了是静默的**，而且静默得很像"今天没行情"：

| 写法 | 用户看到的 | 真实情况 |
|---|---|---|
| `boardFilter` 忘了进 `useMemo` 依赖数组 | 打字没反应 | 筛选**从没生效** |
| 拼音字段为空时用 `includes("")` | 敲什么都不缩小范围 | `""` 恒真 = **假绿**（最糟） |
| `<datalist>` 的 `id` 与 `list=` 不一致 | 联想框永远不弹 | 用户以为"这功能没有" |
| 筛选后 `rows=0` 复用"当前没有触发告警"文案 | 以为今天没告警 | 其实是筛没了 |

前两条断言源码结构（前端没有单测框架，仓库只有 `tsc` + `vite build`），
第三条做**跨语言字段名契约**（后端 Python 写 `pinyin_initials`、前端 TS 读
同一个 key —— 改名只改一侧时两边都还"能跑"，只是拼音搜索悄悄失效）。
真正的匹配行为由 `npm run check:boardsearch`（`web/scripts/boardSearchCheck.ts`）
用真实输入输出自证，本文件只保证那条链**接得上**。

守卫本身按仓库《突变自证》纪律验过：见 `MUTATION_NOTES`。
"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WEB = REPO / "web"
SRC = WEB / "src"
PANEL = SRC / "components" / "SectorCrowdingAlertPanel.tsx"
SEARCH = SRC / "boardSearch.ts"
API_TS = SRC / "sectorCrowdingApi.ts"
CHECK_TS = WEB / "scripts" / "boardSearchCheck.ts"
BACKEND = REPO / "src" / "api" / "routes" / "sector_crowding.py"

MUTATION_NOTES = """
突变自证（每条都真的把源码改坏跑一遍，确认对应的守卫变红，然后改回）：
 ① 删掉 `list = filterBoards(list, boardFilter);` → 红的是「筛选必须应用到清单」；
 ② 从 useMemo 依赖数组里删掉 `boardFilter` → 红的是「打字必须能重算」；
 ③ 把后端 `row["pinyin_initials"]` 改名为 `pinyin_initial` → 红的是「字段名三方一致」；
 ④ 把 `<datalist id=...>` 的 id 改名 → 红的是「datalist id 必须与 list= 对上」。
"""


def _read(path: Path) -> str:
    assert path.is_file(), f"文件不存在：{path}"
    return path.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """去掉注释再断言。

    ⚠️ 面板里到处是"为什么这么写"的中文注释，其中就提到了 `filterBoards`
    和 `boardFilter`。不断言前先剥注释，否则"注释里写了"会被当成"代码里写了"
    —— 那种误报会让人以为测试坏了而删掉守卫。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _panel_code() -> str:
    return _strip_comments(_read(PANEL))


def _rows_memo(code: str) -> str:
    """`rows` 那个 `useMemo` 的完整源码（含依赖数组）。"""
    start = code.find("const rows = useMemo(")
    assert start != -1, "面板里找不到 `const rows = useMemo(`，筛选链断了"
    end = code.find("]);", start)
    assert end != -1, "`rows` 的 useMemo 没有正常收尾（找不到 `]);`）"
    return code[start:end + 3]


def _rows_memo_deps(code: str) -> str:
    """`rows` 那个 `useMemo` 末尾的依赖数组。"""
    memo = _rows_memo(code)
    open_bracket = memo.rfind("}, [")
    assert open_bracket != -1, "`rows` 的 useMemo 没有依赖数组（那会让它每帧重算）"
    return memo[open_bracket:]


# ------------------------------------------------- ① 筛选真的接在清单上

def test_filter_is_applied_to_the_board_list() -> None:
    """`filterBoards(list, boardFilter)` 必须真的作用在清单上。"""
    memo = _rows_memo(_panel_code())
    assert "filterBoards(list, boardFilter)" in memo, (
        "`rows` 里没有 `list = filterBoards(list, boardFilter);` —— "
        "筛选框会变成装饰品（打字不改变列表）")


def test_filter_runs_before_sorting() -> None:
    """先筛后排：筛选只缩小范围，不该改变剩下各行的相对顺序。

    顺序反了（先排后筛）本身结果一样，但会让"排名"列与排序语义纠缠；
    更重要的是：一旦有人把筛选挪到 `return list` 之后（即 `rows` 之外），
    这里立刻变红 —— 那种写法会让"复制列表/导出 CSV"漏掉筛选。
    """
    memo = _rows_memo(_panel_code())
    filter_at = memo.find("filterBoards(list, boardFilter)")
    sort_at = memo.find("list.sort(")
    assert filter_at != -1 and sort_at != -1, "找不到筛选或排序调用"
    assert filter_at < sort_at, "筛选必须在排序之前"


# ------------------------------------------------- ② 打字必须能重算

def test_board_filter_is_a_memo_dependency() -> None:
    """`boardFilter` 必须在 `rows` 的依赖数组里。

    漏掉它 = **筛选完全失效**（React 不会重算 memo），而且不报错、不告警 ——
    用户只会觉得"这个搜索框是坏的"。
    """
    deps = _rows_memo_deps(_panel_code())
    assert "boardFilter" in deps, (
        f"`boardFilter` 不在 rows 的依赖数组里，筛选永远不会重算：{deps}")


def test_input_is_two_way_bound_to_the_filter_state() -> None:
    """输入框必须 `value={boardFilter}` + `onChange` 回写（受控组件）。"""
    code = _panel_code()
    assert "value={boardFilter}" in code, "筛选输入框没有绑定 boardFilter"
    assert re.search(r"setBoardFilter\(\s*event\.target\.value\s*\)", code), (
        "输入框 onChange 没有回写 boardFilter（打字不会有任何变化）")
    assert 'const [boardFilter, setBoardFilter] = useState("")' in code, (
        "找不到 boardFilter 的 state 声明")


def test_escape_clears_the_filter() -> None:
    """Esc 清空 —— 用户敲错词时的第一反应。"""
    code = _panel_code()
    esc = code.find('event.key === "Escape"')
    assert esc != -1, "筛选框没有 Esc 处理"
    assert "setBoardFilter(\"\")" in code[esc:esc + 200], (
        "Esc 分支没有清空 boardFilter")
    assert 'onClick={() => setBoardFilter("")}' in code, "没有「清空筛选」按钮"


# ------------------------------------------------- ③ 联想（datalist）接得上

def test_datalist_id_matches_the_input_list_attribute() -> None:
    """`list="..."` 与 `<datalist id="...">` 必须字面一致。

    写错一个字：输入框照常工作、**联想永远不出现**，控制台一声不响。
    """
    code = _panel_code()
    used = re.search(r'list="([^"]+)"', code)
    defined = re.search(r'<datalist id="([^"]+)"', code)
    assert used is not None, "筛选输入框没有 list= 属性（没有联想）"
    assert defined is not None, "面板里没有 <datalist>（联想没有数据源）"
    assert used.group(1) == defined.group(1), (
        f"list={used.group(1)!r} 与 datalist id={defined.group(1)!r} 不一致 "
        "→ 联想永远不会弹出来")


def test_datalist_only_offers_boards_inside_the_list() -> None:
    """联想项只来自**当前清单**，且跳过空名字。"""
    code = _panel_code()
    block = code[code.find("<datalist"):]
    block = block[:block.find("</datalist>")]
    assert "view.map(" in block, "联想项不是从清单（view）生成的"
    assert ".filter(" in block, (
        "联想项没有过滤掉空名字 —— `<option value=\"\">` 会在下拉里出现空条目")


# ------------------------------------------------- ④ 三种"空"不能混为一谈

def test_empty_state_distinguishes_filter_miss_from_no_alert() -> None:
    """筛没了 ≠ 今天没告警：空列表文案必须区分。"""
    code = _panel_code()
    start = code.find("crowding-alert-empty")
    assert start != -1, "找不到空列表提示块"
    block = code[start:start + 900]
    assert "boardFilter" in block, (
        "筛选后 rows=0 时仍在说「当前没有触发告警的板块」—— "
        "用户会把「筛错了词」读成「今天没告警」（静默的假信号）")


# ------------------------------------------------- ⑤ 跨语言字段名契约

def _backend_pinyin_key() -> str:
    src = _read(BACKEND)
    keys = set(re.findall(r'row\["([a-z_]*pinyin[a-z_]*)"\]', src))
    assert len(keys) == 1, f"后端写拼音字段的地方不止一处（{keys}）—— 会漂移"
    return keys.pop()


def test_pinyin_field_name_agrees_across_backend_and_frontend() -> None:
    """后端写的 key、TS 类型声明的 key、匹配函数读的 key —— 必须同一个字。"""
    key = _backend_pinyin_key()
    api_ts = _read(API_TS)
    search_ts = _read(SEARCH)
    assert re.search(rf"^\s*{key}\?:", api_ts, re.M), (
        f"`CrowdingListItem` 里没有声明 `{key}?`（后端下发了、TS 类型不认）")
    assert f"{key}" in search_ts, (
        f"`boardSearch.ts` 没读 `{key}` —— 后端算了拼音也没人用")
    assert key == "pinyin_initials", (
        f"字段名变成 {key!r}：前端/后端/文档三处引用都要同步改，"
        "别只改一边（两边都还能跑，只是拼音搜索悄悄失效）")


def test_frontend_does_not_reimplement_pinyin() -> None:
    """前端不许自己造一份拼音表（同一判断两份实现必然漂移）。"""
    src = _read(SEARCH)
    code = _strip_comments(src)
    banned = ("pypinyin", "PY_INITIALS", "PINYIN_MAP", "pinyinTable")
    hits = [word for word in banned if word in code]
    assert not hits, (
        f"`boardSearch.ts` 里出现了疑似自建拼音表的东西：{hits}；"
        "首字母一律由后端 `to_initials()` 下发")


# ------------------------------------------------- ⑥ 行为自证那条链是接上的

def test_node_check_is_wired_in_package_json() -> None:
    """`npm run check:boardsearch` 必须真的存在并指向存在的脚本文件。"""
    pkg = json.loads(_read(WEB / "package.json"))
    script = pkg.get("scripts", {}).get("check:boardsearch")
    assert script, "web/package.json 里没有 check:boardsearch（行为自证没入口）"
    assert CHECK_TS.is_file(), f"自证脚本不存在：{CHECK_TS}"
    assert "boardSearchCheck" in script, (
        f"check:boardsearch 执行的不是 boardSearchCheck：{script!r}")
    # 判据必须来自**真实实现**：脚本要 import 那个模块，而不是自己再写一遍
    # 匹配逻辑（那样测的是副本，改坏了源码照样绿）。
    check_src = _read(CHECK_TS)
    assert re.search(r'from\s+"\.\./src/boardSearch"', check_src), (
        "自证脚本没有 import `../src/boardSearch`（判据必须来自真实实现）")
    assert "filterBoards" in check_src and "matchBoard" in check_src, (
        "自证脚本没有同时覆盖 filterBoards 与 matchBoard")


# ------------------------------------------------- ⑦ 后端补充拼音这一列

def test_with_pinyin_matches_the_shared_implementation() -> None:
    """`_with_pinyin` 的值必须等于 `to_initials()`（唯一实现），空名字给空串。"""
    from src.api.routes.sector_crowding import _with_pinyin
    from src.quant.stock_directory import to_initials

    rows = [
        {"sector_code": "885959.TI", "sector_name": "化工原料"},
        {"sector_code": "885000.TI", "sector_name": ""},
    ]
    out = _with_pinyin(rows)
    assert out[0]["pinyin_initials"] == to_initials("化工原料")
    assert out[0]["pinyin_initials"], "中文板块名没算出拼音首字母（pypinyin 缺了？）"
    assert out[1]["pinyin_initials"] == "", (
        "空板块名给了非空拼音 —— 前端会因此把不相干的行也筛出来")
    # 前端 `matchBoard` 依赖"空拼音不参与匹配"，所以这里必须是空串而不是缺 key。
    assert "pinyin_initials" in out[1], "空名字那行缺了 pinyin_initials 这个 key"
