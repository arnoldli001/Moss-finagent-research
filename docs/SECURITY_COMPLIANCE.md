# 安全合规设计

> 面向**机构内部**投研与量化平台。设计依据与行业实践对照见
> [`MULTI_TENANCY_DESIGN.md`](MULTI_TENANCY_DESIGN.md)；本文只列**要求 → 落地 → 验证**。

---

## 一、设计原则

| 原则 | 落地 | 验证 |
|---|---|---|
| **零信任**：不信任任何网络位置 | 每个请求都要凭证；无凭证直接 401，**不降级到默认租户** | `test_unauthenticated_never_silently_degrades` |
| **默认拒绝** | 未在策略表声明的 (角色, 动作) 一律拒绝 | `test_default_deny_for_undeclared_action` |
| **数据分级驱动权限** | `DataClass` 四级（PUBLIC/INTERNAL/CONFIDENTIAL/RESTRICTED）× `Principal.clearance` | `test_clearance_is_independent_from_role` |
| **审计不可篡改** | JSONL 哈希链，独立目录存放 | `test_access_audit_is_hash_chained` |
| **最小权限** | 动作级授权；列表场景用 `visible_data_class()` 做**下推**过滤 | `test_role_matrix_blocks_cross_desk_access` |
| **纵深防御** | 网络 → 数据库原生 RLS → 应用层 RLS，三层都要有 | `test_rls_is_not_a_noop` |

---

## 二、数据分级（四级制）

| 级别 | `DataClass` | 定义与示例 | 访问要求 |
|---|---|---|---|
| 核心数据 | `RESTRICTED` | 未公开政策预判、核心策略逻辑、MNPI | **双人复核 + 显式跨墙审批** |
| 重要数据 | `CONFIDENTIAL` | 产业链图谱、内部研报、策略参数、持仓 | 角色管控 + **清关等级校验** |
| 敏感一般数据 | `INTERNAL` | 个股分析报告、财务数据 | 权限管控 |
| 常规一般数据 | `PUBLIC` | 公开行情、已发布研报摘要 | 基础访问控制 |

**关键点：级别与角色是两个正交维度。** 合规岗级别高但**看不到策略源码**；
PM 级别够但**看不到研究端的在研结论**（隔离墙）。混成一个维度会导致"越权即升权"。

---

## 三、多租户隔离

### 三层结构

```
网络层    生产 / 办公 / 研究分区，跨区走堡垒机        （部署要求，见 MULTI_TENANCY_DESIGN 第四节）
   ↓
数据库层  PostgreSQL 原生 RLS + 每连接 app.tenant_id  （rls.postgres_rls_ddl() 生成）
   ↓
应用层    请求级 Principal + 查询期下推 tenant_id     （TenancyMiddleware + tenant_column_guard）
```

### 关键约束

- **身份只能服务端派生**：`src/core/tenancy.py` 不提供"从请求头设置租户"的公开接口；
  开发期用请求头需显式开 `MOSS_ALLOW_HEADER_IDENTITY=1`，且该身份被标记并告警。
- **未登记的表必须报错**：让"忘了加隔离"表现为失败，而不是静默无隔离。
- **缓存必须带租户**：内容型缓存走 `tenant_cache_key()`；`CONFIDENTIAL` 以上还要带清关等级。
- **应用层 RLS 不是安全边界**：它防疏漏，数据库原生策略才是边界。两者缺一不可。

### 权限模型

**RBAC × ABAC × 信息隔离墙**，三个维度正交：

| 维度 | 问题 | 实现 |
|---|---|---|
| RBAC | 能做什么动作 | `Role` × `_POLICY` 矩阵 |
| ABAC | 能看什么等级 | `DataClass` vs `clearance` |
| 隔离墙 | 能看哪个部门的在研内容 | `WallGroup` + `WallCrossing` |

---

## 四、安全红线（均由 CI 合规门禁强制）

| 红线 | 落地 | 验证用例 |
|---|---|---|
| 禁止硬编码 API Key / 密码 / Token | 全部走环境变量或密钥管理 | `test_no_hardcoded_secrets` |
| 禁止日志输出隐私数据 / 完整 Prompt / 模型原始响应 | `src/core/redaction.py`（**写侧**脱敏） | `test_prompt_redaction_keeps_audit_value_without_body` |
| 所有 LLM 输出附置信度与免责声明 | 网关统一注入 | `test_disclaimer_exists_in_readme_and_api` |
| 核心数据访问触发双人审批 | `authorize()` 返回 `requires_four_eyes` | `test_export_and_admin_need_four_eyes` |
| 所有数据库查询参数化 | 租户条件走 `?` 占位符下推 | `test_no_string_interpolated_sql_in_tenant_tables` |

> ⚠️ **脱敏必须在写入侧**。读侧过滤意味着敏感数据已经落盘，一次 `cat` 就泄漏。
> 代价是"事后想分析原文时没有了"——所以 `redact_prompt` 保留前缀、长度与内容哈希，
> 兼顾审计价值与不落正文。

---

## 五、审计要求

| 要求 | 落地 |
|---|---|
| 独立存储 | `data/audit/access_audit.jsonl`（与业务库分开，独立备份与权限） |
| 防篡改 | 每条 `chain_hash = H(前条 hash ‖ 本条内容)` |
| 可归因 | `user_id / tenant_id / roles / clearance / wall / auth_source / 结果 / 耗时` |
| 覆盖完整 | 埋点在**中间件**，没有路由能绕过 |
| 留存期 | 生产按 ≥5 年配置（`AGENTS.md` 要求 ≥3 年）；归档与只读挂载由运维执行 |
| 写失败要吵 | `logger.error`，**绝不静默** |

---

## 六、合规检查清单

上线前逐条确认（生产环境）：

- [ ] `MOSS_TENANCY_ENFORCE=1`（强制鉴权已开）
- [ ] `MOSS_IDENTITY_SECRET` 来自密钥管理服务，**不在 `.env`**
- [ ] `MOSS_ALLOW_HEADER_IDENTITY` **留空**（开发用身份已关）
- [ ] `MOSS_AUDIT_DIR` 独立挂载、只追加、独立备份
- [ ] 数据库层 RLS 策略已执行（`postgres_rls_ddl()`）
- [ ] 所有新表已在 `rls.TENANT_COLUMNS` 登记，或写进 `GLOBAL_TABLES` 并注明原因
- [ ] 合规门禁 CI job 为 required check（失败不允许合并）
- [ ] 所有输出附带免责声明
- [ ] 敏感数据操作日志留存 ≥5 年
- [ ] 无硬编码敏感信息
- [ ] 所有数据库查询参数化
- [ ] 所有数据点携带溯源元数据（`source_url / publish_time / fetch_time / raw_content_hash`）

---

## 七、本模块的演进边界（诚实说明）

| 项 | 现状 | 说明 |
|---|---|---|
| 身份验证 | HMAC 签名载荷，无过期/吊销 | 足以表达"服务端派生、不可伪造"，**不是生产级 IdP**；替换点已隔离在 `principal_from_token()` |
| 四眼原则 | 只有声明式标注 | 没有内置审批流；`requires_four_eyes` 是接入点 |
| 观察池/限制池 | 只有授权层 | 本系统不含下单，交易前置校验属执行层 |
| 数据出境 | 未涉及 | 数据源全在境内 |
| 多租户的数据库策略 | 提供 DDL，由运维执行 | 应用不会自动建策略（DDL 变更不应由业务进程执行） |
