"""免鉴权白名单 / 运维文档 / Dockerfile 里的**路径字符串**，必须真有路由在服务它（`CHG-0128`）。

## 为什么需要这条判据

"探针路径"在这个仓库里写在 **4 个地方**：

1. `src/api/tenancy_middleware.py::_PUBLIC_PATHS`（免鉴权）
2. `src/api/login_gate.py::PUBLIC_EXACT`（免登录）
3. `Dockerfile` 的 `HEALTHCHECK`
4. `docs/*.md` 里运维/照着敲的命令

**此前没有任何东西把它们与真实路由表对过一遍。** 而在字符串层面，
"写在一个 `frozenset` 里"与"有一条路由在服务它"**长得一模一样** ——
这正是本项目反复记录的那类失效：*名字被写进代码/文档之后，就获得了"它是真的"的外观*。

2026-09-30 实测后果（`CHG-0128`，全部是一次核对里查出来的）：

* `_PUBLIC_PATHS` 的 5 条里 **3 条没有路由**：`/api/v1/metrics/health`、
  `/api/v1/metrics/ready`、`/healthz`。后两条的注释还写着"既有公开探针，
  保持兼容"——**"既有"不成立**，它们只存在于
  `docs/PLATFORM_MULTI_TENANCY_DESIGN.md` 的**计划**里，是从计划抄进代码的；
* `docs/DEMO_GUIDE.md` 让者用 `curl .../api/v1/metrics/health` 自检
  "服务活着"，**期望 200、实际 404** ⇒ 照文档走会得出"服务挂了"的结论；
* `Dockerfile` 的 `HEALTHCHECK` 打 `/health`（真实路径是 `/api/v1/health`），
  而那个聚合探针最坏 **142.8 秒**、开鉴权后返回 **401** ⇒ `urlopen` 遇 4xx
  抛异常 ⇒ **容器永远 unhealthy**。

## 判据怎么取"真实路由表"（这一步很容易取错层）

**不能**直接遍历 `app.routes`：这个 FastAPI 版本里 `include_router` 不再把子路由
摊平进 `app.routes`，而是塞一个 `fastapi.routing._IncludedRouter`（`.path` 是 `None`）
⇒ 遍历只看得到 6 条（4 条文档路由 + 1 个包装对象 + 1 个静态挂载），
**会把所有路径误报成 DEAD**（本判据写作过程中我的探针就是这么错的）。
改用 **`app.openapi()["paths"]`**：公开 API、跨版本稳定、能穿透嵌套路由，
且 `docs_url=None` 的实例（prod/pilot）照样能生成。
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: 由 `StaticFiles(directory=_dist, html=True)` 挂载在 `/` 上服务的路径。
#:
#: 它们**不是** APIRoute，所以不在 `openapi()["paths"]` 里 —— 但确实是
#: "有东西在服务它"。`/favicon.ico` 同时出现在两份白名单里，
#: 由这个静态挂载（或前端构建产物）服务。
_STATIC_SERVED: frozenset[str] = frozenset({"/", "/index.html", "/favicon.ico"})

#: **可执行**的文档：运维/会照着敲命令的那几份。
#:
#: 历史复盘（`INCIDENT_*` / `SESSION_*` / `INTERVIEW_*` / `SECTOR_CROWDING` 等）
#: **故意不在内**：那些文档引用当年的坏 URL（`/none`、`/...`、带 `?$m` 的 shell
#: 变量）本身就是它们的叙述内容，拿"路径必须存在"去要求它们是**把判据用错了地方**。
CONTRACT_FILES: tuple[str, ...] = (
    "Dockerfile",
    "docs/DEMO_GUIDE.md",
    "docs/OPS_GUIDE.md",
)

#: `http://127.0.0.1:8100/path?query` / `http://localhost:8100/path` 里的 path 段。
#: 字符类排除引号、反引号、竖线（表格分隔）、反斜杠、空白，避免把行尾噪声吃进来。
_URL_RE = re.compile(r"(?:127\.0\.0\.1|localhost):\d+(/[^\s\"'`|\\]*)")
#: 废止痕迹 `~~...~~` 里的路径是**故意留着的旧名字**，不该被要求存在。
_STRUCK_RE = re.compile(r"~~.+?~~", re.DOTALL)


def _served_paths() -> frozenset[str]:
    """真实被服务的路径集合（真实路由 ∪ 静态挂载）。"""
    from src.api.main import app

    routes = frozenset(app.openapi().get("paths", {}))
    assert routes, (
        "`app.openapi()` 返回 0 条路径 —— 判据失去了**唯一的**事实来源，"
        "此时它会静默地把一切都判成 DEAD 或一切放行。"
    )
    return routes | _STATIC_SERVED


def _probe_url_paths(text: str) -> list[str]:
    """抽出文本里所有 `host:port/path` 的 path 段（去 query/fragment、去废止痕迹）。"""
    cleaned = _STRUCK_RE.sub(" ", text)
    out: list[str] = []
    for m in _URL_RE.finditer(cleaned):
        path = m.group(1).split("?", 1)[0].split("#", 1)[0].rstrip(".,;:)")
        if path.strip("/.") == "":  # `/`、`/...` 之类不是可判定的路径
            continue
        out.append(path)
    return out


def _offenders(entries, served: frozenset[str]) -> list[str]:
    """`entries` 里**没有**被服务的那些（纯函数，便于自证）。"""
    return sorted({e for e in entries if e not in served})


def _whitelists() -> dict[str, frozenset[str]]:
    from src.api.login_gate import PUBLIC_EXACT
    from src.api.tenancy_middleware import _PUBLIC_PATHS

    return {"tenancy._PUBLIC_PATHS": _PUBLIC_PATHS, "login_gate.PUBLIC_EXACT": PUBLIC_EXACT}


# ---------------------------------------------------------------- 主判据


def test_every_public_path_is_actually_served() -> None:
    """★ 白名单里的每一条，都必须真有路由（或静态挂载）在服务它。

    这是本文件的主判据：它把 `CHG-0128` 那个形状（"写了但没实现"）变成红色。
    """
    served = _served_paths()
    dead: list[str] = []
    for name, entries in _whitelists().items():
        for bad in _offenders(entries, served):
            dead.append(f"{name}: {bad}")
    assert not dead, (
        "免鉴权/免登录白名单里有**没有路由**的路径 —— 它们不授予任何东西，"
        "却让人以为探针可用（照文档敲命令会拿到 404 并判『服务挂了』）：\n  "
        + "\n  ".join(dead)
        + "\n\n要么补上路由（在 `src/api/main.py` 或对应 router 里），"
        "\n要么从白名单删掉并把废止痕迹留给 `CHG` 号。"
    )


def test_two_whitelists_agree_on_api_paths() -> None:
    """两份白名单在 `/api/` 域上必须是**同一套事实**。

    它们由两个中间件各自持有（登录门槛 / 租户鉴权），只改一处必然漂移 ——
    漂移的后果是"过了登录却过不了鉴权"或反之，且**没有任何报错**，
    只表现为某个探针在某个环境下 401。允许的差异只有非 API 路径：
    `login_gate` 多出 `/`、`/index.html`（SPA 外壳必须免登录，
    否则"要登录才能打开登录页"）。
    """
    ten, gate = _whitelists()["tenancy._PUBLIC_PATHS"], _whitelists()["login_gate.PUBLIC_EXACT"]
    t_api = {p for p in ten if p.startswith("/api/")}
    g_api = {p for p in gate if p.startswith("/api/")}
    assert t_api == g_api, (
        f"两份白名单的 API 路径不一致：\n  tenancy 独有 = {sorted(t_api - g_api)}"
        f"\n  login_gate 独有 = {sorted(g_api - t_api)}"
    )


def test_runbook_and_dockerfile_paths_exist() -> None:
    """运维/文档与 `Dockerfile` 里出现的**每一个** `host:port/path` 都必须存在。

    这条盯的是根因层：路径字符串写在文档里时，**没有任何东西会去核对它**。
    """
    served = _served_paths()
    bad: list[str] = []
    scanned: dict[str, int] = {}
    for rel in CONTRACT_FILES:
        f = PROJECT_ROOT / rel
        assert f.is_file(), f"契约文件不见了：{rel}（判据不该静默跳过）"
        paths = _probe_url_paths(f.read_text(encoding="utf-8", errors="replace"))
        scanned[rel] = len(paths)
        bad += [f"{rel}: {p}" for p in _offenders(paths, served)]
    assert not bad, (
        "这些文档/Dockerfile 里的路径**没有路由**（照着做会失败）：\n  "
        + "\n  ".join(bad)
    )
    # 自证：抽取器真的抽到了东西（正则写坏了会"零命中 ⇒ 恒绿"）。
    # 门槛取 2 而不是当前实际条数：正常改文档（删掉一个 curl 示例）不该让判据变红，
    # 但"正则彻底失效"必须被抓到。
    assert sum(scanned.values()) >= 2, f"抽取器几乎没抽到路径，判据可能是恒绿的：{scanned}"


def test_guard_selfproof_flags_dead_and_passes_live() -> None:
    """判据自证：喂一个**假**的死路径必须被抓到，喂真路径不许误伤。

    没有这一条，"判据恒绿"与"契约成立"在测试报告里长得一样。
    """
    served = _served_paths()
    assert _offenders(["/api/v1/definitely_not_a_route_12345"], served) == [
        "/api/v1/definitely_not_a_route_12345"
    ], "假死路径没被抓到 ⇒ 判据恒绿"
    assert _offenders(["/healthz", "/api/v1/health/live"], served) == [], (
        "真路径被误判成 DEAD ⇒ 判据会误报"
    )
    # 抽取器自证：废止痕迹里的旧路径不算数
    assert _probe_url_paths("见 ~~`http://127.0.0.1:8100/api/v1/old`~~") == [], (
        "废止痕迹里的路径被当成活路径了"
    )
    assert _probe_url_paths("curl http://127.0.0.1:8100/healthz | 200") == ["/healthz"], (
        "抽取器把行尾噪声（表格竖线/期望值）吃进路径了"
    )


# ---------------------------------------------------------------- 新公开面自身的判据


@pytest.fixture()
def client(tmp_path, monkeypatch):
    """与既有 `/health` 契约测试同一套隔离（`MOSS_ENV=test` + 临时库）。"""
    monkeypatch.setenv("MOSS_SQLITE_PATH", str(tmp_path / "publicpath.db"))
    monkeypatch.setenv("LLM_AUDIT_DIR", str(tmp_path / "audit"))
    monkeypatch.setenv("MOSS_ENV", "test")
    from fastapi.testclient import TestClient

    from src.api.main import app

    with TestClient(app) as c:
        yield c


def test_healthz_is_live_and_leaks_nothing(client) -> None:
    """根级 `/healthz` 真的活着，且**不含任何部署信息**（它是免鉴权的）。

    设计文档要求"`/healthz` 保持公开但**不得泄露内部细节**"
    （`docs/PLATFORM_MULTI_TENANCY_DESIGN.md`）—— 这里把那条要求做成判据。
    """
    r = client.get("/healthz")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body.get("ok") is True, body
    assert set(body) == {"ok", "ts"}, f"存活探针的返回体只允许 ok/ts，实际 {sorted(body)}"
    raw = r.text.lower()
    for leak in ("pid", "version", "env", "tenant", "hostname"):
        assert leak not in raw, f"免鉴权的存活探针里泄露了 {leak!r}：{r.text}"


def test_healthz_and_health_live_are_the_same_implementation(client) -> None:
    """两条存活探针必须**同源**（`liveness_payload()`），否则会漂移成一个说活一个说死。"""
    from src.api.routes.research import liveness_payload

    assert client.get("/healthz").json().keys() == client.get("/api/v1/health/live").json().keys()
    assert set(liveness_payload()) == {"ok", "ts"}
