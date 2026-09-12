# 缓存设计规范

## 一、四级缓存架构

| 缓存层级 | 存储介质 | 特点 | 典型用途 | TTL策略 |
|----------|----------|------|----------|---------|
| L1 内存缓存 | Python dict / LRU Cache | 访问最快，进程内共享 | 热数据、频繁读取的小对象 | 60秒 |
| L2 本地文件缓存 | 磁盘文件（pickle/gzip） | 进程重启后可复用 | 历史行情、新闻全文 | 24小时 |
| L3 Redis缓存 | 分布式内存 | 多进程/多实例共享 | 跨实例共享的行情与中间结果 | 5-30分钟 |
| L4 数据库缓存 | PostgreSQL / SQLite | 长期持久化，支持索引查询 | 重要数据、审计日志 | 永久（按合规要求） |

## 二、缓存策略规则

- **读缓存优先**：所有数据查询必须先查缓存，命中则直接返回，未命中才穿透到上游数据源。
- **写缓存失效**：数据更新时，必须同时失效所有层级的关联缓存。使用发布-订阅模式通知所有缓存节点。
- **缓存预热**：系统启动时，自动将高频查询的数据（如核心指数行情、宏观指标）预加载到L1和L3缓存。
- **缓存降级**：Redis不可用时，自动降级到L2本地文件缓存；L2也不可用时，直接查询数据库并记录告警。

## 三、缓存键设计规则

缓存键必须遵循统一格式：

```
{tenant_id}:{data_type}:{indicator}:{date_range}:{params_hash}
```

- 必须包含租户ID，确保多租户隔离。
- `params_hash`为查询参数的SHA256哈希，确保不同查询参数对应不同缓存键。
- 禁止使用模糊匹配的缓存键，避免缓存击穿。

## 四、缓存命中率监控

- 每层缓存必须记录：hit_count、miss_count、hit_rate。
- 目标命中率：L1 ≥ 60%，L3 ≥ 80%，综合命中率 ≥ 85%。
- 命中率低于目标值时，自动分析原因并输出优化建议。

## 五、缓存一致性规则

| 数据类型 | 一致性延迟上限 |
|----------|---------------|
| 实时行情 | ≤ 1秒 |
| 宏观数据 | ≤ 5分钟 |
| 财务数据 | ≤ 1小时 |

超过一致性延迟上限时，必须强制刷新缓存。

## 六、实现示例

```python
class CacheManager:
    def __init__(self):
        self.l1 = LRUCache(maxsize=1000)
        self.l2 = FileCache("cache/")
        self.l3 = RedisCache("localhost:6379")

    async def get(self, key: str, tenant_id: str):
        # L1查询
        if key in self.l1:
            return self.l1[key]
        # L2查询
        if self.l2.exists(key):
            value = self.l2.get(key)
            self.l1[key] = value
            return value
        # L3查询
        value = await self.l3.get(key)
        if value:
            self.l1[key] = value
            self.l2.set(key, value)
            return value
        return None

    async def set(self, key: str, value, ttl: int):
        self.l1[key] = value
        self.l2.set(key, value, ttl)
        await self.l3.set(key, value, ttl)

    async def invalidate(self, pattern: str):
        self.l1.clear()
        self.l2.clear(pattern)
        await self.l3.delete_pattern(pattern)
```
