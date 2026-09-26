"""应用配置。

敏感项（API Key、密码、Token）一律经环境变量注入，禁止硬编码；
仅本地Demo默认值（如postgres口令）允许作为兜底，生产必须覆盖。
"""

from __future__ import annotations

import os
from functools import lru_cache
from typing import Any, Final

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

#: QMT_ENABLED 的真值字面量（大小写不敏感）
_QMT_TRUTHY: Final = ("1", "true", "yes", "on")

#: 允许的运行环境。**没有"未设置"这个合法值** —— 见 `assert_environment_consistency`。
#:
#: | 环境 | 谁能访问 | 鉴权 | 数据库 | 通知 |
#: |---|---|---|---|---|
#: | `dev` | 只应本机 | 默认**不强制** [注1] | `data/dev/`（可丢） | 无凭据时打日志 |
#: | `test` | 只应 CI | 不重要（用临时库） | 临时库 | 内存 |
#: | `pilot` | **可以对外**（客户） | **强制** [注2] | **独立** `data/pilot/` | **必须真发邮件** |
#: | `prod` | 对外 | 强制 | Postgres | 真发邮件 |
#:
#: [注1] 未带凭证时按本地开发身份放行（仅限本机）。
#: [注2] 与 prod 同一套门槛，唯独允许 SQLite，且强制自报单实例。
#:
#: `pilot` 是 2026-09-23 新增的**第四档**，存在的理由很具体：
#: 客户要在公网用，而 `prod` 今天**起不来**（它要求 `DATA_BACKEND=postgres`，
#: 但事件/档案/认证/自选池四族表的 Postgres 后端尚未实现）。
#: 直接在 `dev` 上开门是**不可接受**的（见 `assert_environment_consistency`
#: 里 pilot 与 dev 的差别清单）。所以造一个"门槛与 prod 相同、
#: **唯独**允许 SQLite"的档，并且**强制它自报是单实例**。
#:
#: ⚠️ `pilot` **不是** `prod` 的别名：它没有多副本、没有 HA、
#: 重启期间不可用。任何"高可用/零停机"的承诺都不能挂在 pilot 上。
ENVS: Final = ("dev", "test", "pilot", "prod")

#: 只在生产环境生效的开关注入（用于自检时读"环境变量本身"而非 Settings 字段：
#: 有些开关如 MOSS_ALLOW_HEADER_IDENTITY 是直接读 os.environ 的）
_PROD_FORBIDDEN_ENV_FLAGS: Final = (
    ("MOSS_ALLOW_HEADER_IDENTITY",
     "生产禁止开启请求头身份（客户端改一个头就能越权）"),
)


def parse_qmt_enabled(value: object) -> bool:
    """`QMT_ENABLED` 的真值解析：1/true/yes/on 为开，其余（含空串/未设置）为关。

    刻意不抛异常：这个开关的语义是"有权限的部署打开它"，
    配置写错时按关闭处理（QMT 本来就无权限、不存在），比让进程起不来合理。
    抽成模块级纯函数是为了单测能直接覆盖各种写法。
    """
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in _QMT_TRUTHY


def _env_field(name: str, default: str) -> Any:
    """声明一个"从环境变量读、同时保留同名默认值"的字符串字段。

    ## 为什么需要这个helper（本轮实测踩到的**严重**陷阱）

    直觉写法是：

    ```python
    sqlite_path: str = os.environ.get("MOSS_SQLITE_PATH", "data/moss_finagent.db")
    ```

    它在**独立脚本里完全正常**，但在 pydantic-settings 下有一个致命性质：
    `os.environ.get(...)` 是在**模块导入时**求值并成为**字段默认值**的；
    而 pydantic 对"有默认值且没有 `validation_alias`"的字段**根本不去环境里找**
    （它认为默认值就是最终来源）。于是：

      - `monkeypatch.setenv("MOSS_SQLITE_PATH", ...)` **完全无效**
        （`monkeypatch` 在测试运行期改环境变量，而默认值早就求好了）；
      - 更糟的是**看起来生效了**：脚本里、`python -c` 里都对，
        只有"先导入、后设环境变量"的场景失效 —— 而 pytest 恰恰就是这种场景。

    ## 实际后果（不是理论风险）

    `tests/unit/test_auth_routes.py` 的夹具用 `monkeypatch.setenv` 把库指向临时目录，
    本以为每个用例一个隔离库；由于本陷阱，**它一直指向真实的
    `data/moss_finagent.db`**，夹具里的 `clear_all()` 真的在清生产库的认证表。
    症状被伪装成"登录莫名 401""用户名已存在""接口偶发 429"，
    在"单独跑通过、全量跑偶发失败"之间摇摆 —— 极难归因。

    ## 正确写法

    `validation_alias=AliasChoices(ENV_NAME, FIELD_NAME)`：
      - 环境变量**优先**（这是 `MOSS_SQLITE_PATH` 这类开关的意义）；
      - 同时保留**按字段名构造**的能力（`Settings(sqlite_path="...")`
        在 `test_env_guard.py` 里被大量使用，去掉会直接破坏既有测试）。

    `env` 字段此前已单独修过同一个坑（见那里 `validation_alias` 的注释）；
    这个 helper 把它变成**统一约定**，避免下一个字段再踩。
    """
    return Field(default=default,
                 validation_alias=AliasChoices(name, name.upper(), name.lower()))


