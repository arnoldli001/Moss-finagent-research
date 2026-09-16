"""做T权重档案仓储端口（domain层抽象，依赖倒置）。

infrastructure 提供具体实现（SQLite），service 与 api 只依赖此抽象，
禁止在领域层直接连接数据库（项目编码规范）。

为什么用「仓储」而不是 JSON 文件目录：
本项目的 `quant/strategy_store.py` 已经就"档案放哪"给过结论 ——
JSON 文件做不到跨记录查询（"哪类股性的票最容易做T"、"我调过哪些票"、
按更新时间排序、计数），只能每次全量读进内存再排。
做T权重档案是同型需求（同一实体多字段 + 时间戳 + 按代码查 + 列全量），
所以走数据库；JSON 更适合导出格式而非主档案。
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.domain.intraday.models import IntradayProfile


class IntradayProfileRepository(ABC):
    """做T权重档案持久化端口。"""

    @abstractmethod
    async def ensure_schema(self) -> None:
        """幂等建表/补列（进程启动时调用一次）。"""

    @abstractmethod
    async def upsert(self, profile: IntradayProfile) -> IntradayProfile:
        """按 code 写入或更新（保留 created_at，推进 updated_at）。"""

    @abstractmethod
    async def get(self, code: str) -> IntradayProfile | None:
        """按代码取档案（不存在返回 None，不抛异常）。"""

    @abstractmethod
    async def list(self, *, limit: int = 200) -> list[IntradayProfile]:
        """按更新时间倒序列出档案。"""

    @abstractmethod
    async def delete(self, code: str) -> bool:
        """删除档案；返回是否真的删掉了一行（幂等）。"""

    async def close(self) -> None:
        """释放连接资源（默认无操作，连接池后端覆盖）。"""
        return None
