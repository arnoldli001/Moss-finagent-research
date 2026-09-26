"""动态连接器加载器：扫描 data/dynamic_connectors/ 目录，加载AI生成的连接器。

A19编码Agent生成的连接器写入此目录后，由本加载器在运行时导入并注册到
ConnectorRouter。加载失败的连接器记录日志并跳过，不影响已注册的静态连接器。

设计原则：
- 动态连接器与静态连接器共享同一BaseConnector协议，对A01透明；
- 加载顺序在静态连接器之后（动态为补充，不覆盖已有指标）；
- 每个动态连接器文件须通过code_validator静态验证（写入时已做），加载时再做导入冒烟；
- 提供reload()供A19写入新连接器后热加载，无需重启进程。
"""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from typing import Any

from src.infrastructure.connectors.base import BaseConnector

logger = logging.getLogger(__name__)

# 动态连接器存放目录（与静态连接器分开，便于审查与清理）
DYNAMIC_DIR = Path(__file__).resolve().parents[3] / "data" / "dynamic_connectors"


class DynamicConnectorLoader:
    """扫描并加载动态连接器，返回(connector实例, supports函数)列表。"""

    def __init__(self, directory: Path | str | None = None) -> None:
        self._dir = Path(directory) if directory else DYNAMIC_DIR
        self._dir.mkdir(parents=True, exist_ok=True)
        self._loaded: dict[str, BaseConnector] = {}  # module_name → instance

    def load_all(self) -> list[tuple[BaseConnector, Any]]:
        """加载目录下全部连接器，返回路由列表[(connector, supports)]。"""
        routes: list[tuple[BaseConnector, Any]] = []
        self._dir.mkdir(parents=True, exist_ok=True)
        for py_file in sorted(self._dir.glob("*.py")):
            if py_file.name.startswith("_"):
                continue
            try:
                connector = self._load_file(py_file)
            except Exception as exc:  # noqa: BLE001
                logger.warning("动态连接器加载失败(%s): %s", py_file.name, exc)
                continue
            if connector is None:
                continue
            supports = getattr(connector, "supports", None)
            if supports is None:
                logger.warning("动态连接器%s无supports方法，跳过", py_file.name)
                continue
            self._loaded[py_file.stem] = connector
            routes.append((connector, supports))
            logger.info("动态连接器已加载: %s → %s",
                        py_file.name, connector.__class__.__name__)
        return routes

    def reload(self) -> list[tuple[BaseConnector, Any]]:
        """清空缓存并重新加载（A19写入新文件后调用）。"""
        self._loaded.clear()
        return self.load_all()

    def list_loaded(self) -> list[dict[str, Any]]:
        """返回已加载连接器的能力摘要（供API展示）。"""
        return [
            {
                "module": name,
                "class": conn.__class__.__name__,
                "capabilities": conn.get_capabilities(),
            }
            for name, conn in self._loaded.items()
        ]

    def covering(self, indicator: str) -> list[str]:
        """返回**已声明支持该指标**的连接器类名（空列表 = 没人管这个指标）。

        A19 的幂等守卫用它：该指标已经有能用的连接器时就不该再花钱生成一遍。

        ⚠️ **按 `get_capabilities()["indicators"]` 精确比对，不能改用
        `supports()`**。生成出来的连接器的 `supports` 往往写成**前缀正则**
        （实测 `comm_gold_price` 是 `^comm:.*$`），那是给路由用的宽松判据 ——
        拿它做"是否已覆盖"会让 `comm:y` 也算命中 `comm:gold_price` 的连接器，
        于是任何同前缀的新指标都**永远不会被生成**，而且不报错。
        （这个坑是先写错、被 `test_code_engineer_rejects_unsafe_code` 逮到的。）
        """
        target = (indicator or "").strip()
        if not target:
            return []
        hits: list[str] = []
        for conn in self._loaded.values():
            try:
                declared = conn.get_capabilities().get("indicators") or []
            except Exception:  # noqa: BLE001 能力描述坏掉不该影响其它连接器
                logger.debug("连接器能力描述读取失败: %s", conn.__class__.__name__,
                             exc_info=True)
                continue
            if target in declared:
                hits.append(conn.__class__.__name__)
        return hits

    def _load_file(self, py_file: Path) -> BaseConnector | None:
        module_name = f"dyn_{py_file.stem}"
        spec = importlib.util.spec_from_file_location(module_name, str(py_file))
        if spec is None or spec.loader is None:
            return None
        # 避免模块名冲突
        if module_name in sys.modules:
            del sys.modules[module_name]
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        # 查找BaseConnector子类
        connector_cls = None
        for attr_name in dir(module):
            attr = getattr(module, attr_name)
            if (isinstance(attr, type)
                    and issubclass(attr, BaseConnector)
                    and attr is not BaseConnector):
                connector_cls = attr
                break
        if connector_cls is None:
            return None
        return connector_cls()


# 模块级单例（供ConnectorRouter与A19共享）
_dynamic_loader: DynamicConnectorLoader | None = None


def get_dynamic_loader() -> DynamicConnectorLoader:
    global _dynamic_loader
    if _dynamic_loader is None:
        _dynamic_loader = DynamicConnectorLoader()
    return _dynamic_loader
