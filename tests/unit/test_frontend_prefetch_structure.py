"""前端「保活预取」结构的回归守卫（2026-09-26 用户报障，第二次）。

## 报障原话

> "热点&研报小作文、事件告警 每次打开这个界面不能预加载到浏览器吗，
>   切界面还是会有 2 秒延迟，数据可以在本地服务器后台处理，界面打开的时候
>   应该立即能加载到或者提前把数据放到前端服务器。"

## 这两页为什么慢（两层原因，缺一不可）

1. **缓存是冷的** —— 预加载原来只写在 `useAuth.login()` 里；
   而绝大多数访问是**带 remember-me Cookie 刷新页面**，
   走 `probe()` 路径，**从不预取**。加上缓存有 TTL（告警 5 分钟 /
   情报 10 分钟），放着就过期。
2. **组件被卸载重挂** —— `App.tsx` 用三元链渲染视图，切走即卸载，
   切回来 `useState` 全归零（连展开的卡片、滚动位置都丢）。

修复分别是 `web/src/panelPrefetch.ts`（保活续期）与
`web/src/components/KeepAlive.tsx`（保持挂载）。

## 这些守卫守什么

前端没有单测框架（仓库只有 `tsc` + `vite build`），所以这里用**源码结构断言**
守住几个"一旦写错就静默退化"的不变式 —— 它们不会报错，
只会让延迟悄悄回来，靠人工 review 很难发现：

1. `KeepAlive` **不能**被放进三元链（否则它自己被卸载，保活失效）；
2. 那两个面板**不能**再出现在三元链里（否则双份渲染）；
3. 保活续期**必须**真的强制刷新（`force=true`），否则缓存永远停在第一次；
4. 续期间隔**必须小于**最短的缓存 TTL（否则续期之间会有一段冷窗口）；
5. 登录与刷新**两条**进入路径都要触发预取。
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web" / "src"


def _read(rel: str) -> str:
    p = WEB / rel
    assert p.is_file(), f"前端源码不存在：{p}"
    return p.read_text(encoding="utf-8")


# ---------------------------------------------------------------- 结构不变式

def _strip_comments(src: str) -> str:
    """去掉 `/* ... */` 与 `// ...` 注释。

    ⚠️ **必须去注释再断言**：这段三元链里就有解释"为什么 `KeepAlive`
    不能放进来"的注释，直接把源码当断言对象会**把说明文字当成代码**误报。
    这类"注释里提到某个标识符"的误报很容易让人以为测试坏了而删掉测试 ——
    那正好丢掉了守卫。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def _ternary_part() -> str:
    """主 ErrorBoundary 里那条三元链的源码片段（**已去注释**）。"""
    src = _read("App.tsx")
    start = src.find('{view === "admin" && auth.user ?')
    assert start != -1, "找不到主三元链起点"
    end = src.find("未知视图", start)
    assert end != -1, "找不到三元链的未知视图兜底分支"
    close = src.find("</div>", end)
    assert close != -1, "找不到兜底分支的结束标签"
    close = src.find(")}", close)
    assert close != -1, "找不到三元链终点"
    return _strip_comments(src[start:close + 2])


def test_keep_alive_is_not_mounted_inside_the_ternary_chain():
    """`KeepAlive` 不能在三元链里**被渲染** —— 那会让它自己被卸载，保活失效。

    这是最容易写错的一处：直觉上会写成
    `{view === "alerts" ? <KeepAlive active><AlertsPanel/></KeepAlive> : ...}`，
    看起来对，实际上 `KeepAlive` 的 `everActive` 会随卸载一起归零。
    """
    part = _ternary_part()
    assert "<KeepAlive" not in part, (
        "<KeepAlive> 出现在主三元链里 —— 它会被一起卸载，保活失效。"
        "必须放在三元链之外、且自身始终挂载。")


def test_panels_are_not_rendered_in_the_ternary_chain():
    """保活面板不能再出现在三元链里 —— 否则与保活副本双份渲染。"""
    part = _ternary_part()
    for comp in ("<IntelPanel", "<AlertsPanel", "<FundFlowPanel"):
        assert comp not in part, (
            f"{comp} 仍在三元链里渲染 —— 会与保活副本同时挂载，出现双份面板")


