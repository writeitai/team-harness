from dataclasses import dataclass
import importlib.util
from pathlib import Path
from typing import Any
from typing import Callable
from typing import TYPE_CHECKING
import warnings

from team_harness.config import SKILLS_USER_DIR

if TYPE_CHECKING:
    from team_harness.config import Config
    from team_harness.coordinator.client import CoordinatorClient

SKILL_DIRS = [SKILLS_USER_DIR, Path.cwd() / "skills"]


@dataclass
class SkillContext:
    client: "CoordinatorClient"
    config: "Config"

    async def read_file(self, path: str) -> str:
        from team_harness.tools.fs_tools import read_file

        return await read_file(path)

    async def write_file(self, path: str, content: str) -> str:
        from team_harness.tools.fs_tools import write_file

        return await write_file(path, content)


@dataclass
class Skill:
    name: str
    description: str
    parameters_schema: dict
    execute: Callable[..., Any]


def load_skills(extra_dirs: list[Path] | None = None) -> list[Skill]:
    skill_dirs = list(SKILL_DIRS)
    if extra_dirs:
        skill_dirs.extend(extra_dirs)

    skills: list[Skill] = []
    for directory in skill_dirs:
        if not directory.exists():
            continue
        for path in sorted(directory.glob("*.py")):
            module_name = f"team_harness_skill_{path.stem}_{abs(hash(path))}"
            try:
                spec = importlib.util.spec_from_file_location(module_name, path)
                if spec is None or spec.loader is None:
                    raise ImportError(f"Unable to load spec for {path}")
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
            except Exception as exc:
                warnings.warn(f"Failed to load skill {path}: {exc}", stacklevel=2)
                continue
            required = ("name", "description", "parameters_schema", "execute")
            if not all(hasattr(module, attr) for attr in required):
                warnings.warn(
                    f"Skipping skill {path}: missing one of {required}", stacklevel=2
                )
                continue
            skills.append(
                Skill(
                    name=module.name,
                    description=module.description,
                    parameters_schema=module.parameters_schema,
                    execute=module.execute,
                )
            )
    return skills
