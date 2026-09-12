# 数据契约

## 一、溯源元数据

每个数据点必须携带以下元数据：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| data_id | UUID | 是 | 唯一标识 |
| source_name | String | 是 | 数据源名称 |
| source_url | String | 是 | 原始获取URL |
| source_type | Enum | 是 | official/api/report/news |
| publish_time | DateTime | 是 | 数据发布时间 |
| fetch_time | DateTime | 是 | 系统获取时间 |
| fetch_method | Enum | 是 | web_crawl/api_call/manual_input |
| raw_content_hash | String | 是 | SHA256哈希 |
| processed_by | String | 是 | 处理Agent |
| process_time | DateTime | 是 | 处理时间 |
| confidence | Float | 是 | 0.0-1.0 |
| verified | Boolean | 是 | 是否交叉验证 |

## 二、数据分级

| 级别 | 定义 | 示例 | 访问要求 |
|------|------|------|----------|
| CORE | 核心数据 | 未公开政策预判、核心策略逻辑 | 双人审批 |
| IMPORTANT | 重要数据 | 产业链图谱、内部研报 | 角色管控 |
| SENSITIVE | 敏感一般数据 | 个股分析报告、财务数据 | 权限管控 |
| NORMAL | 常规一般数据 | 公开行情、已发布研报摘要 | 基础控制 |

## 三、数据更新频率

| 数据类型 | 更新频率 |
|----------|----------|
| GDP、CPI、PPI、PMI | 随官方发布 |
| 社融、M2 | 每月10-15日 |
| 行业库存、产能利用率 | 月度 |
| 产业链价格 | 日频/周频 |
| 实时行情 | 实时（≤500ms） |
| 财务数据 | T+1日 |
| 估值指标 | 每日盘后 |
| 厄尔尼诺指数 | 月度/周度 |

## 四、数据源清单

| 数据类型 | 数据源 | 获取方式 |
|----------|--------|----------|
| 宏观经济 | 国家统计局 data.stats.gov.cn | API/网页 |
| CPI/PPI | 国家统计局数据发布库 | API |
| 央行数据 | 央行 www.pbc.gov.cn | 网页 |
| 全球流动性 | FRED fred.stlouisfed.org | API |
| 厄尔尼诺 | NOAA psl.noaa.gov | 文件下载 |
| A股行情 | AkShare / Tushare Pro / BaoStock | API |
| 券商研报 | 慧博投研 www.hibor.com.cn | 订阅 |
| 大宗商品 | 生意社 www.100ppi.com | 网页 |