def test_ternary_returns_null_for_keepalive_views():
    """三元链对这四个视图必须返回 `null`（内容已交给保活层）。"""
    part = _ternary_part()
    for key in ("intel-hot", "alerts", "fundflow"):
        assert f'view === "{key}"' in part, (
            f"三元链里没有对 {key} 视图的显式分支 —— 会落到未知视图兜底")
    idx = part.find('view === "alerts"')
    tail = part[idx:idx + 300]
    assert re.search(r"\?\s*\(\s*null", tail), (
        "intel/alerts/fundflow 分支应返回 null（内容由 KeepAlive 承载）")


# ------------------------------------------------- 视图键三处一致（2026-09-26 报障）

def _view_keys() -> list[str]:
    """`useState` 里那个 `view` 联合类型的全部视图键。"""
    src = _read("App.tsx")
    m = re.search(r"useState<\s*([^>]*?)\s*>\s*\(\s*\"", src, re.S)
    assert m, "找不到 `view` 的 useState 联合类型"
    keys = re.findall(r'"([A-Za-z0-9_-]+)"', m.group(1))
    assert len(keys) >= 10, f"解析出来的视图键太少（{keys}），正则可能失效了"
    return keys


def _hash_view_keys() -> list[str]:
    """`HASH_VIEWS`（可写进 URL 的白名单）。"""
    src = _read("App.tsx")
    m = re.search(r"const HASH_VIEWS = \[(.*?)\]\s*as const", src, re.S)
    assert m, "找不到 HASH_VIEWS"
    return re.findall(r'"([A-Za-z0-9_-]+)"', m.group(1))


def test_every_view_key_has_an_explicit_branch():
    """每个视图键都必须有显式分支 —— 漏一个就静默落到"未知视图"红框。

    用户报障 2026-09-26：「未知视图：research（请刷新页面；若持续出现请反馈）」。
    「投研分析」正是漏掉的那个：它的内容单独挂在三元链**之外**
    （`{view === "research" && (<>…</>)}`），链里没有它的分支，
    于是**每次打开这一页**顶上都会多一行红框 —— 功能其实正常，纯噪声，
    所以谁都没在 review 里看出来。现在它已经搬进链里，这条守卫防它再被搬出去。
    """
    part = _ternary_part()
    missing = [k for k in _view_keys() if f'view === "{k}"' not in part]
    assert not missing, (
        f"这些视图键在三元链里没有分支：{missing} —— 会落到「未知视图：…」兜底。"
        "要么在链里补分支；要么（确实由链外承载时）按 intel/alerts 的写法"
        "显式返回 null 并写清理由。")


def test_hash_whitelist_matches_view_keys():
    """`HASH_VIEWS` 与 `view` 联合类型必须一一对应（多了少了都是 bug）。

    实测踩到过两种方向：
      · **多**：`"mypools"`（「我的自选池」2026-09-23 已删除）留在白名单里，
        旧书签 `#view=mypools` 会通过校验、切到不存在的视图，页面上出现
        "未知视图：mypools"红框 —— 白名单本来就是为了避免这个；
      · **少**：新加的页签忘了进白名单 → 刷新/发链接回不到那一页（静默退化）。
    """
    keys, hash_keys = set(_view_keys()), set(_hash_view_keys())
    assert not (hash_keys - keys), (
        f"HASH_VIEWS 里有 view 联合类型中不存在的键：{sorted(hash_keys - keys)} —— "
        "旧链接会切到不存在的视图（未知视图红框）")
    assert not (keys - hash_keys), (
        f"这些视图键不在 HASH_VIEWS 里：{sorted(keys - hash_keys)} —— "
        "该页签刷新后会掉回默认页")


def test_unknown_view_fallback_is_last_resort():
    """兜底分支必须还在链尾 —— 它是"脏 hash 别白屏"的最后一道，不能删。"""
    src = _read("App.tsx")
    assert "未知视图" in src, "「未知视图」兜底分支被删了 —— 脏 hash 会白屏"
    part = _ternary_part()
    assert part.rstrip().endswith(")}"), "三元链结构变了，兜底分支可能不在链尾"


