"""``kelpie doctor`` with ``[adapters.claude_code]``: task lists, hooks, registration, stop dir."""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.claude_code import doctor as cc_doctor
from kelpie.doctor import Check, run_checks

NOW = datetime.now(UTC)
EVENTS = [*cc_doctor.EVENTS, cc_doctor.STOP_EVENT]


class Home:
    """A synthetic ``~/.claude`` and a kelpie.toml pointing at it."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.claude = root / ".claude"
        self.tasks = self.claude / "tasks"
        self.spool = self.claude / "kelpie" / "spool.jsonl"
        self.stop = self.claude / "kelpie" / "stop"

    def config(self, extra: str = "", lists: str = '["farm"]') -> Path:
        path = self.root / "kelpie.toml"
        path.write_text(
            "[storage]\npath = ':memory:'\n"
            f"[adapters.claude_code]\ntasks_dir = '{self.tasks}'\ntask_lists = {lists}\n"
            f"spool = '{self.spool}'\nstop_dir = '{self.stop}'\n{extra}"
        )
        return path

    def task(self, task_id: str, status: str) -> None:
        folder = self.tasks / "farm"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / f"{task_id}.json").write_text(json.dumps({"id": task_id, "status": status}))

    def line(self, age_s: float) -> None:
        self.spool.parent.mkdir(parents=True, exist_ok=True)
        rec = {"type": "prompt", "at": time.time() - age_s, "session_id": "s-1", "v": 1}
        with self.spool.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def settings(
        self, events: list[str], command: str = "python3 ~/.claude/kelpie/hook.py"
    ) -> None:
        hooks: dict[str, Any] = {
            e: [{"matcher": "*", "hooks": [{"type": "command", "command": command}]}]
            for e in events
        }
        self.claude.mkdir(parents=True, exist_ok=True)
        (self.claude / "settings.json").write_text(json.dumps({"hooks": hooks}))

    def checks(self, config: Path | None = None) -> dict[str, Check]:
        found = run_checks(config or self.config(), environ={})
        return {c.name: c for c in found}


@pytest.fixture
def home(tmp_path: Path) -> Home:
    return Home(tmp_path)


def statuses(checks: dict[str, Check]) -> dict[str, str]:
    return {n: c.status for n, c in checks.items() if n.startswith("claude code")}


def test_healthy_setup(home: Home) -> None:
    home.task("1", "in_progress")
    home.task("2", "pending")
    home.line(age_s=120)
    home.settings(EVENTS)
    checks = home.checks()
    assert statuses(checks) == {
        "claude code task lists": "PASS",
        "claude code hooks fire": "PASS",
        "claude code hook registered": "PASS",
        "claude code stop markers": "PASS",
    }
    assert "farm: 2 tasks, 1 in progress" in checks["claude code task lists"].detail
    assert "2m old" in checks["claude code hooks fire"].detail
    assert "created on the first stop" in checks["claude code stop markers"].detail
    assert not home.stop.exists()  # doctor creates nothing
    # Hermes checks are still reported, skipped
    assert checks["kanban.db"].status == "SKIP"


def test_not_configured_adds_no_checks() -> None:
    assert not statuses({c.name: c for c in run_checks(None, environ={})})


def test_disabled_adapter_skips(home: Home) -> None:
    checks = home.checks(home.config("enabled = false\n"))
    assert set(statuses(checks).values()) == {"SKIP"}


def test_missing_list_fails_with_a_fix(home: Home) -> None:
    check = home.checks()["claude code task lists"]
    assert check.status == "FAIL" and "not found" in check.detail
    assert "CLAUDE_CODE_TASK_LIST_ID" in check.fix


def test_no_lists_is_a_skip(home: Home) -> None:
    home.line(age_s=10)
    checks = home.checks(home.config(lists="[]"))
    assert checks["claude code task lists"].status == "SKIP"
    assert checks["claude code hooks fire"].status == "PASS"


def test_hooks_quiet_farm_skips_busy_farm_fails(home: Home) -> None:
    home.task("1", "pending")
    assert home.checks()["claude code hooks fire"].status == "SKIP"
    home.task("2", "in_progress")
    check = home.checks()["claude code hooks fire"]
    assert check.status == "FAIL" and "spool missing" in check.detail and "hook" in check.fix
    home.spool.parent.mkdir(parents=True, exist_ok=True)
    home.spool.write_text("junk\n")
    assert "no readable lines" in home.checks()["claude code hooks fire"].detail
    home.line(age_s=7200)
    assert "2h old" in home.checks()["claude code hooks fire"].detail


def test_spool_disabled(home: Home) -> None:
    home.task("1", "pending")
    checks = home.checks(_config_without_spool(home))
    assert checks["claude code hooks fire"].status == "SKIP"


def _config_without_spool(home: Home) -> Path:
    path = home.config()
    path.write_text(path.read_text().replace(f"spool = '{home.spool}'", "spool = false"))
    return path


@pytest.mark.parametrize(
    ("events", "status", "needle"),
    [
        (cc_doctor.EVENTS, "FAIL", "PreToolUse (needed to stop sessions)"),
        (["PostToolUse"], "FAIL", "SessionStart"),
        ([], "SKIP", "may be in project settings"),
    ],
)
def test_registration(home: Home, events: list[str], status: str, needle: str) -> None:
    home.settings(list(events))
    check = home.checks()["claude code hook registered"]
    assert check.status == status and needle in check.detail


def test_registration_without_stop_needs_no_pre_tool_use(home: Home) -> None:
    home.settings(list(cc_doctor.EVENTS))
    path = home.config()
    path.write_text(path.read_text().replace(f"stop_dir = '{home.stop}'", "stop_dir = false"))
    checks = home.checks(path)
    assert checks["claude code hook registered"].status == "PASS"
    assert checks["claude code stop markers"].status == "SKIP"


@pytest.mark.parametrize("content", ["{broken", json.dumps(["list"]), json.dumps({"hooks": 3})])
def test_unusable_settings_are_skipped(home: Home, content: str) -> None:
    home.claude.mkdir(parents=True)
    (home.claude / "settings.json").write_text(content)
    assert home.checks()["claude code hook registered"].status == "SKIP"


def test_no_settings_file(home: Home) -> None:
    check = home.checks()["claude code hook registered"]
    assert check.status == "SKIP" and "not found" in check.detail


def test_other_hooks_do_not_count(home: Home) -> None:
    home.settings(EVENTS, command="~/.claude/stop-hook-git-check.sh")
    assert home.checks()["claude code hook registered"].status == "SKIP"


def test_stop_dir_states(home: Home, tmp_path: Path) -> None:
    home.stop.mkdir(parents=True)
    assert home.checks()["claude code stop markers"].detail.endswith("writable")
    home.stop.chmod(0o500)
    try:
        if os.access(home.stop, os.W_OK):
            pytest.skip("running as a user that ignores permissions")
        check = home.checks()["claude code stop markers"]
        assert check.status == "FAIL" and "not writable" in check.detail
    finally:
        home.stop.chmod(0o700)
    home.stop.rmdir()
    home.stop.write_text("")
    assert "not a directory" in home.checks()["claude code stop markers"].detail
    home.stop.unlink()
    (tmp_path / "real").mkdir()
    home.stop.symlink_to(tmp_path / "real")
    check = home.checks()["claude code stop markers"]
    assert check.status == "FAIL" and "symlink" in check.detail


def test_an_old_hook_fails_when_stop_is_on(home: Home) -> None:
    home.task("1", "in_progress")
    home.spool.parent.mkdir(parents=True)
    home.spool.write_text(
        json.dumps({"type": "prompt", "at": time.time(), "session_id": "s"}) + "\n"
    )
    check = home.checks()["claude code hooks fire"]
    assert check.status == "FAIL" and "without stop support" in check.detail
    path = home.config()
    path.write_text(path.read_text().replace(f"stop_dir = '{home.stop}'", "stop_dir = false"))
    assert home.checks(path)["claude code hooks fire"].status == "PASS"


# --- review regressions ---------------------------------------------------------------------


def _hooks_doc(
    events: list[str], matcher: str = "*", command: str = "python3 ~/.claude/kelpie/hook.py"
) -> dict[str, Any]:
    return {
        e: [{"matcher": matcher, "hooks": [{"type": "command", "command": command}]}]
        for e in events
    }


def _write_settings(home: Home, data: Any) -> Path:
    home.claude.mkdir(parents=True, exist_ok=True)
    path = home.claude / "settings.json"
    path.write_text(data if isinstance(data, str) else json.dumps(data))
    return path


def test_hostile_settings_never_hang_or_crash(home: Home, tmp_path: Path) -> None:
    home.claude.mkdir(parents=True)
    path = home.claude / "settings.json"
    os.mkfifo(path)  # would block a plain open()
    check = home.checks()["claude code hook registered"]
    assert check.status == "SKIP" and "not readable" in check.detail
    path.unlink()
    path.symlink_to("/dev/zero")  # unbounded read through a symlink
    assert home.checks()["claude code hook registered"].status == "SKIP"
    path.unlink()
    path.write_text("[" * 200_000)  # RecursionError in json
    check = home.checks()["claude code hook registered"]
    assert check.status == "SKIP" and "not valid JSON" in check.detail
    path.write_text(" " * (cc_doctor.MAX_SETTINGS + 1))
    assert "too large" in home.checks()["claude code hook registered"].detail


def test_settings_content_never_reaches_the_output(home: Home) -> None:
    _write_settings(home, '{"env": {"ANTHROPIC_API_KEY": "sk-SECRET"}, "hooks": [}')
    check = home.checks()["claude code hook registered"]
    assert "SECRET" not in check.render()


def test_disable_all_hooks_fails(home: Home) -> None:
    _write_settings(home, {"disableAllHooks": True, "hooks": _hooks_doc(EVENTS)})
    check = home.checks()["claude code hook registered"]
    assert check.status == "FAIL" and "disableAllHooks" in check.detail


def test_narrow_matchers_fail(home: Home) -> None:
    hooks = _hooks_doc(list(cc_doctor.EVENTS))
    hooks.update(_hooks_doc(["PreToolUse"], matcher="Bash"))
    _write_settings(home, {"hooks": hooks})
    check = home.checks()["claude code hook registered"]
    assert check.status == "FAIL" and "only for some tools" in check.detail


def test_hook_run_from_a_checkout_counts(home: Home) -> None:
    command = "python3 /opt/kelpie/src/kelpie/adapters/claude_code/hook.py"
    _write_settings(home, {"hooks": _hooks_doc(EVENTS, command=command)})
    assert home.checks()["claude code hook registered"].status == "PASS"


def test_future_spool_lines_do_not_pass(home: Home) -> None:
    home.task("1", "in_progress")
    home.line(age_s=-86400)  # a day ahead: forged or mis-clocked
    assert home.checks()["claude code hooks fire"].status == "FAIL"


def test_stop_dir_under_a_file_cannot_be_created(home: Home, tmp_path: Path) -> None:
    blocker = tmp_path / "file"
    blocker.write_text("")
    blocker.chmod(0o700)
    path = home.config()
    path.write_text(
        path.read_text().replace(f"stop_dir = '{home.stop}'", f"stop_dir = '{blocker}/stop'")
    )
    assert home.checks(path)["claude code stop markers"].status == "FAIL"


def test_spans_skip_on_an_idle_claude_code_farm(home: Home, tmp_path: Path) -> None:
    """With Claude Code only, an idle farm (no watched task in progress) skips the spans
    check instead of failing on old spans."""
    from datetime import timedelta

    from kelpie.core.storage import SqliteSpanStore
    from tests.traces.test_span_store import span

    db = tmp_path / "kelpie.db"
    now = datetime.now(UTC)
    store = SqliteSpanStore(db)
    store.append_spans([span(1, now - timedelta(days=30), end=now - timedelta(days=30))], now)
    store.close()
    home.task("1", "pending")
    path = home.config()
    path.write_text(path.read_text().replace("path = ':memory:'", f"path = '{db}'"))
    check = home.checks(path)["OTLP spans arrive"]
    assert check.status == "SKIP"
    home.task("2", "in_progress")
    check = home.checks(path)["OTLP spans arrive"]
    assert check.status == "FAIL" and "OTEL_EXPORTER_OTLP_TRACES_" in check.fix


def test_dot_star_matcher_covers_every_tool(home: Home) -> None:
    hooks = _hooks_doc(list(cc_doctor.EVENTS))
    hooks.update(_hooks_doc(["PreToolUse"], matcher=".*"))
    _write_settings(home, {"hooks": hooks})
    assert home.checks()["claude code hook registered"].status == "PASS"


def test_symlinked_settings_say_why(home: Home, tmp_path: Path) -> None:
    real = tmp_path / "dotfiles-settings.json"
    real.write_text(json.dumps({"hooks": _hooks_doc(EVENTS)}))
    home.claude.mkdir(parents=True)
    (home.claude / "settings.json").symlink_to(real)
    check = home.checks()["claude code hook registered"]
    assert check.status == "SKIP" and "a symlink, not followed" in check.detail
