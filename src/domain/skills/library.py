"""技能库（SkillLibrary）：SKILL.md 的 PTD 三级加载 + 边界控制 + 质量门。

目录结构（两级）：``skills/{agent_dir}/{skill_name}/SKILL.md``，
references 位于 ``skills/{agent_dir}/{skill_name}/references/``。

PTD（渐进式披露）三级加载，控制注入LLM的token量：
- L0 索引 ``list_skills``：仅frontmatter摘要（name/description/tags），
  供system prompt列出"可用技能"，LLM按需点名；
- L1 正文 ``load_skill``：技能被选中后才返回SKILL.md全文（执行步骤/输出格式）；
- L2 参考 ``load_reference``：SKILL.md内声明的references/*.md按需加载。

边界控制：每个agent只能访问映射目录（agent_dirs）下的技能；references 走
frontmatter白名单 + 路径穿越防护，杜绝任意文件读取。

质量门 ``validate_all``：frontmatter完整性（name/description必填）+ token
预算（L0索引条目<100、frontmatter整体<150、正文<5K）+ references文件存在性。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")

# token预算（设计文件4.2）：L0索引条目<100为prompt注入成本的真实口径；
# frontmatter整体含references路径故放宽至150；正文<5K。
L0_INDEX_TOKEN_LIMIT = 100
FRONTMATTER_TOKEN_LIMIT = 150
BODY_TOKEN_LIMIT = 5000

# agent_id → 允许访问的技能目录（默认映射，装配时可覆盖）。
# 设计文件三-各Agent Skill详细设计 ↔ supervisor.py 的agent_id体系。
DEFAULT_AGENT_DIRS: dict[str, list[str]] = {
    "A01_collector": ["event-collector"],
    "A05_verifier": ["policy-analyst"],
    "A06_extractor": ["news-analyst"],
    "A07_sentiment": ["sentiment-monitor"],
    "A08_macro": ["policy-analyst", "calendar-agent"],
    "A09_meso": ["supply-chain-analyst", "crowding-monitor"],
    "A10_micro": ["valuation-agent"],
    "A11_fin_risk": ["financial-screener", "risk-assessor"],
    "A12_compliance": ["risk-assessor"],
    "A13_tech": ["crowding-monitor", "opportunity-assessor"],
    "A14_consumer": ["crowding-monitor", "opportunity-assessor"],
    "A15_cyclical": ["crowding-monitor", "opportunity-assessor"],
    "A16_pharma": ["crowding-monitor", "opportunity-assessor"],
    "A17_recommend": ["summary-agent", "opportunity-assessor", "portfolio-manager"],
}


def estimate_tokens(text: str) -> int:
    """粗估token：CJK≈0.75 token/字、其他≈0.25 token/字符（仅用于预算门）。"""
    cjk = sum(1 for ch in text if "\u4e00" <= ch <= "\u9fff")
    return max(1, int(cjk * 0.75 + (len(text) - cjk) * 0.25))


def index_tokens(meta: SkillMeta) -> int:
    """L0索引条目的token估算（实际注入prompt的量：name+description+tags）。"""
    return estimate_tokens(json.dumps(meta.to_index(), ensure_ascii=False))


@dataclass(frozen=True)
class SkillMeta:
    """技能元数据（L0索引的载体）。"""

    name: str
    description: str
    tags: list[str]
    references: list[str]
    skill_dir: str
    frontmatter_tokens: int
    body_tokens: int

    def to_index(self) -> dict[str, object]:
        """L0索引条目：只含frontmatter摘要，不含正文。"""
        return {"name": self.name, "description": self.description, "tags": self.tags}


class SkillLibrary:
    """SKILL.md文件库：PTD三级加载 + agent边界隔离 + 质量检查。"""

    def __init__(
        self, root: str | Path,
        agent_dirs: dict[str, list[str]] | None = None,
    ) -> None:
        self._root = Path(root)
        self._agent_dirs = dict(agent_dirs or DEFAULT_AGENT_DIRS)

    # ---------- 内部解析 ----------

    @staticmethod
    def _split_frontmatter(text: str) -> tuple[dict[str, object], str, bool]:
        """拆分 ``---\\nyaml\\n---`` 头与正文，返回(meta, body, has_frontmatter)。"""
        stripped = text.lstrip("\ufeff").lstrip()
        if not stripped.startswith("---"):
            return {}, stripped, False
        parts = stripped.split("---", 2)
        if len(parts) < 3:
            return {}, stripped, False
        try:
            meta = yaml.safe_load(parts[1]) or {}
        except yaml.YAMLError:
            return {}, parts[2], True
        return (meta if isinstance(meta, dict) else {}), parts[2].lstrip("\n"), True

    def _dirs_for(self, agent_id: str) -> list[str]:
        return self._agent_dirs.get(agent_id, [])

    def known_agents(self) -> list[str]:
        """已配置技能目录映射的agent_id列表（只读副本）。"""
        return list(self._agent_dirs.keys())

    def _agent_root(self, agent_id: str, dir_name: str) -> Path:
        """校验agent目录在边界内且不逃逸root，返回其路径。"""
        if not _NAME_RE.match(dir_name) or dir_name not in self._dirs_for(agent_id):
            raise LookupError(
                f"agent {agent_id} 无权访问技能目录 {dir_name}",
            )
        path = (self._root / dir_name).resolve()
        if self._root.resolve() not in path.parents:
            raise PermissionError(f"技能目录 {dir_name} 逃逸库root")
        return path

    def _meta_from_text(self, text: str, skill_dir: str) -> SkillMeta:
        meta, body, has_fm = self._split_frontmatter(text)
        name = str(meta.get("name") or skill_dir) if has_fm else skill_dir
        description = str(meta.get("description") or "")
        tags_raw = meta.get("tags")
        tags = [str(t) for t in tags_raw] if isinstance(tags_raw, list) else []
        refs_raw = meta.get("references")
        refs = [str(r) for r in refs_raw] if isinstance(refs_raw, list) else []
        fm_tokens = estimate_tokens(text.split("---")[1] if text.startswith("---") else "")
        return SkillMeta(
            name=name, description=description, tags=tags, references=refs,
            skill_dir=skill_dir,
            frontmatter_tokens=fm_tokens, body_tokens=estimate_tokens(body),
        )

    def _iter_skills(self, dir_name: str) -> list[tuple[Path, SkillMeta]]:
        """遍历agent目录下已落盘技能，返回(SKILL.md路径, meta)列表。"""
        out: list[tuple[Path, SkillMeta]] = []
        for skill_md in sorted((self._root / dir_name).glob("*/SKILL.md")):
            try:
                meta = self._meta_from_text(
                    skill_md.read_text(encoding="utf-8"), skill_md.parent.name,
                )
            except (OSError, yaml.YAMLError):
                continue
            out.append((skill_md, meta))
        return out

    def _locate(self, agent_id: str, name: str) -> tuple[str, str]:
        """在agent边界内按frontmatter name或技能目录名查找，返回(agent_dir, skill_dir)。"""
        for dir_name in self._dirs_for(agent_id):
            if not (self._root / dir_name).is_dir():
                continue  # 目录未落盘静默跳过（支持渐进生成）
            for _path, meta in self._iter_skills(dir_name):
                if name in (meta.name, meta.skill_dir):
                    return dir_name, meta.skill_dir
        available = [
            m.name for d in self._dirs_for(agent_id) if (self._root / d).is_dir()
            for _p, m in self._iter_skills(d)
        ]
        raise LookupError(f"agent {agent_id} 无名为 {name} 的技能（可用：{available}）")

    # ---------- PTD三级加载 ----------

    def list_skills(self, agent_id: str) -> list[dict[str, object]]:
        """L0：返回agent可用技能的索引摘要（frontmatter only）。"""
        indexes: list[dict[str, object]] = []
        for dir_name in self._dirs_for(agent_id):
            if not (self._root / dir_name).is_dir():
                continue  # 目录未生成本地跳过，不报错（支持渐进落盘）
            for _path, meta in self._iter_skills(dir_name):
                indexes.append(meta.to_index())
        return indexes

    def load_skill(self, agent_id: str, name: str) -> str:
        """L1：返回SKILL.md全文（frontmatter+正文）。"""
        dir_name, skill_dir = self._locate(agent_id, name)
        skill_md = self._agent_root(agent_id, dir_name) / skill_dir / "SKILL.md"
        return skill_md.read_text(encoding="utf-8")

    def load_reference(self, agent_id: str, skill_name: str, ref_name: str) -> str:
        """L2：按需加载references/*.md（frontmatter白名单+穿越防护）。"""
        dir_name, skill_dir_name = self._locate(agent_id, skill_name)
        skill_dir = self._agent_root(agent_id, dir_name) / skill_dir_name
        meta = self._meta_from_text(
            (skill_dir / "SKILL.md").read_text(encoding="utf-8"), skill_dir_name,
        )
        if ref_name not in meta.references:
            raise PermissionError(
                f"技能 {meta.name} 未声明参考 {ref_name}（声明：{meta.references}）",
            )
        ref_path = (skill_dir / ref_name).resolve()
        if skill_dir.resolve() not in ref_path.parents:
            raise PermissionError(f"参考路径越界：{ref_name}")
        if not ref_path.is_file():
            raise LookupError(f"参考文件不存在：{ref_name}")
        return ref_path.read_text(encoding="utf-8")

    # ---------- 确定性技能匹配（关键词触发，零额外LLM调用） ----------

    @staticmethod
    def trigger_terms(meta: SkillMeta) -> list[str]:
        """从description提取触发短语表；未声明时退化为技能名本身。"""
        m = re.search(r"触发短语[:：](.+)", meta.description, re.DOTALL)
        if not m:
            return [meta.name]
        return [t.strip() for t in re.split(r"[、，,；;。\n]", m.group(1)) if t.strip()]

    @staticmethod
    def trigger_terms_text(index_entry: dict[str, object] | SkillMeta) -> list[str]:
        """触发短语提取（接受 L0 dict 或 SkillMeta）—— 第十轮 A17 prompt 精简用。

        L0 dict 来自 ``list_skills()`` 返回值（只含 name/description/tags）；
        仍能从 description 提取触发短语，避免路径上必须先建 SkillMeta 实例。
        """
        desc = str(index_entry.get("description", "")) if index_entry else ""
        name = str(index_entry.get("name", "")) if index_entry else ""
        m = re.search(r"触发短语[:：](.+)", desc, re.DOTALL)
        if not m:
            return [name] if name else []
        return [t.strip() for t in re.split(r"[、，,；;。\n]", m.group(1)) if t.strip()]

    def match_skills(
        self, agent_id: str, text: str, *, max_skills: int = 2,
    ) -> list[dict[str, str]]:
        """user_query/focus 命中frontmatter触发短语 → 返回技能正文（L1）。

        确定性匹配不依赖LLM工具调用，适合单次调用模式的分析/信息层Agent；
        命中技能按边界内目录顺序取前 max_skills 个，防prompt超载。
        """
        if not text:
            return []
        hits: list[dict[str, str]] = []
        for dir_name in self._dirs_for(agent_id):
            if len(hits) >= max_skills:
                break
            if not (self._root / dir_name).is_dir():
                continue
            for path, meta in self._iter_skills(dir_name):
                if len(hits) >= max_skills:
                    break
                if any(t in text for t in self.trigger_terms(meta)):
                    hits.append({"name": meta.name, "content": path.read_text(encoding="utf-8")})
        return hits

    # ---------- 质量门 ----------

    def validate_all(self) -> list[str]:
        """扫描库root下全部 ``*/*/SKILL.md``，返回问题清单（空=全部通过）。"""
        problems: list[str] = []
        if not self._root.is_dir():
            return [f"技能库root不存在：{self._root}"]
        for skill_md in sorted(self._root.glob("*/*/SKILL.md")):
            agent_dir = skill_md.parent.parent.name
            skill_name = skill_md.parent.name
            label = f"{agent_dir}/{skill_name}/SKILL.md"
            if not _NAME_RE.match(agent_dir) or not _NAME_RE.match(skill_name):
                problems.append(f"{label}: 目录名不合法")
                continue
            text = skill_md.read_text(encoding="utf-8")
            meta, body, has_fm = self._split_frontmatter(text)
            if not has_fm:
                problems.append(f"{label}: frontmatter缺失")
                continue
            if not meta:
                problems.append(f"{label}: frontmatter为空或不可解析（name/description必填）")
                continue
            if not meta.get("name"):
                problems.append(f"{label}: name必填")
            if not str(meta.get("description") or "").strip():
                problems.append(f"{label}: description必填")
            elif len(str(meta.get("description"))) < 15:
                problems.append(
                    f"{label}: description应含触发短语+时序定位+关键词（当前过短）",
                )
            fm_tokens = estimate_tokens(text.split("---")[1])
            if fm_tokens >= FRONTMATTER_TOKEN_LIMIT:
                problems.append(f"{label}: frontmatter {fm_tokens} tokens 超预算150")
            skill_meta = self._meta_from_text(text, skill_name)
            l0 = index_tokens(skill_meta)
            if l0 >= L0_INDEX_TOKEN_LIMIT:
                problems.append(f"{label}: L0索引条目 {l0} tokens 超预算100")
            body_tokens = estimate_tokens(body)
            if body_tokens >= BODY_TOKEN_LIMIT:
                problems.append(f"{label}: 正文 {body_tokens} tokens 超预算5000")
            for ref in meta.get("references") or []:
                if not (skill_md.parent / str(ref)).is_file():
                    problems.append(f"{label}: 参考文件不存在 {ref}")
        return problems