def test_keep_alive_lazily_mounts():
    """`KeepAlive` 必须惰性挂载 —— 从没打开过的页面不该付构建/取数成本。"""
    src = _read("components/KeepAlive.tsx")
    assert "everActive" in src, "缺少 everActive 惰性挂载标记"
    assert re.search(r"if\s*\(!everActive\.current\)\s*return null", src), (
        "KeepAlive 必须在 everActive 为假时 return null（惰性挂载）")
    assert "display" in src and "none" in src, (
        "隐藏必须用 display:none —— 条件渲染会卸载，等于没做保活")


def test_keep_alive_hides_from_assistive_tech():
    """隐藏的面板要对读屏器隐藏，否则辅助技术仍会读到不可见内容。"""
    src = _read("components/KeepAlive.tsx")
    assert "aria-hidden" in src, "隐藏时缺少 aria-hidden"


# ---------------------------------------------------------------- 预取不变式

def test_keepalive_interval_is_shorter_than_shortest_cache_ttl():
    """续期间隔必须**小于**最短的缓存 TTL，否则中间会出现冷窗口。

    告警缓存 TTL 5 分钟、情报 10 分钟（各自模块的 `MAX_AGE_MS`）。
    续期若 ≥ 5 分钟，用户在这段空档点开就是一次完整的隧道往返 = 2 秒。
    """
    pf = _read("panelPrefetch.ts")
    alerts = _read("alertsCache.ts")

    m = re.search(r"KEEPALIVE_MS\s*=\s*([0-9_*\s]+?);", pf)
    assert m, "panelPrefetch.ts 里找不到 KEEPALIVE_MS"
    keepalive = eval(m.group(1).replace("_", ""))  # noqa: S307 常量表达式

    m2 = re.search(r"MAX_AGE_MS\s*=\s*([0-9_*\s]+?);", alerts)
    assert m2, "alertsCache.ts 里找不到 MAX_AGE_MS"
    alerts_ttl = eval(m2.group(1).replace("_", ""))  # noqa: S307

    assert keepalive < alerts_ttl, (
        f"续期间隔 {keepalive}ms 不小于告警缓存 TTL {alerts_ttl}ms —— "
        "续期之间会出现缓存已过期、面板还是冷的窗口")


def test_keepalive_uses_force_refresh():
    """续期必须 `force=true` 绕过新鲜度去重，否则缓存永远停在第一次。"""
    src = _read("panelPrefetch.ts")
    assert re.search(r"prefetchPanels\(true\)", src), (
        "保活续期没有用 force=true —— 会被 PRELOAD_FRESH_MS 判定『还新鲜』"
        "而直接返回，缓存永远不更新，TTL 一到又变冷")
    # alertsCache 必须支持 force 并真的跳过新鲜度检查
    ac = _read("alertsCache.ts")
    assert "force" in ac, "preloadAlerts 不支持 force 参数"


def test_both_entry_paths_prefetch():
    """**登录**与**刷新（已有会话）**两条路径都必须触发预取。

    这是本次报障的根因：预取原来只在 `login()` 里，
    刷新页面走的是 `probe()`，那条路径从不预取 —— 于是每次刷新后
    第一次点面板必然等一个完整往返。
    """
    app = _read("App.tsx")
    assert "startPanelKeepAlive" in app, (
        "App.tsx 没有启动保活预取 —— 刷新页面这条路径不会预取")
    # 触发点必须在登录后的组件里（不是 login 动作里）
    assert re.search(r"useEffect\(\(\)\s*=>\s*\{[^}]*startPanelKeepAlive", app, re.S), (
        "保活预取必须挂在 useEffect（组件挂载即触发），而不是只在登录动作里")

    auth = _read("hooks/useAuth.ts")
    assert "prefetchPanels" in auth, (
        "登录路径没有预取；登录后应立刻取一次（用户马上就会用到）")


