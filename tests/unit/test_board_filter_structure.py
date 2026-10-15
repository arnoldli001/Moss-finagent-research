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
    assert "const [boardFilter, setBoardFilter] = useState(" in code, (
        "找不到 boardFilter 的 state 声明")


def test_escape_handles_both_dropdown_and_clear() -> None:
    """Esc 先收联想、再清筛选 —— 分两步（只想关下拉时别把词清了）。"""
    code = _panel_code()
    esc = code.find('event.key === "Escape"')
    assert esc != -1, "筛选框没有 Esc 处理"
    branch = code[esc:esc + 320]
    assert "setFilterFocused(false)" in branch, "Esc 没有先收联想下拉"
    assert 'setBoardFilter("")' in branch, "Esc 分支没有清空 boardFilter"
    assert 'onClick={() => setBoardFilter("")}' in code, "没有「清空筛选」按钮"


# ------------------------------------------------- ③ 输入框必须在「板块」列头里
#
# 用户 2026-10-14 报障原话：「当前前端的 板块那一列 我看还没支持搜索过滤板块功能」
# —— 功能其实已经在工具栏里跑着（`找板块`），用户就是**没找到**。
# 所以下面这条守的是**位置**，不是功能：输入框一旦被挪回工具栏/别处，立刻变红。

def test_filter_input_lives_inside_the_board_column_header() -> None:
    """筛选框必须在「板块」列表头里（用户第一眼就是往这一列找）。"""
    code = _panel_code()
    header_start = code.find("crowding-alert-header")
    assert header_start != -1, "找不到表头容器"
    header = code[header_start:]
    assert "crowding-hfilter" in header, (
        "「板块」列表头里没有筛选格（`crowding-hfilter`）—— "
        "输入框又被挪回工具栏了，用户找不到（这就是这次的报障）")
    cell_start = header.find("crowding-hfilter")
    cell = header[cell_start:cell_start + 1200]
    assert "crowding-col-filter" in cell, "列头里的筛选输入框不见了"
    assert "value={boardFilter}" in cell, "列头那个输入框没绑 boardFilter"
    # 列头那格原来是 `<button>`（点它排序）—— 按钮里套输入框是非法 HTML
    assert re.search(r"<span className=\"crowding-hcell center crowding-hfilter\">",
                     code), "列头筛选格必须用 <span> 容器，不能是 <button>"


def _dropdown_block(code: str) -> str:
    """联想下拉那一段 JSX（从 `suggestOpen && popRect` 到 `document.body)}`）。

    ⚠️ 必须**只取这一段**再断言：同一个文件里告警阈值编辑面板也用 `createPortal`
    —— 拿整个文件去 `"createPortal(" in code` 会被那一处**顶绿**，
    把这里的 portal 拆掉时守卫照样绿（第一版就是这么写的，突变自证抓出来了）。
    """
    start = code.find("suggestOpen && popRect")
    assert start != -1, "找不到联想下拉的渲染条件（`suggestOpen && popRect`）"
    end = code.find("document.body)}", start)
    assert end != -1, "找不到联想下拉的 portal 收尾（`document.body)}`）"
    return code[start:end + len("document.body)}")]


def test_suggestion_dropdown_escapes_the_scroll_container() -> None:
    """联想下拉必须 portal 到 body + `position: fixed`。

    `.crowding-alert-scroll` 是 `overflow: auto` —— 就地渲染的下拉会被**裁掉**，
    表现就是"联想弹不出来"（同一个文件里告警阈值编辑面板踩过同一脚）。
    """
    code = _panel_code()
    assert "crowding-filter-pop" in code, "找不到联想下拉的类名"
    block = _dropdown_block(code)
    assert "createPortal(" in block, (
        "联想下拉没有 portal 到 body —— 会被滚动容器裁掉（弹不出来）")
    assert "document.body)}" in block, "portal 的挂载点不是 document.body"
    assert "crowding-filter-pop" in block, "portal 里不是联想下拉"
    # 点候选不能被 onBlur 抢跑：mousedown 必须 preventDefault
    assert "onMouseDown={(event) => event.preventDefault()}" in block, (
        "候选列表没有阻止 mousedown 默认行为 —— 点候选会先失焦、click 还没派发"
        "下拉就关了，用户点了没反应")
    css = (REPO / "web" / "src" / "styles.css").read_text(encoding="utf-8")
    pop_css = css[css.find(".crowding-filter-pop"):]
    assert "position: fixed" in pop_css[:200], (
        "`.crowding-filter-pop` 没有 position: fixed（portal 坐标是视口坐标）")


