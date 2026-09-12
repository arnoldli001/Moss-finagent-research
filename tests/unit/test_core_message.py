"""core.message 标准消息格式测试。"""

import pytest
from pydantic import ValidationError

from src.core.exceptions import MessageFormatError
from src.core.message import Message, build_message


def test_build_message_with_business_type():
    msg = build_message(
        sender="A08_macro",
        receiver="A17_advisory",
        message_type="macro.cycle_report",
        payload={"conclusion": "ok"},
        data_sources=["data.stats.gov.cn"],
    )

    assert msg.message_id.startswith("msg_")
    assert msg.metadata.audit_id.startswith("audit_")
    assert msg.metadata.data_sources == ["data.stats.gov.cn"]
    assert msg.metadata.version == "1.0"


def test_build_message_with_standard_type():
    msg = build_message("A01_data", "A02_data", "system.task_dispatch", {})
    assert msg.message_type == "system.task_dispatch"


def test_invalid_business_type_rejected():
    with pytest.raises(MessageFormatError):
        build_message("a", "b", "bad_type_without_dot", {})


def test_message_model_rejects_bad_type():
    with pytest.raises(ValidationError):
        Message(sender="a", receiver="b", message_type="no_dot", payload={})
