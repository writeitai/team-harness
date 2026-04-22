# pyright: reportMissingParameterType=false

from types import SimpleNamespace

from team_harness.config import Config
from team_harness.coordinator.system_prompt import build_system_prompt
from team_harness.coordinator.system_prompt import COORDINATOR_PROMPT
from team_harness.coordinator.system_prompt import DEFAULT_WORKER_FOOTER


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
    assert "Patience Protocol" in prompt
    assert "HARD FLOOR" in prompt
    assert "STDERR GROWTH RULE" in prompt
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


def test_build_system_prompt_uses_config_coordinator_prompt():
    config = Config(
        coordinator_prompt=(
            "custom base prompt {allowed_agent_types} {cwd} "
            "{current_utc_time} {session_output_dir}"
        ),
        cwd="/tmp/project",
    )

    prompt = build_system_prompt(config, ["codex"], [], session_output_dir="/tmp/out")

    assert "custom base prompt" in prompt
    assert COORDINATOR_PROMPT not in prompt


def test_build_system_prompt_includes_suffix_note_when_present():
    config = Config(worker_suffix="Follow the repo conventions.", cwd="/tmp/project")

    prompt = build_system_prompt(config, ["codex"], [], session_output_dir="/tmp/out")

    assert (
        "The following suffix is automatically appended to every worker prompt."
        in prompt
    )
    assert "DO NOT duplicate these instructions in spawn_agent prompts." in prompt
    assert "Follow the repo conventions." in prompt


def test_build_system_prompt_omits_suffix_section_when_empty():
    config = Config(worker_suffix="", cwd=".")

    prompt = build_system_prompt(config, ["codex"], [], session_output_dir="/tmp/out")

    assert (
        "The following suffix is automatically appended to every worker prompt."
        not in prompt
    )
    assert "DO NOT duplicate these instructions in spawn_agent prompts." not in prompt


def test_config_defaults_use_builtin_base_prompt():
    config = Config()

    assert config.coordinator_prompt == COORDINATOR_PROMPT
    assert config.worker_suffix == ""
    assert config.worker_footer == DEFAULT_WORKER_FOOTER


def test_default_worker_footer_constant_is_exposed():
    assert "{session_output_dir}" in DEFAULT_WORKER_FOOTER


def test_build_system_prompt_with_default_config_uses_coordinator_prompt():
    config = Config(cwd="/tmp")

    prompt = build_system_prompt(config, ["codex"], [], session_output_dir="/tmp/out")

    assert "You are the coordinator for team-harness." in prompt
    assert "Available agent types: codex" in prompt


def test_system_prompt_contains_api_error_failover_protocol():
    prompt = build_system_prompt(
        config=Config(cwd="/repo"),
        allowed_types=["codex", "claude"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "API Error Failover Protocol" in prompt
    assert "failure_classification" in prompt
    assert "DIFFERENT agent type" in prompt
    assert "infrastructure failures, not trajectory errors" in prompt


def test_system_prompt_respawn_prohibition_references_failover():
    prompt = build_system_prompt(
        config=Config(cwd="/repo"),
        allowed_types=["codex"],
        skills=[],
        session_output_dir="/repo/_outputs/run_123",
    )

    assert "Exception: API error failovers" in prompt
