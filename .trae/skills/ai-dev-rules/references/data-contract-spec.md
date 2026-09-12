# 数据契约规范

## 溯源元数据强制字段

每个数据点必须包含：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| data_id | UUID | 是 | 唯一标识 |
| source_name | String | 是 | 数据源名称 |
| source_url | String | 是 | 原始获取URL |
| publish_time | DateTime | 是 | 数据发布时间 |
| fetch_time | DateTime | 是 | 系统获取时间 |
| raw_content_hash | String | 是 | SHA256哈希 |
| confidence | Float | 是 | 0.0-1.0 |
| verified | Boolean | 是 | 是否交叉验证 |

## 数据分级标记

每个数据表/字段必须标记安全级别：

- CORE：核心数据，双人审批
- IMPORTANT：重要数据，角色管控
- SENSITIVE：敏感一般数据，团队内共享
- NORMAL：常规一般数据，全员可访问
