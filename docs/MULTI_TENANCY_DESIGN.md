# 多租户与合规设计（面向机构内部量化部门）

> 本文说明**为什么这样设计**，落地位置与验证方式见每节末尾的"落地/验证"。
> 面向读者：面试官、接手的新同学、合规与信息安全评审。

---

## 零、一句话背景

这个系统是**机构内部**的投研与量化平台，不是面向公众的 SaaS。这个前提决定了
隔离的重点与商用系统**不一样**：

| | 面向公众的 SaaS | 机构内部量化平台 |
|---|---|---|
| 主要风险 | 客户 A 看到客户 B 的数据 | **研究员看到别人的在研策略**；**投资端看到研究端的未发布结论** |
| 监管焦点 | 个保法、数据出境 | **《证券法》第 54 条（利用未公开信息交易）**、信息隔离墙、留痕 |
| 隔离粒度 | 租户 = 客户公司 | **租户 = 部门/策略组**，且**同租户内还要按清关等级与隔离墙再分** |
| 审计要求 | 一般留存 | **留痕至少 5 年**（很多机构按 20 年执行），且不可篡改 |
| 最怕的事 | 数据泄漏 | **越权访问留不下痕迹**——查不出来比泄漏本身更致命 |

---

## 一、行业实践：机构是怎么做的

### 1.1 三层隔离，缺一不可

```
网络层   生产网 / 办公网 / 研究网 分区，跨区走堡垒机
   ↓
数据库层  PostgreSQL 原生 RLS（CREATE POLICY）+ 每连接设置 app.tenant_id
   ↓
应用层   请求级 Principal + 每查询下推 tenant_id 条件
```

**最常见的错误是"只做应用层"**，然后因为某条原生 SQL、某个后台脚本、
某次运维直连而破功。反过来"只做数据库层"也不行：策略层的信息隔离墙
（谁能看哪类内容）在 SQL 里表达不了。

> **落地**：`postgres_rls_ddl()` 生成数据库层策略；
> `tenant_column_guard()` 做应用层下推；`TenancyMiddleware` 注入请求级身份。
> **验证**：`tests/unit/test_tenancy.py` 断言"未登记的表必须报错而不是放行"。

### 1.2 权限模型：RBAC × ABAC × 隔离墙

三个维度**正交**，不要混成一个：

| 维度 | 回答的问题 | 本项目的落地 |
|---|---|---|
| **RBAC**（角色） | 这个人**能做什么动作** | `Role` × `_POLICY` 动作矩阵 |
| **ABAC**（属性） | 这个人**能看什么等级的数据** | `DataClass` vs `Principal.clearance` |
| **信息隔离墙** | 这个人**能看到哪个部门的在研内容** | `WallGroup` + `WallCrossing` |

**为什么必须分开**：合规岗权限高（要看全部审计），但**不需要**看策略源码；
PM 权限低，但**需要**看持仓。如果把"级别"和"角色"合成一个维度，
必然出现"越权即升权"——为了让他看持仓而给了策略权限。

> **落地**：`src/core/policy.py:authorize()`。
> **验证**：`test_clearance_is_independent_from_role` 断言低清关研究员
> 即使角色正确也读不到 CONFIDENTIAL 策略。

### 1.3 信息隔离墙（Chinese Wall）

《证券法》第 54 条禁止利用未公开信息交易。机构的标准做法：

- **观察池 / 限制池**：进入限制池的标的全公司禁止交易；
- **跨墙审批**：研究转投资、或投资要看研究在研结论，必须经合规审批并**留痕**；
- **控制部门天然跨墙**：风控/合规/审计不跨墙就没法履职，但要"只读 + 全留痕"。

**本项目的关键设计**：跨墙不是一个配置项，而是一个**必须显式传参的对象**：

```python
authorize(READ_STRATEGY, resource, wall_crossing=WallCrossing(
    approver="compliance-01", reason="监管核查"))
```

不传 `WallCrossing` 就拒绝，且 `approver/reason` 为必填、会自动盖时间戳并进审计。
这样"跨墙"在代码里**无法悄悄发生**。

> **验证**：`test_wall_crossing_requires_explicit_credential`。

### 1.4 四眼原则（双人复核）

高风险动作——**建租户、改权限、接新数据源、批量导出**——单次授权通过**不等于可以执行**，
必须走双人复核。本项目把它做成**声明式标注**：`authorize()` 返回
`requires_four_eyes=True`，调用方必须据此走审批流。

