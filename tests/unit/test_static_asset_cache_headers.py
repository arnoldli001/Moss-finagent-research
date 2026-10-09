"""静态资源的缓存策略：**入口禁缓存 + 指纹资源永久缓存**（`CHG-0178`）。

## 为什么值得单独一个文件（2026-10-08 "主线挖掘要等 2-3 秒" 那一轮）

这条公网链路上**每次请求固定 ~1.0~1.2 s**（64 字节的 `/health/live`
实测 1.03~1.74 s，见 `web/src/mainlineCache.ts` 的实测账）。而
Starlette 的 `StaticFiles` **根本不发 `Cache-Control`** —— 浏览器只能用
启发式新鲜度（≈ `(Date - Last-Modified) * 10%`）：

    一份 8 小时前的构建  →  只有约 48 分钟的新鲜期
    ⇒ 之后每次加载都要为**每个**资源回源校验一次，每次一趟公网往返

给**文件名带内容指纹**的资源加 `immutable` 之后，这一趟**必然为 0**。

## 但"加缓存头"这件事有两个方向都能出错，所以两个方向都要判

| 写错的方式 | 用户看到 |
|---|---|
| 给**没指纹**的文件发 `immutable` | **永远**看不到更新（比不缓存更糟） |
| 忘了禁入口 HTML 的缓存 | 新构建发布后客户刷新也看不到 |
| 判据写成"路径以 `/assets/` 开头" | 等价于第一种（`/assets/` 下可能放着没指纹的东西） |
| 判据写成 `{8}` 精确位数 | 换了哈希长度后**静默失效**，回到每次回源 |

所以这里既有**纯函数的行为判据**（喂输入看输出），也有**真实 HTTP 判据**
（打真产物），而不是"源码里有这个字串"。
"""

from __future__ import annotations

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
MAIN = ROOT / "src" / "api" / "main.py"

JS = "application/javascript"
CSS = "text/css"
HTML = "text/html; charset=utf-8"
SVG = "image/svg+xml"


# ======================================================================
# 一、纯函数：`(路径, Content-Type)` → 该发哪个头
# ======================================================================

@pytest.mark.parametrize("path", [
    "/assets/index-B19sP6Zv.js",
    "/assets/MainlinePanel-PHmZiqkf.js",
    "/assets/index-AJDYqH_B.css",       # 指纹里带下划线（base64url）
    "/assets/panel-9f8e7d6c5b4a.js",    # 更长的指纹
])
def test_fingerprinted_assets_are_immutable(path: str) -> None:
    """带内容指纹的构建产物 → 一年 + `immutable`（连校验请求都不发）。"""
    from src.api.main import ASSET_CACHE_CONTROL, static_cache_control

    assert static_cache_control(path, JS if path.endswith("js") else CSS) \
        == ASSET_CACHE_CONTROL
    assert "immutable" in ASSET_CACHE_CONTROL


@pytest.mark.parametrize("path", [
    "/",
    "/index.html",
    "/mainline",          # SPA 的任意前端路由，回的都是 index.html
])
def test_entry_html_is_never_cached(path: str) -> None:
    """入口 HTML 必须每次回源 —— 否则新构建发布了客户也看不到。"""
    from src.api.main import HTML_CACHE_CONTROL, static_cache_control

    assert static_cache_control(path, HTML) == HTML_CACHE_CONTROL


@pytest.mark.parametrize("path", [
    "/assets/logo.svg",         # 没指纹：改了内容文件名不变
    "/assets/notHashed.js",
    "/assets/app.js",
    "/favicon.svg",
    "/api/v1/health/live",      # 接口不该被静态策略碰到
    "/assets/deep/nested-x1y2z3w4.js",   # 子目录里的资源不由本判据负责
])
def test_unfingerprinted_paths_get_no_policy(path: str) -> None:
    """**反向判据**：没有指纹的文件不许拿到 `immutable`。

    给它发 `immutable` 意味着"改了内容浏览器也不来问" —— 用户会**永远**
    停在旧版，而日志里一个字都不会有。
    """
    from src.api.main import static_cache_control

    assert static_cache_control(path, JS) is None


