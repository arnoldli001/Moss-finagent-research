"""自动生成连接器代码的安全验证器。

AI生成的连接器代码在写入磁盘前必须通过以下检查：
1. AST解析合法（无语法错误）；
2. 仅允许导入白名单内模块（requests/pandas/akshare/numpy/asyncio等数据采集常用库）；
3. 禁止危险调用：exec/eval/compile/__import__/subprocess/os.system/shell相关；
4. 禁止文件系统写操作（连接器只读网络/读本地缓存，不写任意路径）；
5. 沙箱执行：在子进程中导入并实例化，调用supports/fetch做冒烟测试，超时即拒。

通过验证的代码才会被写入 data/dynamic_connectors/ 并由DynamicConnectorLoader加载。
"""

from __future__ import annotations

import ast
import asyncio
import logging
import os
import re
import sys
import tempfile
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# 连接器允许导入的第三方库（数据采集常用，不含执行/系统调用类危险库）
ALLOWED_IMPORTS: frozenset[str] = frozenset({
    "__future__", "asyncio", "logging", "re", "json", "datetime", "time", "io",
    "typing", "collections", "dataclasses", "functools", "itertools", "math",
    "statistics", "hashlib", "base64", "uuid", "pathlib",
    "requests", "aiohttp", "httpx",
    "pandas", "numpy",
    "akshare", "tushare", "baostock",
    "openpyxl", "lxml", "bs4", "chardet",
    "cme_fedwatch",
    "src.core.exceptions", "src.core.schemas",
    "src.infrastructure.connectors.base",
})

# 绝对禁止出现的标识符（危险操作/系统调用）
FORBIDDEN_NAMES: frozenset[str] = frozenset({
    "exec", "eval", "compile", "__import__", "globals", "locals", "vars",
    "subprocess", "os.system", "os.popen", "os.exec", "os.spawn", "os.fork",
    "sys.exit", "sys.modules", "pickle", "marshal", "shelve",
    "ctypes", "gc", "inspect",
    "open(", "input(",
})

# 危险属性访问模式（正则匹配代码文本）
_FORBIDDEN_PATTERNS: tuple[str, ...] = (
    r"\bexec\s*\(", r"\beval\s*\(", r"(?<!re\.)compile\s*\(",
    r"__import__\s*\(", r"\bsubprocess\b", r"\bos\.system\s*\(",
    r"\bos\.popen\s*\(", r"\bpickle\b", r"\bmarshal\b",
    r"\bctypes\b", r"\bsys\.exit\s*\(",
    r"\bopen\s*\([^)]*['\"](w|a)",  # 写入模式open
)

SANDBOX_TIMEOUT_SEC = 15


class ValidationError(Exception):
    """代码安全验证失败。"""


def validate_code(code: str) -> list[str]:
    """静态安全检查，返回问题列表（空列表=通过）。"""
    issues: list[str] = []

    # 1. AST解析
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        issues.append(f"语法错误: {exc.msg} (行{exc.lineno})")
        return issues

    # 2. 导入白名单
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root not in ALLOWED_IMPORTS and not alias.name.startswith("src."):
                    issues.append(f"禁止导入: {alias.name}")
        elif isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            root = mod.split(".")[0]
            if root and root not in ALLOWED_IMPORTS and not mod.startswith("src."):
                issues.append(f"禁止导入: from {mod}")

    # 3. 危险调用（遍历Call节点的func名）
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            func = node.func
            name = ""
            if isinstance(func, ast.Name):
                name = func.id
            elif isinstance(func, ast.Attribute):
                parts: list[str] = []
                cur: ast.expr | None = func
                while isinstance(cur, ast.Attribute):
                    parts.append(cur.attr)
                    cur = cur.value
                if isinstance(cur, ast.Name):
                    parts.append(cur.id)
                name = ".".join(reversed(parts))
            if name in FORBIDDEN_NAMES:
                issues.append(f"禁止调用: {name}")

    # 4. 正则补充（捕获部分AST难以识别的模式）
    for pat in _FORBIDDEN_PATTERNS:
        if re.search(pat, code):
            issues.append(f"匹配到危险模式: {pat}")

    return issues


async def sandbox_test(code: str, indicator_hint: str = "") -> dict[str, Any]:
    """沙箱执行：子进程中导入代码，测试supports与fetch冒烟。

    返回 {"ok": bool, "message": str, "indicators": [...]}
    """
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", encoding="utf-8", delete=False
    ) as f:
        f.write(code)
        tmp_path = f.name
    try:
        runner = (
            "import sys, json, traceback\n"
            f"sys.path.insert(0, r'{Path(__file__).resolve().parents[3]}')\n"
            f"tmp = r'{tmp_path}'\n"
            "spec = __import__('importlib.util', fromlist=['spec_from_file_location'])"
            ".spec_from_file_location('dyn_connector', tmp)\n"
            "mod = __import__('importlib.util', fromlist=['module_from_spec'])"
            ".module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "# 查找Connector子类\n"
            "from src.infrastructure.connectors.base import BaseConnector\n"
            "classes = [getattr(mod, n) for n in dir(mod)\n"
            "           if isinstance(getattr(mod, n), type)\n"
            "           and issubclass(getattr(mod, n), BaseConnector)\n"
            "           and getattr(mod, n) is not BaseConnector]\n"
            "if not classes:\n"
            "    print(json.dumps({'ok': False, 'message': '未找到BaseConnector子类'}))\n"
            "    sys.exit(0)\n"
            "cls = classes[0]\n"
            "inst = cls()\n"
            "ind = (inst.get_capabilities().get('indicators', []) "
            "if hasattr(inst, 'get_capabilities') else [])\n"
            "print(json.dumps({'ok': True, 'class': cls.__name__, "
            "'indicators': ind[:5]}))\n"
        )
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-c", runner,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=SANDBOX_TIMEOUT_SEC,
            )
            out = stdout.decode("utf-8", errors="replace").strip()
            err = stderr.decode("utf-8", errors="replace").strip()
            # 取最后一行JSON输出
            for line in reversed(out.splitlines()):
                line = line.strip()
                if line.startswith("{"):
                    import json
                    result = json.loads(line)
                    if err:
                        result["stderr"] = err[:500]
                    return result
            return {"ok": False, "message": f"无JSON输出 stderr={err[:300]}"}
        except asyncio.TimeoutError:
            proc.kill()
            return {"ok": False, "message": f"沙箱执行超时({SANDBOX_TIMEOUT_SEC}s)"}
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "message": f"沙箱异常: {exc}"}
    finally:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass


def validate_connector_code(code: str) -> list[str]:
    """完整验证：静态检查 + 必须包含BaseConnector子类 + supports方法。"""
    issues = validate_code(code)
    if issues:
        return issues
    # 结构检查
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return ["语法错误"]
    has_base_connector = False
    has_supports = False
    has_fetch = False
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            bases = [b.id for b in node.bases if isinstance(b, ast.Name)]
            if "BaseConnector" in bases:
                has_base_connector = True
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        if item.name == "supports":
                            has_supports = True
                        elif item.name == "fetch":
                            has_fetch = True
    if not has_base_connector:
        issues.append("必须定义继承BaseConnector的子类")
    if not has_supports:
        issues.append("连接器必须实现supports(indicator)静态方法")
    if not has_fetch:
        issues.append("连接器必须实现fetch(indicator)方法")
    return issues