class Settings(BaseSettings):
    """全局配置（.env / 环境变量自动加载）。"""

    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore",
        # ★ `populate_by_name=True` 是 `_env_field` 能成立的前提。
        #
        # pydantic 的规则：字段一旦写了 `validation_alias`，就**只**按 alias 取值，
        # 字段名本身失效。而 `_env_field` 必须同时满足两种用法：
        #   ① 环境变量（含 `MOSS_` 前缀）—— 靠 alias 里的 `name`；
        #   ② `Settings(sqlite_path="...")` —— `tests/unit/test_env_guard.py`
        #      大量这样构造，去掉就会直接破坏既有测试。
        # 实测（四种声明方式的对照）：只有"alias + populate_by_name"两者皆通。
        populate_by_name=True,
    )

    # 应用
    app_name: str = "Moss-FinAgent-Research"
    #: 运行环境。取值 dev / test / prod（见 `ENVS`）。
    #:
    #: 为什么必须显式声明而不是"猜"：生产环境的判据（用什么库、必不必须强制鉴权、
    #: 能不能开请求头身份）全都依赖它。此前 `MOSS_ENV=test` 只在 `manage.py test`
    #: 里设置，而 `Settings` 没有这个字段、启动时也不校验 —— 等于**没有任何东西
    #: 能判断"我现在是不是生产"**，所有"生产必须做 X"的检查都无从落地。
    #:
    #: ⚠️ **必须显式写 `validation_alias`**：`env` 是 pydantic-settings 的**保留键名**
    #: （`model_config` 里的 `env_file` / `env_prefix` 都在这个命名空间），
    #: 靠"字段名大写"的自动映射**不会**把 `MOSS_ENV` 读进来 —— 实测表现为
    #: `MOSS_ENV=prod` 却始终拿到默认值 `dev`，而自检因此**静默失效**。
    env: str = Field(default="dev", validation_alias=AliasChoices("MOSS_ENV", "env"))
    debug: bool = False
    log_level: str = "INFO"

    # API
    api_host: str = "0.0.0.0"
    api_port: int = 8100

    # 模型网关
    ollama_base_url: str = "http://localhost:11434"
    deepseek_base_url: str = "https://api.deepseek.com"
    deepseek_api_key: str = _env_field("DEEPSEEK_API_KEY", "")  # 仅经环境变量注入
    model_config_path: str = "configs/models.yaml"
    llm_timeout_seconds: float = 120.0
    llm_cache_enabled: bool = True
    llm_cache_dir: str = "data/llm_cache"
    llm_cache_ttl_hours: float = 24.0
    llm_semantic_threshold: float = 0.85  # n-gram余弦≥该值判语义命中
    llm_audit_dir: str = "data/audit"
    # Token预算：单任务DeepSeek调用累计token上限，超过则拒绝后续调用（防超支）
    #
    # 200000 而不是原来的 30000。实测依据（data/audit/llm_audit.jsonl 里 85 个任务）：
    #   中位数 40,723 / p90 123,985（含可疑样本，见下）
    # 一次正常的 full 分析实测 38,207 token（7 次调用）—— **旧预算 30000 连它都装不下**，
    # 会在中途掐断并报"token预算已耗尽"。200000 给到约 5 倍余量。
    #
    # 这个上限现在主要用来兜住真正的异常，而不是限制正常任务。审计里能看到两类：
    #   1. 单次输入 23 万 token 的调用（llm_input_char_hard_cap 似乎没在所有
    #      调用路径上生效，值得另行排查）；
    #   2. 一个 trace 下 122 次 reasoning 调用、且次次 out=4096 顶满上限（疑似失控循环）。
    # 注意：审计日志存在同一批调用被记到两个 trace_id 下的情况（明细字节级相同），
    # 按 trace 聚合统计时要把这类重复算进去，否则会高估单任务消耗。
    #
    # 按 deepseek-flash 口径，200000 token 约 0.3~0.5 元；要更省可在 .env 里
    # 用 LLM_TOKEN_BUDGET_PER_TASK 覆盖，或把 analysis_type 收窄成 sector/stock。
    llm_token_budget_per_task: int = 200000
    # 单次调用max_tokens上限（由ModelSpec.max_tokens控制，这里是硬截断）
    #
    # ⚠️ 原先写 4096，理由是"3072 推理 + 1024 正文足够"。**实测这个假设不成立**：
    # DeepSeek 的推理 token **计入输出上限**，所以推理长的调用会先把预算烧在
    # 思维链上 —— `src/mainline/relevance.py` 的逐股打分（一次要输出 10~20 条
    # JSON）约有 20% 的调用 `tokens_out` 正好撞到 4096、**正文一个字都没有**
    # （返回空响应，且重试同样撞墙，因为失败是确定性的而非抖动）。
    #
    # 所以上限抬到 32768。仍然**保留**"护栏"的定位：普通调用由
    # `configs/models.yaml` 里各自的 `max_tokens`（多为 4096）控制，
    # 只有显式传 `max_tokens=` 的调用（如打分）才会用到更高预算。
    # 注意：`ModelSpec.max_tokens` 若超过本值仍会被截断到本值。
    llm_max_tokens_hard_cap: int = 32768
    # 单次调用输入prompt字符数硬上限（防数据淹没用户问题，约2000 token）
    llm_input_char_hard_cap: int = 6000
    # 思维链推理强度全局覆盖（LLM_REASONING_EFFORT）。
    # 空串 = 用网关内置的「按层级」映射（light/medium→low，reasoning/decision→high）；
    # 显式填 none/low/high/max 则对所有层级强制该强度（DeepSeek 官方取值）。
    llm_reasoning_effort: str = ""

    # 数据层
    data_backend: str = "sqlite"  # sqlite | postgres（经DATA_BACKEND环境变量切换）
    # SQLite 主库路径。⚠️ `MOSS_SQLITE_PATH` 的作用是**让重型离线任务跑在副本上**
    # （全量重打分约 90 分钟，直接压在生产库上会：① 长时间占写锁，
    # 把用户的手动刷新挤掉——实测已经发生两次；② `--force` 先清空再重算，
    # 期间界面上是空的）。
    # 指向副本 → 生产库全程可读可写，且**结果验证通过后再换表**，不通过就什么都不做。
    #
    # ⚠️⚠️ **绝对不要退回 `os.environ.get(...)` 当默认值**（见 `_env_field` 的说明）。
    # 那个写法让本字段**读不到环境变量**，而它是"测试用临时库"的唯一开关 ——
    # 后果是**测试直接读写生产库**（`tests/unit/test_auth_routes.py` 的
    # `clear_all()` 真的清过 `data/moss_finagent.db` 的认证表）。
    sqlite_path: str = _env_field("MOSS_SQLITE_PATH", "data/moss_finagent.db")
    postgres_dsn: str = "postgresql+asyncpg://moss_finagent:moss_finagent@localhost:5432/moss_finagent"
    sqlite_dsn: str = "sqlite:///data/moss_finagent.db"
    # 本地QMT导出CSV行情目录（如 D:/quantTrader/data，含SH/SZ子目录）；空则不启用
    local_quote_dir: str = _env_field("LOCAL_QUOTE_DIR", "")
    # 迅投QMT终端是否纳入日线采集链（默认**关闭**）
    #
    # 实测（2026-09）：本机 QMT 终端已无行情权限且不在运行（127.0.0.1:58610 不通），
    # 它排在链首时每次取数都要先等一次连接超时，最后还得回退；
    # 且它导出的本地CSV已停更（停在 2026-08-31）。所以 QMT 及其导出CSV
    # 都降级为**最低优先级 + 显式开关**：有权限的部署把 QMT_ENABLED 打开即可恢复，
    # 没有权限的部署不必为它付一次必然失败的等待。
    # 真值解析："1"/"true"/"yes"/"on"（大小写不敏感）为开，其余为关。
    #
    # ⚠️ 必须显式声明 env 别名并给出 field_validator（踩过的坑）：
    # config 里字段名与 env 名大小写不同（qmt_enabled / QMT_ENABLED），
    # 不加 alias 时 pydantic 会用**严格 bool 解析器**去吃 `QMT_ENABLED` 的原值，
    # 于是 `.env` 里一行空的 `QMT_ENABLED=`（或 `no`/`off` 之外的任意写法）
    # 直接抛 ValidationError，**整个应用起不来**（pydantic 的 bool 只认
    # true/false/1/0/yes/no/on/off 那一小撮字面量，空串不在其中）。
    # 用 validation_alias 显式绑定，再用 validator(mode="before") 统一解释，
    # 配置写坏时最坏是"当成关闭"，而不是启动崩。
    qmt_enabled: bool = Field(
        default_factory=lambda: parse_qmt_enabled(os.environ.get("QMT_ENABLED")),
        validation_alias=AliasChoices("QMT_ENABLED", "qmt_enabled"))

    @field_validator("qmt_enabled", mode="before")
    @classmethod
    def _validate_qmt_enabled(cls, value: object) -> bool:
        """统一走 parse_qmt_enabled（含空串/异常写法 → 关闭，不抛错）。"""
        return parse_qmt_enabled(value)
    redis_url: str = "redis://localhost:6379/0"
    redis_cache_enabled: bool = False  # 开启后query_points走Redis缓存（Redis不可达自动降级）
    data_cache_ttl_seconds: int = 300
    celery_broker_url: str = "redis://localhost:6379/1"
    scheduler_dir: str = "data/scheduler"  # 定时作业运行记录（runs.jsonl）
    scheduler_run_log_ttl_days: int = 90  # 运行记录保留天数（audit_cleanup作业清理）

    # 数据保留与新闻缓存（性能优化）
    # 数据点保留上限：仅保留最近 N 年（按 period_date 清理超期数据点）
    data_retention_years: int = 10  # env DATA_RETENTION_YEARS
    # 新闻/快讯缓存：新闻属高频信息，TTL 内重复分析直接读库不重复网络
    news_cache_enabled: bool = True
    news_cache_ttl_seconds: int = 600  # env NEWS_CACHE_TTL_SECONDS
    news_retention_days: int = 30  # 新闻缓存自身保留窗口（env NEWS_RETENTION_DAYS）

    # ------------------------------------------------------------------
    # 增量保留策略（2026-09-25 数据库审计后新增）
    #
    # ## 为什么需要这一组
    #
    # 审计发现：全项目只有 `retention_service.py` 在做清理，而它**只覆盖
    # `fact_data_points` 与 `news_cache`**；其余约 40 张表**没有任何清理**，
    # 其中一批是"天然随时间无限累积"的流水/令牌/台账表。主库当时 6.32GB，
    # 其中 3.96GB 是"删了但没回收"的空闲页（`auto_vacuum=0`）。
    #
    # ## 默认值的取舍：分两类
    #
    # - **默认开启（>0）**：认证/告警/通知类流水。它们在保留期后没有任何
    #   业务价值，且审计证据表明它们只会单调增长（过期只 UPDATE 状态、
    #   不删行）。这类不清理纯粹是泄漏。
    # - **默认关闭（=0）**：`sector_crowding_daily` 与 `auction_*` 这类
    #   **历史行情数据**。它们原本是"有意永久保留"的（`sector_crowding/
    #   refresh.py` 明确写着"不删历史 —— 删历史不可逆，停止更新可逆"）。
    #   删这些数据**不可恢复**，所以保留期由使用者显式开启，我们不替用户决定。
    #   主库实测体积：sector_crowding_daily 218 万行/318MB、auction_snapshot
    #   每行约 15.7KB（字节增速最快，约 235MB/年）；想控制体积就设成非 0。
    #
    # 单位一律"天"；`_years` / `_weeks` 后缀的按各自单位算。
    # 一律经 `_env_field` 暴露（与其他字段同一约定），环境变量名为字段名大写。
    retention_notify_log_days: int = _env_field("RETENTION_NOTIFY_LOG_DAYS", 180)
    retention_session_days: int = _env_field("RETENTION_SESSION_DAYS", 90)
    retention_remember_token_days: int = _env_field(
        "RETENTION_REMEMBER_TOKEN_DAYS", 90)
    retention_verification_code_days: int = _env_field(
        "RETENTION_VERIFICATION_CODE_DAYS", 30)
    retention_password_reset_days: int = _env_field(
        "RETENTION_PASSWORD_RESET_DAYS", 30)
    # 告警**删除**保留天数（与 `alert_expire_days` 的分工见下）。
    #
    # 用户口径（2026-09-26）："事件告警的信息最多保留三天，超过3天的信息
    # 自动溢出删除。" —— 365 → **3**。
    #
    # ## 与 `alert_expire_days` 的关系（两个字段都要，别合并）
    #
    #   `alert_expire_days` = 3   决定 `expire_time = trigger_time + 3天`；
    #                             读路径 `_expire_due` 据此把到期的置 `expired`
    #                             （**只是不显示，行还在**）
    #   `retention_alert_days` = 3   `retention_passes` 按 `expire_time` **真正
    #                             DELETE**，并级联删 `user_alert_read`
    #
    # 两者取同一个值（3）是刻意的：到期即删除，不留"已过期但还占着库"的中间态。
    # 调大前者、调小后者会出现"删掉了还没过期的告警"——所以**改就一起改**。
    retention_alert_days: int = _env_field("RETENTION_ALERT_DAYS", 3)
    retention_event_days: int = _env_field("RETENTION_EVENT_DAYS", 730)
    # ---- 以下默认 0 = 不清理（历史行情数据，开启前请确认可接受不可恢复）----
    retention_crowding_daily_years: int = _env_field(
        "RETENTION_CROWDING_DAILY_YEARS", 0)
    retention_crowding_metric_weeks: int = _env_field(
        "RETENTION_CROWDING_METRIC_WEEKS", 0)
    retention_auction_days: int = _env_field("RETENTION_AUCTION_DAYS", 0)
    retention_quant_selection_days: int = _env_field(
        "RETENTION_QUANT_SELECTION_DAYS", 0)
    # 单批删除行数。**分批是硬要求**：一次性 DELETE 几百万行会长时间持有
    # 写锁，把做T/资金流的写请求全部顶成 "database is locked"
    # （`event_sqlite_base` 记录了同类教训）。每批之间让出事件循环。
    retention_batch_size: int = _env_field("RETENTION_BATCH_SIZE", 2000)
    retention_max_batches_per_table: int = _env_field(
        "RETENTION_MAX_BATCHES_PER_TABLE", 500)

    # ------------------------------------------------------------------
    # 情报流后台预热（2026-09-25 用户口径）
    #
    # > "服务启动就加载已有数据。用户打开本页就直接加载已有数据，然后后台检查
    # >   是否是工作日且相比上次抓取间隔2小时，若满足则记录此刻时间并执行一次
    # >   后台抓取调用…这样就不存在首屏冷启动。"
    #
    # 解决的问题：`GET /feed` 的契约是"请求永不等待采集"，冷启动时先下发
    # **空壳**，后台重建完再经 WS 通知前端回拉 —— 于是用户会看到几秒的
    # "正在采集"。预热把"重建"挪到启动期与后台周期，请求永远命中已建好的缓存。
    # ------------------------------------------------------------------
    #: 预热后台循环的检查间隔（秒）。默认 15 分钟 —— 与用户口径里
    #: "记录时间+15分钟后自动获取"的节奏一致。
    intel_prewarm_tick_seconds: int = _env_field(
        "INTEL_PREWARM_TICK_SECONDS", 900)
    #: 两次**上游抓取**之间的最小间隔（小时）。默认 2 小时。
    #:
    #: 依据：情报内容的最小更新节奏是分钟级、而知识星球的增量游标是 2 小时
    #: 一轮（见 `zsxq_incremental`），所以 2 小时内重抓拿不到实质新内容，
    #: 只会白打上游。`0` = 每次 tick 都抓（排查用，别在生产上这么设）。
    intel_prewarm_min_interval_hours: float = _env_field(
        "INTEL_PREWARM_MIN_INTERVAL_HOURS", 2.0)
    #: 启动后多久开始第一次预热检查（秒）。
    #:
    #: 比 `_WARM_DATA_HEALTH_DELAY`（20s）与板块预热（60s）更晚：情报聚合要打
    #: 十几路外部源，与首屏/行情预热抢磁盘和 GIL 会把 `/health` 拖慢
    #: （本文件里有多处同类实测教训）。
    intel_prewarm_startup_delay_seconds: int = _env_field(
        "INTEL_PREWARM_STARTUP_DELAY_SECONDS", 90)
    #: 预热总开关。`false` = 回到"请求触发重建"（旧行为）。
    intel_prewarm_enabled: bool = _env_field("INTEL_PREWARM_ENABLED", True)

    # ------------------------------------------------------------------
    # 投资日历 / 热度 的**慢聚合缓存**（2026-09-26 用户报障首屏 5~10 秒）
    #
    # 实测（本机 8110，真实会话）：
    #
    #     /intel/feed?limit=60        热 36 ms   ← 不是瓶颈
    #     /intel/calendar?horizon_days=45   **11,133 ms**  675 KB
    #     /intel/heat                  **3,472 ms**
    #
    # 日历 11 秒的构成（逐源计时）：宏观 8,731 ms、财报 2,402 ms、
    # 解禁 1,137 ms、交易日 852 ms。宏观之所以占八成，是因为它**按天逐个
    # 请求**财经日历（30 天 = 30 次 HTTP，每次约 250 ms，见 `calendar.py`
    # 的 `FEED_FETCH_MAX_DAYS`）。
    #
    # 而这四类内容**一天之内几乎不变**（休市日是交易所规则、解禁与财报
    # 是既定日程、宏观发布日程提前公布），却在每一次打开页面时重付一遍。
    # 所以这里按小时级缓存，并在服务启动后后台预热一次 —— 用户永远命中热的。
    # ------------------------------------------------------------------
    #: 日历/热度的缓存有效期（小时）。默认 6 小时。
    #:
    #: 为什么敢这么长：这几类都是"已公布的日程"。宏观**数据值**（预期/前值/
    #: 公布）更新略快，但它们只影响日历条目里的数字，不影响"哪天有发布"。
    #: `0` = 关闭缓存（每次现拉，排查用，别在生产上这么设）。
    intel_slow_cache_hours: float = _env_field(
        "INTEL_SLOW_CACHE_HOURS", 6.0)

    # 数据源Token
    tushare_token: str = _env_field("TUSHARE_TOKEN", "")

    # 做T辅助模块（多因子打分 + 做T信号）
    # 策略参数（权重/阈值/箱体指数/板块/自选池）在 configs/intraday.yaml，按mtime热重载
    intraday_config_path: str = "configs/intraday.yaml"
    intraday_enabled: bool = True

    # 事件监控与自动告警（事件采集→LLM分析→阈值告警→WebSocket/邮件）
    alert_scan_candidate_limit: int = 30       # 单次扫描进入LLM的候选事件上限
    alert_keywords: str = _env_field(
        "ALERT_KEYWORDS", "")                  # 逗号分隔自定义预筛词（空则仅内置）
    alert_confidence_min: float = 0.70         # 置信度门槛，低于则不推送
    alert_risk_high: int = 75                  # 风险分高/中/低阈值
    alert_risk_medium: int = 60
    alert_risk_low: int = 45
    alert_opp_high: int = 80                   # 机会分高/中/低阈值
    alert_opp_medium: int = 65
    alert_opp_low: int = 50
    # 告警有效期（天）。**到期先置 expired（列表隐藏），再被保留作业真正删除。**
    #
    # 用户口径（2026-09-26）："事件告警的信息最多保留三天，超过3天的信息
    # 自动溢出删除。" —— 所以 7 → **3**。
    #
    # 两个机制的分工（别混）：
    #   · 本配置 → `expire_time` 字段，`_expire_due` 把到期的置成 `expired`
    #     （**读路径里的懒迁移**，只是让列表不再显示，数据还在库里）；
    #   · `retention.alerts` 作业 → `prune_alerts_before()` 真正 **DELETE**，
    #     这才是"溢出删除"。
    # 只做前者的话库会无限增长（过期行永远留着），只做后者的话到期告警
    # 在下次作业前仍然可见 —— 两层都要。
    alert_expire_days: int = 3
    alert_cooldown_hours: int = 24             # 同事件同类型告警冷却（小时）
    alert_list_limit: int = 100                # 列表接口单次上限
    # 站内告警最低级别（解决规格AC-5：默认medium+才产生告警，low档事件仅入库；
    # 调成 low 后 [45,60)/[50,65) 分段也会生成低级告警）
    alert_min_level: str = "medium"
    # 邮件通道（QQ邮箱SMTP；授权码经环境变量注入，未配置则自动降级为站内告警）
    # 邮件门控按告警类型与分数（替代原alert_email_min_level级别门槛）：
    # 风险类 risk_score 严格 > alert_email_risk_min_score（默认>69）；
    # 机会类 opportunity_score >= alert_email_opp_min_score（默认≥85）。
    alert_email_risk_min_score: float = 69.0
    alert_email_opp_min_score: float = 85.0
    alert_email_min_level: str = "high"        # 已废弃（旧级别门槛），保留兼容旧env
    alert_smtp_host: str = "smtp.qq.com"
    alert_smtp_port: int = 465
    alert_smtp_user: str = _env_field("ALERT_SMTP_USER", "")
    alert_smtp_auth_code: str = _env_field("ALERT_SMTP_AUTH_CODE", "")
    alert_email_to: str = "2693888583@qq.com"
    alert_email_from_name: str = "Moss投研事件告警"

    @property
    def alert_email_enabled(self) -> bool:
        """邮件通道是否具备发送条件（按授权码与收件人推导，T1）。"""
        return bool(
            self.alert_smtp_user and self.alert_smtp_auth_code
            and self.alert_email_to)

    # ------------------------------------------------------------------
    # 环境维度
    # ------------------------------------------------------------------

    @property
    def is_prod(self) -> bool:
        return self.env == "prod"

    @property
    def is_pilot(self) -> bool:
        """对外试点环境（门槛同 prod，但允许 SQLite 单实例）。"""
        return self.env == "pilot"

    @property
    def is_public(self) -> bool:
        """**这个进程会对非本机用户提供服务吗**。

        它才是"要不要按公网标准做安全"的判据，而不是 `is_prod`：
        试点实例同样在公网上，同样必须
          · 给 Cookie 打 `Secure`（否则凭据在 HTTPS 隧道上仍会明文回发）；
          · 关掉 `/auth/_debug/whoami` 这类泄露库路径与用户规模的诊断端点；
          · 不下发验证码的调试答案。
        以前这些地方写的是 `is_prod`，加了 pilot 之后**每一处漏改都是一个洞**，
        所以统一收敛到这一个属性上（名字上也不容易看错）。
        """
        return self.env in ("prod", "pilot")

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"

    @property
    def is_single_instance(self) -> bool:
        """是否只能单实例运行（SQLite 单写者约束）。

        用于启动横幅与自省输出：把"不能起第二个副本"这件事从
        "设计文档第 10 章写过"变成**运行时可读的状态**。
        """
        return self.data_backend.strip().lower() != "postgres"

    @field_validator("env", mode="before")
    @classmethod
    def _normalize_env(cls, value: object) -> str:
        """环境名归一化：大小写/空白不敏感；未知取值直接报错（不静默兜底）。

        ⚠️ 这里**刻意不"失败即回落 dev"**：把 `MOSS_ENV=production`（写错）
        静默当成 dev，后果是"生产部署带着开发配置跑起来"——正好是自检要防的事。
        """
        text = str(value or "").strip().lower()
        if text == "production":
            text = "prod"
        if not text:
            raise ValueError(
                "MOSS_ENV 不能为空：必须显式声明运行环境"
                f"（{' / '.join(ENVS)}）。"
                "它决定生产自检、数据隔离与鉴权强度，"
                "见 docs/PLATFORM_MULTI_TENANCY_DESIGN.md §8.7")
        if text not in ENVS:
            raise ValueError(
                f"MOSS_ENV={value!r} 非法，可选：{' / '.join(ENVS)}")
        return text


