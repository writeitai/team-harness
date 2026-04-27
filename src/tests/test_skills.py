# pyright: reportMissingParameterType=false, reportArgumentType=false

from pathlib import Path

import pytest

from team_harness.skills.loader import load_skill_metadata


@pytest.fixture(autouse=True)
def _isolate_home(tmp_path, monkeypatch):
    """Prevent discovery of real ~/.agents/skills/ during tests."""
    fake_home = tmp_path / "fake-home"
    fake_home.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))


def _make_skill(
    base: Path,
    name: str,
    *,
    description: str = "A test skill",
    fm_name: str | None = None,
) -> Path:
    """Create a SKILL.md in base/name/ with YAML frontmatter."""
    skill_dir = base / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    fm_name_line = f"name: {fm_name}\n" if fm_name else ""
    (skill_dir / "SKILL.md").write_text(
        f"---\n{fm_name_line}description: {description}\n---\n\n# {name}\n\nInstructions here.\n",
        encoding="utf-8",
    )
    return skill_dir


def test_discover_skills_from_project_dir(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    skills_root = project / ".agents" / "skills"
    _make_skill(skills_root, "my-skill", description="My skill does things")

    skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 1
    assert skills[0].name == "my-skill"
    assert skills[0].description == "My skill does things"
    assert skills[0].path == skills_root / "my-skill" / "SKILL.md"
    assert skills[0].skill_dir == skills_root / "my-skill"


def test_frontmatter_parsing(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    skills_root = project / ".agents" / "skills"
    _make_skill(
        skills_root, "parser-skill", description="Parses things", fm_name="parser-skill"
    )

    skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 1
    assert skills[0].name == "parser-skill"
    assert skills[0].description == "Parses things"


def test_missing_name_falls_back_to_dir_name(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    skills_root = project / ".agents" / "skills"
    # No fm_name => no name field in frontmatter
    _make_skill(skills_root, "fallback-skill", description="Has no name field")

    skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 1
    assert skills[0].name == "fallback-skill"


def test_missing_description_skips_with_warning(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / ".agents" / "skills" / "no-desc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: no-desc\n---\n\nBody.\n", encoding="utf-8"
    )

    with pytest.warns(UserWarning, match="Missing description"):
        skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 0


def test_invalid_name_skipped(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    skills_root = project / ".agents" / "skills"
    # Uppercase name directory
    skill_dir = skills_root / "BadName"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: Bad name skill\n---\n\nBody.\n", encoding="utf-8"
    )

    with pytest.warns(UserWarning, match="Invalid skill name"):
        skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 0


def test_no_skill_dirs_returns_empty(tmp_path):
    project = tmp_path / "project"
    project.mkdir()

    skills = load_skill_metadata(cwd=str(project))

    assert skills == []


def test_non_utf8_file_produces_warning_and_is_skipped(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / ".agents" / "skills" / "binary-skill"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_bytes(b"\xff\xfe\x00\x00invalid")

    with pytest.warns(UserWarning, match="Cannot read"):
        skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 0


def test_dotfile_dirs_skipped(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    hidden_dir = project / ".agents" / "skills" / ".hidden"
    hidden_dir.mkdir(parents=True)
    (hidden_dir / "SKILL.md").write_text(
        "---\ndescription: Hidden skill\n---\n\nBody.\n", encoding="utf-8"
    )

    skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 0


def test_name_mismatch_produces_warning_but_uses_dir_name(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    skills_root = project / ".agents" / "skills"
    _make_skill(
        skills_root, "real-name", description="Mismatched", fm_name="wrong-name"
    )

    with pytest.warns(UserWarning, match="does not match"):
        skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 1
    assert skills[0].name == "real-name"


def test_project_skills_override_user_skills(tmp_path):
    # _isolate_home already patches Path.home to fake_home
    fake_home = Path.home()
    project = tmp_path / "project"
    project.mkdir()

    project_skills = project / ".agents" / "skills"
    _make_skill(project_skills, "shared-skill", description="Project version")

    user_skills = fake_home / ".agents" / "skills"
    _make_skill(user_skills, "shared-skill", description="User version")

    skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 1
    assert skills[0].description == "Project version"


def test_no_frontmatter_produces_warning(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / ".agents" / "skills" / "no-fm"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "# Just a heading\n\nNo frontmatter.\n", encoding="utf-8"
    )

    with pytest.warns(UserWarning, match="No YAML frontmatter"):
        skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 0


def test_discovers_repo_root_skills_from_nested_cwd(tmp_path):
    repo = tmp_path / "repo"
    nested = repo / "src" / "deep" / "dir"
    nested.mkdir(parents=True)
    skills_root = repo / ".agents" / "skills"
    _make_skill(skills_root, "root-skill", description="Found from nested cwd")

    skills = load_skill_metadata(cwd=str(nested))

    assert len(skills) == 1
    assert skills[0].name == "root-skill"


def test_invalid_yaml_frontmatter_warns_and_skips(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / ".agents" / "skills" / "bad-yaml"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n: invalid: yaml: [broken\n---\n\nBody.\n", encoding="utf-8"
    )

    with pytest.warns(UserWarning, match="Invalid YAML frontmatter"):
        skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 0


def test_non_mapping_frontmatter_skips(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / ".agents" / "skills" / "list-yaml"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n- item1\n- item2\n---\n\nBody.\n", encoding="utf-8"
    )

    with pytest.warns(UserWarning, match="No YAML frontmatter"):
        skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 0


def test_discover_skills_from_user_dir(tmp_path):
    fake_home = Path.home()
    project = tmp_path / "project"
    project.mkdir()
    # No project skills — only user global
    user_skills = fake_home / ".agents" / "skills"
    _make_skill(user_skills, "global-skill", description="User global skill")

    skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 1
    assert skills[0].name == "global-skill"


@pytest.mark.parametrize(
    "dirname", ["-leading", "trailing-", "double--hyphen", "has_underscore", "A" * 65]
)
def test_invalid_name_variants_rejected(tmp_path, dirname):
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / ".agents" / "skills" / dirname
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: Bad name variant\n---\n\nBody.\n", encoding="utf-8"
    )

    with pytest.warns(UserWarning, match="Invalid skill name"):
        skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 0


def test_non_string_description_coerced(tmp_path):
    project = tmp_path / "project"
    project.mkdir()
    skill_dir = project / ".agents" / "skills" / "int-desc"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\ndescription: 123\n---\n\nBody.\n", encoding="utf-8"
    )

    skills = load_skill_metadata(cwd=str(project))

    assert len(skills) == 1
    assert skills[0].description == "123"
