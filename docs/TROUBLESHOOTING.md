# 故障排查指南

## 一、Ollama相关问题

### 问题：显存不足
**现象**：Ollama报错“CUDA out of memory”
**解决方案**：
- 使用Q4量化模型：`ollama pull qwen3.5:4b-q4_0`
- 降低max_tokens：在configs/models.yaml中设置max_tokens=1024
- 关闭不必要的后台服务

### 问题：模型响应慢
**现象**：本地模型响应超过10秒
**解决方案**：
- 检查GPU利用率：`nvidia-smi`
- 减少并发请求数
- 使用更小的模型（如qwen3.5:1.5b）

## 二、数据库相关问题

### 问题：PostgreSQL连接失败
**现象**：`connection refused`
**解决方案**：
- 检查PostgreSQL是否启动：`docker ps`
- 检查.env中的连接字符串
- 检查端口是否被占用：`netstat -ano | findstr 5432`

### 问题：Redis连接超时
**现象**：`Redis timeout`
**解决方案**：
- 检查Redis是否启动：`redis-cli ping`
- 检查防火墙设置
- 增加连接超时时间

## 三、API相关问题

### 问题：DeepSeek API返回429
**现象**：`Rate limit exceeded`
**解决方案**：
- 降低调用频率
- 使用指数退避重试
- 切换到备用模型

### 问题：API Key无效
**现象**：`401 Unauthorized`
**解决方案**：
- 检查.env中的API Key
- 确认API Key是否过期
- 确认账户余额充足

## 四、Agent相关问题

### 问题：Agent超时
**现象**：Agent执行超过预设超时时间
**解决方案**：
- 检查数据源是否可用
- 检查LLM调用是否超时
- 增加超时时间配置

### 问题：Agent输出格式错误
**现象**：JSON解析失败
**解决方案**：
- 检查Prompt中的格式约束
- 增加输出验证和重试机制
- 使用Pydantic模型强制校验

## 五、缓存相关问题

### 问题：缓存命中率低
**现象**：hit_rate < 50%
**解决方案**：
- 检查缓存键设计是否合理
- 增加TTL时间
- 检查数据更新频率是否过高

### 问题：缓存不一致
**现象**：缓存数据与数据库不一致
**解决方案**：
- 检查写缓存失效逻辑
- 检查发布-订阅通知是否正常
- 强制刷新缓存

## 六、日志与监控

### 查看Agent执行日志
```bash
tail -f logs/agents/A08_macro.log
```

### 查看API调用日志
```bash
tail -f logs/api/requests.log
```

### 查看审计日志
```bash
tail -f logs/audit/trace.log
```
