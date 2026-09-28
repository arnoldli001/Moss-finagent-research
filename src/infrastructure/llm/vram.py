"""本地显存能力探测 —— 决定"这一跳能不能真的用本地模型"。

## 为什么需要（2026-09-28 实测，端到端证据）

本机只有 **8GB 显存（RTX 4060）**，而 `configs/models.yaml` 里的本地模型是
按"有本地模型可用"选的，**没有考虑上限**。后果不是"慢"，是**拉不起来**：

    CUDA0 (RTX 4060) | 8187 = 7099 + (5319 = 4643 + 576 + 100) + (-4231)
    sched.go:556  "llama-server model predicted to exceed available memory, evicting"

`qwen3:8b-q4_K_M` 占 5.5GB 驻留时，规划层要用的 `qwen2.5:1.5b` 换不进来 →
请求挂死 → 规划层 `in=0 out=0 120294ms` → 无声回退规则式规划 → 端到端 123.45s。

## 本模块的判据（三层，从便宜到贵）

1. **模型已在 Ollama 里驻留** → 不需要新显存 → 直接用本地（最省，也最快）
2. 未驻留，但**空闲显存够**（含安全余量）→ 用本地
3. 未驻留且**空闲显存不够** → **不要把本地钉死**，让降级链保留云端备源

第 3 条是本模块存在的理由：它把"钉死本地导致挂死"换成"显存不够就走云端"。
代价是**会花钱** —— 所以每次改道都必须写进审计（见 `gateway` 的调用点），
不许静默。

## 判不了怎么办（fail-open 到"不花钱"一侧）

`nvidia-smi` 不存在（非 N 卡机器 / 沙箱 / 驱动异常）时返回 `None`。
调用方必须把"判不了"当作**维持原配置**（照常钉本地），而不是当作"显存不足"
去花钱 —— 默认值即护栏：不确定时选不花钱的那边。
"""
from __future__ import annotations

import logging
import subprocess
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

#: 本地模型默认预留的安全余量（MB）。
#: 为什么要余量：Ollama 除权重外还要 KV cache / compute buffer / context，
#: 实测 qwen2.5:1.5b（权重约 1.0GB）整机占用明显高于权重本身。
DEFAULT_VRAM_MARGIN_MB = 512

#: 未在 models.yaml 声明 `vram_mb` 时的兜底估算（MB）。
DEFAULT_MODEL_VRAM_MB = 2048

#: 探测结果缓存 TTL（秒）。nvidia-smi 是子进程（约 50~150ms），
#: 每次调用都起一个会拖慢链路；显存变化的时间尺度远大于 10 秒。
PROBE_TTL_SEC = 10.0