> **为什么批量导出要单独管**：逐条权限检查挡不住"循环调 1000 次导出接口"。
> 导出是数据外泄的主通道，必须单独授权 + 单独审计 + 双人复核。
> **验证**：`test_export_and_admin_need_four_eyes`。

---

## 二、本项目的落地

### 2.1 身份只能由服务端派生

```python
# ❌ 业界最常见的越权写法（本项目**刻意不提供**这个接口）
tenant_id = request.headers.get("X-Tenant-Id", "default")

# ✅ 凭证 → 校验 → 派生 Principal → contextvar
principal = resolve_principal(request)   # Bearer token 优先
if principal is None and enforcement:    # 默认拒绝，不降级到 default 租户
    return 401
```

`src/core/tenancy.py` **不暴露**任何"从请求头直接设置租户"的公开接口。
开发期需要请求头声明身份时，必须显式设 `MOSS_ALLOW_HEADER_IDENTITY=1`，
且该身份会被标记 `auth_source=header-dev` 并**打 WARN 日志**。

**未认证不是"默认租户"，而是一个可检测状态**：

```python
_UNAUTHENTICATED = Principal(..., authenticated=False)   # 哨兵
def require_principal() -> Principal:                     # 业务代码统一用它
    if principal is _UNAUTHENTICATED:
        raise TenantError("当前上下文没有已认证身份")
```

> **验证**：`test_unauthenticated_is_detectable_not_silent`。

### 2.2 缓存是跨租户泄漏的重灾区

内容型缓存（LLM 响应、行情、研报）如果只按内容做 key，A 的请求会命中 B 的条目。
所有内容型缓存必须走 `tenant_cache_key()`，它做**两级隔离**：

| 内容等级 | key 包含 | 理由 |
|---|---|---|
| < CONFIDENTIAL | `tenant` | 部门内共享是合理的，加清关等级只会白白降低命中率 |
| ≥ CONFIDENTIAL | `tenant + clearance` | 同部门里合规岗与实习生的清关不同，共用等于间接泄漏 |

> **验证**：`test_tenant_cache_key_isolates_tenants` +
> `test_sensitive_cache_key_also_isolates_by_clearance`。

### 2.3 RLS 必须诚实

**本模块此前是个假实现**：`_inject_tenant_filter` 取了租户、遍历了表，
然后 `return stmt` —— docstring 声称注入了 `WHERE tenant_id = ?`，实际什么都没做。
**那比没有更危险**，因为它让人以为已经隔离了。

重写后的原则：

1. **只做它真正能做到的事**：查询期下推（`tenant_column_guard`）+ 碰租户表却无身份时报错；
2. **不做隐式语句改写**：自动往任意 SELECT 插 `WHERE` 会破坏 JOIN/聚合/迁移，
   而且很容易写出"被 UNION 绕过"的假隔离；
3. **未登记的表直接抛错**：让"忘了登记"表现为失败，而不是静默没有隔离；
4. **边界写在文档里**：应用层 RLS 防疏漏，**数据库原生策略才是边界**。

> **验证**：`test_rls_is_not_a_noop`（断言租户条件非空且参数正确下推）、
> `test_undeclared_table_raises_not_silently_passes`。

### 2.4 审计：独立存放 + 哈希链 + 租户维度

| 要求 | 落地 |
|---|---|
| 独立存储 | `data/audit/access_audit.jsonl`，与业务库**分开**（同库同权限等于没审计） |
| 不可篡改 | 每条带 `chain_hash = H(前一条 hash ‖ 本条内容)`，改一条后面全对不上 |
| 可归因 | 记录 `user_id / tenant_id / roles / clearance / wall / auth_source` |
| 覆盖完整 | 埋点在**中间件**而非各路由 —— 保证没有路由能绕过 |
| 失败要吵 | 审计写失败打 `logger.error`，**绝不静默** |

> **验证**：`test_access_audit_is_hash_chained`。

### 2.5 日志脱敏：把红线变成代码

`SECURITY_COMPLIANCE.md` 的红线写着"禁止在日志中输出用户隐私数据、完整 Prompt 内容"，
但仓库里此前**没有任何实现**——而 `AGENTS.md` 同时要求记录 `prompt_hash`。
两者放一起就会出现"为了合规而把完整 prompt 写进日志"的荒诞结果。

`src/core/redaction.py` 提供写侧脱敏（**不在读侧过滤**：读侧意味着数据已落盘）：

- `redact_prompt()`：只留**前缀 80 字 + 长度 + 内容哈希**。前缀够聚类、
  哈希够做缓存归因、长度够发现"prompt 异常膨胀"，正文不落盘。
