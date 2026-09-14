"""core.config 与 core.schemas 基础测试。"""

from src.core.config import get_settings
from src.core.schemas import Confidence, TraceStep


def test_settings_defaults():
    settings = get_settings()
    assert settings.app_name == "Moss-FinAgent-Research"
    assert settings.api_port == 8000
    assert settings.ollama_base_url == "http://localhost:11434"


def test_trace_step_types():
    step = TraceStep(step=1, step_type="data_retrieval", description="获取PPI数据")
    assert step.duration_ms is None
    assert Confidence.HIGH.value == "high"
