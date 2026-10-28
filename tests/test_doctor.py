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
from kelpie.doctor import SHOWN_RULES, Check, run_checks
from kelpie.resources.prometheus import PrometheusConfig
from tests.adapters.kanban import Board
from tests.resources.fake_prometheus import FakeProm, fake_prometheus, vector
from tests.traces.test_span_store import span

NOW = datetime.now(UTC)
DOWN = "http://127.0.0.1:9"  # the discard port: nothing listens there


def count(name: str, queries: dict[str, str] | None = None) -> str:
    """The doctor's question for one of rule 7's queries."""
    config = PrometheusConfig("http://x", {}, queries=queries or {})
    return f"count by (instance) (\n{config.query_text(name)}\n)"


class Farm:
    """A temporary Hermes farm: board, spool, span store and a kelpie.toml for them."""

    def __init__(self, root: Path, prom: FakeProm) -> None:
        self.root = root
        self.prom = prom
        prom.responses.update(
            {
                count(name): vector(({"instance": "mac-1:9100"}, "3"))
                for name in ("disk_size", "path_bytes")
            }
        )
        self.board = Board(root / "kanban.db")
        self.spool = root / "spool.jsonl"
        self.db = root / "kelpie.db"
        self.extra = ""
        self.adapter_extra = ""
        # Rule 7's source: doctor fails without one (incident 19).
        self.resources = (
            f'[resources.prometheus]\nurl = "{prom.url}"\nhosts = {{ mac-1 = "mac-1:9100" }}\n'
        )

    def config(self) -> Path:
        path = self.root / "kelpie.toml"
        path.write_text(
            f'[storage]\npath = "{self.db}"\n'
            f'[adapters.hermes]\ndb_path = "{self.board.path}"\nspool = "{self.spool}"\n'
            f"{self.adapter_extra}\n{self.resources}{self.extra}"
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
    with fake_prometheus() as prom:
        f = Farm(tmp_path, prom)
        yield f
        f.board.close()


def test_healthy_farm_passes_everything_but_prometheus(farm: Farm) -> None:
    farm.make_healthy()
    checks = farm.checks()
    assert {n: c.status for n, c in checks.items()} == {
        "config loads": "PASS",
        "rule modes": "PASS",
        "kanban.db": "PASS",
        "hooks fire": "PASS",
        "OTLP spans arrive": "PASS",
        "traces token kept private": "SKIP",
        "models have prices": "PASS",
        "providers have a spend mode": "SKIP",
        "worktrees readable": "SKIP",
        "stop ends workers": "PASS",
        "PR blocks have conditions": "PASS",
        "snooze markers parse": "SKIP",
        "disk watched": "PASS",
        "process counts fresh": "SKIP",
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


@pytest.mark.parametrize(
    ("section", "variable"),
    [
        (
            '[adapters.github_issues]\nrepos = ["example/repo"]\ntoken_env = "KELPIE_X_TOKEN"\n',
            "KELPIE_X_TOKEN",
        ),
        ('[adapters.linear]\nteams = ["ENG"]\ntoken_env = "KELPIE_X_KEY"\n', "KELPIE_X_KEY"),
    ],
)
def test_an_adapter_token_that_is_not_set_is_one_failure_not_a_crash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, section: str, variable: str
) -> None:
    monkeypatch.delenv(variable, raising=False)
    path = tmp_path / "kelpie.toml"
    path.write_text(section)
    (check,) = run_checks(path, environ={})
    assert (check.status, check.name) == ("FAIL", "config loads")
    assert f"${variable} is not set" in check.detail
    assert "set the variable" in check.fix


def test_no_hermes_section_skips_the_board_checks() -> None:
    checks = {c.name: c.status for c in run_checks(None, environ={})}
    assert checks["kanban.db"] == checks["hooks fire"] == checks["worktrees readable"] == "SKIP"
    assert checks["PR blocks have conditions"] == checks["stop ends workers"] == "SKIP"
    assert checks["rule modes"] == "PASS"


def test_rule_modes_on_the_defaults_list_observe_from_default_mode(farm: Farm) -> None:
    check = farm.checks()["rule modes"]
    assert check.status == "PASS"
    assert check.detail.startswith("dry_run on; observe: ")
    assert check.detail.endswith(" rules (default_mode)")


# A channel for alerts and suggestions; its token is only read when kelpie serve starts.
ALERTS = (
    '[[alerts.channels]]\ntype = "telegram"\ntoken_env = "KELPIE_TEST_BOT"\nchat_id = "-100777"\n'
)
HUB = (  # its agents' adapters are known only once they report
    '[hub]\nlisten = "127.0.0.1:9465"\nallow_plain_http = true\n'
    '[[hub.agents]]\nname = "laptop"\ntoken_env = "KELPIE_TEST_AGENT_TOKEN"\n'
)


@pytest.mark.parametrize(
    ("extra", "listed"),
    [
        (  # what the live farm runs: a stop and a suggestion, chosen per rule
            '[engine]\ndry_run = false\n[engine.rules.loop_breaker]\nmode = "act"\n'
            '[engine.rules.needs_you]\nmode = "suggest"\n' + ALERTS,
            "dry_run off; act: loop_breaker (per rule); suggest: needs_you (per rule); observe: ",
        ),
        ("[engine]\ndry_run = false\n", "dry_run off; observe: "),
        (  # suggest for every rule sends alerts, never changes the board: deliberate enough
            '[engine]\ndry_run = false\ndefault_mode = "suggest"\n'
            '[engine.rules.resources]\nmode = "observe"\n' + ALERTS,
            "observe: resources (per rule)",
        ),
    ],
)
def test_deliberate_modes_pass_and_are_listed(farm: Farm, extra: str, listed: str) -> None:
    farm.extra = extra
    check = farm.checks()["rule modes"]
    assert check.status == "PASS", check.detail
    assert listed in check.detail


def test_explicit_and_default_rules_in_one_mode_are_told_apart(farm: Farm) -> None:
    farm.extra = '[engine]\ndry_run = false\ndefault_mode = "suggest"\n'
    farm.extra += '[engine.rules.needs_you]\nmode = "suggest"\n'
    detail = farm.checks()["rule modes"].detail
    assert "suggest: needs_you (per rule), " in detail and " more (default_mode)" in detail


def test_act_on_a_farm_that_cannot_carry_it_out_warns(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"  # no CI connector: nothing cancels a build
    path.write_text('[engine]\ndry_run = false\n[engine.rules.ci_economy]\nmode = "act"\n')
    check = {c.name: c for c in run_checks(path, environ={})}["rule modes"]
    assert check.status == "WARN"
    assert "act: ci_economy (per rule)" in check.detail
    assert "in act but cannot act on this farm: ci_economy (Kelpie cancels no builds" in (
        check.detail
    )
    assert "its actions become alerts" in check.detail
    assert "no [[alerts.channels]]: the alerts, suggestions and action notices of ci_economy" in (
        check.detail
    )
    assert "docs/act-checklist.md" in check.fix and "[[alerts.channels]]" in check.fix


def test_act_that_can_act_without_an_alert_channel_warns(tmp_path: Path) -> None:
    """No channel, no alerter: an act rule loses its alerts and the notices of its actions."""
    path = tmp_path / "kelpie.toml"  # rule 7 only alerts: nothing is missing to act
    path.write_text('[engine]\ndry_run = false\n[engine.rules.resources]\nmode = "act"\n')
    check = {c.name: c for c in run_checks(path, environ={})}["rule modes"]
    assert check.status == "WARN"
    assert "cannot act" not in check.detail
    assert "action notices of resources are only logged and dropped" in check.detail
    path.write_text(path.read_text() + ALERTS)
    assert {c.name: c for c in run_checks(path, environ={})}["rule modes"].status == "PASS"


def test_dry_run_with_rules_configured_to_act_warns_that_nothing_acts(farm: Farm) -> None:
    farm.extra = '[engine.rules.loop_breaker]\nmode = "act"\n'
    farm.extra += '[engine.rules.needs_you]\nmode = "suggest"\n'
    check = farm.checks()["rule modes"]
    assert check.status == "WARN"
    assert "dry_run on; act: loop_breaker (per rule)" in check.detail
    assert "dry_run is on, so nothing acts and nothing is sent" in check.detail
    assert "loop_breaker in act and needs_you in suggest" in check.detail
    assert "dry_run = false" in check.fix and "docs/act-checklist.md" in check.fix


def test_act_from_default_mode_warns(farm: Farm) -> None:
    farm.extra = '[engine]\ndry_run = true\ndefault_mode = "act"\n'
    check = farm.checks()["rule modes"]
    assert check.status == "WARN"
    assert "act comes from default_mode, not per rule" in check.detail
    assert check.detail.startswith("dry_run on; act: ") and " rules (default_mode);" in check.detail
    assert " more; act comes from" in check.detail  # the rules that cannot act, cut short
    assert 'set default_mode = "observe"' in check.fix


def test_act_that_kelpie_serve_refuses_fails_and_only_warns_under_dry_run(farm: Farm) -> None:
    act = '[engine.rules.exit_contract]\nmode = "act"\n'
    farm.extra = "[engine]\ndry_run = false\n" + act  # Hermes cannot requeue: serve refuses
    check = farm.checks()["rule modes"]
    assert check.status == "FAIL"
    assert "kelpie serve refuses to start" in check.detail and "exit_contract" in check.detail
    assert 'mode = "suggest" or "observe"' in check.fix and "act-checklist" in check.fix
    farm.extra = act  # dry_run on: serve starts, nothing acts
    check = farm.checks()["rule modes"]
    assert check.status == "WARN"
    assert "kelpie serve refuses it once dry_run is off" in check.detail
    # Lifting dry_run first would lead straight into the FAIL: take the rule out of act first.
    assert check.fix.startswith('set mode = "suggest" or "observe" in [engine.rules.<name>]')
    assert check.fix.index("before dry_run goes off") < check.fix.index("set dry_run = false")


def test_act_from_default_mode_that_serve_refuses_fails_with_a_default_mode_fix(
    farm: Farm,
) -> None:
    farm.extra = '[engine]\ndry_run = false\ndefault_mode = "act"\n'
    check = farm.checks()["rule modes"]
    assert check.status == "FAIL"
    assert "kelpie serve refuses to start" in check.detail
    assert " more" in check.detail.rsplit(";", 1)[1]  # the refused rules are cut short
    assert check.fix.startswith('set default_mode = "observe" (')
    assert "[engine.rules.<name>]" not in check.fix


def test_a_refused_rule_set_per_rule_beside_act_by_default_gets_both_fixes(farm: Farm) -> None:
    farm.extra = '[engine]\ndry_run = false\ndefault_mode = "act"\n'
    farm.extra += '[engine.rules.exit_contract]\nmode = "act"\n'
    fix = farm.checks()["rule modes"].fix
    assert 'set default_mode = "observe" and set mode = "suggest" or "observe"' in fix
    assert "for exit_contract" in fix


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        ('[engine.rules.dispatch_gate]\nmode = "act"\n', "rule 'dispatch_gate' parks ready"),
        (
            '[engine.rules.conflict_warning]\nmode = "act"\nserialize = true\n',
            "rule 'conflict_warning' serializes tasks",
        ),
    ],
)
def test_parks_that_nothing_wakes_warn(farm: Farm, extra: str, needle: str) -> None:
    farm.extra = "[engine]\ndry_run = false\n" + extra
    check = farm.checks()["rule modes"]
    assert check.status == "WARN"
    assert needle in check.detail and "parked tasks are not woken" in check.detail
    assert "[engine.rules.wait_condition]" in check.fix
    farm.extra += '[engine.rules.wait_condition]\nmode = "act"\n' + ALERTS  # rule 2 wakes them
    assert farm.checks()["rule modes"].status == "PASS"