- `redact()`：API Key / 身份证 / 手机号 / 银行卡 / 邮箱。
- `safe_extra()`：结构化字段按**键名白名单**打码（含中文键 `持仓`/`密码`）。
- `assert_no_secret()`：门禁用检测器。

> **验证**：`tests/unit/test_compliance_gate.py`。

---

## 三、合规门禁（CI 会拦）

合规要求写在文档里没人执行，写成测试就有人执行。

`.github/workflows/ci.yml` 里有独立的 **`compliance` job**，失败**不允许合并**
（与普通单测失败的处置不同）。它跑 `tests/unit/test_compliance_gate.py`，
每条用例对应一条要求：

| 用例 | 对应要求 |
|---|---|
| `test_no_hardcoded_secrets` | `AGENTS.md`：禁止硬编码 API Key / 数据库密码 |
| `test_env_example_has_no_real_values` | 同上，扫 `.env.example` |
| `test_prompt_redaction_keeps_audit_value_without_body` | 红线：禁止输出完整 Prompt |
| `test_log_leak_detector_catches_raw_secrets` | 门禁自身必须有效（防安全幻觉） |
| `test_rls_is_not_a_noop` | 多租户必须真隔离 |
| `test_unauthenticated_never_silently_degrades` | 零信任：不静默降级 |
| `test_access_audit_is_hash_chained` | 审计不可篡改 |
| `test_no_string_interpolated_sql_in_tenant_tables` | 所有查询参数化 |
| `test_disclaimer_exists_in_readme_and_api` | 所有输出附免责声明 |

**门禁上线当天就抓到三个真问题**（都在 `.env.example`）：

1. `POSTGRES_PASSWORD=moss_finagent` —— 一个**能用的默认密码**，会诱导部署时直接照抄；
2. `ALERT_EMAIL_TO=2693888583@qq.com` —— **真实个人邮箱**进了版本库；
3. `DEEPSEEK_API_KEY=sk-your-deepseek-key-here` —— 形似真 key，
   且让"占位符该长什么样"没有明确约定。

三条都已修（统一改成 `REPLACE_ME` 并加注释说明约定）。

---

## 四、部署检查清单

上线前逐条确认（生产环境）：

```bash
# 1. 强制鉴权必须打开
MOSS_TENANCY_ENFORCE=1

# 2. 身份签名密钥必须来自密钥管理服务（不是 .env）
MOSS_IDENTITY_SECRET=$(vault kv get -field=secret secret/moss/identity)

# 3. 开发用的请求头身份必须关闭
MOSS_ALLOW_HEADER_IDENTITY=      # 留空

# 4. 审计目录独立挂载（只追加、独立备份、独立权限）
MOSS_AUDIT_DIR=/var/log/moss/audit

# 5. 数据库层 RLS 策略已执行
python -c "from src.infrastructure.security.rls import postgres_rls_ddl; \
           print(postgres_rls_ddl())" | psql -U dba moss
```

| 项 | 未做时的后果 |
|---|---|
| 强制鉴权未开 | 所有人都是 `local` 租户的研究员身份 |
| 签名密钥写在 `.env` | 泄漏 `.env` = 可伪造任意身份 |
| 请求头身份未关 | 客户端改 `X-Tenant-Id` 即越权（**最严重**） |
| 审计目录与业务同卷 | 一次 `rm -rf` 连审计一起没 |
| 未执行 RLS DDL | 只有应用层防线，裸 SQL 与运维直连可绕过 |

---

## 五、已知不足与演进

| 不足 | 现状 | 演进 |
|---|---|---|
| 身份验证是 HMAC 签名载荷 | 自实现、无过期、无吊销 | 换机构 IdP 的 OIDC / JWT（保持 `principal_from_token` 签名不变即可） |
| 无跨租户审计员视图 | 审计员需换租户上下文 | 加"审计员只读跨租户"的显式接口 + 更强留痕 |
| 四眼原则只做标注 | 没有内置审批流 | 接工单系统；`requires_four_eyes` 已给出接入点 |
| RLS 只覆盖已登记的表 | 新表需手动登记 | 加 CI 检查：新增表必须有租户列或写进 `GLOBAL_TABLES` |
| 观察池/限制池未接入交易链路 | 只有授权层 | 接交易前置校验（本系统不含下单，属执行层） |
| 数据出境未涉及 | 数据源全在境内 | 若接入境外数据源需单独评估 |
