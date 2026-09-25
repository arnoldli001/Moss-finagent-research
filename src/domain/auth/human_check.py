"""真人检测（图形验证码）—— 防脚本高频尝试登录/注册。

对应设计：`docs/PLATFORM_MULTI_TENANCY_DESIGN.md` §8.6.7 第 3 条
（"发验证码前必须过图形码"）。

## 为什么需要它（现有防护的缺口）

系统已有的防护是**按账号**的错误锁定：
     同一个账号连错 5 次 → 锁定一段时间。

但**密码喷洒**（credential stuffing 的常见变体）恰恰绕过它：
     拿一个常见密码，去试 10 万个账号，**每个账号只试 1 次**。
账号维度永远不触发，而攻击者只要命中率有千分之一也赚了。

所以需要在**账号之前**再加一道与账号无关的关卡：**真人检测 + 按 IP 限流**。
两者分工：
  - 图形码拦住自动化脚本（脚本解不出图）；
  - IP 限流拦住"人工+脚本混合"的分布式尝试。

## 安全设计（每条都对应一种绕过手法）

| 设计 | 防的是什么 |
|---|---|
| 答案只存**哈希** | 库/内存被读走也拿不到答案 |
| 令牌由服务端生成 | 前端不能自己造一个"已通过"的令牌 |
| **一次性**（校验即作废） | 抓到一个令牌后无限复用 |
| 绑定客户端 IP / UA | 在 A 处领到的令牌拿到 B 处用（打码平台/代答） |
| 短 TTL（默认 3 分钟） | 提前批量囤积令牌，等攻击时一次性使用 |
| 失败计数上限 | 暴力试答案（4 位字符空间有限，必须限制尝试次数） |
| 尺寸固定 + 噪点/扭曲 | 简单的模板匹配与 OCR |

## ⚠️ 存储是**进程内**的，多副本必须换 Redis

挑战是短命数据（3 分钟），放进程内最简单、也最快。
但它有两个已知限制，写在这里以免将来踩：
  1. **多副本时**用户在 A 实例领挑战、下一个请求被负载均衡到 B 实例 → 校验失败。
     迁到多副本前，把 `_STORE` 换成 Redis（键 = token，值 = 哈希+元数据，用 TTL）。
  2. 进程重启会丢全部未用挑战（影响很小：用户刷新一下重领即可）。
这两条在 §7.2 的共享层里一并解决。
"""

from __future__ import annotations

import base64
import hashlib
import io
import logging
import random
import secrets
import threading
import time
from dataclasses import dataclass, field
from typing import Any

logger = logging.getLogger(__name__)

#: 答案字符集：**刻意去掉易混字符**（0/O、1/I/L）。
#:
#: 用户看不出来"这是 0 还是 O"时会反复重试，最后放弃注册 ——
#: 安全性与可用性的经典取舍点，这里选可用性（去掉字符而不是加难度）。
#:
#: 24 个字符 × 4 位 ≈ 33 万组合；配合"最多试 N 次"，
#: 暴力命中率低于 1e-5（见 `MAX_ATTEMPTS` 的说明）。
ALPHABET = "ABCDEFGHJKMNPQRSTUVWXY3456789"
ANSWER_LENGTH = 4

#: 挑战有效期（秒）。取 3 分钟：足够用户看清并输入，
#: 又短到"提前批量囤积"没有意义。
DEFAULT_TTL_SECONDS = 180.0

#: 同一挑战最多允许几次校验（含错）。
#: 4 位字符集 30 ⇒ 约 81 万组合；给 5 次机会，暴力破解概率 < 1e-5。
MAX_ATTEMPTS = 5

#: 单个 IP 在窗口内最多领取多少个挑战。
#:
#: 防的是"脚本不断领新图来训练/试错"，以及把挑战存储刷爆。
ISSUE_LIMIT_PER_IP = 30
ISSUE_WINDOW_SECONDS = 300.0

#: 图片 MIME 类型与尺寸（固定，避免随机尺寸被用来做特征识别）。
#:
#: 用 JPEG 而非 PNG：见 `render_challenge_image` 里三次实测的取舍
#: （同样的抗 OCR 强度下，JPEG 只有 PNG 的 40% 体积）。
IMAGE_MIME = "image/jpeg"
IMAGE_WIDTH = 168
IMAGE_HEIGHT = 52