def test_a_parking_rule_that_cannot_act_is_not_also_said_to_strand_parks(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"  # no platform adapter: the gate parks nothing
    path.write_text('[engine]\ndry_run = false\n[engine.rules.dispatch_gate]\nmode = "act"\n')
    detail = {c.name: c for c in run_checks(path, environ={})}["rule modes"].detail
    assert "cannot act on this farm: dispatch_gate" in detail
    assert "parked tasks are not woken" not in detail


RULES = [
    "exit_contract",
    "wait_condition",
    "orphan_status",
    "stale_blocks",
    "ping_pong",
    "bottlenecks",
]


@pytest.mark.parametrize("n", [SHOWN_RULES, SHOWN_RULES + 1])
def test_rule_names_are_counted_past_shown_rules(farm: Farm, n: int) -> None:
    assert len(RULES) > SHOWN_RULES
    farm.extra = "".join(f'[engine.rules.{r}]\nmode = "observe"\n' for r in RULES[:n])
    detail = farm.checks()["rule modes"].detail
    listed = ", ".join(RULES[:n]) if n <= SHOWN_RULES else f"{n} rules"
    assert f"observe: {listed} (per rule), " in detail
    assert " more (default_mode)" in detail


def test_suggestions_without_an_alert_channel_warn(farm: Farm) -> None:
    farm.extra = '[engine]\ndry_run = false\n[engine.rules.needs_you]\nmode = "suggest"\n'
    check = farm.checks()["rule modes"]
    assert check.status == "WARN"
    assert "no [[alerts.channels]]: the alerts, suggestions and action notices of needs_you" in (
        check.detail
    )
    assert "[[alerts.channels]]" in check.fix
    farm.extra += ALERTS
    assert farm.checks()["rule modes"].status == "PASS"


def test_a_hub_does_not_judge_what_its_agents_carry_out(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"  # no local adapter: on a farm this would be a gap
    path.write_text('[engine]\ndry_run = false\n[engine.rules.orphan_status]\nmode = "act"\n')
    alone = {c.name: c for c in run_checks(path, environ={})}["rule modes"]
    assert alone.status == "WARN" and "orphan_status (no platform adapter" in alone.detail
    path.write_text(path.read_text() + HUB + ALERTS)
    check = {c.name: c for c in run_checks(path, environ={})}["rule modes"]
    assert check.status == "PASS", check.detail
    assert "act: orphan_status (per rule)" in check.detail


def test_cli_exits_0_on_a_rule_modes_warning(
    farm: Farm, prometheus: str, capsys: pytest.CaptureFixture[str]
) -> None:
    farm.make_healthy()
    farm.extra = '[engine.rules.loop_breaker]\nmode = "act"\n'  # dry_run on: a WARN
    assert main(["--config", str(farm.config()), "doctor", "--prometheus", prometheus]) == 0
    out = capsys.readouterr().out
    assert "WARN  rule modes: " in out and "\n  fix: " in out
    assert "FAIL" not in out


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


def test_snooze_markers_that_serve_ignores_warn(farm: Farm) -> None:
    farm.make_healthy()
    check = farm.checks()["snooze markers parse"]
    assert check.status == "SKIP" and "markers are off" in check.detail  # the default
    farm.extra = "[snooze]\nmarker = true\n"
    day = (NOW + timedelta(days=3)).date().isoformat()
    blocked(farm, "t_2", f"CEO deferred R2 until:{day}")
    check = farm.checks()["snooze markers parse"]
    assert (
        check.status == "PASS" and "1 block reason(s) carry a valid until: marker" in check.detail
    )
    blocked(farm, "t_10", "until:2026-13-01")
    blocked(farm, "t_3", "until:soon and until:later")
    blocked(farm, "t_4", f"until:{(NOW + timedelta(days=60)).date().isoformat()}")
    check = farm.checks()["snooze markers parse"]
    assert check.status == "WARN"
    assert "4 marker(s) ignored in 3 block reason(s) (t_3, t_4, t_10)" in check.detail
    assert "t_3: until:soon: not a valid date" in check.detail
    assert "fix: write until:YYYY-MM-DD" in check.render()
    farm.extra = "[snooze]\nmarker = false\n"
    assert farm.checks()["snooze markers parse"].status == "SKIP"


def test_snooze_markers_read_every_local_board(farm: Farm, monkeypatch: pytest.MonkeyPatch) -> None:
    from dataclasses import replace

    from kelpie.app import load_settings
    from kelpie.core.model import Block, Snapshot, Status, Task
    from kelpie.core.snooze import SnoozeSettings
    from kelpie.doctor import check_snooze_markers

    since = NOW - timedelta(days=1)
    card = Task(
        "card-1",
        "Synthetic card",
        Status.BLOCKED,
        since,
        source="openclaw",
        block=Block("until:2026-02-31", since),
    )
    monkeypatch.setattr(
        "kelpie.doctor.local_board", lambda s, now: Snapshot(now=now, tasks=(card,))
    )
    settings = replace(load_settings(farm.config()), snooze=SnoozeSettings(marker=True))
    check = check_snooze_markers(settings, NOW)
    assert check.status == "WARN" and "card-1: until:2026-02-31: not a valid date" in check.detail


def test_snooze_markers_on_an_unreadable_board_skip(farm: Farm) -> None:
    farm.extra = "[snooze]\nmarker = true\n"
    farm.board.conn.execute("DROP TABLE tasks")
    farm.board.conn.commit()
    check = farm.checks()["snooze markers parse"]
    assert check.status == "SKIP" and "board not readable" in check.detail


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


def test_a_farm_whose_disks_nobody_watches_fails(farm: Farm) -> None:
    farm.make_healthy()
    assert "mac-1" in farm.checks()["disk watched"].detail
    # Configured, but node_exporter is missing on a host (incident 19).
    farm.resources = farm.resources.replace(
        'mac-1 = "mac-1:9100" }', 'mac-1 = "mac-1:9100", mac-2 = "mac-2:9100" }'
    )
    check = farm.checks()["disk watched"]
    assert check.status == "FAIL" and "no disk readings for mac-2" in check.detail
    # Disks read, but no directory sizes: a warning.
    farm.prom.responses[count("path_bytes")] = vector()
    farm.resources = farm.resources.replace(', mac-2 = "mac-2:9100"', "")
    check = farm.checks()["disk watched"]
    assert check.status == "WARN" and "no directory sizes from mac-1" in check.detail
    # The queries rule 7 runs are the ones asked, custom ones too.
    custom = {"disk_size": "my_disk_size_bytes", "path_bytes": "my_dirs  # from a recording"}
    farm.prom.responses[count("path_bytes")] = farm.prom.responses[count("disk_size")]
    for name in custom:
        farm.prom.responses[count(name, custom)] = farm.prom.responses[count("disk_size")]
    farm.resources += (
        '[resources.prometheus.queries]\ndisk_size = "my_disk_size_bytes"\n'
        'path_bytes = "my_dirs  # from a recording"\n'
    )
    assert farm.checks()["disk watched"].status == "PASS"
    farm.resources = farm.resources.split("[resources.prometheus.queries]")[0]
    # A token Prometheus refuses: rule 7 reads nothing.
    farm.prom.status = 401
    check = farm.checks()["disk watched"]
    assert check.status == "FAIL" and "requires a token" in check.detail  # none was sent
    farm.prom.status = 200
    # A token_env that is not set.
    farm.resources += 'token_env = "NO_SUCH_KELPIE_TOKEN"\n'
    check = farm.checks()["disk watched"]
    assert check.status == "FAIL" and "NO_SUCH_KELPIE_TOKEN" in check.detail
    farm.resources = farm.resources.replace('token_env = "NO_SUCH_KELPIE_TOKEN"\n', "")
    # Prometheus does not answer: a warning, doctor may run elsewhere.
    farm.resources = farm.resources.replace(farm.prom.url, DOWN)
    check = farm.checks()["disk watched"]
    assert check.status == "WARN" and "did not answer" in check.detail
    farm.resources = ""
    check = farm.checks()["disk watched"]
    assert check.status == "FAIL" and "disk is not watched" in check.detail
    assert "node_exporter" in check.fix


def test_a_prometheus_without_subqueries_is_a_warning(farm: Farm) -> None:
    """The growth query uses subqueries (Prometheus 2.7+): an older one rejects it."""
    farm.make_healthy()
    growth = count("disk_growth")
    farm.prom.responses[growth] = vector(({"instance": "mac-1:9100"}, "2"))
    check = farm.checks()["disk watched"]
    assert check.status == "PASS" and "no disk growth" not in check.detail
    # Rejected: the hint names the version.
    farm.prom.responses[growth] = {"status": "error", "errorType": "bad_data"}
    check = farm.checks()["disk watched"]
    assert check.status == "WARN" and "the disk_growth query failed (query_failed" in check.detail
    assert "Prometheus 2.7 or later" in check.fix
    # Another failure is no version problem.
    farm.prom.responses[growth] = []
    check = farm.checks()["disk watched"]
    assert check.status == "WARN" and "(invalid_response" in check.detail
    assert "2.7" not in check.fix
    # Reported together with missing directory sizes, not instead of them.
    farm.prom.responses[count("path_bytes")] = vector()
    check = farm.checks()["disk watched"]
    assert "the disk_growth query failed" in check.detail
    assert "no directory sizes from mac-1" in check.detail
    assert "kelpie-path-sizes.sh" in check.fix


def test_no_growth_rate_yet_is_only_a_note(farm: Farm) -> None:
    """A new Prometheus has no growth for half a window; slices of too few scrapes neither."""
    farm.make_healthy()
    check = farm.checks()["disk watched"]  # the fake answers the growth query with nothing
    assert check.status == "PASS" and "no disk growth rate yet for mac-1" in check.detail
    assert "8 scrape intervals" in check.detail


def test_a_mac_whose_process_count_stopped_is_a_warning(farm: Farm) -> None:
    farm.make_healthy()
    mac = vector(({"instance": "mac-1:9100"}, "1"))
    farm.prom.responses['count by (instance) (node_uname_info{sysname="Darwin"})'] = mac
    ages = "max by (instance) (time() - " + (
        'node_textfile_mtime_seconds{file=~"(.*/)?kelpie_processes[.]prom"})'
    )
    farm.prom.responses[ages] = vector(({"instance": "mac-1:9100"}, str(3 * 3600)))
    check = farm.checks()["process counts fresh"]
    assert check.status == "WARN" and "mac-1 (last written 3h ago)" in check.detail
    assert "process-limit.plist" in check.fix
    farm.prom.responses[ages] = vector()
    assert "no kelpie_processes.prom" in farm.checks()["process counts fresh"].detail
    farm.prom.responses[count("processes")] = mac  # the script writes again
    check = farm.checks()["process counts fresh"]
    assert check.status == "PASS" and "fresh process counts from mac-1" in check.detail


def test_process_counts_skip_without_a_mac_or_an_answer(farm: Farm) -> None:
    farm.make_healthy()
    check = farm.checks()["process counts fresh"]
    assert check.status == "SKIP" and "no macOS host" in check.detail
    farm.prom.status = 500
    assert farm.checks()["process counts fresh"].status == "SKIP"
    farm.prom.status = 200
    farm.resources = ""
    assert farm.checks()["process counts fresh"].status == "SKIP"


def test_stop_ends_workers_names_the_backend_or_why_not(tmp_path: Path) -> None:
    from kelpie.adapters.hermes import HermesAdapter
    from kelpie.adapters.hermes.proctable import Libproc, Procfs, host_table
    from kelpie.doctor import check_worker_stop

    adapter = HermesAdapter(db_path=tmp_path / "kanban.db", spool=None)
    linux = check_worker_stop(adapter, Procfs())
    assert (linux.status, linux.name) == ("PASS", "stop ends workers")
    assert linux.detail.startswith("via Linux /proc;")
    nolib = check_worker_stop(adapter, Libproc(error="libproc cannot be loaded (x)"))
    assert nolib.status == "WARN" and nolib.detail.startswith("libproc cannot be loaded (x):")
    other = check_worker_stop(adapter, host_table("win32"))
    assert other.status == "WARN" and "on Linux or macOS" in other.fix
    proc = tmp_path / "proc"
    (proc / "self").mkdir(parents=True)
    (proc / "self" / "stat").write_text("1 (x) S 0\n")
    (proc / "self" / "mountinfo").write_text("22 1 0:21 / /proc rw - proc proc rw,hidepid=2\n")
    hidden = check_worker_stop(adapter, Procfs(proc))
    assert hidden.status == "WARN" and "hidepid" in hidden.detail
    adapter.end_worker = False
    off = check_worker_stop(adapter, Procfs())
    assert off.status == "WARN" and off.fix == "set [adapters.hermes] end_worker = true"
