"""认证域：注册 / 登录 / 会话 / 找回 / 绑定（**邮箱通道**）。

设计文档：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.6。

分层约定（与项目既有依赖方向一致）：
    api（路由/中间件） → domain（本包，业务策略） → infrastructure（仓储/通知）

本包**不 import FastAPI**：它要能被单测直接驱动，也能被将来的后台任务复用
（例如"到期前 7 天提醒"的定时邮件）。

⚠️ 这里**刻意不做子模块的 re-export**：
`service.py` 与 `human_check.py` 各自体量不小，调用方都用显式路径
（`from src.domain.auth.service import AuthService`）。集中 re-export 会让
"这个名字从哪来"难查，还会让 `import src.domain.auth` 顺带拉起整个认证服务
（含通知渠道装配）。
"""
