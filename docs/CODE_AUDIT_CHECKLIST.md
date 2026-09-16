# LLM 生成代码——安全审计清单与测试用例

> 版本 2026-09-15 | 配合 `src/infrastructure/connectors/code_validator.py`

---

## 一、代码审计清单

### 1.1 静态验证（AST 层）

| # | 检查项 | 方法 | 通过条件 | 危险等级 |
|---|--------|------|----------|----------|
| S1 | 语法合法性 | `ast.parse(code)` | 无 SyntaxError | 🔴 阻断 |
| S2 | 导入白名单 | 遍历 `ast.Import` / `ast.ImportFrom` | 所有导入在 `ALLOWED_IMPORTS` 或 `src.*` | 🔴 阻断 |
| S3 | 禁止 exec/eval | 遍历 `ast.Call` 节点 | 函数名不在 `FORBIDDEN_NAMES` | 🔴 阻断 |
| S4 | 禁止 __import__ | 同上 | 不含 `__import__` | 🔴 阻断 |
| S5 | 禁止 subprocess | 同上 | 不含 `subprocess.*` | 🔴 阻断 |
| S6 | 禁止 os.system/popen | 同上 | 不含 `os.system` / `os.popen` | 🔴 阻断 |
| S7 | 禁止 pickle/marshal | 同上 | 不含 `pickle` / `marshal` | 🟡 高 |
| S8 | 禁止 ctypes/gc/inspect | 同上 | 不含 `ctypes` / `gc` / `inspect` | 🟡 高 |
| S9 | 禁止 sys.exit | 同上 | 不含 `sys.exit` | 🟡 高 |
| S10 | 禁止文件写入 | 正则 `open\([^)]*['\"](w\|a)` | 不匹配 | 🔴 阻断 |
| S11 | 必须有 BaseConnector 子类 | 遍历 `ast.ClassDef` | 至少一个类继承 BaseConnector | 🟡 结构 |
| S12 | 必须有 supports 方法 | 遍历类方法 | 存在 `supports` | 🟡 结构 |
| S13 | 必须有 fetch 方法 | 同上 | 存在 `fetch` | 🟡 结构 |
| S14 | 正则补充检查 | `_FORBIDDEN_PATTERNS` | 所有正则不匹配 | 🔴 阻断 |

### 1.2 沙箱执行（运行时层）

| # | 检查项 | 方法 | 通过条件 |
|---|--------|------|----------|
| R1 | 子进程隔离 | `asyncio.create_subprocess_exec` | 主进程不受影响 |
| R2 | 超时 kill | `asyncio.wait_for(15s)` | 15s 内完成 |
| R3 | 导入成功 | 子进程 `spec.loader.exec_module` | 无 ImportError |
| R4 | 实例化成功 | `cls()` | 无 TypeError |
| R5 | supports 可调用 | `inst.supports("test")` | 返回 bool |
| R6 | fetch 可调用 | `inst.fetch("test")` | 不抛异常 |
| R7 | 返回 JSON | stdout 最后一行 JSON | `json.loads` 成功 |

### 1.3 数据质量验证（fetch 后）

| # | 检查项 | 通过条件 |
|---|--------|----------|
| D1 | 非空 | `len(points) > 0` |
| D2 | 有 period_date | 每个 DataPoint 的 `period_date` 非空 |
| D3 | 有 value | 每个 DataPoint 的 `value` 是数值 |
| D4 | 有 source_name | 每个 DataPoint 的 `source_name` 非空 |

---

## 二、测试用例

### 2.1 正向用例

#### TC-01: 合法 AkShare 连接器应通过

```python
VALID_CODE = '''
from __future__ import annotations
import asyncio, logging
from src.core.schemas import DataPoint, FetchMethod
from src.infrastructure.connectors.base import BaseConnector

class TestConnector(BaseConnector):
    source_name = "test"
    source_url = "https://example.com"

    @staticmethod
    def supports(indicator: str) -> bool:
        return indicator.startswith("test:")

    async def fetch(self, indicator, start_date=None, end_date=None):
        return [DataPoint(
            indicator=indicator, period_date="2026-09-01",
            value=1.0, source_name="test",
            source_url="https://example.com",
            confidence=0.8,
            extra={"fetch_method": FetchMethod.ONLINE},
        )]

    def get_capabilities(self):
        return {"simulated": False, "indicators": ["test:"]}
'''
# 预期: validate_connector_code(VALID_CODE) == []
# 预期: sandbox_test(VALID_CODE) == {"ok": True}
```