@lru_cache
def get_settings() -> Settings:
    """进程内单例配置。"""
    return Settings()


def assert_environment_consistency(
    settings: Settings, *, environ: dict[str, str] | None = None
) -> list[str]:
    """**启动自检**：环境标记与连接目标必须自洽；返回问题清单（空 = 通过）。

    为什么必须做：三环境最大的风险不是"配错"，而是
    **"以为连的是测试，实际连的是生产"**。那种事故没有报警、只有事后发现。

    为什么建议"拒绝启动"而不是"打警告"：警告会被日志淹没；
    **启动失败是唯一 100% 会被看见的提示** —— 与 `rls.py` 的
    "让漏配表现为失败，而不是静默无隔离" 同源。

    抽成纯函数（`environ` 可注入）是为了单测不必改真实进程环境变量。
    """
    env = (environ if environ is not None else os.environ)
    problems: list[str] = []

    def flag(name: str) -> bool:
        return str(env.get(name, "")).strip().lower() in _QMT_TRUTHY

    label = {"prod": "生产", "pilot": "对外试点"}.get(settings.env, settings.env)

    # ---- ① 公网环境（prod / pilot）的公共门槛 ------------------------
    #
    # ★ 这一组**两档共用同一段代码**，是"pilot 不等于 dev"的全部依据。
    #   加 pilot 时最容易犯的错是"只改路径、不改门槛"，那等于开了个
    #   端口号不同、安全等级相同的 dev —— 所以物理上让它们不可能只满足一档。
    #   与"身份/租户"相关的两条（TENANCY_ENFORCE / IDENTITY_SECRET）**只在 prod**
    #   要求，原因见 ②：会话→Principal 的桥还没建，在 pilot 上强行打开
    #   会把每一个浏览器请求都变成 401（连登录页都打不开）。
    if settings.is_public:
        if flag("MOSS_ALLOW_HEADER_IDENTITY"):
            problems.append(
                f"{label}禁止开启 MOSS_ALLOW_HEADER_IDENTITY"
                "（客户端改请求头即可越权）")
        if _is_console_notifier(env):
            problems.append(
                f"{label}禁止显式使用 Console 通知通道"
                "（会把验证码打进日志 = 直接泄露）")
        # ★★ 这一条是**补的一个真洞**：`_is_console_notifier` 只看
        #    "有没有显式指定 console"，而 `build_notifier` 在**完全没有
        #    SMTP 凭据**时也会回落到 ConsoleNotifier（那是给它 dev 用的便利）。
        #    于是"prod 但忘了配邮箱"以前能顺利通过自检 —— 验证码全部被
        #    写进 `data/run/backend.log`，而界面上还提示"已发送"。
        #    补上这条，等于把"先配 SMTP 再对外"从建议变成**强制**。
        if not (str(getattr(settings, "alert_smtp_user", "") or "").strip()
                and str(getattr(settings, "alert_smtp_auth_code", "") or "").strip()):
            problems.append(
                f"{label}必须配置真实邮件通道（ALERT_SMTP_USER + "
                "ALERT_SMTP_AUTH_CODE）—— 缺凭据时通知会**回落到 "
                "ConsoleNotifier**，验证码被直接写进日志文件，"
                "而界面上仍提示「已发送」")

    # ---- ② pilot 专属 -------------------------------------------------
    if settings.is_pilot:
        backend = settings.data_backend.strip().lower()
        if backend != "sqlite":
            # 说清"不是你不能用，是后端还没实现"，避免有人配了 postgres
            # 之后看到一堆莫名其妙的 ConfigError 以为是配置写错。
            problems.append(
                f"对外试点暂只支持 DATA_BACKEND=sqlite（当前 {settings.data_backend!r}）"
                "—— 事件告警/做T档案/认证/自选池四族表的 Postgres 后端尚未实现，"
                "配 postgres 会在装配阶段抛 ConfigError。"
                "要真正多副本请先补齐 Postgres 后端并改用 MOSS_ENV=prod")
        # 独立库：路径必须自带 pilot 字样。
        # 防的是最危险的一种"省事"——把试点实例指向 dev 库（客户数据与
        # 调试数据混在一起）或指向那个 6.4GB 的本机库。
        if "pilot" not in str(settings.sqlite_path).lower():
            problems.append(
                f"对外试点的 SQLite 路径必须含 'pilot' 以证明是独立库"
                f"（当前 {settings.sqlite_path!r}）——"
                "否则会与 dev/本机库混用，客户数据与调试数据互相污染")
        # 单实例必须"自报"：把隐式风险变成一条被记录下来的决定。
        if not flag("MOSS_PILOT_SINGLE_INSTANCE_ACK"):
            problems.append(
                "对外试点必须显式承认单实例约束：设 "
                "MOSS_PILOT_SINGLE_INSTANCE_ACK=1。"
                "含义 = 我已知悉此实例**没有**多副本、没有高可用、"
                "重启期间对外不可用、SQLite 单写者且只能起一个进程；"
                "不接受任何 SLA 承诺。不设这个开关就启动，"
                "等于让运维以为它是 prod")
        # ★ 反向检查：pilot 上**不许**打开多租户强制鉴权。
        #   为什么是"不许"而不是"必须"：`TenancyMiddleware` 的
        #   `resolve_principal` 只认 `Authorization: Bearer <签名令牌>` 和
        #   开发用请求头身份，**不认本项目的会话 Cookie**（`moss_sid`），
        #   而前端从不发 Authorization。所以一旦打开，每个浏览器请求都会被
        #   判成"未认证"→ 401 —— 连 `/`（登录页本身）都打不开，
        #   表现为"整个网站白屏 401"，而原因看上去像前端坏了。
        #   pilot 的登录门槛由 `LoginGateMiddleware` 承担（认 Cookie，
        #   见 `src/api/login_gate.py`）。等会话→Principal 的桥建好之后，
        #   这一条要改成"必须打开"，那时它才是真正的加固。
        if flag("MOSS_TENANCY_ENFORCE"):
            problems.append(
                "对外试点**不要**设 MOSS_TENANCY_ENFORCE=1：多租户中间件只认 "
                "Bearer 令牌、不认会话 Cookie，打开它会让所有浏览器请求 401"
                "（连登录页都打不开）。pilot 的登录门槛由 LoginGateMiddleware "
                "自动承担；等「会话→Principal」桥接落地后再改回必须打开")

    # ---- ③ prod 专属 --------------------------------------------------
    if settings.is_prod:
        if settings.data_backend.strip().lower() != "postgres":
            problems.append(
                f"生产必须用 postgres（当前 {settings.data_backend!r}）——"
                "SQLite 单写者无法多副本，且重启即全站不可写")
        dsn = (settings.postgres_dsn or "").lower()
        if "localhost" in dsn or "127.0.0.1" in dsn:
            problems.append("生产 DB 不应指向 localhost（确认是否误连本机库）")
        if not flag("MOSS_TENANCY_ENFORCE"):
            problems.append(
                "生产必须设 MOSS_TENANCY_ENFORCE=1（否则未带凭证的请求"
                "会以本地开发身份放行，公网上等于任何人都是内部研究员）")
        if not str(env.get("MOSS_IDENTITY_SECRET", "")).strip():
            problems.append(
                "生产必须配置 MOSS_IDENTITY_SECRET（否则可伪造任意身份）")

    # ---- ④ 非公网（dev / test）：防"反向误连" -------------------------
    if not settings.is_public:
        dsn = (settings.postgres_dsn or "").lower()
        if "prod" in dsn:
            problems.append(
                f"非公网环境（{settings.env}）的 DB 指向疑似生产库，拒绝启动（防误操作）")
        if "prod" in str(settings.sqlite_path).lower():
            problems.append(
                f"非公网环境（{settings.env}）的 SQLite 路径含 'prod'，拒绝启动")

    return problems


