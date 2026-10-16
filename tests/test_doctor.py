"""``kelpie doctor``: every check passes, fails or is skipped, and the exit code follows."""

from __future__ import annotations

import json
import shutil
import subprocess
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from kelpie.cli import main
from kelpie.core.storage import SqliteSpanStore
from kelpie.doctor import Check, run_checks
from tests.adapters.kanban import Board
from tests.traces.test_span_store import span

NOW = datetime.now(UTC)
DOWN = "http://127.0.0.1:9"  # the discard port: nothing listens there


class Farm:
    """A temporary Hermes farm: board, spool, span store and a kelpie.toml for them."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.board = Board(root / "kanban.db")
        self.spool = root / "spool.jsonl"
        self.db = root / "kelpie.db"
        self.extra = ""
        self.adapter_extra = ""

    def config(self) -> Path:
        path = self.root / "kelpie.toml"
        path.write_text(
            f'[storage]\npath = "{self.db}"\n'
            f'[adapters.hermes]\ndb_path = "{self.board.path}"\nspool = "{self.spool}"\n'
            f"{self.adapter_extra}\n{self.extra}"
        )
        return path

    def checks(self, prometheus: str | None = None) -> dict[str, Check]:
        found = run_checks(self.config(), prometheus, now=lambda: NOW, environ={})
        return {c.name: c for c in found}

    def spool_line(self, age: timedelta) -> None:
        line = {"type": "tool", "at": (NOW - age).timestamp(), "tool": "terminal"}
        self.spool.write_text(json.dumps(line) + "\n")

    def add_span(self, age: timedelta) -> None:
        store = SqliteSpanStore(self.db)
        store.append_spans([span(1, NOW - age, end=NOW - age)], NOW)
        store.close()

    def make_healthy(self) -> None:
        self.board.task("t_1", "ready", NOW - timedelta(hours=2))
        self.spool_line(timedelta(minutes=5))
        self.add_span(timedelta(minutes=5))


@pytest.fixture
def farm(tmp_path: Path) -> Iterator[Farm]:
    f = Farm(tmp_path)
    yield f
    f.board.close()


def test_healthy_farm_passes_everything_but_prometheus(farm: Farm) -> None:
    farm.make_healthy()
    checks = farm.checks()
    assert {n: c.status for n, c in checks.items()} == {
        "config loads": "PASS",
        "observe only": "PASS",
        "kanban.db": "PASS",
        "hooks fire": "PASS",
        "OTLP spans arrive": "PASS",
        "worktrees readable": "SKIP",
        "PR blocks have conditions": "PASS",
        "Prometheus reachable": "SKIP",
    }
    assert "1 tasks" in checks["kanban.db"].detail
    assert "5m old" in checks["hooks fire"].detail


def test_bad_config_is_one_failure(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text("[nonsense]\n")
    (check,) = run_checks(path, environ={})
    assert (check.status, check.name) == ("FAIL", "config loads")
    assert "nonsense" in check.detail
    assert check.render().splitlines()[1].startswith("  fix: ")


def test_no_hermes_section_skips_the_board_checks() -> None:
    checks = {c.name: c.status for c in run_checks(None, environ={})}
    assert checks["kanban.db"] == checks["hooks fire"] == checks["worktrees readable"] == "SKIP"
    assert checks["PR blocks have conditions"] == "SKIP"
    assert checks["observe only"] == "PASS"


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ("[engine]\ndry_run = false\n", "dry_run is off"),
        ('[engine]\ndefault_mode = "suggest"\n', "exit_contract"),
        ('[engine.rules.exit_contract]\nmode = "act"\n', "exit_contract"),
    ],
)
def test_observe_only_fails_with_a_fix(farm: Farm, extra: str, needle: str) -> None:
    farm.extra = extra
    check = farm.checks()["observe only"]
    assert check.status == "FAIL"
    assert needle in check.detail
    assert "dry_run = true" in check.fix


def test_board_missing_fails(farm: Farm) -> None:
    farm.board.path.unlink()
    check = farm.checks()["kanban.db"]
    assert check.status == "FAIL"
    assert "not found" in check.detail
    assert "db_path" in check.fix


def test_hooks_and_spans_skip_on_a_quiet_farm(farm: Farm) -> None:
    farm.board.task("t_1", "ready", NOW - timedelta(hours=2))
    farm.spool_line(timedelta(hours=3))
    farm.add_span(timedelta(hours=5))
    checks = farm.checks()
    assert checks["hooks fire"].status == checks["OTLP spans arrive"].status == "SKIP"
    assert "no task is running" in checks["hooks fire"].detail
    assert "0 running" in checks["kanban.db"].detail


def test_hooks_missing_unreadable_and_old_spool_fail_while_a_task_runs(farm: Farm) -> None:
    farm.board.task("t_1", "running", NOW - timedelta(hours=2))
    assert farm.checks()["hooks fire"].detail.startswith("spool missing")
    farm.spool.write_text("not json\n")
    assert "no readable lines" in farm.checks()["hooks fire"].detail
    farm.spool_line(timedelta(hours=3))
    check = farm.checks()["hooks fire"]
    assert check.status == "FAIL"
    assert "3h old" in check.detail
    assert "plugin" in check.fix


def test_hooks_skipped_when_spool_disabled(farm: Farm) -> None:
    farm.adapter_extra = "spool = false\n"
    path = farm.config()
    text = path.read_text().replace(f'spool = "{farm.spool}"\n', "")
    path.write_text(text)
    checks = {c.name: c for c in run_checks(path, environ={})}
    assert checks["hooks fire"].status == "SKIP"


def test_spans_never_arrived_is_a_skip_and_creates_nothing(farm: Farm) -> None:
    farm.board.task("t_1", "running", NOW - timedelta(hours=2))
    check = farm.checks()["OTLP spans arrive"]
    assert check.status == "SKIP"
    assert "optional" in check.detail
    assert not farm.db.exists()  # read-only: a missing store is not created


def test_spans_that_stopped_fail_while_a_task_runs(farm: Farm) -> None:
    farm.board.task("t_1", "running", NOW - timedelta(hours=2))
    farm.add_span(timedelta(hours=5))
    check = farm.checks()["OTLP spans arrive"]
    assert check.status == "FAIL"
    assert "5h old" in check.detail
    assert "hermes-otel" in check.fix


def test_spans_skipped_when_traces_are_disabled(farm: Farm) -> None:
    farm.extra = "[traces]\nenabled = false\n"
    assert farm.checks()["OTLP spans arrive"].status == "SKIP"


@pytest.mark.skipif(shutil.which("git") is None, reason="git is not installed")
def test_worktrees_pass_and_fail(farm: Farm, tmp_path: Path) -> None:
    good, broken = tmp_path / "good", tmp_path / "broken"
    good.mkdir()
    (broken / ".git").mkdir(parents=True)
    subprocess.run(["git", "-C", str(good), "init", "-q"], check=True, capture_output=True)
    for task, path in (("t_1", good), ("t_2", broken)):
        farm.board.task(task, "running", NOW - timedelta(hours=1))
        farm.board.update(task, workspace_kind="worktree", workspace_path=str(path))
    check = farm.checks()["worktrees readable"]
    assert check.status == "FAIL"
    assert check.detail.startswith("1 ok, 1 failed")
    assert "git failed" in check.detail
    assert "git_state = false" in check.fix
    farm.board.update("t_2", status="done")  # only the good workspace is left
    check = farm.checks()["worktrees readable"]
    assert (check.status, check.detail) == ("PASS", "1 ok, 0 failed")


def test_worktrees_skipped_without_worktrees_or_with_git_state_off(farm: Farm) -> None:
    farm.board.task("t_1", "running", NOW)  # scratch workspace
    assert farm.checks()["worktrees readable"].detail.startswith("no live task")
    farm.adapter_extra = "git_state = false\n"
    assert farm.checks()["worktrees readable"].detail == "git_state = false"


@pytest.fixture
def prometheus() -> Iterator[str]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200 if self.path == "/-/ready" else 404)
            self.end_headers()
            self.wfile.write(b"ready")

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_port}"
    server.shutdown()
    server.server_close()


def test_prometheus_reachable(farm: Farm, prometheus: str) -> None:
    assert farm.checks(prometheus)["Prometheus reachable"].status == "PASS"


def test_prometheus_given_but_down_fails(farm: Farm) -> None:
    check = farm.checks(DOWN)["Prometheus reachable"]
    assert check.status == "FAIL"
    assert "--prometheus" in check.fix


def test_prometheus_only_http_urls(farm: Farm) -> None:
    check = farm.checks("file:///etc/passwd")["Prometheus reachable"]
    assert check.status == "FAIL"
    assert "only http" in check.detail


def test_prometheus_url_from_environment(farm: Farm) -> None:
    found = run_checks(farm.config(), environ={"KELPIE_PROMETHEUS_URL": DOWN})
    assert {c.name: c.status for c in found}["Prometheus reachable"] == "FAIL"


def test_cli_prints_lines_and_exits_1_on_failure(
    farm: Farm, capsys: pytest.CaptureFixture[str]
) -> None:
    farm.make_healthy()
    assert main(["--config", str(farm.config()), "doctor", "--prometheus", DOWN]) == 1
    out = capsys.readouterr().out
    assert "PASS  config loads: " in out
    assert "FAIL  Prometheus reachable: " in out
    assert "\n  fix: start Prometheus" in out


def test_cli_exits_0_when_nothing_fails(
    farm: Farm, prometheus: str, capsys: pytest.CaptureFixture[str]
) -> None:
    farm.make_healthy()
    assert main(["--config", str(farm.config()), "doctor", "--prometheus", prometheus]) == 0
    assert "FAIL" not in capsys.readouterr().out


# --- block reasons that name PRs (live 24 h review) --------------------------------------

GITHUB = '[connectors.github]\nrepos = ["example/app"]\n'
PATTERN = (
    "[[block_conditions]]\npattern = 'PR\\s*#(?P<pr>\\d+)'\nkind = \"pr_merged\"\n"
    'repo = "example/app"\n'
)


def blocked(farm: Farm, tid: str, reason: str, kind: str = "needs_input") -> None:
    farm.board.task(tid, "blocked", NOW - timedelta(hours=3))
    payload = {"kind": kind, "reason": reason}
    farm.board.event(tid, "blocked", NOW - timedelta(hours=2), payload)


@pytest.mark.parametrize(
    "reason",
    [
        "automerge PR #412",
        "waiting for example/app#12 to merge",
        "see https://github.com/example/app/pull/7",
        "pull/7 is red",
        "PR-412 first",
        "PR: 412 needs a rebase",
        "after pull request 12 lands",
        "waiting for the merge of #12",
    ],
)
def test_reasons_that_name_a_pr(reason: str) -> None:
    from kelpie.doctor import _names_a_pr

    assert _names_a_pr(reason)


@pytest.mark.parametrize(
    "reason",
    [
        "blocked on issue #45",
        "see ticket #123",
        "attempt #3 failed",
        "build #1234 red",
        "colour #000000",
        "which certificate should I use?",
    ],
)
def test_reasons_that_name_no_pr(reason: str) -> None:
    from kelpie.doctor import _names_a_pr

    assert not _names_a_pr(reason)


def test_pr_blocks_without_a_github_connector_warn(farm: Farm) -> None:
    farm.make_healthy()
    blocked(farm, "t_2", "automerge PR #412")
    blocked(farm, "t_3", "which certificate?")
    check = farm.checks()["PR blocks have conditions"]
    assert check.status == "WARN" and "1 block reason(s) name a PR (t_2)" in check.detail
    assert "[connectors.github] is not configured" in check.detail
    assert "fix: add [connectors.github]" in check.render()
    farm.extra = GITHUB.replace("[connectors.github]\n", "[connectors.github]\nenabled = false\n")
    assert "is disabled" in farm.checks()["PR blocks have conditions"].detail


def test_pr_blocks_without_patterns_with_unmatched_wording_and_unresolved_refs(
    farm: Farm,
) -> None:
    farm.make_healthy()
    blocked(farm, "t_2", "automerge PR #412")
    blocked(farm, "t_3", "waiting for https://github.com/example/app/pull/7")
    blocked(farm, "t_4", "merge of other/repo PR #9 first")
    farm.extra = GITHUB
    check = farm.checks()["PR blocks have conditions"]
    assert check.status == "WARN" and "no [[block_conditions]] pattern" in check.detail
    ready_only = PATTERN + 'applies_to = ["ready_task"]\n'
    farm.extra = GITHUB + ready_only
    assert "no [[block_conditions]] pattern" in farm.checks()["PR blocks have conditions"].detail
    farm.extra = GITHUB + PATTERN
    check = farm.checks()["PR blocks have conditions"]
    assert check.status == "WARN"
    assert "1 match no [[block_conditions]] pattern (t_3)" in check.detail
    # A bare "#9" with no repo = on the pattern, or another repository, is not checkable.
    bare = PATTERN.replace('repo = "example/app"\n', "")
    farm.extra = GITHUB + bare
    check = farm.checks()["PR blocks have conditions"]
    assert "match but name no PR of [connectors.github] repos" in check.detail


def test_pr_blocks_all_resolved_pass_and_warnings_do_not_fail(
    farm: Farm, capsys: pytest.CaptureFixture[str]
) -> None:
    farm.make_healthy()
    blocked(farm, "t_2", "automerge PR #412")
    blocked(farm, "t_3", "circuit breaker: PR #1 build failed", kind="guard")  # not counted
    farm.extra = GITHUB + PATTERN
    check = farm.checks()["PR blocks have conditions"]
    assert check.status == "PASS" and "1 block reason(s)" in check.detail
    farm.extra = ""
    assert main(["--config", str(farm.config()), "doctor"]) == 0  # a WARN is not a failure
    out = capsys.readouterr().out
    assert "WARN  PR blocks have conditions" in out and "FAIL" not in out.replace("FAIL ", "")


def test_many_pr_blocks_are_cut_short(farm: Farm) -> None:
    farm.make_healthy()
    for n in range(2, 12):
        blocked(farm, f"t_{n}", f"automerge PR #{n}")
    detail = farm.checks()["PR blocks have conditions"].detail
    assert "(t_2, t_3, t_4, t_5, t_6 and 5 more)" in detail
