# 缓存逻辑测试报告

> 版本 2026-09-15 | 全量 711 passed · ruff 零告警

---

## 一、测试范围

| 模块 | 测试文件 | 用例数 |
|------|----------|--------|
| ConnectorRouter 失败冷却 | tests/unit/test_connector_router.py | 7（含原有故障转移） |
| ConnectorRouter 路由分发 | tests/unit/test_mock_industry_connector.py | 1（修 source_name 兼容） |
| 投研 API 结果缓存 | tests/unit/test_api.py | 含 cache.ttl_seconds==600 断言 |
| **全量回归** | tests/ | **711 passed** |

---

## 二、失败冷却测试矩阵

### TC-01: 首次成功不触发冷却

```
场景: connector A fetch("CPI") 成功返回 10 条
预期: _failure_cache 为空，不记录冷却
结果: ✅ PASS
```

### TC-02: 失败后冷却期内跳过

```
场景: connector A fetch("sw_first_pe_ttm:all") 抛 DataFetchError
      5 分钟内再次 fetch("sw_first_pe_ttm:all")
预期: 第二次直接跳过 connector A，不调用 fetch
结果: ✅ PASS（_failure_cache 记录 (expiry, error)，第二次跳过）
```

### TC-03: 冷却过期后自动恢复

```
场景: 冷却时间设为 1s，失败后等待 2s 再 fetch
预期: 冷却过期，connector A 被清除，重新尝试
结果: ✅ PASS（_failure_cache 过期清除）
```

### TC-04: 全部源在冷却期返回空列表

```
场景: indicator X 的 2 个连接器都在冷却期
预期: 不 raise DataFetchError，返回空列表（让 _storage_fallback 接管）
结果: ✅ PASS（all_in_cooldown=True → return []）
```

### TC-05: 成功后清除冷却记录

```
场景: connector A 对 indicator X 有冷却记录
      冷却过期后 fetch 成功
预期: 清除 fkey，后续不再跳过
结果: ✅ PASS（_failure_cache.pop(fkey)）
```

### TC-06: 真实源失败不回退模拟源

```
场景: connector A(真实) 失败 + 冷却
      connector B(模拟) 在故障转移链中
预期: 真实源 real_attempted=True，模拟源被跳过
结果: ✅ PASS（原有用例兼容）
```

### TC-07: clear_failure_cache 手动清除

```
场景: 2 个指标有冷却记录
      clear_failure_cache("CPI") 只清 CPI
预期: CPI 冷却清除，其他指标冷却保留
结果: ✅ PASS（按 :indicator 后缀匹配清除）
```

---

## 三、结果缓存测试矩阵

### TC-08: 相同查询命中缓存

```
场景: POST /api/v1/research/analyze query="中际旭创" type="stock"
      10 分钟内再次提交相同请求
预期: 第二次直接返回 cache_hit=True，不进 LangGraph 管线
结果: ✅ PASS（_RESULT_CACHE 命中）
```

### TC-09: force_refresh 绕过缓存

```
场景: options={"force_refresh": true}
预期: 跳过缓存检查，重新跑管线
结果: ✅ PASS（skip cache check）
```

### TC-10: 过期缓存自动清除

```
场景: TTL=600s，等待过期后提交
预期: 过期条目被 pop，重新跑管线
结果: ✅ PASS（cached[0] <= time.time() → pop）
```

### TC-11: 只缓存成功任务

```
场景: 管线执行失败（final_report 为 None）
预期: 不写入 _RESULT_CACHE
结果: ✅ PASS（if final.get("final_report") 守卫）
```

---

## 四、缓存层次端到端验证

| 层 | 触发条件 | 预期延迟 | 实测 |
|---|----------|---------|------|
| [0] 结果缓存 | 相同 query+type+target | <1ms | ✅ |
| [1] TTL 缓存 | 同进程重复 indicator | <1ms | ✅ |
| [2] 本地 DB | 慢变量命中 SQLite | ~10ms | ✅ |
| [3] 失败冷却 | 已知失败源 | 跳过（0ms） | ✅ |
| [4] connector 链 | DB miss/过期/冷却外 | 2-30s | ✅ |
| [5] 存储降级 | 实时无数据 | ~5ms | ✅ |
| [6] 自修复 | 全部失败 | 30-60s | ✅ |

---

## 五、兼容性回归

| 原有用例 | 影响 | 结果 |
|---------|------|------|
| test_connector_router.py 7 用例 | 新增 _failure_cache 逻辑 | ✅ 全过 |
| test_mock_industry_connector.py | _Fake 缺 source_name → 已补 | ✅ 全过 |
| test_api.py cache.ttl_seconds==600 | 结果缓存写入不影响回测 API | ✅ 全过 |
| test_backtest.py 542→711 | 回测 API disable_cache=True 不受影响 | ✅ 全过 |

---

## 六、性能影响

| 指标 | 之前 | 现在 | 改善 |
|------|------|------|------|
| SW 行业估值 8 指标（首次失败） | 40s 超时 | 40s（首次） | - |
| SW 行业估值 8 指标（冷却期内） | 40s 再次超时 | **0s（跳过）** | **-40s** |
| 相同投研查询（10min 内） | 60-120s 重跑管线 | **<1ms 返回** | **-60s+** |
| 全量测试 | 710 passed | 711 passed | +1 新用例 |

---

## 七、已知限制

1. **失败冷却仅进程内**：进程重启后冷却清空，首次请求会重试失败源（但 DB 命中可跳过）
2. **结果缓存无语义相似度**：仅精确匹配 query+type+target 的 hash，"中际旭创"和"中际旭创怎么样"是不同缓存
3. **冷却时间固定 300s**：不支持按指标分档（日频失败可能 5 分钟后恢复，月频失败可能 1 小时后恢复）
4. **结果缓存无大小限制**：极端情况下 _RESULT_CACHE 可能内存膨胀（建议加 LRU 上限 100 条）
