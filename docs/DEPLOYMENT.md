# 部署指南（Demo版）

## 一、硬件要求

| 配置项 | 最低要求 | 推荐配置 |
|--------|----------|----------|
| GPU | 8GB VRAM | RTX 4060 8GB |
| 内存 | 16GB | 32GB |
| 磁盘 | 50GB SSD | 100GB SSD |

## 二、软件依赖

| 组件 | 版本 | 用途 |
|------|------|------|
| Python | 3.10+ | 开发语言 |
| PostgreSQL | 14+ | 关系数据库 |
| Redis | 7+ | 缓存 |
| SQLite | 3+ | 轻量存储 |
| Ollama | 最新 | 本地模型推理 |
| Docker | 24+ | 容器化 |
| uv | 最新 | Python依赖管理 |

## 三、本地环境搭建

### 3.1 安装Ollama

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama pull qwen3.5:4b
ollama pull gemma4:e4b
OLLAMA_HOST=0.0.0.0 ollama serve
```

### 3.2 安装数据库

```bash
docker run -d --name postgres -e POSTGRES_PASSWORD=finagent -p 5432:5432 postgres:14
docker run -d --name redis -p 6379:6379 redis:7
```

### 3.3 安装Python依赖

```bash
uv sync
```

### 3.4 配置环境变量

复制.env.example为.env，填入API Key等配置。

### 3.5 初始化数据库

```bash
python scripts/setup_dev.py
python scripts/seed_data.py
```

## 四、启动服务

```bash
# 启动API服务
uvicorn src.api.main:app --reload --port 8000

# 启动Worker
celery -A src.worker worker --loglevel=info

# 启动调度器
celery -A src.worker beat --loglevel=info
```

## 五、成本控制

- 本地模型：零API成本，仅电费
- 云端API：按需调用，月预算100-200元
- 建议使用DeepSeek-V4-Flash空闲时段API

## 六、常见问题

### Ollama显存不足

- 使用Q4量化模型
- 降低max_tokens
- 关闭不必要的后台服务

### 数据库连接失败

- 检查PostgreSQL是否启动
- 检查.env中的连接字符串