def test_prefetch_never_throws():
    """预取失败只等于"回到优化前的行为"，绝不能把错误抛给调用方。"""
    src = _read("panelPrefetch.ts")
    assert "allSettled" in src, (
        "两个目标的预取必须用 allSettled 互相隔离 —— "
        "一个失败不该拖累另一个")


def test_prefetch_static_imports_shared_caches():
    """共享缓存模块必须**静态**导入。

    动态导入一个已经被静态导入的模块不会真的分包，只会让 Vite 报
    "dynamically imported but also statically imported" 警告 ——
    是"看起来做了代码分割、其实没有"的假象。
    """
    src = _read("panelPrefetch.ts")
    for mod in ("alertsCache", "intelCache", "intelApi"):
        assert not re.search(rf'import\(\s*"\./{mod}"\s*\)', src), (
            f"{mod} 被动态导入 —— 它已被面板静态导入，应改为静态 import")


def test_logout_resets_prefetch_state():
    """登出必须重置预取记账，否则下一个人登录后保活会以为"刚取过"而跳过。"""
    src = _read("hooks/useAuth.ts")
    assert "resetPrefetchState" in src, "登出没有重置预取记账"


# ------------------------------------------------- 主线挖掘快照缓存（2026-10-08 报障）
#
# > "为什么每次打开 主线挖掘，热加载 切界面也要等 2-3 秒？"
#
# 量出来的账：**服务端 p50 = 19 ms**（`data/pilot/access_audit`，今天 27 次），
# 但这条公网链路上**每次请求固定 ~1.0~1.2 s**（64 字节的 `/health/live`
# 也要 1.03~1.74 s），而 `MainlinePanel` 在三元链里、切走即卸载
# ⇒ 没有客户端缓存就是"每切一次等一趟往返"。
#
# 修法与 alerts/intel 逐条对齐（**不自造一套**，见 AGENTS.md《性能硬约束》）：
# `mainlineCache.ts` 提供 localStorage 缓存，面板首帧先画、请求照发，
# 保活预取负责续期。下面守的就是"改错任何一处都会静默退回冷路径"的几处。

def _exported_number(src: str, name: str) -> int:
    """从 TS 源码里读一个形如 `export const X = 10 * 60 * 1000;` 的常量。"""
    m = re.search(rf"\b{name}\s*=\s*([0-9_*\s]+?);", src)
    assert m, f"{name} 找不到（常量名被改了？）"
    return int(eval(m.group(1).replace("_", "")))  # noqa: S307 常量表达式


def test_mainline_cache_ttl_outlives_keepalive():
    """主线缓存的 TTL 必须**大于**保活续期间隔，否则续期之间是冷窗口。

    这一条是 `test_keepalive_interval_is_shorter_than_shortest_cache_ttl`
    的同类判据 —— 加一个缓存就要加一条，否则"最短的那个 TTL"会悄悄换人。
    """
    keepalive = _exported_number(_read("panelPrefetch.ts"), "KEEPALIVE_MS")
    ttl = _exported_number(_read("mainlineCache.ts"), "MAINLINE_MAX_AGE_MS")
    assert ttl > keepalive, (
        f"主线缓存 TTL {ttl}ms 不大于保活续期 {keepalive}ms —— "
        "续期之间会出现缓存已过期、面板又是冷的窗口（用户又要等一趟往返）")


def test_prefetch_covers_mainline_snapshot():
    """保活预取必须**覆盖主线快照** —— 只做缓存不做续期，TTL 一到又是冷的。

    同时守两件事：目标进了 `allSettled` 列表（失败互不拖累）、
    登出记账里也有它（否则下一个人登录时会被判"刚取过"而跳过）。
    """
    src = _read("panelPrefetch.ts")
    assert "prefetchMainline" in src, "panelPrefetch 没有预取主线快照"
    m = re.search(r"prefetchPanels[\s\S]*?allSettled\(\[([\s\S]*?)\]\)", src)
    assert m, "找不到 prefetchPanels 的 allSettled 列表"
    assert "prefetchMainline(force)" in m.group(1), (
        "prefetchMainline 没有进 allSettled 列表 —— 不会被登录/刷新/续期触发")
    reset = src[src.find("export function resetPrefetchState"):]
    assert "lastDone.mainline" in reset[:400], (
        "resetPrefetchState 没有重置 mainline 的记账 —— "
        "换个人登录后第一次预取会被跳过")


