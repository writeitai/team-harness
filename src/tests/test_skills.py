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
    model = "test/model"
    api_base = "http://localhost:9999"
    provider = "openai_compat"

    async def chat(self, messages, tools=None, stream=False, token_callback=None):
        return ChatResponse(
            choices=[ChoiceRecord(message=MessageRecord(content="summary"))]
        )

    async def get_models(self):
        return {"data": []}

    async def aclose(self):
        return None


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
        schema={
            "type": "function",
            "function": {
                "name": skill.name,
                "description": skill.description,
                "parameters": skill.parameters_schema,
            },
        },
        fn=_make_wrapper(skill=skill, skill_ctx=ctx),
    )
    assert (
        await registry.execute(name="demo", arguments={"path": str(target)}) == "hello"
    )


def test_load_skills_uses_passed_cwd_for_project_skills(monkeypatch, tmp_path):
    user_dir = tmp_path / "user-skills"
    cwd_one = tmp_path / "project-one"
    cwd_two = tmp_path / "project-two"
    (cwd_one / "skills").mkdir(parents=True)
    (cwd_two / "skills").mkdir(parents=True)
    (cwd_one / "skills" / "one.py").write_text(
        textwrap.dedent(
            """
            name = "one"
            description = "project one"
            parameters_schema = {"type": "object", "properties": {}}
            async def execute(ctx):
                return "one"
            """
        )
    )
    (cwd_two / "skills" / "two.py").write_text(
        textwrap.dedent(
            """
            name = "two"
            description = "project two"
            parameters_schema = {"type": "object", "properties": {}}
            async def execute(ctx):
                return "two"
            """
        )
    )
    monkeypatch.setattr("team_harness.skills.loader.SKILLS_USER_DIR", user_dir)
    monkeypatch.chdir(cwd_two)

    skills = load_skills(cwd=cwd_one)

    assert [skill.name for skill in skills] == ["one"]
