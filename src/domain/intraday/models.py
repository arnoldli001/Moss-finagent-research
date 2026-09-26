"""做T权重档案模型：**按个股股性保存的一套打分/档位参数**。

## 它解决什么问题

项目原本已有「个股微调」机制（`src/intraday/config.py::CodeOverride`，
写在 `configs/intraday.yaml` 的 `overrides:` 段），但它有两个硬伤：

1. **没法在前端编辑** —— `overrides` 段连写回函数都没有，只能手改 YAML；
2. **一处笔误全库失效** —— YAML 解析失败会让整个做T模块回退内置默认参数
   （`config.py` 的 `load_error` 机制），用户辛苦调的一套档案会"整体消失"。

所以档案改存数据库（`dim_intraday_profile` 表），并保留 YAML `overrides` 作为
兼容入口。**优先级：数据库档案 > YAML overrides > 全局配置**，
且当前生效的是哪一份必须在面板的 `gaps` 里明说 ——
同一只票两套参数下结论不同，用户必须知道看的是哪一套。

## 字段设计

`weights` / `daily_weights` / `thresholds` / `daily_thresholds` / `levels`
全部是**稀疏差分**（只写要覆盖的键），与 `CodeOverride` 的语义一致：
前端滑杆只动了几项时，不该把另外几十项也固化下来 ——
否则以后改全局默认值，这只票不会跟着变，而用户完全不知道。

`character_profile` 保存"保存那一刻的股性快照"：股性会随行情漂移，
回看半年前的一条档案时，得能知道当时是凭什么参数推荐的。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, field_validator

from src.core.exceptions import ConfigError

if TYPE_CHECKING:  # pragma: no cover - 仅类型检查期使用，避免 domain → intraday 的硬依赖
    from src.intraday.config import CodeOverride

# 来源标记：面板会据此显示"这条档案是手工调的还是股性自动预填的"
SOURCE_MANUAL = "manual"
SOURCE_AUTO_CHARACTER = "auto_character"
SOURCE_IMPORT = "import"
SOURCES = (SOURCE_MANUAL, SOURCE_AUTO_CHARACTER, SOURCE_IMPORT)


class IntradayProfile(BaseModel):
    """一只票的做T参数档案（数据库中的一行）。"""

    code: str = Field(description="6位证券代码（主键）")
    name: str = ""
    # ---- 打分参数（稀疏差分：只写被改过的键）----
    weights: dict[str, float] = Field(
        default_factory=dict, description="分时做T因子权重覆盖（14因子中的若干项）")
    daily_weights: dict[str, float] = Field(
        default_factory=dict, description="日线做T因子权重覆盖（7因子中的若干项）")
    thresholds: dict[str, float] = Field(
        default_factory=dict, description="分时动手线/提示线覆盖 {action, hint}")
    daily_thresholds: dict[str, float] = Field(
        default_factory=dict, description="日线动手线/提示线覆盖")
    # ---- 回踩/冲高数值计算 ----
    levels: dict[str, float] = Field(
        default_factory=dict,
        description="档位覆盖：min_band_pct/max_band_pct/stop_loss_pct/"
                    "atr_stop_mult/touch_band_pct/dip_fallback_atr 等")
    # ---- 溯源 ----
    character_profile: dict[str, Any] = Field(
        default_factory=dict, description="保存时的股性画像快照（可复现推荐依据）")
    template: str = Field(default="", description="使用的权重模板 key（如 swing/dragon）")
    source: str = Field(default=SOURCE_MANUAL)
    note: str = ""
    created_at: str = ""
    updated_at: str = ""

    @field_validator("code")
    @classmethod
    def _six_digit(cls, value: str) -> str:
        """代码必须是6位数字 —— 与 `config._validate_overrides` 同一口径。

        非法代码直接报错而不是静默存下：档案是按代码查的，
        存进去一个查不到的键，等于用户白调了一遍。
        """
        text = str(value or "").strip()
        if not (text.isdigit() and len(text) == 6):
            raise ConfigError(f"权重档案的代码必须是6位数字：{value!r}")
        return text

    @field_validator("source")
    @classmethod
    def _known_source(cls, value: str) -> str:
        text = (value or SOURCE_MANUAL).strip()
        if text not in SOURCES:
            raise ConfigError(
                f"未知的档案来源 {value!r}，可选：{', '.join(SOURCES)}")
        return text

    def as_override(self) -> CodeOverride:
        """转成配置层的 `CodeOverride`（由 config 层负责校验字段名）。

        **必须走构造器**：`IntradayConfig.for_code` 内部用 `model_copy`，
        而 pydantic v2 的 `model_copy` 不做校验 —— 数据库里的未知字段名会被
        静默塞进模型，变成"改了却没生效"这种最难查的问题。
        """
        from src.intraday.config import CodeOverride

        return CodeOverride(
            weights=dict(self.weights), daily_weights=dict(self.daily_weights),
            thresholds=dict(self.thresholds), levels=dict(self.levels))

    def is_empty(self) -> bool:
        return not (self.weights or self.daily_weights or self.thresholds
                    or self.daily_thresholds or self.levels)

    def describe(self) -> str:
        """人话描述（面板上显示"这只票改了什么"）。"""
        parts: list[str] = []
        if self.weights:
            parts.append("分时权重 " + ", ".join(
                f"{k}={v:g}" for k, v in sorted(self.weights.items())))
        if self.daily_weights:
            parts.append("日线权重 " + ", ".join(
                f"{k}={v:g}" for k, v in sorted(self.daily_weights.items())))
        if self.thresholds:
            parts.append("阈值 " + ", ".join(
                f"{k}={v:g}" for k, v in sorted(self.thresholds.items())))
        if self.daily_thresholds:
            parts.append("日线阈值 " + ", ".join(
                f"{k}={v:g}" for k, v in sorted(self.daily_thresholds.items())))
        if self.levels:
            parts.append("档位 " + ", ".join(
                f"{k}={v:g}" for k, v in sorted(self.levels.items())))
        return "；".join(parts) or "（无覆盖项）"

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()
