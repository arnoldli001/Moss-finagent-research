"""做T领域模型（与 `src/intraday/models.py` 的运行期视图模型分开）。

为什么单独一个包：
`src/intraday/models.py` 里是 Bar/Quote/ScoreCard 这类**一次快照的视图对象**，
它们随行情每次重算；而这里放的是**用户资产** —— 权重档案是被持久化、
被跨请求复用、被用户手工维护的东西。两者生命周期完全不同：
视图模型改字段只要前端跟着改，档案模型改字段要考虑存量数据怎么迁移。

依赖倒置（项目编码规范「所有数据访问必须通过统一数据层」）：
- 端口在 domain（`repository.py`）
- 实现在 infrastructure（`intraday_profile_sqlite_repo.py`）
- service / api 只依赖端口
"""

from __future__ import annotations

from .models import IntradayProfile

__all__ = ["IntradayProfile"]