def test_columns_header_input_is_not_a_datalist() -> None:
    """不许再用原生 `<datalist>` 当联想源。

    原生 datalist 只按 `<option value>`（板块名）匹配：敲全拼 `huagong`
    时**一个候选都不弹**，而"拼音池"正是用户要的东西。留着它还会和
    我们自己的下拉叠成两层浮层。
    """
    code = _panel_code()
    assert "<datalist" not in code, (
        "面板里还留着 <datalist> —— 它只按板块名匹配，敲拼音不弹候选，"
        "而且会和我们自己的下拉叠在一起")


# ------------------------------------------------- ③b 筛选记录要记得住

def test_filter_state_is_seeded_from_cache_and_written_back() -> None:
    """筛选记录：启动时读缓存、变化时写缓存（用户要求"保留在前端缓存里"）。"""
    code = _panel_code()
    assert "readBoardFilter()" in code, "面板没有读筛选记录（每次都从默认视图开始）"
    assert re.search(r"useState\(\(\) => readBoardFilter\(\)\)", code), (
        "缓存的读法必须是 `useState(() => ...)` 惰性初值 —— "
        "直接写成 `useState(readBoardFilter())` 会每帧 parse 一遍 JSON")
    assert "writeBoardFilter({" in code, "面板没有回写筛选记录"
    effect = code[code.find("writeBoardFilter({"):]
    effect = effect[:effect.find("]);")]
    assert "boardFilter" in effect, "写缓存的依赖数组里没有 `boardFilter`"
    # ★ 写进去的**载荷**里也只能有筛选词：deps 只管"什么时候重算"，
    #   把开关塞进载荷才是 2026-10-15 空表事故的真正入口
    #   （突变自证第一次跑时漏的正是这一条）。
    payload = effect[effect.find("writeBoardFilter("):]
    payload = payload[:payload.find(")")]
    assert "onlyAlerts" not in payload and "onlyTriggered" not in payload, (
        f"写缓存的载荷里又出现了视图开关：{payload}")
    # 依赖数组同样只该有那一个字段
    deps = effect[effect.find("}, ["):]
    assert "onlyAlerts" not in deps and "onlyTriggered" not in deps, (
        f"写缓存的依赖数组里又出现了视图开关：{deps}")


def test_cache_remembers_only_the_query_not_the_hiding_switches() -> None:
    """★ 回归守卫（2026-10-15 事故）：缓存里**只能**有筛选词。

    用户报障「高拥挤度告警 这个板块的数据怎么不展示了」。查实：当天
    `alert_on` 对**每一行**都是 `False`（"已触发"= **0** 行），而 v1 把
    「只看告警 / 只看已触发」也记进了缓存 ⇒ 前一天点过「只看已触发」，
    第二天打开面板就是**一张空表**，看着完全像"数据没了"。

    所以这里三条一起守（缺一条都可能让事故复发）：
      ① 缓存**类型**里只有 `q`（写进去的第二类字段迟早会被读回来）；
      ② 缓存模块的**读**函数忽略多余字段（手改 localStorage 也不该能藏数据）；
      ③ 面板**不**拿缓存去初始化那两个开关。
    """
    cache = (REPO / "web" / "src" / "boardFilterCache.ts").read_text(encoding="utf-8")
    body = _strip_comments(cache)
    type_block = body[body.find("export type BoardFilterState"):]
    type_block = type_block[:type_block.find("};")]
    assert "q: string" in type_block, "缓存类型里没有筛选词"
    assert "onlyAlert" not in type_block and "onlyTriggered" not in type_block, (
        f"缓存类型里又出现了会把数据藏光的开关：{type_block}")
    assert re.search(r"return \{ q \};", body), (
        "缓存模块的 sanitize 没有把多余字段丢掉 —— 手改 localStorage / 旧版本残留的"
        "`onlyTriggered: true` 会重新生效，把面板变成空表")
    assert re.search(r"const VERSION = 2", body), (
        "缓存结构版本没有升到 2 —— v1 的旧记录（带开关）会继续被读出来，"
        "已经中招的用户刷新页面也不会恢复")

    panel = _panel_code()
    assert "cachedFilter?.q" in panel, "面板没有用缓存的筛选词初始化输入框"
    assert "cachedFilter?.onlyAlerts" not in panel, (
        "面板又用缓存初始化「只看告警」了 —— 这正是 2026-10-15 空表事故的成因")
    assert "cachedFilter?.onlyTriggered" not in panel, (
        "面板又用缓存初始化「只看已触发」了 —— 当天 alert_on 全为 False 时，"
        "这一条会让面板一打开就是空的")