def describe_environment(settings: Settings) -> str:
    """一句话说清"这个进程是什么档、有哪些限制"，用于启动日志与自省端点。

    为什么要一个函数而不是在各个调用点拼字符串：`pilot` 与 `prod` 的差别
    （单实例 / 无 HA / SQLite 单写者）**必须出现在每一次启动日志里** ——
    它是运行时可读的事实，而不是设计文档第 10 章的一句承诺。
    日志里看得到，值班的人才知道不能承诺 SLA。
    """
    if settings.is_prod:
        return "prod（生产：多副本 + Postgres）"
    if settings.is_pilot:
        return ("pilot（**对外试点**：登录门槛强制 + 真发邮件 + 独立库，"
                "但单实例、SQLite 单写者、无高可用、重启期间对外不可用 "
                "—— 不承诺 SLA；多租户 DataClass 平面尚未与会话打通）")
    if settings.is_dev:
        return ("dev（本机开发：**鉴权默认不强制**、通知可能打日志、库可丢 "
                "—— 绝不可对外）")
    return f"{settings.env}（测试）"


def _is_console_notifier(environ: dict[str, str]) -> bool:
    """通知通道是否被显式配成 Console（仅看环境变量，不 import 通知模块）。

    未配置时**不算问题**：默认走邮箱（SMTP），Console 只可能被显式指定。
    """
    channel = str(environ.get("MOSS_NOTIFY_CHANNEL", "")).strip().lower()
    return channel in {"console", "stdout", "print"}