def query_free_vram_mb() -> int | None:
    """空闲显存（MB）。取所有 GPU 中最大的那个（多卡时按最宽裕的算）。

    返回 `None` = **判不了**（不是 0）。调用方必须区分这两者：
    "没量到"与"量到 0"的处置完全相反。
    """
    try:
        proc = subprocess.run(
            ["nvidia-smi", "--query-gpu=memory.free",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        logger.debug("nvidia-smi 不可用（判不了显存）：%s", exc)
        return None
    if proc.returncode != 0:
        logger.debug("nvidia-smi 退出码 %s：%s", proc.returncode,
                     (proc.stderr or "").strip()[:120])
        return None
    values: list[int] = []
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            values.append(int(float(line.split()[0])))
        except (ValueError, IndexError):
            continue
    return max(values) if values else None


def query_resident_models(base_url: str, *, timeout: float = 2.0) -> frozenset[str]:
    """Ollama 当前**已驻留**（已占好显存）的模型名集合。

    返回空集 = 判不了或确实没有驻留。**这两种情况在本模块里处置相同**
    （都当作"需要新显存"去查空闲量），所以不区分 —— 但如果将来要区分，
    记住空集不代表"没有驻留"，也代表"问不到"。
    """
    import json
    import urllib.request

    url = base_url.rstrip("/") + "/api/ps"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:  # noqa: S310
            payload = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as exc:  # noqa: BLE001 探测失败不该影响主链路
        logger.debug("查询 Ollama 驻留模型失败（%s）：%s", url, exc)
        return frozenset()
    names: set[str] = set()
    for item in (payload.get("models") or []):
        for key in ("name", "model"):
            v = item.get(key)
            if v:
                names.add(str(v))
    return frozenset(names)


def decide_local_usable(
    *,
    model_name: str,
    required_mb: int,
    free_mb: int | None,
    resident: frozenset[str],
    margin_mb: int = DEFAULT_VRAM_MARGIN_MB,
) -> tuple[bool, str]:
    """纯判据：这一跳能不能真的用本地模型。

    Returns: `(可否用本地, 人话原因)`。原因会进日志与审计，所以要能读懂。
    """
    if model_name in resident:
        return True, f"{model_name} 已驻留显存，无需新加载"
    if free_mb is None:
        # 判不了 → 维持原配置（照常钉本地）。不花钱的一侧是安全侧。
        return True, "显存判不了（nvidia-smi 不可用），维持原配置"
    need = required_mb + margin_mb
    if free_mb >= need:
        return True, (f"空闲显存 {free_mb}MB ≥ 需要 {need}MB"
                      f"（权重 {required_mb} + 余量 {margin_mb}）")
    return False, (f"空闲显存 {free_mb}MB < 需要 {need}MB"
                   f"（权重 {required_mb} + 余量 {margin_mb}），"
                   f"{model_name} 拉不起来")


@dataclass
class LocalCapacity:
    """带 TTL 缓存的显存能力查询。

    可注入 `free_probe` / `resident_probe` 以便测试（不需要真显卡）。
    """

    base_url: str = "http://localhost:11434"
    margin_mb: int = DEFAULT_VRAM_MARGIN_MB
    ttl_sec: float = PROBE_TTL_SEC
    free_probe: object = None       # Callable[[], int | None]
    resident_probe: object = None   # Callable[[str], frozenset[str]]
    _at: float = 0.0
    _free: int | None = None
    _resident: frozenset[str] = field(default_factory=frozenset)

    def _refresh(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and (now - self._at) < self.ttl_sec and self._at > 0:
            return
        free_fn = self.free_probe or query_free_vram_mb
        res_fn = self.resident_probe or query_resident_models
        try:
            self._free = free_fn()          # type: ignore[operator]
        except Exception:  # noqa: BLE001 探测失败 = 判不了
            logger.debug("空闲显存探测异常", exc_info=True)
            self._free = None
        try:
            self._resident = res_fn(self.base_url)  # type: ignore[operator]
        except Exception:  # noqa: BLE001
            logger.debug("驻留模型探测异常", exc_info=True)
            self._resident = frozenset()
        self._at = now

    def check(self, model_name: str, required_mb: int) -> tuple[bool, str]:
        """同步版。⚠️ **不要在事件循环里直接调** —— 探测会起 `nvidia-smi`
        子进程（约 150ms）并阻塞。异步路径请用 `acheck()`。"""
        self._refresh()
        return decide_local_usable(
            model_name=model_name, required_mb=required_mb,
            free_mb=self._free, resident=self._resident,
            margin_mb=self.margin_mb)

    async def acheck(self, model_name: str,
                     required_mb: int) -> tuple[bool, str]:
        """异步版：探测丢进线程池，**绝不阻塞事件循环**。

        为什么必须这样（本项目硬约束）：`complete()` 是 async 的，而同一条
        事件循环上还跑着所有并发请求。同步起子进程会冻结它们 ——
        `cache.py` 的注释记着同款教训（9ms 同步 IO → 事件循环停顿 22.3ms）。

        探测失败/超时一律**沿用旧值**（首次为 None = 判不了）——
        `decide_local_usable` 对"判不了"的处置是**维持原配置**，
        即不花钱的一侧，所以这里吞掉异常是安全的。
        """
        import asyncio

        if self._needs_refresh():
            try:
                await asyncio.wait_for(
                    asyncio.to_thread(self._refresh, force=True), timeout=3.0)
            except (TimeoutError, Exception):  # noqa: BLE001
                logger.debug("显存探测超时/失败，沿用旧值", exc_info=True)
        return decide_local_usable(
            model_name=model_name, required_mb=required_mb,
            free_mb=self._free, resident=self._resident,
            margin_mb=self.margin_mb)

    def _needs_refresh(self) -> bool:
        now = time.monotonic()
        return self._at <= 0 or (now - self._at) >= self.ttl_sec

    def snapshot(self) -> dict[str, object]:
        """给 /health 之类的可观测面用（含"判不了"的原样表达）。"""
        self._refresh()
        return {
            "free_vram_mb": self._free,          # None = 判不了，不是 0
            "resident_models": sorted(self._resident),
            "margin_mb": self.margin_mb,
        }
