"""拥挤度页签「条件轮询 + SWR」的回归守卫（2026-09-27）。

## 报障原话

> "行业轮动日报、板块拥挤度 这两个界面来回切换，每次加载内容就延迟1-2秒"

`KeepAlive` 那一步（互切不卸载）已经在 `FundFlowPanel.tsx` 里落地、实测互切
0.03~0.08 s。但**体感延迟并没有跟着消失**，因为还有两条独立的漏水点：

1. **轮询在隐藏时照打**。本页签进 KeepAlive 后常驻，60 s 轮询继续跑；
   而原来的守卫是 `document.hidden` —— 它只知道"浏览器标签页有没有被切走"，
   不知道"**应用内**另一个子页签把我 `display:none` 了"。实测后端
   `config_list` 被打了 614 次，用户那两个 IP 每分钟各一条，
   包括没在看这个页签的时候。
2. **`reload()` 清空数据**。原写法每次 `setLoading(true)` 且不保留旧
   `payload`，于是轮询一触发表格就闪"读取中…"，切回来正好撞上就是一张空表 ——
   用户感知仍然是 1~2 秒（其实数据早就在手里了）。

## 这两个守卫守什么

前端没有单测框架（仓库只有 `tsc` + `vite build`），所以用**源码结构断言**
守住这两条 —— 它们退化时**不会报错**，只会让延迟悄悄回来：

1. 轮询的可见性判据**必须**是"渲染盒"（`offsetParent`），不能退回
   `document.hidden`；
2. `reload()` **必须**保留旧数据（stale-while-revalidate），不能无条件
   `setLoading(true)` 且把 payload 置空。

⚠️ 与 `test_frontend_prefetch_structure.py` 同理：断言前**先去掉注释**，
否则说明文字里的 `document.hidden` 会把注释当成代码误报。
"""

from __future__ import annotations

import re
from pathlib import Path

WEB = Path(__file__).resolve().parents[2] / "web" / "src"


def _read(rel: str) -> str:
    p = WEB / rel
    assert p.is_file(), f"前端源码不存在：{p}"
    return p.read_text(encoding="utf-8")


def _strip_comments(src: str) -> str:
    """去掉 `/* ... */`、`// ...` 与 docstring 之外的注释。

    必须去注释再断言 —— 本文件里有大量解释"为什么不能用 document.hidden"
    的说明文字，直接把源码当断言对象会把说明当成代码。
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


# ------------------------------------------------------- ① 条件轮询（可见性）

def test_crowding_poll_uses_rendered_box_not_document_hidden() -> None:
    """轮询必须按"自己这一栏是否可见"决定，而不是 `document.hidden`。

    判据用 `offsetParent`（渲染盒）：祖先 `display:none` 时子元素自身
    仍然是 block，用 `getComputedStyle().display` 会误判为可见
    （见 frontend-change-guardrails 第六节）。
    """
    src = _strip_comments(_read("components/SectorCrowdingTab.tsx"))

    assert "offsetParent" in src, (
        "SectorCrowdingTab 的可见性判据里必须用 offsetParent（渲染盒）。\n"
        "KeepAlive 用 display:none 隐藏子树，document.hidden 对它无效。")

    # 轮询 effect 里必须调用 isTabVisible，而不是直接读 document.hidden
    poll = re.search(r"setInterval\((.*?),\s*POLL_MS\)", src, flags=re.S)
    assert poll, "找不到拥挤度的 POLL_MS 轮询"
    body = poll.group(1)
    assert "isTabVisible" in body, (
        "轮询回调里必须走 isTabVisible(...)；直接读 document.hidden 会在"
        "用户切到别的子页签时继续打隧道（实测每分钟一条）。")
    assert "document.hidden" not in body, (
        "轮询回调里不要再直接用 document.hidden —— KeepAlive 的隐藏它看不见。")


def test_crowding_tab_root_has_ref_for_visibility_probe() -> None:
    """根节点必须挂 ref，否则 `isTabVisible` 拿不到可判定的元素。"""
    src = _strip_comments(_read("components/SectorCrowdingTab.tsx"))
    assert re.search(r'className="crowding-root"\s+ref=\{rootRef\}', src), (
        '`.crowding-root` 上必须挂 `ref={rootRef}`，'
        "isTabVisible 靠它判断 KeepAlive 有没有把自己藏起来。")


# ------------------------------------------------------------- ② SWR 语义

def test_crowding_reload_keeps_existing_payload() -> None:
    """`reload()` 必须保留旧数据：已有 payload 时**不许**进 loading 态。

    这是"切回来闪空表"的根因 —— 数据本来就在手里，却被 `setLoading(true)`
    + 清空 payload 变成了空白。
    """
    src = _strip_comments(_read("components/useCrowdingList.ts"))

    # 必须存在"有数据就走 refreshing、没数据才 loading"的分支
    assert "setRefreshing(true)" in src, (
        "useCrowdingList 必须区分 refreshing（后台核对）与 loading（首次加载）。")
    assert re.search(r"if\s*\(\s*hasData\s*\)\s*setRefreshing\(true\)", src), (
        "reload() 必须写成 `if (hasData) setRefreshing(true); else setLoading(true);`\n"
        "—— 已有数据时进 refreshing，界面继续画旧数据。")
    assert re.search(r"else\s+setLoading\(true\)", src), (
        "没有数据时才允许进 loading（首次加载）。")

    # payload 不能被无条件清空
    assert not re.search(r"setPayload\(\s*null\s*\)", src), (
        "reload() 里不许 setPayload(null) —— 那正是『闪空表』的写法。")


def test_crowding_reload_guards_stale_responses() -> None:
    """并发 reload（快速连点「只看概念板块」）必须丢弃过期响应。

    后发的可能先回；没有序号守卫时，先发那个的 finally 会把状态清掉，
    表现为"按钮提前恢复、数据却是旧的"。
    """
    src = _strip_comments(_read("components/useCrowdingList.ts"))
    assert "reqRef" in src, (
        "useCrowdingList 必须有在飞请求序号（reqRef）守卫过期响应。")
    assert re.search(r"seq\s*!==\s*reqRef\.current", src), (
        "reload() 必须在 setPayload/setError 前比对 `seq !== reqRef.current`，"
        "把被取代的响应丢弃。")


def test_crowding_controller_exposes_refreshing() -> None:
    """控制器必须把 `refreshing` 暴露出去，否则按钮没有可用的状态。"""
    src = _strip_comments(_read("components/useCrowdingList.ts"))
    assert re.search(r"refreshing:\s*boolean", src), (
        "CrowdingListController 类型里必须有 `refreshing: boolean`。")
    # 返回对象里也要有
    ret = src[src.rfind("return {"):]
    assert re.search(r"^\s*refreshing,\s*$", ret, flags=re.M), (
        "返回值里必须带上 refreshing。")