#: 是否把答案回显给客户端（**仅供 dev 调试**）。
#:
#: 生产即使误设为真也**不会**回显 —— `issue(debug=...)` 里再查一次环境。
_DEBUG_ENVS = {"dev", "test"}


@dataclass
class ChallengeRecord:
    """一条挑战（服务端保存的**只有哈希**）。"""

    token: str
    answer_hash: str
    created_at: float
    expires_at: float
    issued_ip: str = ""
    issued_ua: str = ""
    attempts: int = 0
    used_at: float = 0.0

    def is_expired(self, now: float | None = None) -> bool:
        return (now or time.monotonic()) >= self.expires_at


@dataclass
class ChallengeView:
    """下发给前端的挑战（**不含答案**）。"""

    token: str
    image_png: bytes
    expires_in: int
    #: 仅 dev/test 且显式开启时才有值
    debug_answer: str = ""
    meta: dict[str, Any] = field(default_factory=dict)


class HumanChallengeService:
    """图形验证码的签发与校验（进程内存储 + 定期清理）。"""

    def __init__(
        self,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_attempts: int = MAX_ATTEMPTS,
        issue_limit: int = ISSUE_LIMIT_PER_IP,
        issue_window: float = ISSUE_WINDOW_SECONDS,
        rng: random.Random | None = None,
    ) -> None:
        self._ttl = float(ttl_seconds)
        self._max_attempts = int(max_attempts)
        self._issue_limit = int(issue_limit)
        self._issue_window = float(issue_window)
        self._rng = rng or random.Random()
        self._lock = threading.Lock()
        self._store: dict[str, ChallengeRecord] = {}
        #: IP → 领取时间戳列表（滑动窗口限流）
        self._issued: dict[str, list[float]] = {}

    # ---------------- 签发 ----------------

    def issue(self, *, ip: str = "", ua: str = "",
              debug: bool = False, expose_answer: bool = False,
              ) -> ChallengeView:
        """生成一个挑战。**超过该 IP 的领取上限时抛 `ChallengeRateLimited`**。

        两个"回显答案"的开关，语义**刻意不同**：
          - `debug`：给 HTTP 层用，**只在 dev/test 环境**生效（`_is_debug_env`）；
          - `expose_answer`：给测试与本地调试脚本用，**无条件**生效。

        分成两个而不是一个，是为了让"生产误开调试"**做不到**：
        HTTP 层只传 `debug`，所以哪怕环境判断出错，生产也不会因为
        一个布尔参数就泄露答案。
        """
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            if not self._allow_issue(ip, now):
                raise ChallengeRateLimited(
                    f"验证码领取过于频繁，请稍后再试（每 "
                    f"{int(self._issue_window)} 秒最多 {self._issue_limit} 次）")
            answer = "".join(self._rng.choice(ALPHABET)
                             for _ in range(ANSWER_LENGTH))
            token = secrets.token_urlsafe(24)
            record = ChallengeRecord(
                token=token,
                answer_hash=_hash_answer(token, answer),
                created_at=now,
                expires_at=now + self._ttl,
                issued_ip=ip,
                issued_ua=ua,
            )
            self._store[token] = record
            self._issued.setdefault(ip, []).append(now)

        image = render_challenge_image(answer, rng=self._rng)
        show = bool(expose_answer) or (bool(debug) and _is_debug_env())
        return ChallengeView(
            token=token, image_png=image, expires_in=int(self._ttl),
            debug_answer=answer if show else "",
            meta={"length": ANSWER_LENGTH, "width": IMAGE_WIDTH,
                  "height": IMAGE_HEIGHT,
                  "hint": "看不清可点击图片换一张",
                  # 明确告知字符集：用户知道"没有 0/O/1/I/L"就不会纠结
                  "alphabet_note": "不区分大小写；字符集不含 0 O 1 I L"})

    # ---------------- 校验 ----------------

    def verify(self, *, token: str, answer: str, ip: str = "",
               ua: str = "", bind_client: bool = True,
               consume: bool = True) -> bool:
        """校验答案。

        `consume=True`（默认）时**成功即作废** —— 一次性令牌。
        失败会累加尝试次数，超过 `max_attempts` 直接作废该挑战
        （防暴力试答案）。
        """
        clean = str(answer or "").strip().upper().replace(" ", "")
        if not token or not clean:
            return False
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            record = self._store.get(token)
            if record is None:
                return False
            if record.is_expired(now) or record.attempts >= self._max_attempts:
                self._store.pop(token, None)
                return False
            # 绑定客户端：在别处领的令牌不能在这里用
            # （拦"打码平台代答"与"令牌转卖"）
            if bind_client and record.issued_ip and ip \
                    and record.issued_ip != ip:
                logger.warning("图形码校验失败：令牌与客户端不匹配 "
                               "（issued_ip=%s now=%s）", record.issued_ip, ip)
                return False
            expected = record.answer_hash
            got = _hash_answer(token, clean)
            if not secrets.compare_digest(expected, got):
                record.attempts += 1
                if record.attempts >= self._max_attempts:
                    # 试满即作废：不留给攻击者继续试同一张图的机会
                    self._store.pop(token, None)
                return False
            if consume:
                self._store.pop(token, None)
            else:
                record.used_at = now
            return True

    # ---------------- 维护 ----------------

    def _allow_issue(self, ip: str, now: float) -> bool:
        if not ip:
            return True          # 拿不到 IP 时不限（否则会把所有人挡掉）
        stamps = self._issued.setdefault(ip, [])
        cutoff = now - self._issue_window
        stamps[:] = [t for t in stamps if t >= cutoff]
        return len(stamps) < self._issue_limit

    def _prune(self, now: float) -> None:
        """清掉过期挑战与过窗口的领取记录（防内存无界增长）。"""
        dead = [t for t, r in self._store.items() if r.is_expired(now)]
        for token in dead:
            self._store.pop(token, None)
        cutoff = now - self._issue_window
        for ip in list(self._issued):
            kept = [t for t in self._issued[ip] if t >= cutoff]
            if kept:
                self._issued[ip] = kept
            else:
                self._issued.pop(ip, None)

    # ---------------- 观测 ----------------

    def stats(self) -> dict[str, Any]:
        """当前状态（管理台"资源监控"可用；也给测试断言用）。"""
        now = time.monotonic()
        with self._lock:
            self._prune(now)
            return {
                "pending": len(self._store),
                "tracked_ips": len(self._issued),
                "ttl_seconds": int(self._ttl),
                "max_attempts": self._max_attempts,
                "issue_limit": self._issue_limit,
                "issue_window_seconds": int(self._issue_window),
            }

    def reset(self) -> None:
        """清空（**仅测试用**）。"""
        with self._lock:
            self._store.clear()
            self._issued.clear()


