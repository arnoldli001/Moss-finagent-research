"""环境维度与生产启动自检的单元测试（docs/PLATFORM_MULTI_TENANCY_DESIGN.md §8.7.2）。

为什么这几条重要：三环境最大的风险不是"配错"，而是
**"以为连的是测试，实际连的是生产"** —— 那种事故没有报警，只有事后发现。
所以自检本身必须有测试，否则它会随一次重构静默失效
（本轮就实测到一次：`env` 是 pydantic-settings 的保留键名，
不写 `validation_alias` 时 `MOSS_ENV=prod` 会被忽略、拿到默认 `dev`，
于是所有生产检查**静默失效**）。
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.core.config import (
    ENVS,
    Settings,
    assert_environment_consistency,
    get_settings,
)

#: 生产环境的一套"自洽"配置（自检应返回 0 条问题）
_PROD_OK_ENV = {
    "MOSS_TENANCY_ENFORCE": "1",
    "MOSS_IDENTITY_SECRET": "unit-test-secret-0123456789abcdef",
}


def _prod_settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "data_backend": "postgres",
        "postgres_dsn": "postgresql+asyncpg://u:p@db.internal:5432/moss",
        # ★ 公网环境**必须**有真实邮件通道。2026-09-23 之前这条没被检查，
        #   于是"prod 但忘了配邮箱"能通过自检，而 `build_notifier` 会静默
        #   回落到 ConsoleNotifier —— 验证码被写进 backend.log，
        #   界面上还提示"已发送"。
        "alert_smtp_user": "noreply@example.com",
        "alert_smtp_auth_code": "unit-test-smtp-code",
    }
    base.update(overrides)
    # _env_file=None：不读仓库里的 .env，避免测试受本机配置影响
    return Settings(_env_file=None, env="prod", **base)  # type: ignore[arg-type]


def _pilot_settings(**overrides: object) -> Settings:
    """对外试点的一套"自洽"配置（自检应返回 0 条问题）。

    ⚠️ 与 prod 的差别**只有两条**：允许 SQLite、路径必须是独立的 pilot 库。
    其余门槛（真发邮件、禁请求头身份、禁 console 通道）完全一致 ——
    测试里刻意保持这种"只差两条"的形状，改动时会一眼看出来。
    """
    base: dict[str, object] = {
        "data_backend": "sqlite",
        "sqlite_path": "data/pilot/moss_pilot.db",
        "alert_smtp_user": "noreply@example.com",
        "alert_smtp_auth_code": "unit-test-smtp-code",
    }
    base.update(overrides)
    return Settings(_env_file=None, env="pilot", **base)  # type: ignore[arg-type]


#: pilot 自洽配置对应的环境变量视图
_PILOT_OK_ENV = {"MOSS_PILOT_SINGLE_INSTANCE_ACK": "1"}


# ======================================================================
# 环境名解析
# ======================================================================

def test_moss_env_is_actually_read() -> None:
    """★ 回归：`MOSS_ENV` 必须真的映射到 `Settings.env`。

    这一条防的是"自检静默失效"——`env` 与 pydantic-settings 的 `env_file`
    同处一个命名空间，若没写 `validation_alias`，环境变量会被忽略而**不报错**。
    """
    settings = Settings(_env_file=None, MOSS_ENV="prod")  # type: ignore[call-arg]
    assert settings.env == "prod", (
        "MOSS_ENV 没有被读进 Settings.env —— 检查 env 字段的 validation_alias")
    assert settings.is_prod


# ======================================================================
# ★ 环境变量优先级：防"测试写进生产库"
# ======================================================================

#: 所有"必须能从环境变量覆盖"的路径/密钥类字段 → (环境变量名, 与默认值不同的探针值)
#:
#: 这些都是曾经用 `os.environ.get(...)` 当**默认值**的字段（`_env_field` 之前
#: 的写法）。那种写法在导入时求值，pydantic 认为"有默认值就不用查环境"，
#: 于是运行期设的环境变量**完全无效**。
_ENV_OVERRIDABLE: list[tuple[str, str, str]] = [
    ("sqlite_path", "MOSS_SQLITE_PATH", "/tmp/probe-isolated.db"),
    ("local_quote_dir", "LOCAL_QUOTE_DIR", "/tmp/probe-quotes"),
    ("tushare_token", "TUSHARE_TOKEN", "probe-tushare-token"),
    ("alert_smtp_user", "ALERT_SMTP_USER", "probe@example.com"),
    ("alert_smtp_auth_code", "ALERT_SMTP_AUTH_CODE", "probe-auth-code"),
    ("alert_keywords", "ALERT_KEYWORDS", "探针,关键词"),
    ("deepseek_api_key", "DEEPSEEK_API_KEY", "probe-deepseek-key"),
]


@pytest.mark.parametrize("field,env_name,probe", _ENV_OVERRIDABLE)
def test_path_and_secret_fields_read_environment(
        field: str, env_name: str, probe: str, monkeypatch) -> None:
    """★ 回归：这些字段**必须**在运行期读环境变量（不能只在导入时读一次）。

    ## 为什么这条测试是本文件里最重要的之一

    曾经 `sqlite_path` 写成 `os.environ.get("MOSS_SQLITE_PATH", "data/...")`。
    它在脚本里、在 `python -c` 里**都正常**，唯独"先 import、后设环境变量"
    的场景失效 —— 而 **pytest 恰恰就是这种场景**
    （夹具用 `monkeypatch.setenv` 改环境变量）。

    后果不是"测试脏了"，而是：`tests/unit/test_auth_routes.py` 的夹具
    以为自己在用临时库，**实际一直在读写 `data/moss_finagent.db`**，
    夹具里的 `clear_all()` 真的清生产库的认证表。症状被伪装成
    "登录莫名 401""用户名已存在""偶发 429"，在"单独跑通过、全量跑偶发失败"
    之间摇摆，极难归因到"某个字段没读环境变量"。

    所以这条测试用**运行期 setenv**（而不是构造参数）来验证，
    这正是它区别于 `test_field_name_construction_still_works` 的地方。
    """
    from src.core.config import get_settings

    monkeypatch.setenv("MOSS_ENV", "test")
    monkeypatch.setenv(env_name, probe)
    get_settings.cache_clear()
    try:
        actual = getattr(get_settings(), field)
    finally:
        get_settings.cache_clear()
    assert actual == probe, (
        f"{env_name} 没有被读进 Settings.{field}（拿到 {actual!r}）。"
        f"检查该字段是否退回了 `os.environ.get(...)` 当默认值 —— "
        f"那样只有导入前设的环境变量才生效。"
        f"正确写法是 `_env_field(\"{env_name}\", <默认值>)`。")


def test_field_name_construction_still_works() -> None:
    """★ 反向约束：修环境变量优先级**不能**破坏按字段名构造。

    `Settings(sqlite_path=...)` 是本文件与其它测试大量使用的写法。
    给字段加 `validation_alias` 后，pydantic 默认**只认 alias**、字段名失效 ——
    必须同时开 `populate_by_name=True` 才能两者兼得。
    这条测试就是防止有人把 `populate_by_name` 删掉。
    """
    settings = Settings(_env_file=None, sqlite_path="data/dev/moss.db")
    assert settings.sqlite_path == "data/dev/moss.db", (
        "按字段名构造失效了 —— 检查 Settings.model_config 里的 populate_by_name")


def test_settings_loads_the_real_repo_sqlite_path_by_default() -> None:
    """没有环境变量时，默认值必须仍是生产库路径（不能被探针值污染）。"""
    settings = Settings(_env_file=None)
    assert settings.sqlite_path == "data/moss_finagent.db"


@pytest.mark.parametrize("raw,expected", [
    ("prod", "prod"),
    ("PROD", "prod"),
    ("  Prod  ", "prod"),
    ("production", "prod"),      # 常见误写，归一化到 prod
    ("dev", "dev"),
    ("test", "test"),
])
def test_env_normalization(raw: str, expected: str) -> None:
    assert Settings(_env_file=None, env=raw).env == expected


def test_env_must_be_explicit() -> None:
    """空环境名必须报错，不能静默回落 dev（否则生产会带着开发配置跑起来）。"""
    for bad in ("", "   ", None):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, env=bad)


def test_unknown_env_is_rejected_not_defaulted() -> None:
    """未知环境名报错，**不**静默兜底成 dev。"""
    with pytest.raises(ValidationError) as exc:
        Settings(_env_file=None, env="staging")
    assert "MOSS_ENV" in str(exc.value)
    assert "staging" in str(exc.value)


def test_envs_tuple_covers_all() -> None:
    assert set(ENVS) == {"dev", "test", "pilot", "prod"}


def test_pilot_is_not_dev() -> None:
    """`pilot` 是"对外"档，`is_public` 必须包含它。

    这是加第四档时最容易漏的一处：全项目"要不要按公网标准做安全"的判据
    都收敛在 `is_public` 上（Cookie 的 `Secure`、调试端点、OpenAPI 文档、
    登录门槛）。漏掉 pilot 就等于"公网实例按开发标准跑"。
    """
    assert Settings(_env_file=None, env="pilot").is_pilot is True
    assert Settings(_env_file=None, env="pilot").is_public is True
    assert Settings(_env_file=None, env="prod").is_public is True
    assert Settings(_env_file=None, env="dev").is_public is False
    assert Settings(_env_file=None, env="test").is_public is False


def test_pilot_is_single_instance_sqlite_only() -> None:
    """单实例事实要能从配置读出来（启动横幅与自省都依赖它）。"""
    assert Settings(_env_file=None, env="pilot",
                    data_backend="sqlite").is_single_instance is True
    assert Settings(_env_file=None, env="prod", data_backend="postgres",
                    postgres_dsn="postgresql://u:p@db.internal:5432/x"
                    ).is_single_instance is False


def test_describe_environment_labels_pilot_limits() -> None:
    """启动日志必须把 pilot 的限制说出来，而不是只在文档里承诺。"""
    from src.core.config import describe_environment

    text = describe_environment(Settings(_env_file=None, env="pilot"))
    for word in ("对外试点", "单实例", "不承诺 SLA"):
        assert word in text, f"pilot 的启动说明里缺了「{word}」：{text}"
    assert "绝不可对外" in describe_environment(
        Settings(_env_file=None, env="dev"))


# ======================================================================
# 生产自检：必须逐条命中
# ======================================================================

def test_prod_ok_config_passes() -> None:
    """自洽的生产配置：0 条问题（否则自检会变成"永远报警"而被人忽略）。"""
    assert assert_environment_consistency(_prod_settings(), environ=_PROD_OK_ENV) == []


def test_prod_rejects_sqlite() -> None:
    problems = assert_environment_consistency(
        _prod_settings(data_backend="sqlite"), environ=_PROD_OK_ENV)
    assert any("postgres" in p for p in problems)


def test_prod_rejects_localhost_db() -> None:
    problems = assert_environment_consistency(
        _prod_settings(postgres_dsn="postgresql://u:p@localhost:5432/x"),
        environ=_PROD_OK_ENV)
    assert any("localhost" in p for p in problems)


def test_prod_requires_tenancy_enforce() -> None:
    """未开强制鉴权 = 所有未带凭证的请求都会以本地开发身份放行。"""
    env = {k: v for k, v in _PROD_OK_ENV.items() if k != "MOSS_TENANCY_ENFORCE"}
    problems = assert_environment_consistency(_prod_settings(), environ=env)
    assert any("MOSS_TENANCY_ENFORCE" in p for p in problems)


def test_prod_requires_identity_secret() -> None:
    env = {k: v for k, v in _PROD_OK_ENV.items() if k != "MOSS_IDENTITY_SECRET"}
    problems = assert_environment_consistency(_prod_settings(), environ=env)
    assert any("MOSS_IDENTITY_SECRET" in p for p in problems)


def test_prod_forbids_header_identity() -> None:
    """请求头身份在生产必须关闭：客户端改一个头就能越权。"""
    env = dict(_PROD_OK_ENV, MOSS_ALLOW_HEADER_IDENTITY="1")
    problems = assert_environment_consistency(_prod_settings(), environ=env)
    assert any("HEADER_IDENTITY" in p for p in problems)


@pytest.mark.parametrize("value", ["1", "true", "YES", "on"])
def test_prod_forbids_console_notifier(value: str) -> None:
    """生产用 Console 通知通道 = 把验证码打进日志（直接泄露）。"""
    env = dict(_PROD_OK_ENV, MOSS_NOTIFY_CHANNEL="console")
    problems = assert_environment_consistency(_prod_settings(), environ=env)
    assert any("Console" in p or "日志" in p for p in problems)


def test_prod_allows_smtp_notifier() -> None:
    env = dict(_PROD_OK_ENV, MOSS_NOTIFY_CHANNEL="email")
    assert assert_environment_consistency(_prod_settings(), environ=env) == []


def test_prod_mismatch_reports_all_problems() -> None:
    """一次列全，而不是只报第一条 —— 否则用户要反复启动 N 次才配好。"""
    problems = assert_environment_consistency(
        _prod_settings(data_backend="sqlite",
                       postgres_dsn="postgresql://u:p@127.0.0.1:5432/x"),
        environ={"MOSS_ALLOW_HEADER_IDENTITY": "1"})
    assert len(problems) >= 5


# ======================================================================
# 非生产：防"反向误连"
# ======================================================================

@pytest.mark.parametrize("env_name", ["dev", "test"])
def test_non_prod_rejects_prod_looking_db(env_name: str) -> None:
    """调试实例打到生产库同样危险 —— 必须拒绝，而不是"你是 dev 所以随便"。"""
    settings = Settings(
        _env_file=None, env=env_name,
        postgres_dsn="postgresql://u:p@prod-db.internal:5432/moss")
    problems = assert_environment_consistency(settings, environ={})
    assert any("生产" in p for p in problems)


def test_non_prod_rejects_prod_sqlite_path() -> None:
    settings = Settings(_env_file=None, env="dev",
                        sqlite_path="data/prod/moss.db")
    problems = assert_environment_consistency(settings, environ={})
    assert any("prod" in p for p in problems)


def test_non_prod_does_not_require_prod_flags() -> None:
    """非生产**不该**因为"没开强制鉴权"而报错（那会影响本地开发体验）。"""
    settings = Settings(_env_file=None, env="dev", data_backend="sqlite",
                        sqlite_path="data/dev/moss.db")
    assert assert_environment_consistency(settings, environ={}) == []


# ======================================================================
# 单例与测试环境的一致性
# ======================================================================

def test_get_settings_returns_cached_singleton() -> None:
    assert get_settings() is get_settings()


def test_manage_test_env_is_self_consistent() -> None:
    """`manage.py test` 造出来的隔离环境必须能过自检（否则测试跑不起来）。"""
    from manage import build_test_env  # noqa: PLC0415 局部导入避免拖慢收集

    env = build_test_env("C:/tmp/unit")
    assert env["MOSS_ENV"] == "test"
    settings = Settings(_env_file=None, env=env["MOSS_ENV"],
                        sqlite_path=env["SQLITE_PATH"])
    assert assert_environment_consistency(settings, environ=env) == []


# ======================================================================
# 公网环境：没有真实邮件通道 = 验证码进日志（2026-09-23 补的真洞）
# ======================================================================

@pytest.mark.parametrize("env_name", ["prod", "pilot"])
def test_public_env_requires_real_smtp_credentials(env_name: str) -> None:
    """★ 缺 SMTP 凭据时通知会**回落到 ConsoleNotifier**，把验证码打进日志。

    原来的检查只看 `MOSS_NOTIFY_CHANNEL` 有没有被显式设成 console，
    而 `build_notifier` 在**完全没凭据**时也会退回 console（那是给 dev 的便利）。
    两者叠加的后果是：一个"prod 但忘了配邮箱"的部署能通过自检顺利上线，
    验证码全部写进 `data/run/backend.log`，而界面上提示"已发送"。
    对一个对外实例来说，这等于把登录凭据公开。
    """
    settings = (_prod_settings() if env_name == "prod"
                else _pilot_settings())
    settings = settings.model_copy(update={"alert_smtp_auth_code": ""})
    env = dict(_PROD_OK_ENV if env_name == "prod" else _PILOT_OK_ENV)
    problems = assert_environment_consistency(settings, environ=env)
    assert any("ALERT_SMTP_AUTH_CODE" in p for p in problems), problems


def test_public_env_with_smtp_passes() -> None:
    assert assert_environment_consistency(
        _pilot_settings(), environ=_PILOT_OK_ENV) == []


# ======================================================================
# pilot：对外试点档（门槛与 prod 同源，只放宽 SQLite）
# ======================================================================

def test_pilot_rejects_non_pilot_sqlite_path() -> None:
    """★ 独立库是硬要求：路径必须自带 pilot 字样。

    防的是最危险的"省事"——把客户用的试点实例指向 dev 库
    （客户数据与调试数据混在一起）或指向本机那个 6.4GB 的库。
    """
    problems = assert_environment_consistency(
        _pilot_settings(sqlite_path="data/dev/moss_dev.db"),
        environ=_PILOT_OK_ENV)
    assert any("pilot" in p for p in problems), problems


def test_pilot_requires_single_instance_acknowledgement() -> None:
    """必须显式承认"单实例、无 HA" —— 把隐式风险变成被记录的决定。"""
    problems = assert_environment_consistency(_pilot_settings(), environ={})
    assert any("MOSS_PILOT_SINGLE_INSTANCE_ACK" in p for p in problems), problems


def test_pilot_rejects_postgres_with_explanation() -> None:
    """试点只支持 SQLite，且要**说清原因**（后端未实现），不是含糊拒绝。"""
    problems = assert_environment_consistency(
        _pilot_settings(data_backend="postgres"),
        environ=_PILOT_OK_ENV)
    hit = [p for p in problems if "postgres" in p]
    assert hit, problems
    assert "尚未实现" in hit[0], hit[0]


def test_pilot_forbids_tenancy_enforce_with_actionable_reason() -> None:
    """★ 反向检查：pilot 上打开 MOSS_TENANCY_ENFORCE 会**锁死整站**。

    `TenancyMiddleware.resolve_principal` 只认 `Authorization: Bearer` 与
    开发用请求头，**不认会话 Cookie**，而前端从不发 Authorization。
    所以打开它之后每个浏览器请求都是 401 —— 连 `/`（登录页）都打不开，
    表现为"整个网站白屏 401"，而原因看上去像前端坏了。
    这条测试保证后人不会把这个坑重新挖开。
    """
    env = dict(_PILOT_OK_ENV, MOSS_TENANCY_ENFORCE="1")
    problems = assert_environment_consistency(_pilot_settings(), environ=env)
    hit = [p for p in problems if "MOSS_TENANCY_ENFORCE" in p]
    assert hit, problems
    assert "登录页" in hit[0] or "401" in hit[0], hit[0]


def test_pilot_forbids_header_identity() -> None:
    env = dict(_PILOT_OK_ENV, MOSS_ALLOW_HEADER_IDENTITY="1")
    problems = assert_environment_consistency(_pilot_settings(), environ=env)
    assert any("MOSS_ALLOW_HEADER_IDENTITY" in p for p in problems), problems


def test_pilot_forbids_console_notifier() -> None:
    env = dict(_PILOT_OK_ENV, MOSS_NOTIFY_CHANNEL="console")
    problems = assert_environment_consistency(_pilot_settings(), environ=env)
    assert any("Console" in p or "日志" in p for p in problems), problems


def test_pilot_does_not_require_prod_only_flags() -> None:
    """pilot 不该要求 Postgres / 身份密钥（那两样是 prod 的门槛）。

    否则运维会看到一组**无法满足**的要求（Postgres 后端还没实现），
    然后开始绕过自检 —— 自检一旦被绕过就彻底失去意义。
    """
    problems = assert_environment_consistency(_pilot_settings(),
                                             environ=_PILOT_OK_ENV)
    assert not any("postgres" in p for p in problems), problems
    assert not any("MOSS_IDENTITY_SECRET" in p for p in problems), problems


# ======================================================================
# ★ `--env` 自检必须针对**目标环境**（2026-09-23 实测复现的静默失效）
# ======================================================================

def test_temporary_environ_restores_absent_and_present(monkeypatch) -> None:
    """还原要区分"原本没有"与"原本有" —— 只 pop 会误删父进程的变量。"""
    from manage import _temporary_environ  # noqa: PLC0415

    monkeypatch.delenv("MOSS_ENV", raising=False)
    monkeypatch.setenv("MOSS_SQLITE_PATH", "keep-me.db")
    with _temporary_environ({"MOSS_ENV": "pilot",
                            "MOSS_SQLITE_PATH": "data/pilot/x.db"}):
        import os

        assert os.environ["MOSS_ENV"] == "pilot"
        assert os.environ["MOSS_SQLITE_PATH"] == "data/pilot/x.db"
    import os

    assert "MOSS_ENV" not in os.environ, "原本不存在 → 必须删掉"
    assert os.environ["MOSS_SQLITE_PATH"] == "keep-me.db", "原本存在 → 必须还原"


def test_manage_env_selfcheck_validates_the_target_environment(
    monkeypatch, capsys,
) -> None:
    """★ 回归：`--env pilot` 的自检必须**真的跑 pilot 的规则**。

    原实现用 `Settings()`（读**父进程**环境）做自检，只把注入后的环境
    当"标志位视图"传进去。于是父 shell 里没有 `MOSS_ENV` 时
    `Settings().env == "dev"` → `is_pilot`/`is_prod` 全为 False
    → **目标环境的规则整段不执行** → `problems == []` → 打印"自检通过"。

    也就是说 `manage.py start --env prod`（或 pilot）**从来没跑过生产自检**，
    而它自己还以为跑了。这正是自检本来要防的那类事故（"以为连的是测试、
    实际连的是生产"），只不过这次失效的是自检本身，且**没有任何迹象**。

    这里把 pilot 预设里"承认单实例"那一项抽掉：如果自检真的在按 pilot 的
    规则跑，就一定会报出来；如果它还在按 dev 跑，就会一条都不报。
    """
    import manage  # noqa: PLC0415

    monkeypatch.delenv("MOSS_ENV", raising=False)
    # 只注入环境名与路径，**故意不给** MOSS_PILOT_SINGLE_INSTANCE_ACK
    monkeypatch.setattr(manage, "pilot_isolation_env", lambda: {
        "MOSS_ENV": "pilot",
        "MOSS_SQLITE_PATH": "data/pilot/moss_pilot.db",
    })
    extra, rc = manage._prepare_environment("pilot")  # noqa: SLF001
    captured = capsys.readouterr()
    output = captured.out + captured.err

    assert extra["MOSS_ENV"] == "pilot"
    assert rc != 0, (
        "自检又变回空转了：pilot 的门槛一条都没跑。\n" + output)
    assert "MOSS_PILOT_SINGLE_INSTANCE_ACK" in output, output


def test_manage_env_selfcheck_does_not_leak_environ(monkeypatch) -> None:
    """自检期间注入的环境变量必须还原，不能污染调用它的 shell。"""
    import manage  # noqa: PLC0415

    monkeypatch.delenv("MOSS_ENV", raising=False)
    monkeypatch.setattr(manage, "pilot_isolation_env", lambda: {
        "MOSS_ENV": "pilot",
        "MOSS_SQLITE_PATH": "data/pilot/moss_pilot.db",
        "MOSS_PILOT_SINGLE_INSTANCE_ACK": "1",
    })
    manage._prepare_environment("pilot")  # noqa: SLF001

    import os

    assert "MOSS_ENV" not in os.environ
    assert "MOSS_PILOT_SINGLE_INSTANCE_ACK" not in os.environ


# ======================================================================
# test 环境必须真的隔离（CHG-0063，2026-09-28 补）
# ======================================================================
#
# 规则文档《数据库管理》§6 反模式第一条是「同库同账号，靠 env 字段区分」。
# 补这一档时的实测现场比该反模式更糟：`manage.py start --env test` **没有任何
# 隔离分支**，直接落 `MOSS_SQLITE_PATH` 的默认值 `data/moss_finagent.db`
# （manage.py 自己标注"生产用"），而 `manage.py test` 却走 `build_test_env()`
# 的临时目录 —— **同一个 `test` 两套语义**。

def test_start_test_env_is_isolated_and_self_consistent() -> None:
    """`--env test` 必须拿到独立目录，**且改完之后仍然起得来**。

    反向测试（"收紧判据"必须配的那条）：只证明"不隔离会被拒"是不够的 ——
    还要证明"隔离之后自检通过"，否则改动方向就是"把能跑的也一起拒了"。
    """
    import manage  # noqa: PLC0415

    env = manage.test_isolation_env()
    assert env["MOSS_ENV"] == "test"
    assert "test" in env["MOSS_SQLITE_PATH"].lower().replace("\\", "/"), env
    assert env["MOSS_SQLITE_PATH"].endswith("moss_test.db"), env
    # 不得落回默认主库
    assert "moss_finagent" not in env["MOSS_SQLITE_PATH"].lower(), env

    settings = Settings(_env_file=None, env="test",
                        sqlite_path=env["MOSS_SQLITE_PATH"])
    assert assert_environment_consistency(settings, environ=env) == [], (
        "隔离路径竟然过不了自检 —— 那是把能跑的也一起拒了")


def test_test_env_pointing_at_default_db_is_rejected() -> None:
    """`--env test` 指向默认主库 → 必须被拒（这就是补这一档的原始缺陷）。"""
    settings = Settings(_env_file=None, env="test",
                        sqlite_path="data/moss_finagent.db")
    problems = assert_environment_consistency(settings, environ={})
    assert any("test 环境必须真的隔离" in p for p in problems), problems


def test_all_three_isolation_envs_isolate_cache_and_audit() -> None:
    """dev / test / pilot 三档都必须把**缓存与两类审计**改道，且互不相同。

    为什么要断言"互不相同"而不是"各自非空"：三个实例指向同一个目录时，
    每个值都非空、断言照样通过 —— 而那正是要防的（共用缓存/审计）。
    """
    import manage  # noqa: PLC0415

    envs = {
        "dev": manage.dev_isolation_env(),
        "test": manage.test_isolation_env(),
        "pilot": manage.pilot_isolation_env(),
    }
    for name, env in envs.items():
        for key in ("MOSS_SQLITE_PATH", "LLM_CACHE_DIR", "MOSS_AUDIT_DIR",
                    "LLM_AUDIT_DIR", "SCHEDULER_DIR"):
            assert key in env, f"{name} 缺少 {key}"
            assert env[key].strip(), f"{name} 的 {key} 为空"
    for key in ("MOSS_SQLITE_PATH", "LLM_CACHE_DIR", "MOSS_AUDIT_DIR"):
        seen = [env[key].replace("\\", "/").lower() for env in envs.values()]
        assert len(set(seen)) == 3, f"{key} 三档没有互不相同：{seen}"

