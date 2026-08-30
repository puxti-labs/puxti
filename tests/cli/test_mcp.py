"""`puxti mcp init` — write / print the agent skill; `puxti mcp serve` help."""

from pathlib import Path

from puxti.cli import app
from tests.cli._helpers import plain, runner

_SKILL_PATH = Path(".claude") / "skills" / "puxti-analytics" / "SKILL.md"


def test_mcp_init_help():
    result = runner.invoke(app, ["mcp", "init", "--help"])
    assert result.exit_code == 0
    out = plain(result.output)
    assert "--print" in out
    assert "--force" in out


def test_mcp_init_writes_skill_file(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["mcp", "init"])
    assert result.exit_code == 0
    dest = tmp_path / _SKILL_PATH
    assert dest.exists()
    content = dest.read_text()
    # Frontmatter + the four tools + the honor-latest rule + the footer template.
    assert content.startswith("---\nname: puxti-analytics")
    for tool in ("describe_entity", "definition_history", "impact_of_change", "consumers"):
        assert tool in content
    assert "Honor the latest version" in content
    assert "via puxti" in content
    assert "SKILL.md" in plain(result.output)


def test_mcp_init_print_goes_to_stdout_without_writing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    result = runner.invoke(app, ["mcp", "init", "--print"])
    assert result.exit_code == 0
    # --print must not create the file.
    assert not (tmp_path / _SKILL_PATH).exists()
    out = result.output
    assert "name: puxti-analytics" in out
    # The `[brackets]`/`<placeholders>` in the skill survive (not eaten by rich markup).
    assert "impact_of_change" in out
    assert "<entity_id>" in out


def test_mcp_init_refuses_to_overwrite_without_force(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = runner.invoke(app, ["mcp", "init"])
    assert first.exit_code == 0
    (tmp_path / _SKILL_PATH).write_text("edited by hand")

    second = runner.invoke(app, ["mcp", "init"])
    assert second.exit_code == 1
    assert "already exists" in plain(second.output)
    # Existing content is untouched.
    assert (tmp_path / _SKILL_PATH).read_text() == "edited by hand"


def test_mcp_init_force_overwrites(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    runner.invoke(app, ["mcp", "init"])
    (tmp_path / _SKILL_PATH).write_text("edited by hand")

    result = runner.invoke(app, ["mcp", "init", "--force"])
    assert result.exit_code == 0
    assert "puxti-analytics" in (tmp_path / _SKILL_PATH).read_text()
