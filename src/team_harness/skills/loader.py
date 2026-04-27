"""Agent Skills standard loader.

Discovers SKILL.md files from skill roots, parses YAML frontmatter,
and returns metadata for the coordinator system prompt.
"""

from dataclasses import dataclass
from pathlib import Path
import warnings

import yaml

_MAX_DEPTH = 6
_MAX_DIRS_PER_ROOT = 2000


@dataclass
class SkillMetadata:
    name: str
    description: str
    path: Path
    skill_dir: Path


def load_skill_metadata(*, cwd: str | Path | None = None) -> list[SkillMetadata]:
    """Scan skill roots and return metadata for all discovered skills."""
    roots = _resolve_skill_roots(cwd)
    seen_names: dict[str, SkillMetadata] = {}
    for root in roots:
        if not root.exists():
            continue
        for skill in _scan_root(root):
            # Project skills override user/global skills of the same name
            if skill.name not in seen_names:
                seen_names[skill.name] = skill
    return list(seen_names.values())


def _resolve_skill_roots(cwd: str | Path | None) -> list[Path]:
    """Return skill root directories in priority order (project first, then global)."""
    resolved_cwd = Path(cwd).resolve() if cwd else Path.cwd().resolve()
    roots: list[Path] = []

    # Walk parent directories from cwd up to filesystem root
    # looking for .agents/skills/ directories (project-local)
    current = resolved_cwd
    visited: set[Path] = set()
    while True:
        candidate = current / ".agents" / "skills"
        if candidate.is_dir() and candidate not in visited:
            roots.append(candidate)
            visited.add(candidate)
        parent = current.parent
        if parent == current:
            break
        current = parent

    # User-global skills
    user_global = Path.home() / ".agents" / "skills"
    if user_global not in visited:
        roots.append(user_global)

    return roots


def _scan_root(root: Path) -> list[SkillMetadata]:
    """BFS scan a skill root for SKILL.md files, max depth _MAX_DEPTH."""
    skills: list[SkillMetadata] = []
    dirs_scanned = 0
    queue: list[tuple[Path, int]] = [(root, 0)]

    while queue and dirs_scanned < _MAX_DIRS_PER_ROOT:
        directory, depth = queue.pop(0)
        dirs_scanned += 1

        skill_md = directory / "SKILL.md"
        if skill_md.is_file():
            skill = _parse_skill(skill_md)
            if skill is not None:
                skills.append(skill)
            continue  # Don't recurse into skill directories

        if depth >= _MAX_DEPTH:
            continue

        try:
            children = sorted(directory.iterdir())
        except OSError:
            continue

        for child in children:
            if child.is_dir() and not child.name.startswith("."):
                queue.append((child, depth + 1))

    return skills


def _parse_skill(skill_md: Path) -> SkillMetadata | None:
    """Parse a SKILL.md file and return metadata, or None if invalid."""
    try:
        text = skill_md.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        warnings.warn(f"Cannot read {skill_md}: {exc}", stacklevel=2)
        return None

    frontmatter = _extract_frontmatter(text)
    if frontmatter is None:
        warnings.warn(f"No YAML frontmatter in {skill_md}", stacklevel=2)
        return None

    # Directory name is canonical (per review feedback)
    dir_name = skill_md.parent.name
    fm_name = frontmatter.get("name")
    if fm_name and fm_name != dir_name:
        warnings.warn(
            f"Skill {skill_md}: frontmatter name '{fm_name}' does not match "
            f"directory name '{dir_name}'; using directory name",
            stacklevel=2,
        )
    name = dir_name

    # Validate name
    if not _is_valid_name(name):
        warnings.warn(f"Invalid skill name '{name}' in {skill_md}", stacklevel=2)
        return None

    raw_description = frontmatter.get("description", "")
    if not raw_description:
        warnings.warn(f"Missing description in {skill_md}", stacklevel=2)
        return None

    description = str(raw_description)
    if len(description) > 1024:
        description = description[:1024]

    return SkillMetadata(
        name=name, description=description, path=skill_md, skill_dir=skill_md.parent
    )


def _extract_frontmatter(text: str) -> dict | None:
    """Extract YAML frontmatter from between --- delimiters."""
    lines = text.split("\n")
    if not lines or lines[0].strip() != "---":
        return None

    end_idx = None
    for i, line in enumerate(lines[1:], 1):
        if line.strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return None

    yaml_text = "\n".join(lines[1:end_idx])
    try:
        data = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        warnings.warn(f"Invalid YAML frontmatter: {exc}", stacklevel=2)
        return None

    if not isinstance(data, dict):
        return None

    return data


def _is_valid_name(name: str) -> bool:
    """Check if a skill name follows the naming convention."""
    if not name or len(name) > 64:
        return False
    if name.startswith("-") or name.endswith("-"):
        return False
    if "--" in name:
        return False
    return all(c.isalnum() or c == "-" for c in name) and name == name.lower()
