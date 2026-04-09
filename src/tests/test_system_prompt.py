# pyright: reportMissingParameterType=false

from types import SimpleNamespace

from team_harness.config import Config
from team_harness.coordinator.system_prompt import build_system_prompt


def test_system_prompt_contains_coordinator_identity():
    prompt = build_system_prompt(
        config=Config(cwd="/repo"),
        allowed_types=["codex", "gemini"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "You are the coordinator for team-harness." in prompt
    assert "You are not a coding agent." in prompt


def test_system_prompt_contains_values_and_monitoring_guidance():
    prompt = build_system_prompt(
        config=Config(cwd="/repo"),
        allowed_types=["codex", "gemini"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "Clarity:" in prompt
    assert "Pragmatism:" in prompt
    assert "Rigor:" in prompt
    assert "Patience:" in prompt
    assert "`wait_for_any`" in prompt
    assert "`read_new_agent_output`" in prompt
    assert "Use the todo tools aggressively." in prompt


def test_system_prompt_includes_runtime_context_and_output_directory():
    prompt = build_system_prompt(
        config=Config(cwd="/repo"),
        allowed_types=["codex", "gemini"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "Available agent types: codex, gemini" in prompt
    assert "Working directory: /repo" in prompt
    assert "Current UTC time:" in prompt
    assert "Session output directory: /repo/_outputs/run_123" in prompt
    assert "Artifact review:" in prompt


def test_system_prompt_excludes_legacy_agent_summary_filenames():
    prompt = build_system_prompt(
        config=Config(cwd="/repo"),
        allowed_types=["codex"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "AGENT_SUMMARY.md" not in prompt
    assert "AGENT_PROGRESS.md" not in prompt


def test_system_prompt_appends_extensions_and_skills():
    prompt = build_system_prompt(
        config=Config(cwd="/repo", system_prompt_extension="Extra config rules"),
        allowed_types=["codex"],
        skills=[
            SimpleNamespace(name="skill-a", description="First skill"),
            SimpleNamespace(name="skill-b", description="Second skill"),
        ],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "Extra config rules" in prompt
    assert "Additional tools (skills) available:" in prompt
    assert "- skill-a: First skill" in prompt
    assert "- skill-b: Second skill" in prompt
