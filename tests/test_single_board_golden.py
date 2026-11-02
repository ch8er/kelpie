"""Single-board output stays as it was (#283): golden files for what the board label, the
per-board report and the board name in alerts and ``kelpie why`` must not change on a farm
with one board (``tests/golden/``). Run with ``UPDATE_GOLDEN=1`` to rewrite them,
then review the diff: a change here is a change every single-board farm sees."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from pathlib import Path

from kelpie.app import ControlLoop, load_rules
from kelpie.core.config import EngineConfig
from kelpie.core.engine import Engine
from kelpie.core.model import Block, Dependency, Owner, Session, Snapshot, Status, Task
from kelpie.metrics import Metrics
from kelpie.report.daily import render_summary
from kelpie.report.run import generate
from kelpie.why import as_json, explain_snapshot, render
from tests.report.test_daily import BOARD, DAY, db, sources  # noqa: F401 (db: a fixture)
from tests.test_metrics_fleet import NOW
from tests.test_metrics_fleet import _snapshot as fleet_snapshot

GOLDEN = Path(__file__).parent / "golden"
UPDATE = "UPDATE_GOLDEN"  # not KELPIE_*: conftest unsets those
# Metric families with a board label on a farm with several boards.
BOARD_FAMILIES = (
    "kelpie_tasks",
    "kelpie_prework_",
    "kelpie_role_",
    "kelpie_idle_agents",
    "kelpie_adapter_errors_total",
)


def assert_golden(name: str, actual: str) -> None:
    path = GOLDEN / name
    content = actual if actual.endswith("\n") else actual + "\n"
    if os.environ.get(UPDATE) == "1":
        GOLDEN.mkdir(exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
        return
    assert path.exists(), f"no golden {name}: run the tests with {UPDATE}=1 to write it"
    assert content == path.read_text(encoding="utf-8"), f"{name} differs from its golden"


def _families(text: str) -> str:
    keep = []
    for line in text.splitlines():
        name = line.split(" ")[2] if line.startswith("# ") else line
        if name.startswith(BOARD_FAMILIES):
            keep.append(line)
    return "\n".join(keep)


def test_metrics_of_one_board() -> None:
    snapshot = fleet_snapshot()
    prework = Task("T-6", "t", Status.BACKLOG, NOW - timedelta(hours=3), platform_status="triage",
                   prework=True)  # fmt: skip
    returned = Task("T-7", "t", Status.DONE, NOW - timedelta(hours=1), role="coder")
    snapshot = Snapshot(
        now=snapshot.now,
        owners=(*snapshot.owners, Owner("c-2", "coder")),
        tasks=(*snapshot.tasks, prework, returned),
        sessions=(*snapshot.sessions, Session("S-9", "T-7", "c-2", NOW - timedelta(hours=2))),
    )
    metrics = Metrics()
    metrics.set_adapters(["hermes"])
    metrics.adapter_failed("hermes")
    ControlLoop(Engine([]), lambda: snapshot, metrics).tick_once()
    assert_golden("metrics_one_board.txt", _families(metrics.render()))


def test_report_of_one_board(db: Path) -> None:  # noqa: F811 (the fixture)
    report, markdown = generate(sources(db, hour_usd=40.0), DAY, BOARD)
    assert_golden("report_one_board.md", markdown)
    assert_golden("report_one_board_summary.txt", render_summary(report))


def test_why_of_one_board() -> None:
    hour = timedelta(hours=1)
    task = Task("T-1", "Ship it", Status.BLOCKED, NOW - 2 * hour, owner_id="dev", role="coder",
                block=Block("waiting_ci", NOW - 2 * hour), source="hermes")  # fmt: skip
    after = Task("T-2", "Next", Status.READY, NOW - hour)
    snapshot = Snapshot(
        now=NOW,
        owners=(Owner("dev", "coder"),),
        tasks=(task, after),
        dependencies=(Dependency("T-2", "T-1"),),
        sessions=(Session("S-1", "T-1", "dev", NOW - 3 * hour, ended_at=NOW - 2 * hour),),
    )
    config = EngineConfig.from_mapping({"rules": {"stale_blocks": {}, "orphan_status": {}}})
    ex = explain_snapshot(load_rules(config), snapshot, "T-1")
    assert ex is not None
    assert_golden("why_one_board.txt", render(ex))
    assert_golden("why_one_board.json", json.dumps(as_json(ex), indent=2, sort_keys=True))


def test_why_names_the_board_of_three() -> None:
    """With three boards (#283) ``why`` names the board on a line of its own and in JSON."""
    hour = timedelta(hours=1)
    tasks = tuple(
        Task(f"{b}:t_1", "Ship it", Status.BLOCKED, NOW - hour, block=Block("x", NOW - hour),
             board=b)  # fmt: skip
        for b in ("app-a", "app-b", "svc")
    )
    snapshot = Snapshot(now=NOW, tasks=tasks)
    config = EngineConfig.from_mapping({"rules": {"stale_blocks": {}}})
    ex = explain_snapshot(load_rules(config), snapshot, "app-b:t_1")
    assert ex is not None
    lines = render(ex).splitlines()
    assert lines[:2] == ["Task app-b:t_1: Ship it", "  board    app-b"]
    assert as_json(ex)["task"]["board"] == "app-b"
