"""The ``board`` label on per-task and per-adapter metrics (#283): three boards of one
Hermes home (two app projects and a service), the label bounded to configured names."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from kelpie.adapters.hermes import HermesAdapter
from kelpie.adapters.hermes.boards import HermesBoards
from kelpie.app import ControlLoop, merged_snapshot
from kelpie.core.engine import Engine
from kelpie.core.model import Owner, Snapshot, Status, Task
from kelpie.metrics import Metrics
from tests.adapters.kanban import Board
from tests.test_single_board_golden import _families, assert_golden

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
NAMES = ("app-a", "app-b", "svc")


def three_boards(tmp_path: Path, broken: str | None = None) -> HermesBoards:
    boards = {n: Board(tmp_path / f"{n}.db") for n in NAMES}
    hour = timedelta(hours=1)
    boards["app-a"].task("t_1", "ready", NOW - hour)
    boards["app-a"].task("t_2", "running", NOW - 2 * hour, assignee="dev-1", started=NOW - hour)
    boards["app-b"].task("t_1", "done", NOW - 5 * hour, completed=NOW - hour)
    boards["app-b"].task("t_3", "triage", NOW - 4 * hour)
    boards["svc"].task("t_9", "blocked", NOW - 3 * hour, assignee="ops-1", block_kind="needs_input")
    for b in boards.values():
        b.close()
    kids = [
        HermesAdapter(
            db_path=tmp_path / ("gone.db" if n == broken else f"{n}.db"),
            spool=None,
            git_state=False,
            board=n,
            name=f"hermes:{n}",
        )
        for n in NAMES
    ]
    return HermesBoards(kids)


def scrape(adapter: HermesBoards) -> str:
    metrics = Metrics()
    metrics.set_adapters([adapter.name])
    metrics.set_boards(adapter.name, adapter.board_names)
    source = merged_snapshot([adapter], metrics, clock=lambda: NOW)
    ControlLoop(Engine([]), source, metrics).tick_once()
    return metrics.render()


def test_three_boards_label_tasks_stages_and_roles(tmp_path: Path) -> None:
    text = scrape(three_boards(tmp_path))
    assert 'kelpie_tasks{board="app-a",status="ready"} 1' in text
    assert 'kelpie_tasks{board="app-a",status="in_progress"} 1' in text
    assert 'kelpie_tasks{board="app-b",status="done"} 1' in text
    assert 'kelpie_tasks{board="svc",status="blocked"} 1' in text
    assert 'kelpie_tasks{board="svc",status="ready"} 0' in text  # every status of every board
    assert 'kelpie_prework_tasks{board="app-b",platform_status="triage"} 1' in text
    assert 'kelpie_tasks_by_stage{board="app-a",role="dev",stage="working"}' in text
    assert 'kelpie_tasks{status="ready"}' not in text  # no unlabelled series beside them
    assert 'board=""' not in text  # every task is on a configured board
    for name in NAMES:
        assert f'kelpie_adapter_errors_total{{adapter="hermes",board="{name}"}} 0' in text
    # agents are the home's, shared by the boards: no board label on them
    assert "kelpie_role_agents{role=" in text and "kelpie_idle_agents{role=" in text
    assert_golden("metrics_three_boards.txt", _families(text))


def test_a_broken_board_counts_under_its_label(tmp_path: Path) -> None:
    text = scrape(three_boards(tmp_path, broken="svc"))
    assert 'kelpie_adapter_errors_total{adapter="hermes",board="svc"} 1' in text
    assert 'kelpie_adapter_errors_total{adapter="hermes",board="app-a"} 0' in text
    assert 'kelpie_adapter_errors_total{adapter="hermes"} 0' in text


def test_label_values_never_come_from_the_data() -> None:
    metrics = Metrics()
    metrics.set_boards("hermes", NAMES)
    tasks = (
        Task("app-a:t_1", "t", Status.DONE, NOW, role="dev", board="app-a"),
        Task("x:t_1", "t", Status.READY, NOW, board="x\nevil"),  # not configured
        Task("cc/1", "t", Status.READY, NOW),  # another adapter: no board
        # an agent's board on a hub: its name is the agent's, not this config's
        Task("h1:app-a:t_2", "t", Status.READY, NOW, board="app-a", host="h1"),
    )
    snap = Snapshot(now=NOW, tasks=tasks, owners=(Owner("d", "dev"),))
    ControlLoop(Engine([]), lambda: snap, metrics).tick_once()
    text = metrics.render()
    boards = {
        line.split('board="')[1].split('"')[0] for line in text.splitlines() if 'board="' in line
    }
    assert boards == {*NAMES, ""}
    assert 'kelpie_tasks{board="",status="ready"} 3' in text
    assert 'kelpie_role_tasks_done_24h{board="app-a",role="dev"} 1' in text
    assert 'kelpie_role_tasks_done_24h{board="svc",role="dev"} 0' in text
    metrics.adapter_failed("hermes", "x")  # an unconfigured board keeps the old form
    assert 'kelpie_adapter_errors_total{adapter="hermes/x"} 1' in metrics.render()
