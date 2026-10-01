# Moss-FinAgent-Research API 容器（multi-stage，最终镜像 < 300MB）
# 用法：
#   docker build -t moss-finagent .
#   docker run -p 8100:8100 \
#       -e DEEPSEEK_API_KEY=xxx \
#       -e DATA_BACKEND=sqlite \
#       moss-finagent
#
#   # 带数据卷（审计链、缓存持久化）
#   docker run -p 8100:8100 -v ./data:/app/data \
#       -e DEEPSEEK_API_KEY=xxx moss-finagent
#
# 带前端一起跑：docker compose up -d（见 docker-compose.yml）

# ========== 阶段 1：依赖编译 ==========
FROM ghcr.io/astral-sh/uv:0.6-python3.12-slim AS build
WORKDIR /app

# 锁文件 + 依赖声明先 COPY，利用 Docker 缓存层（不依赖 src/）
COPY pyproject.toml uv.lock ./

# 只装运行时依赖（data extra 含 akshare/tushare/xtquant，在容器内不可用也不装）
# 开发依赖在容器中不需要
RUN uv sync --frozen --no-dev --no-data

# ========== 阶段 2：生产镜像 ==========
FROM python:3.12-slim AS runtime
LABEL org.opencontainers.image.title="Moss-FinAgent-Research" \
      org.opencontainers.image.description="LangGraph 多Agent投研系统（量化+AI应用）" \
      org.opencontainers.image.version="0.1.0"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONPATH=/app

WORKDIR /app

# 从 build 阶段拷贝已安装好的虚拟环境（不重装）
COPY --from=build /app/.venv ./.venv
# 源代码
COPY src ./src
COPY configs ./configs
COPY scripts ./scripts
COPY manage.py .
COPY README.md .

# 创建数据目录（审计链、缓存、sqlite）
RUN mkdir -p data/audit data/llm_cache data/repos \
    && chmod 755 data

EXPOSE 8100

# ★ `2026-09-30`（`CHG-0128`）修正探针路径：这里原来打的是 `/health` ——
#   **那不是路由**（真实路径是 `/api/v1/health`），而且即便写对了也不该当存活探针：
#   它会连 Ollama（2s 超时）、校验审计链、聚合数据源健康度，实测最坏 142.8s，
#   还会在开启鉴权后返回 401。`urlopen` 遇 4xx 直接抛异常 ⇒ **容器永远 unhealthy**。
#   改用根级 0 I/O 探针 `/healthz`（免鉴权、不含 pid/环境名/版本号）。
HEALTHCHECK --interval=30s --timeout=5s --retries=3 \
  CMD .venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://localhost:8100/healthz')" || exit 1

# 生产启动（ASGI 多 worker，比同步 FastAPI 更能扛）
# --no-access-log 减小日志体积，生产日志走 JSON
CMD [".venv/bin/python", "-m", "uvicorn", "src.api.main:app", \
     "--host", "0.0.0.0", "--port", "8100", \
     "--workers", "2", "--loop", "uvloop", \
     "--no-access-log"]
