"""容器配置的**跨文件一致性**判据（`CHG-0192` / 债 #17）。

## 这条判据防的是什么

Dockerfile 与 docker-compose.yml 是两份**各自手写**的配置，它们之间有几条
**只能靠人记住**的等式。任何一条破了都**不会报错**，只会在运行时表现为
"数据莫名丢失"或"容器永远 unhealthy"：

| 等式 | 破了的症状 |
|---|---|
| `MOSS_SQLITE_PATH` 的目录 == 卷挂载点 | SQLite 写进容器内层 ⇒ **重启即丢**（不报错，只是数据没了） |
| compose 的 `ports` 右边 == Dockerfile 的 `EXPOSE` == CMD 的 `--port` | 宿主映射到没人听的端口 |
| 容器以非 root 运行 ⇒ 卷目录可写 | 启动后写库 `PermissionError` |
| 启动探针路径必须是**免鉴权且真实存在**的路由 | 探针 401/404 ⇒ 容器永远 unhealthy（已由 `CHG-0128` 修过一次） |

## 判据强度

- `test_sqlite_path_is_inside_the_mounted_volume` —— ★ **跨文件等式**：
  这是我本轮改 compose 时**主动防的那个坑**（写了 `MOSS_SQLITE_PATH`
  就顺手把卷挂成 `/app/data`，否则两者漂移是静默的）。
- `test_container_runs_as_non_root_with_owned_data_dir` —— 债 #17 本体。
- `test_credentials_are_not_hardcoded_in_compose` —— compose 会入库，凭据必须从环境注入。
- `test_healthcheck_path_matches_a_real_public_route` —— 探针路径不许是"看起来像"的。
"""

from __future__ import annotations

import re
from pathlib import Path

import yaml

ROOT = Path(".")
DOCKERFILE = ROOT / "Dockerfile"
COMPOSE = ROOT / "docker-compose.yml"


def _compose() -> dict:
    return yaml.safe_load(COMPOSE.read_text(encoding="utf-8")) or {}


def _app() -> dict:
    return (_compose().get("services") or {}).get("app") or {}


# ======================================================================
# 1. ★ 跨文件等式：SQLite 路径必须在挂载卷内
# ======================================================================

def test_sqlite_path_is_inside_the_mounted_volume():
    """★ `MOSS_SQLITE_PATH` 的目录必须落在某个容器内挂载点上。

    破了的症状是**静默丢数据**：SQLite 写进容器可写层，`docker restart` 之后
    文件消失，而应用不报任何错 —— 下次启动只是"库是空的"。
    """
    env = _app().get("environment") or {}
    sqlite_path = str(env.get("MOSS_SQLITE_PATH") or "")
    assert sqlite_path, "compose 的 app 没设 MOSS_SQLITE_PATH（会用默认相对路径落进容器内层）"

    mounts: list[str] = []
    for spec in _app().get("volumes") or []:
        parts = str(spec).split(":")
        if len(parts) >= 2 and parts[1].startswith("/"):
            mounts.append(parts[1])
    assert mounts, "app 没有任何容器内挂载点 ⇒ 数据无法持久化"

    inside = any(
        sqlite_path == m or sqlite_path.startswith(m.rstrip("/") + "/")
        for m in mounts
    )
    assert inside, (
        f"MOSS_SQLITE_PATH={sqlite_path!r} 不在任何挂载点内 {mounts} ⇒ "
        "SQLite 会写进容器可写层，重启即丢（而且不报错）"
    )


def test_data_dir_is_writable_by_the_non_root_user():
    """★ 卷挂载点 + 非 root 用户 ⇒ 宿主目录必须可写（否则启动就 PermissionError）。

    Dockerfile 侧本条由 `chown -R moss:moss /app/data` 保证；
    宿主侧无法在仓库里保证，所以 Dockerfile 顶部必须写明做法。
    """
    df = DOCKERFILE.read_text(encoding="utf-8")
    assert "chown -R moss:moss /app/data" in df, (
        "建了非 root 用户却没把数据目录属主给它 ⇒ 写库 PermissionError"
    )
    assert "chown -R 10001:10001" in df, (
        "Dockerfile 用法里没告诉用户宿主目录怎么改属主（卷挂载点不归容器管）"
    )


# ======================================================================
# 2. 非 root
# ======================================================================

def test_container_runs_as_non_root_with_owned_data_dir():
    """债 #17 本体：三件必须成对（建用户 / 改属主 / 切 USER），且 USER 在 CMD 之前。"""
    df = DOCKERFILE.read_text(encoding="utf-8")
    assert re.search(r"(?m)^USER\s+moss\s*$", df), "没有切换到非特权用户 ⇒ 容器内是 root"
    assert "useradd" in df, "切了 USER 但没建用户"
    assert df.index("USER moss") < df.index("CMD ["), "USER 必须在 CMD 之前才生效"


# ======================================================================
# 3. 端口三方一致
# ======================================================================

def test_port_is_consistent_across_dockerfile_and_compose():
    """`EXPOSE` / CMD `--port` / compose `ports` 三者必须一致。"""
    df = DOCKERFILE.read_text(encoding="utf-8")
    expose = re.search(r"(?m)^EXPOSE\s+(\d+)", df)
    cmd_port = re.search(r'"--port",\s*"(\d+)"', df)
    assert expose and cmd_port, "Dockerfile 里找不到 EXPOSE 或 CMD 的 --port"
    assert expose.group(1) == cmd_port.group(1), (
        f"EXPOSE {expose.group(1)} != CMD --port {cmd_port.group(1)}"
    )
    ports = _app().get("ports") or []
    assert ports, "compose 的 app 没暴露端口"
    container_port = str(ports[0]).split(":")[-1]
    assert container_port == expose.group(1), (
        f"compose 映射到容器端口 {container_port}，而应用听 {expose.group(1)}"
    )


# ======================================================================
# 4. 凭据与探针
# ======================================================================

def test_credentials_are_not_hardcoded_in_compose():
    """compose 会入库 ⇒ 凭据只能从宿主环境注入（`${VAR:-}` 形式）。"""
    env = _app().get("environment") or {}
    offenders = [k for k, v in env.items()
                 if k.upper().endswith(("API_KEY", "TOKEN", "PASSWORD"))
                 and v and "${" not in str(v)]
    assert not offenders, f"compose 里写死了凭据：{offenders}"


def test_healthcheck_path_matches_a_real_public_route():
    """★ 探针路径必须是**免鉴权且真实存在**的路由。

    这条被修过一次（`CHG-0128`）：原探针打 `/health`（不是路由）且聚合了
    Ollama/审计链（最坏 142.8s）+ 开鉴权后 401 ⇒ `urlopen` 遇 4xx 抛异常
    ⇒ **容器永远 unhealthy**。
    """
    df = DOCKERFILE.read_text(encoding="utf-8")
    m = re.search(r"urlopen\('http://localhost:\d+([^']*)'\)", df)
    assert m, "找不到健康探针的 URL"
    path = m.group(1)
    assert path == "/healthz", (
        f"探针路径是 {path}；根级 `/healthz` 才是那个 0 I/O、免鉴权的存活探针"
    )
    main_src = (ROOT / "src" / "api" / "main.py").read_text(encoding="utf-8")
    assert '"/healthz"' in main_src, "探针路径在应用里没有对应路由"


def test_compose_app_depends_on_both_middlewares():
    """app 必须声明依赖两个中间件（仅启动顺序，不做可用性门槛）。"""
    deps = _app().get("depends_on") or []
    assert set(deps) >= {"postgres", "redis"}, f"depends_on 不完整：{deps}"
