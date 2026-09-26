"""板块剔除清单（`src/fundflow/sector_filter.py`）单元测试。

## 为什么要单测这个

剔除"按名称匹配"这件事的失效方式是**静默**的：清单读不到 → 空集合 →
地域板块重新出现在榜单上，而界面上看起来只是"今天榜单不太一样"。
反过来，如果匹配写宽了（比如用关键词），会把 `汽车一体化压铸` 这类
正常行业板块一起删掉，同样不报错 —— 实测这两个方向都踩过，所以逐条钉住。

真实清单本身也测（用户点名的 4 个板块必须被挡住）：那是**需求**，
不是实现细节，被后续编辑顺手删掉时应当有测试拦下来。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.fundflow import sector_filter

#: 一份最小清单：注意 code 大小写故意混写，验证大小写归一
SAMPLE = """
version: 1
codes:
  - { code: "BK0159.DC", name: "江苏板块" }
  - { code: "bk0596.dc", name: "融资融券" }
  - { code: "BK0001.DC", name: "某统计板" }
considered_kept:
  - { code: "BK0002.DC", name: "正常行业", why: "仅记录，不该被匹配" }
"""


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "exclude.yaml"
    path.write_text(text, encoding="utf-8")
    return path


# ==================== 加载 ====================


def test_load_reads_codes_and_names(tmp_path: Path) -> None:
    cfg = sector_filter.load_sector_exclude(_write(tmp_path, SAMPLE))
    assert cfg.loaded and not cfg.gap
    assert cfg.size == 3
    assert "BK0159.DC" in cfg.codes
    assert "江苏板块" in cfg.names


def test_load_normalizes_code_case(tmp_path: Path) -> None:
    """清单里的 code 大小写混写也要能匹配（上游返回的是大写）。"""
    cfg = sector_filter.load_sector_exclude(_write(tmp_path, SAMPLE))
    assert "BK0596.DC" in cfg.codes, "小写条目应被归一成大写"
    assert cfg.excluded("融资融券", "bk0596.dc"), "传入小写代码也要命中"


def test_load_missing_file_is_fail_open(tmp_path: Path) -> None:
    """文件不存在 → 空清单 + gap，**且不剔除任何板块**。

    方向是刻意的：放行只是"又看到地域板块"，而"全部剔除"会让榜单空白、
    让用户以为数据源坏了。
    """
    cfg = sector_filter.load_sector_exclude(tmp_path / "nope.yaml")
    assert not cfg.loaded and cfg.gap
    assert cfg.size == 0
    assert not cfg.excluded("江苏板块", "BK0159.DC")


def test_load_corrupt_file_is_fail_open(tmp_path: Path) -> None:
    path = _write(tmp_path, "codes: [ this is : not : valid\n")
    cfg = sector_filter.load_sector_exclude(path)
    assert not cfg.loaded and cfg.gap
    assert not cfg.excluded("江苏板块")


def test_load_empty_section_reports_gap(tmp_path: Path) -> None:
    cfg = sector_filter.load_sector_exclude(_write(tmp_path, "version: 1\n"))
    assert not cfg.loaded
    assert "没有可用条目" in cfg.gap


def test_considered_kept_is_not_matched(tmp_path: Path) -> None:
    """`considered_kept` 段只是文档，**不能**参与剔除。"""
    cfg = sector_filter.load_sector_exclude(_write(tmp_path, SAMPLE))
    assert not cfg.excluded("正常行业", "BK0002.DC")


# ==================== 匹配 ====================


def test_excluded_matches_by_name_when_code_absent(tmp_path: Path) -> None:
    """榜单层只有名称（没有代码），必须能只靠名称命中。"""
    config = sector_filter.load_sector_exclude(_write(tmp_path, SAMPLE))
    assert config.excluded("江苏板块")
    assert not config.excluded("半导体")


def test_excluded_matches_by_code_when_name_changed(tmp_path: Path) -> None:
    """上游改名后，只要有代码仍能挡住（这是"代码+名称都匹配"的意义）。"""
    config = sector_filter.load_sector_exclude(_write(tmp_path, SAMPLE))
    assert config.excluded("江苏板块(改名了)", "BK0159.DC")


def test_excluded_ignores_blank_input() -> None:
    empty = sector_filter.SectorExclude()
    assert not empty.excluded("", "")
    assert not empty.excluded("江苏板块")  # 空清单不剔任何东西


# ==================== 过滤与披露 ====================


@pytest.fixture
def sample_config(monkeypatch: pytest.MonkeyPatch, tmp_path: Path
                  ) -> sector_filter.SectorExclude:
    """把模块级缓存换成最小清单：让过滤逻辑脱离真实清单被测。"""
    config = sector_filter.load_sector_exclude(_write(tmp_path, SAMPLE))
    monkeypatch.setattr(sector_filter, "_cached", lambda: config)
    return config


def test_filter_payload_drops_and_counts(
        sample_config: sector_filter.SectorExclude) -> None:
    payload = {"江苏板块": {"code": "BK0159.DC"},
               "半导体": {"code": "BK0009.DC"},
               "融资融券": {"code": "BK0596.DC"}}
    kept, dropped = sector_filter.filter_payload(payload)
    assert set(kept) == {"半导体"}
    assert dropped == 2
    assert kept["半导体"] == {"code": "BK0009.DC"}, "过滤不能改动内容"


def test_filter_payload_without_config_is_passthrough(
        monkeypatch: pytest.MonkeyPatch) -> None:
    """清单不可用时**原样放行**（fail-open），不是清空整个截面。"""
    monkeypatch.setattr(sector_filter, "_cached",
                        lambda: sector_filter.SectorExclude(gap="读不到"))
    payload = {"江苏板块": {"code": "BK0159.DC"}}
    kept, dropped = sector_filter.filter_payload(payload)
    assert kept == payload
    assert dropped == 0


def test_filter_names_drops_and_counts(
        sample_config: sector_filter.SectorExclude) -> None:
    kept, dropped = sector_filter.filter_names(
        ["半导体", "江苏板块", "融资融券", "银行"])
    assert kept == ["半导体", "银行"]
    assert dropped == 2


def test_disclosure_texts() -> None:
    """披露文案的三种状态各自可辨 —— 口径必须写在界面上。"""
    assert sector_filter.disclosure(0, loaded=True) == ""
    assert "未生效" in sector_filter.disclosure(
        0, loaded=False, gap="清单不存在")
    text = sector_filter.disclosure(37, loaded=True)
    assert "37" in text and "剔除" in text


# ==================== code/名称 配对校验 ====================


def test_verify_codes_passes_when_consistent(
        sample_config: sector_filter.SectorExclude) -> None:
    payload = {"江苏板块": {"code": "BK0159.DC"},
               "半导体": {"code": "BK0009.DC"}}
    assert sector_filter.verify_codes(payload) == []


def test_verify_codes_catches_wrong_code(
        sample_config: sector_filter.SectorExclude) -> None:
    """**本轮实测踩过的形状**：清单里写的代码其实属于另一个真实板块。

    名称匹配不上、代码却命中 —— 于是被删的是那个"另一个板块"，
    而它可能是正常行业（实测真实案例：猜 BK1622.DC 想指科创板做市商，
    实际是「镍」）。
    """
    payload = {"江苏板块": {"code": "BK0159.DC"},
               "镍": {"code": "BK0596.DC"}}   # 清单写的是「融资融券」
    problems = sector_filter.verify_codes(payload)
    assert problems, "代码指向了别的板块，必须报出来"
    assert "镍" in problems[0] and "融资融券" in problems[0]


def test_verify_codes_catches_wrong_name(
        sample_config: sector_filter.SectorExclude) -> None:
    """反之：名称在池里，但真实代码与清单写的不一致。"""
    payload = {"江苏板块": {"code": "BK9999.DC"}}
    problems = sector_filter.verify_codes(payload)
    assert problems and "BK0159.DC" in problems[0]


def test_verify_codes_ignores_unlisted_boards(
        sample_config: sector_filter.SectorExclude) -> None:
    """清单外的板块一律不报（绝大多数板块都不在清单里）。"""
    payload = {"半导体": {"code": "BK0009.DC"}, "银行": {"code": "BK0475.DC"}}
    assert sector_filter.verify_codes(payload) == []


def test_verify_codes_noop_without_config(
        monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sector_filter, "_cached",
                        lambda: sector_filter.SectorExclude())
    assert sector_filter.verify_codes({"江苏板块": {"code": "BK0159.DC"}}) == []


# ==================== 真实清单（钉住需求） ====================


@pytest.fixture
def real() -> sector_filter.SectorExclude:
    return sector_filter.load_sector_exclude()


def test_real_config_loads(real: sector_filter.SectorExclude) -> None:
    assert real.loaded, real.gap
    assert real.size > 80, "真实清单不该只剩几条（被误删会是这个形状）"


@pytest.mark.parametrize("name", ["江苏板块", "富时罗素", "融资融券", "华为概念",
                                  "最近多板", "电子", "新材料", "5G概念",
                                  "电池技术"])
def test_real_config_excludes_user_named_boards(
        real: sector_filter.SectorExclude, name: str) -> None:
    """用户点名的板块必须被挡住（这是需求，不是实现细节）。

    后 5 个是用户第二次点名的（2026-09-22）：最近多板 / 电子 / 新材料 /
    5G概念 / 电池技术。
    """
    assert real.excluded(name), f"{name} 应当被剔除"


@pytest.mark.parametrize("name", ["电子车牌", "电子后视镜", "电子化学品",
                                  "F5G概念", "金属新材料", "其他金属新材料",
                                  "电池", "半导体", "CPO概念", "光通信模块",
                                  "银行", "汽车一体化压铸", "重组蛋白"])
def test_real_config_keeps_real_industry_boards(
        real: sector_filter.SectorExclude, name: str) -> None:
    """正常行业板块不能被误伤。

    后几个是"关键词匹配会误伤"的实证案例（含"一体化"与"重组"），
    它们出现在这里是为了防止有人重新引入关键词规则。

    前 6 个专门针对 2026-09-22 那批新增：剔除「电子」时
    「电子车牌 / 电子后视镜 / 电子化学品」不受影响，剔除「新材料」时
    「金属新材料 / 其他金属新材料」不受影响，剔除「5G概念」时
    「F5G概念」不受影响 —— 匹配是**整名相等**，不是包含关系。
    """
    assert not real.excluded(name), f"{name} 是正常行业板块，不该被剔除"
