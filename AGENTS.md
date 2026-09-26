# Moss-FinAgent-Research 项目规则

## 项目定位
基于多Agent协作的AI辅助投研分析系统。面向二级市场提供投资分析与决策。

## 环境配置
- 本地运行Ollama（模型以 configs/models.yaml 为准：qwen2.5:1.5b-instruct-q4_K_M、qwen3:8b-q4_K_M）
- 存储默认SQLite零依赖；PostgreSQL/Redis为可选（DATA_BACKEND=postgres / REDIS_CACHE_ENABLED=true）
- 本地行情：**2026-09-23 起 QMT 排在所有链路的「最后一位」且默认关闭**。原因：本机
  QMT 终端失去行情权限、不再运行（`127.0.0.1:58610` 拒连），且**短期内无法恢复** ——
  放在任何位置之前都只会贡献一次必然失败的 xtquant 连接等待（实测 4~5s）。
  - 现行日线链：AkShare → 腾讯 → Tushare → baostock → 本地CSV → **[QMT 开关]（最末）**
  - 现行分钟/分时/快照链：腾讯 → 新浪 → 东财 → **[QMT 开关]（最末）**
  - ⚠️ **顺序与开关表达同一个意图**，两处都要改对：`configs/intraday.yaml` 的
    `data.intraday_sources` 把 `qmt` 写在**最后**、`data.qmt_enabled: false`。
    因为 `health.rank` 在样本 <3 次时**照抄配置顺序**当先验，所以"配置顺序 =
    默认顺序" —— 只改开关不改顺序，QMT 仍会顶到链首。
  - 将来权限恢复：`QMT_ENABLED=1` + `data.qmt_enabled: true` 即可在链尾兜底
  - 全市场全量下载：`scripts/download_market_data.py`（**取代** `scripts/download_qmt_data.py`，
    落盘到项目自己的 `data/quant/prices*/`，不再依赖 QMT 私有目录）
  - 东财可用性依赖 `MOSS_EM_DIRECT`（TLS SNI 阻断规避，可写 `.env`）；
    但该阻断会漂移，**东财只能当链尾备用**，见 `src/core/eastmoney_direct.py`
  - ⚠️ 竞价链的 `DataConfig.fallback` 已从 `"qmt"` 改为 `"none"`：竞价逐秒序列
    **没有免费替代源**（QMT tick 需权限、eltdx 只有盘中实时、Tushare 无竞价过程），
    如实登记"当前无备源"，不要留一个永远失败的 `"qmt"`
- 核心推理调用DeepSeek-V4-Flash API（DEEPSEEK_API_KEY 经环境变量注入）

## 架构规范
- 采用LangGraph StateGraph搭建Supervisor调度架构
- 18个专业Agent分5层：数据层(A01-A04)、信息层(A05-A07)、分析层(A08-A12)、行业层(A13-A16)、决策层(A17)、审计层(A18)
- Agent间通过标准JSON消息通信，基于MCP协议调用工具
- Demo阶段采用单进程分层架构（src layout），Agent以进程内模块注册到Supervisor，接口无状态；保留演进为独立FastAPI微服务的路径

## 编码规范
- Python 3.10+，使用类型注解
- 所有Agent必须实现统一的BaseAgent接口
- 所有数据访问必须通过统一数据层，禁止直接连接数据库
- 禁止硬编码敏感信息（API Key、数据库密码）
- 所有分析结论必须附带数据溯源标签和推理路径记录

## 数据溯源规范
- 每个数据点必须包含source_url、publish_time、fetch_time、raw_content_hash
- 数据处理链路必须记录每一步的Agent ID和操作类型
- LLM推理必须记录prompt_hash和token数
- 最终结论必须引用所有使用的数据ID

## 安全合规规范
- 所有输出附带免责声明
- 多租户数据通过RLS策略自动隔离
- 审计日志独立存储，不可篡改
- 敏感数据操作日志保存不低于3年

## Trae使用规范
- 使用Spec模式进行系统级开发
- 使用Task工具并行派发多个Agent开发任务
- 每个Agent独立开发、独立测试、独立部署

## Skills引用
@.trae/skills/ai-dev-rules/SKILL.md
@.trae/skills/dev-standards/SKILL.md
@.trae/skills/dev-standards/references/architecture-naming-spec.md