def test_html_wins_over_asset_looking_path() -> None:
    """判定顺序：**先 HTML**。

    `index.html` 万一被放到 `/assets/` 下（或某个没指纹的名字恰好匹配），
    也必须按 HTML 走 `no-cache` —— 顺序反了就是"客户永远看不到新构建"。
    """
    from src.api.main import HTML_CACHE_CONTROL, static_cache_control

    assert static_cache_control("/assets/index-a1b2c3d4.html", HTML) \
        == HTML_CACHE_CONTROL


def test_hash_length_is_not_pinned() -> None:
    """指纹长度判据必须是**下界**，不能写死（写死 `{8}` 会静默失效）。

    失效形态：换了哈希长度后所有资源都拿不到 `immutable`，
    又回到"每次加载回源校验" —— 不报错，只是慢回来。
    """
    from src.api.main import ASSET_CACHE_CONTROL, static_cache_control

    for digest in ("a1b2c3d4", "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6"):
        assert static_cache_control(f"/assets/x-{digest}.js", JS) \
            == ASSET_CACHE_CONTROL


# ======================================================================
# 二、接线：中间件必须真的用这个判据（否则纯函数再对也不生效）
# ======================================================================

def test_middleware_uses_the_shared_decision() -> None:
    """`no_cache_html` 必须调 `static_cache_control`，不许自己再写一份 if。

    "同一个判断两份实现，必有一份被漏改" —— 本项目实测过的形状
    （见 `AGENTS.md`：《护栏保真与效果验证硬约束》）。
    """
    import ast

    tree = ast.parse(MAIN.read_text(encoding="utf-8"))
    funcs = {n.name: n for n in ast.walk(tree)
             if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    assert "no_cache_html" in funcs, "找不到 no_cache_html 中间件"
    called = {n.func.id for n in ast.walk(funcs["no_cache_html"])
              if isinstance(n, ast.Call) and isinstance(n.func, ast.Name)}
    assert "static_cache_control" in called, (
        "中间件没有调用 static_cache_control —— 判据与实现会各走各的")
    # 反向：中间件自己不该再判一次 HTML
    src = ast.get_source_segment(
        MAIN.read_text(encoding="utf-8"), funcs["no_cache_html"]) or ""
    assert "text/html" not in src, (
        "中间件里又写了一份 text/html 判断 —— 两处判据必然漂移")


# ======================================================================
# 三、真实 HTTP：打真产物（`web/dist` 不在时**显式跳过**，不静默放行）
# ======================================================================

def _hashed_asset() -> str | None:
    """从**真实构建产物**里挑一个带指纹的资源路径。"""
    from src.api.main import _dist

    assets = Path(_dist) / "assets"
    if not assets.is_dir():
        return None
    for item in sorted(assets.iterdir()):
        if item.is_file() and item.suffix in {".js", ".css"}:
            return f"/assets/{item.name}"
    return None


def test_real_build_artifacts_carry_the_right_headers() -> None:
    """真实路径 smoke：入口 `no-cache`、指纹资源 `immutable`。"""
    from fastapi.testclient import TestClient

    from src.api.main import _dist, app

    if not Path(_dist).is_dir():
        pytest.skip(f"前端构建产物不在（{_dist}）—— 本判据只在有产物时有意义；"
                    "先跑 `manage.py build`")
    asset = _hashed_asset()
    if asset is None:
        pytest.skip(f"{_dist}/assets 里没有 .js/.css —— 产物可能只构建了一半")

    client = TestClient(app)

    entry = client.get("/")
    assert entry.status_code == 200, entry.text[:200]
    assert entry.headers.get("cache-control") == "no-cache", (
        "入口 HTML 必须每次回源：`Cache-Control` 应为 no-cache，"
        f"实际 {entry.headers.get('cache-control')!r}")

    resp = client.get(asset)
    assert resp.status_code == 200, f"{asset} 取不到：{resp.status_code}"
    assert "immutable" in (resp.headers.get("cache-control") or ""), (
        f"指纹资源 {asset} 没有拿到 immutable —— 每次加载都会为它多跑一趟公网往返")