def test_column_header_survives_an_empty_filter_result() -> None:
    """表头（含筛选框）要在**清单非空**时渲染，而不是"筛完非空"时。

    用 `rows.length > 0` 时，筛选一旦把行筛光（或开关把行藏光），
    表头连同筛选框一起消失 —— 用户只剩一句"没有命中"，却找不到改词的地方。
    """
    code = _panel_code()
    header_at = code.find("crowding-alert-header")
    assert header_at != -1, "找不到表头"
    before = code[max(0, header_at - 400):header_at]
    assert "view.length > 0 && (" in before, (
        "表头的渲染条件不是 `view.length > 0` —— 筛光之后筛选框会一起消失")
    assert "rows.length > 0 && (" not in before, (
        "表头又用 `rows.length > 0` 当条件了：筛光即消失（2026-10-15 的一部分）")


def test_cache_module_is_wired_into_logout() -> None:
    """登出要**调用**清筛选记录（与其它本地缓存一致，"清了哪些"要能读全）。

    ⚠️ 只断言文件名/标识符不够：`await import("../boardFilterCache")` 那行与
    `const { clearBoardFilterCache } = ...` 解构都在，**把调用删掉照样绿**
    （第一版就是这样，突变自证抓出来了）。所以要断言"在 `logout` 里真的调了"。
    """
    auth = (REPO / "web" / "src" / "hooks" / "useAuth.ts").read_text(encoding="utf-8")
    logout_at = auth.find("const logout = useCallback")
    assert logout_at != -1, "找不到 logout 回调"
    body = auth[logout_at:]
    assert re.search(r"clearBoardFilterCache\(\);", body), (
        "`useAuth.logout` 里没有**调用** `clearBoardFilterCache()` —— "
        "换个账号登录还留着上一个人的筛选")
    assert "../boardFilterCache" in body, "清筛选记录时 import 的不是 boardFilterCache 模块"


def test_cache_module_never_throws_and_is_versioned() -> None:
    """缓存模块的三条硬要求：绝不抛 / 有版本 / 结构不对就当没有。"""
    src = (REPO / "web" / "src" / "boardFilterCache.ts").read_text(encoding="utf-8")
    body = _strip_comments(src)
    assert body.count("catch {") >= 3, (
        "读/写/清三处都要自己吞异常（隐私模式、配额满、内容损坏）")
    assert re.search(r"const VERSION = \d+", body), "缓存结构没有版本号"
    assert "parsed?.version !== VERSION" in body, (
        "读到旧结构时没有当'没有记录'处理 —— 升级后会读到形状不对的对象")
    assert "window.localStorage" in body, "没有用 localStorage（活不过一次刷新就不算记住）"


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

def _backend_pinyin_keys() -> set[str]:
    src = _read(BACKEND)
    keys = set(re.findall(r'row\["([a-z_]*pinyin[a-z_]*)"\]', src))
    assert keys, "后端 `_with_pinyin` 一个拼音字段都没写"
    return keys


def test_pinyin_field_names_agree_across_backend_and_frontend() -> None:
    """后端写的 key、TS 类型声明的 key、匹配函数读的 key —— 必须同一批字。

    用户 2026-10-14 追加要求后这里守着**两个**字段（首字母 + 全拼）：
    少守一个，就会出现"后端下了全拼、前端没人读"或反过来的静默失效。
    """
    keys = _backend_pinyin_keys()
    assert keys == {"pinyin_initials", "pinyin_full"}, (
        f"后端写的拼音字段变成 {sorted(keys)}：首字母与全拼两个都要有，"
        "改名/漏写时要同步前端三处（TS 类型 / 匹配 / 拼音提示）")
    api_ts = _read(API_TS)
    search_ts = _read(SEARCH)
    for key in keys:
        assert re.search(rf"^\s*{key}\?:", api_ts, re.M), (
            f"`CrowdingListItem` 里没有声明 `{key}?`（后端下发了、TS 类型不认）")
        assert key in search_ts, (
            f"`boardSearch.ts` 没读 `{key}` —— 后端算了拼音也没人用")