### 2.2 安全阻断用例

#### TC-02: 含 exec 的代码应被拒绝

```python
EXEC_CODE = VALID_CODE.replace(
    "return [DataPoint(",
    "exec('import os')\n    return [DataPoint("
)
# 预期: "exec" in issues  →  阻断
```

#### TC-03: 含 os.system 的代码应被拒绝

```python
OS_CODE = VALID_CODE.replace(
    "return [DataPoint(",
    "import os; os.system('rm -rf /')\n    return [DataPoint("
)
# 预期: "os.system" in issues  →  阻断
```

#### TC-04: 含 __import__ 的代码应被拒绝

```python
IMPORT_CODE = VALID_CODE.replace(
    "return [DataPoint(",
    "mod = __import__('subprocess')\n    return [DataPoint("
)
# 预期: "__import__" in issues  →  阻断
```

#### TC-05: 含 pickle 的代码应被拒绝

```python
PICKLE_CODE = '''
import pickle
from src.infrastructure.connectors.base import BaseConnector
class Bad(BaseConnector):
    @staticmethod
    def supports(i): return False
    async def fetch(self, i, sd=None, ed=None): return []
'''
# 预期: "pickle" in issues  →  阻断
```

#### TC-06: 导入非白名单库应被拒绝

```python
BAD_IMPORT_CODE = '''
import socket
from src.infrastructure.connectors.base import BaseConnector
class Bad(BaseConnector):
    @staticmethod
    def supports(i): return False
    async def fetch(self, i, sd=None, ed=None): return []
'''
# 预期: "socket" in issues  →  阻断
```

#### TC-07: 文件写入模式应被拒绝

```python
WRITE_CODE = VALID_CODE.replace(
    "return [DataPoint(",
    'f = open("/etc/passwd", "w")\n    return [DataPoint('
)
# 预期: 正则 r"open\s*\([^)]*['\"](w|a)" 匹配  →  阻断
```

#### TC-08: 无 BaseConnector 子类应被拒绝

```python
NO_CLASS_CODE = '''
import akshare as ak
def fetch_data():
    return ak.stock_zh_a_spot()
'''
# 预期: "必须定义继承BaseConnector的子类" in issues
```

#### TC-09: 缺少 supports 方法应被拒绝

```python
NO_SUPPORTS_CODE = '''
from src.infrastructure.connectors.base import BaseConnector
class Bad(BaseConnector):
    async def fetch(self, i, sd=None, ed=None): return []
'''
# 预期: "必须实现supports" in issues
```

#### TC-10: 缺少 fetch 方法应被拒绝

```python
NO_FETCH_CODE = '''
from src.infrastructure.connectors.base import BaseConnector
class Bad(BaseConnector):
    @staticmethod
    def supports(i): return False
'''
# 预期: "必须实现fetch" in issues
```

### 2.3 沙箱超时用例

#### TC-11: 死循环代码应被超时 kill

```python
INFINITE_CODE = '''
from src.infrastructure.connectors.base import BaseConnector
class Bad(BaseConnector):
    @staticmethod
    def supports(i): return False
    async def fetch(self, i, sd=None, ed=None):
        while True:
            pass
        return []
'''
# 预期: sandbox_test 超时 15s  →  {"ok": False, "message": "沙箱执行超时"}
```

### 2.4 自修复全链路用例

#### TC-12: 自修复成功后应注册调度

```python
# Mock LLM 返回合法代码 + Mock fetch 返回数据
# 预期:
#   result.success == True
#   result.connector_path 存在
#   result.skill_sedimented == True
#   JOB_REGISTRY 含 dynamic_xxxxxxxx
#   _schedule.json 含该 indicator
```

#### TC-13: 自修复失败不阻断主链路

```python
# Mock LLM 返回非法代码
# 预期:
#   result.success == False
#   _try_self_heal 返回 []
#   collect_node 正常完成（指标缺口上报 A17）
```

#### TC-14: 进程重启后连接器自动加载

```python
# 第一次: 自修复成功 → gap_xxxx.py 写入
# 重启: build_runtime() → DynamicConnectorLoader.load_all()
# 预期: gap_xxxx.py 被加载，Router 含新路由
```

#### TC-15: 进程重启后调度自动恢复

```python
# 第一次: 自修复成功 → _schedule.json 写入
# 重启: build_runtime() → load_dynamic_jobs()
# 预期: JOB_REGISTRY 含 dynamic_xxxxxxxx，cron 正确
```