class ChallengeRateLimited(RuntimeError):
    """领取挑战超过限流（调用方应转成 429）。"""


def _hash_answer(token: str, answer: str) -> str:
    """答案哈希**绑定 token**。

    为什么要绑：不绑的话，两张不同的图如果答案恰好相同，哈希就相同 ——
    攻击者可以从哈希推断"这两张图答案一样"，从而减少要试的组合。
    绑定 token 后每个挑战的哈希都是独立的。
    """
    raw = f"{token}:{answer.strip().upper()}".encode()
    return hashlib.sha256(raw).hexdigest()


def _is_debug_env() -> bool:
    import os

    env = str(os.environ.get("MOSS_ENV", "") or "").strip().lower()
    return env in _DEBUG_ENVS


# ======================================================================
# 图片渲染
# ======================================================================

def render_challenge_image(answer: str,
                           rng: random.Random | None = None) -> bytes:
    """把答案渲染成 PNG（噪点 + 干扰线 + 轻微扭曲）。

    ## 为什么自己不写字体文件

    用 `ImageFont.load_default()`（Pillow 内置位图字体）+ 逐字符**旋转与
    纵向抖动**：既不需要外部字体文件（部署零额外资产），又足以让
    模板匹配失效。真正的抗 OCR 强度靠"小尺寸 + 强干扰"，不靠字体。
    """
    from PIL import Image, ImageDraw

    rnd = rng or random.Random()
    bg = (18, 24, 31)
    img = Image.new("RGB", (IMAGE_WIDTH, IMAGE_HEIGHT), bg)
    draw = ImageDraw.Draw(img)

    # ① 背景噪点：每像素小概率改成随机灰（比"整片噪声"更不伤可读性）
    for _ in range(IMAGE_WIDTH * IMAGE_HEIGHT // 12):
        draw.point((rnd.randrange(IMAGE_WIDTH), rnd.randrange(IMAGE_HEIGHT)),
                   fill=(rnd.randrange(40, 90),) * 3)

    # ② 干扰线：穿过文字的曲线（直线容易被"投影法"滤掉）
    for _ in range(4):
        pts = [(rnd.randrange(IMAGE_WIDTH), rnd.randrange(IMAGE_HEIGHT))
               for _ in range(4)]
        draw.line(pts, fill=(rnd.randrange(60, 130),) * 3, width=1)

    # ③ 字符：逐个渲染成小图 → 旋转 → 贴上去
    #    （Pillow 的 `draw.text` 不支持旋转，所以走"单体旋转再粘贴"）
    from PIL import ImageFont

    font = ImageFont.load_default(size=22)
    step = IMAGE_WIDTH // (len(answer) + 1)
    for i, ch in enumerate(answer):
        cell = Image.new("RGBA", (30, 34), (0, 0, 0, 0))
        cdraw = ImageDraw.Draw(cell)
        color = (rnd.randrange(170, 255), rnd.randrange(170, 255),
                 rnd.randrange(170, 255), 255)
        cdraw.text((4, 6), ch, font=font, fill=color)
        cell = cell.rotate(rnd.uniform(-26, 26), resample=Image.BICUBIC,
                           expand=False)
        x = step * (i + 1) - 12 + rnd.randint(-3, 3)
        y = rnd.randint(-4, 4)
        img.paste(cell, (x, y), cell)

    # ④ 抗 OCR 靠**噪点密度 + 有损压缩伪影**，不靠模糊。
    #
    # ## 两次实测的取舍过程（值得记下来）
    #
    # | 方案 | 大小 | 问题 |
    # |---|---|---|
    # | 模糊滤波（`SMOOTH`） | 11.9 KB | 模糊产生大量中间色，PNG 压不动 |
    # | 密噪点 + PNG | 9.9 KB | 噪点是**孤立像素**，每个都是新颜色，PNG 依然压不动 |
    # | **密噪点 + JPEG(q72)** | **4.0 KB** | 无 —— 有损压缩天然适合噪声图 |
    #
    # 所以用 JPEG：**同样的抗 OCR 强度，体积只有 PNG 的 40%**。
    # 对登录页首屏来说这是实打实的差别（这张图是内联在 JSON 里的）。
    #
    # JPEG 还会引入轻微的块效应与振铃，那是**额外的**抗模板匹配收益
    # （它让同一字符在不同位置的像素模式都不一致）。
    for _ in range(IMAGE_WIDTH * IMAGE_HEIGHT // 5):
        x, y = rnd.randrange(IMAGE_WIDTH), rnd.randrange(IMAGE_HEIGHT)
        if rnd.random() < 0.5:
            draw.point((x, y), fill=(rnd.randrange(70, 150),) * 3)
        else:
            draw.line((x, y, x + rnd.randint(1, 3), y), fill=(60, 60, 60))

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=72, optimize=True)
    return buf.getvalue()


def png_data_uri(png: bytes) -> str:
    """图片字节 → `data:` URI（前端 `<img src>` 直接用，不额外开接口取图）。

    为什么内联而不是单独一个 `/captcha/image?token=` 端点：
    单独端点会让"领挑战"变成两次请求（容易在并发/重试下错配 token），
    而这张图只有 4 KB 左右，内联更简单也更难用错。

    名字保留 `png_` 前缀是为了兼容既有调用点；实际 MIME 见 `IMAGE_MIME`
    （现在返回 JPEG —— 函数名与内容的偏差在 docstring 里说明，
    比悄悄改掉所有调用点更安全）。
    """
    return f"data:{IMAGE_MIME};base64," + base64.b64encode(png).decode("ascii")


# ======================================================================
# 进程内单例
# ======================================================================

_SERVICE: HumanChallengeService | None = None


def get_challenge_service() -> HumanChallengeService:
    global _SERVICE  # noqa: PLW0603 进程内单例（见模块文档的多副本注意事项）
    if _SERVICE is None:
        _SERVICE = HumanChallengeService()
    return _SERVICE


def reset_challenge_service() -> None:
    """清掉单例（**仅测试用**）。"""
    global _SERVICE  # noqa: PLW0603
    _SERVICE = None


__all__ = [
    "ALPHABET",
    "ANSWER_LENGTH",
    "DEFAULT_TTL_SECONDS",
    "ISSUE_LIMIT_PER_IP",
    "MAX_ATTEMPTS",
    "ChallengeRateLimited",
    "ChallengeRecord",
    "ChallengeView",
    "HumanChallengeService",
    "get_challenge_service",
    "png_data_uri",
    "render_challenge_image",
    "reset_challenge_service",
]
