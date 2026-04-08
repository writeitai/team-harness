# pyright: reportMissingParameterType=false, reportArgumentType=false

import textwrap

import pytest

from team_harness.config import Config
from team_harness.coordinator.client import ChatResponse
from team_harness.coordinator.client import ChoiceRecord
from team_harness.coordinator.client import MessageRecord
from team_harness.skills.loader import load_skills
from team_harness.skills.loader import SkillContext
from team_harness.tools import fs_tools
from team_harness.tools.registry import ToolRegistry


class SkillClient:
    async def chat(self, messages, tools=None, stream=False, token_callback=None):
        return ChatResponse(
            choices=[ChoiceRecord(message=MessageRecord(content="summary"))]
        )


@pytest.mark.asyncio
async def test_load_skills_and_ctx(tmp_path):
    valid = tmp_path / "valid.py"
    valid.write_text(
        textwrap.dedent(
            """
            name = "demo"
            description = "demo skill"
            parameters_schema = {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}
            async def execute(path: str, ctx):
                return await ctx.read_file(path)
            """
        )
    )
    invalid = tmp_path / "invalid.py"
    invalid.write_text("x = 1")
    with pytest.warns(UserWarning):
        skills = load_skills(extra_dirs=[tmp_path])
    names = [skill.name for skill in skills]
    assert "demo" in names

    fs_tools.setup_fs()
    target = tmp_path / "note.txt"
    target.write_text("hello")
    ctx = SkillContext(client=SkillClient(), config=Config())
    registry = ToolRegistry()

    def _make_wrapper(skill, skill_ctx):
        async def _wrapper(**args: object) -> str:
            return await skill.execute(ctx=skill_ctx, **args)

        return _wrapper

    skill = next(skill for skill in skills if skill.name == "demo")
    registry.register(
        {
            "type": "function",
            "function": {
                "name": skill.name,
                "description": skill.description,
                "parameters": skill.parameters_schema,
            },
        },
        _make_wrapper(skill, ctx),
    )
    assert await registry.execute("demo", {"path": str(target)}) == "hello"
