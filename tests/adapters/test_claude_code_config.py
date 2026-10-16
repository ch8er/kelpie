"""``[adapters.claude_code]``: parsing, building the adapter, ``kelpie status``."""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path

import pytest

from kelpie.adapters.claude_code import ClaudeCodeAdapter
from kelpie.app import ConfigError, build_adapters, load_settings, render_adapters


def settings(tmp_path: Path, text: str):  # type: ignore[no-untyped-def]
    path = tmp_path / "kelpie.toml"
    path.write_text(text)
    return load_settings(path)


def test_absent_by_default() -> None:
    assert load_settings(None).claude_code is None


def test_defaults(tmp_path: Path) -> None:
    cc = settings(tmp_path, "[adapters.claude_code]\n").claude_code
    assert cc is not None
    assert (cc.tasks_dir, cc.task_lists, cc.spool, cc.lookback, cc.enabled) == (
        Path("~/.claude/tasks"),
        (),
        Path("~/.claude/kelpie/spool.jsonl"),
        timedelta(hours=24),
        True,
    )
    (adapter,) = build_adapters(settings(tmp_path, "[adapters.claude_code]\n"))
    assert isinstance(adapter, ClaudeCodeAdapter)
    assert adapter.tasks_dir == Path("~/.claude/tasks").expanduser()
    assert adapter.stop_dir == Path("~/.claude/kelpie/stop").expanduser()


def test_values_and_both_adapters(tmp_path: Path) -> None:
    s = settings(
        tmp_path,
        f"""
[adapters.hermes]
db_path = "{tmp_path}/kanban.db"
[adapters.claude_code]
tasks_dir = "{tmp_path}/tasks"
task_lists = ["farm", "team-a"]
spool = false
lookback = "72h"
stop_dir = false
""",
    )
    adapters = build_adapters(s)
    assert [a.name for a in adapters] == ["hermes", "claude-code"]
    cc = adapters[1]
    assert isinstance(cc, ClaudeCodeAdapter)
    assert (cc.task_lists, cc.spool, cc.lookback, cc.stop_dir) == (
        ("farm", "team-a"),
        None,
        timedelta(hours=72),
        None,
    )


def test_disabled_builds_nothing(tmp_path: Path) -> None:
    assert build_adapters(settings(tmp_path, "[adapters.claude_code]\nenabled = false\n")) == []


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("adapters.claude_code = 3", "must be a table"),
        ("[adapters.claude_code]\nfoo = 1", "unknown"),
        ("[adapters.claude_code]\nenabled = 'yes'", "enabled"),
        ("[adapters.claude_code]\ntasks_dir = ''", "tasks_dir"),
        ("[adapters.claude_code]\nspool = true", "spool"),
        ("[adapters.claude_code]\ntask_lists = 'farm'", "task_lists"),
        ("[adapters.claude_code]\ntask_lists = ['a', 'a']", "task_lists"),
        ("[adapters.claude_code]\ntask_lists = ['']", "task_lists"),
        ("[adapters.claude_code]\nlookback = 'soon'", "lookback"),
        ("[adapters.claude_code]\nlookback = '0s'", "lookback"),
        ("[adapters.claude_code]\nstop_dir = true", "stop_dir"),
        ("[adapters.claude_code]\nstop_dir = ' '", "stop_dir"),
        ("[adapters.claude]\n", r"unknown \[adapters\] entries"),
    ],
)
def test_bad_config(tmp_path: Path, body: str, message: str) -> None:
    with pytest.raises(ConfigError, match=message):
        settings(tmp_path, body)


def test_status_lists_the_adapter(tmp_path: Path) -> None:
    (tmp_path / "tasks" / "farm").mkdir(parents=True)
    s = settings(
        tmp_path,
        f'[adapters.claude_code]\ntasks_dir = "{tmp_path}/tasks"\ntask_lists = ["farm", "gone"]\n'
        f'spool = "{tmp_path}/spool.jsonl"\n',
    )
    lines = render_adapters(build_adapters(s))
    assert lines[2].startswith("  claude-code: NOT readable: task list 'gone' not found")
    assert lines[3] == f"    task lists: farm, gone ({tmp_path}/tasks)"
    assert lines[4] == f"    spool: missing ({tmp_path}/spool.jsonl)"


def test_hermes_act_guard_holds_next_to_claude_code(tmp_path: Path) -> None:
    """Act-mode rules other than stop would fail on Hermes tasks: still refused at startup
    when Claude Code is configured too; Claude Code alone is fine (its refusals alert)."""
    from kelpie.adapters.claude_code.config import ClaudeCodeSettings
    from kelpie.app import HermesSettings, Settings, build_app
    from kelpie.core.config import EngineConfig

    act = EngineConfig.from_mapping({"dry_run": False, "rules": {"exit_contract": {"mode": "act"}}})
    cc = ClaudeCodeSettings(tasks_dir=tmp_path / "tasks", spool=None)
    hermes = HermesSettings(db_path=tmp_path / "kanban.db")
    with pytest.raises(ConfigError, match="actions the Hermes adapter does not carry out"):
        build_app(Settings(engine=act, db_path=None, hermes=hermes, claude_code=cc))
    build_app(Settings(engine=act, db_path=None, claude_code=cc)).close()