def test_frontend_does_not_reimplement_pinyin() -> None:
    """前端不许自己造一份拼音表（同一判断两份实现必然漂移）。"""
    src = _read(SEARCH)
    code = _strip_comments(src)
    banned = ("pypinyin", "PY_INITIALS", "PINYIN_MAP", "pinyinTable")
    hits = [word for word in banned if word in code]
    assert not hits, (
        f"`boardSearch.ts` 里出现了疑似自建拼音表的东西：{hits}；"
        "首字母与全拼一律由后端 `to_initials()` / `to_full_pinyin()` 下发")


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
    for symbol in ("filterBoards", "matchBoard", "suggestBoards", "pinyinHint"):
        assert symbol in check_src, f"自证脚本没有覆盖 `{symbol}`"
    # ★ 联想候选 ⊆ 筛选结果 这条不变式只能在行为层验（结构判据看不出来），
    #   所以这里守"自证脚本里确实有这条对拍"，防止它被删掉后没人发现。
    assert "不变式" in check_src and "violations" in check_src, (
        "自证脚本里没有「候选集 ⊆ 筛选结果集」的对拍 —— "
        "少了它，联想可能给出点不到的行（比没有联想更糟）")


# ------------------------------------------------- ⑦ 后端补充拼音这两列

def test_with_pinyin_matches_the_shared_implementation() -> None:
    """`_with_pinyin` 两列都必须等于仓库唯一的那个实现，空名字给空串。"""
    from src.api.routes.sector_crowding import _with_pinyin
    from src.quant.stock_directory import to_full_pinyin, to_initials

    rows = [
        {"sector_code": "885959.TI", "sector_name": "化工原料"},
        {"sector_code": "885000.TI", "sector_name": ""},
    ]
    out = _with_pinyin(rows)
    assert out[0]["pinyin_initials"] == to_initials("化工原料")
    assert out[0]["pinyin_full"] == to_full_pinyin("化工原料")
    assert out[0]["pinyin_full"] == "huagongyuanliao", (
        f"全拼不是预期形态：{out[0]['pinyin_full']!r}（应为小写连写、无音节分隔）")
    assert out[0]["pinyin_full"].islower(), (
        "全拼必须小写 —— 前端按小写做包含匹配，大写会让 `huagong` 搜不到")
    assert out[0]["pinyin_full"].isascii(), (
        "全拼里混进了非 ASCII —— 前端敲键盘打不出来")
    for row in out:
        # 前端 `matchBoard` 依赖"空拼音不参与匹配"，所以这里必须是空串而不是缺 key。
        assert "pinyin_initials" in row and "pinyin_full" in row, (
            "有行缺了拼音 key —— 前端读到 `undefined` 会走 `String(...)` 兜底，"
            "虽然不崩，但'缺字段'与'算不出来'就分不开了")
    assert out[1]["pinyin_initials"] == "" and out[1]["pinyin_full"] == "", (
        "空板块名给了非空拼音 —— 前端会因此把不相干的行也筛出来")


def test_full_pinyin_is_what_the_frontend_filter_needs() -> None:
    """把真实板块名走一遍"后端算 → 前端该命中"，堵住"两列都算了但形态对不上"。"""
    from src.api.routes.sector_crowding import _with_pinyin

    out = _with_pinyin([
        {"sector_code": "885800.TI", "sector_name": "半导体材料"},
        {"sector_code": "885633.TI", "sector_name": "染料"},
    ])
    ban = out[0]
    # 前端 `matchBoard` 的判据是"查询串（小写化）是字段（小写化）的子串"
    # （`field.includes(token)`，两边都先过 `strip()`）。所以这里也**必须**小写化
    # —— 第一版忘了，于是 `bdtcl` 对着大写 "BDTCL" 判成不命中，是**判据自己错**
    # （首字母在库里就是大写的），不是实现错。
    lowered = {k: str(v).lower() for k, v in ban.items()}
    for typed in ("bandao", "bdtcl", "ban", "cailiao", "半导体"):
        probe = typed.lower()
        field_hit = (probe in lowered["pinyin_full"]
                     or probe in lowered["pinyin_initials"]
                     or probe in lowered["sector_name"])
        assert field_hit, f"敲 {typed!r} 按四路匹配一个字段都命不中：{ban}"
    assert lowered["pinyin_full"].startswith("bandao"), (
        "全拼必须以名称首个字的拼音开头 —— 否则'敲一半'的联想顺序会乱")
