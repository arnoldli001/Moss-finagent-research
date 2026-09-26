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
    """两个面板不能再出现在三元链里 —— 否则与保活副本双份渲染。"""
    part = _ternary_part()
    for comp in ("<IntelPanel", "<AlertsPanel"):
        assert comp not in part, (
            f"{comp} 仍在三元链里渲染 —— 会与保活副本同时挂载，出现双份面板")


def test_ternary_returns_null_for_keepalive_views():
    """三元链对这三个视图必须返回 `null`（内容已交给保活层）。"""
    part = _ternary_part()
    assert 'view === "intel-hot"' in part and 'view === "alerts"' in part, (
        "三元链里没有对 intel/alerts 视图的显式分支 —— 会落到未知视图兜底")
    idx = part.find('view === "alerts"')
    tail = part[idx:idx + 300]
    assert re.search(r"\?\s*\(\s*null", tail), (
        "intel/alerts 分支应返回 null（内容由 KeepAlive 承载）")


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