def test_mainline_prefetch_uses_the_shared_params():
    """预取**不许自己写死** `top` / `alert_limit`。

    后端 `routes/mainline.py::WARM_TOP/WARM_ALERT_LIMIT` 决定了落盘热快照
    的缓存键指纹；前端一旦漂移，那份热快照就**永远命中不了**，
    而这个失效不报错 —— 只表现为"第一个打开面板的人等一次全市场重算"。
    本项目为同一形状的事故付过代价（见
    `tests/unit/test_api_no_loop_blocking.py::test_warm_uses_the_same_cache_key_as_the_route`）。
    """
    src = _strip_comments(_read("panelPrefetch.ts"))
    assert "mainlineSnapshotParams()" in src, (
        "预取没有走 mainlineSnapshotParams() —— 参数会有第二份来源")
    assert not re.search(r"snapshot\(\s*\{\s*top\s*:", src), (
        "预取里写死了 top —— 必须走 mainlineSnapshotParams()")


def test_mainline_panel_paints_from_cache_before_the_network():
    """面板首帧必须**先画缓存**，且缓存命中时不许再显示加载态。

    两处最容易写错的：
      ① 用 `useEffect` 补一刀而不是 `useState` 初值 ⇒ 第一帧仍会闪
         「正在读取评分…」，用户看到的等待没变；
      ② 缓存命中却仍走非静默分支 ⇒ `loading` 置真，刚画出来的内容
         被加载态盖掉，等于没做。
    """
    panel = _strip_comments(_read("components/MainlinePanel.tsx"))
    assert "readMainlineSnapshot()" in panel, "面板没有读本地缓存"
    # ① 初值里读（不是 effect 里补）
    m = re.search(r"useState<MainlineSnapshot \| null>\(\s*\(\)\s*=>"
                  r"\s*readMainlineSnapshot\(\)", panel)
    assert m, (
        "缓存必须在 `useState` 的惰性初值里读 —— 放 effect 里第一帧仍会闪加载态")
    # ② 取到新的要写回缓存
    assert "writeMainlineSnapshot(" in panel, "面板取到快照后没有写回缓存"
    # ③ 缓存命中时第一次核对必须是静默的
    assert re.search(r"loadSnapshot\(seededRef\.current\)", panel), (
        "缓存命中后第一次核对没有走静默分支 —— 加载态会盖掉刚画出来的内容")


def test_mainline_params_match_the_backend_warm_key():
    """前端请求参数与后端热快照指纹必须**逐项一致**（跨语言契约）。

    这是唯一一处"两边各写一份数字、且没法自动同步"的地方 ——
    所以判据直接跨语言比对。后端那侧见
    `routes/mainline.py` 的 `WARM_TOP` / `WARM_ALERT_LIMIT`。
    """
    ts = _read("mainlineCache.ts")
    py = (Path(__file__).resolve().parents[2]
          / "src" / "api" / "routes" / "mainline.py").read_text(encoding="utf-8")

    def py_const(name: str) -> int:
        m = re.search(rf"^{name}\s*=\s*([0-9]+)\s*$", py, re.M)
        assert m, f"后端找不到 {name}"
        return int(m.group(1))

    assert _exported_number(ts, "MAINLINE_TOP") == py_const("WARM_TOP"), (
        "前端 top 与后端 WARM_TOP 不一致 —— 落盘热快照会永远命中不了（不报错）")
    assert _exported_number(ts, "MAINLINE_ALERT_LIMIT") \
        == py_const("WARM_ALERT_LIMIT"), (
        "前端 alert_limit 与后端 WARM_ALERT_LIMIT 不一致 —— 同上")


def test_logout_clears_mainline_cache():
    """登出必须清主线缓存：里面是**评分与告警**，换个人不该看到。"""
    src = _read("hooks/useAuth.ts")
    assert "clearMainlineCache" in src, "登出没有清主线挖掘缓存"
