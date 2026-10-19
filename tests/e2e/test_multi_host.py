"""One farm on two machines (``tests/sim/hosts.py``): ``kelpie agent`` on each, a central
``kelpie serve`` with a hub.

- Both boards are evaluated, with host-namespaced ids, and an action on one host's task is
  carried out by that host's agent only, through that host's Hermes CLI.
- An agent going silent is told once; its tasks are not treated as abandoned and the alerts
  open on them are neither resolved nor repeated, while the other host carries on.
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.sim import github
from kelpie.sim.board import connect, epoch
from tests.sim import channels
from tests.sim.farm import HERMES_STALE, Farm
from tests.sim.hosts import MultiHostFarm
from tests.sim.kelpie import KelpieServe, wait_for


@pytest.fixture
def hosts(tmp_path: Path) -> Iterator[MultiHostFarm]:
    with channels.serve() as chans, github.serve() as gh:
        sim = MultiHostFarm(tmp_path, chans, gh)
        try:
            yield sim
        finally:
            sim.close()


def live_run(farm: Farm, task_id: str, title: str, minutes: float = 40) -> int:
    b = farm.board
    b.task(task_id, "todo", farm.ago(hours=2), title=title, assignee="ops")
    return b.claim(
        task_id,
        "ops",
        farm.ago(minutes=minutes),
        heartbeat=farm.ago(seconds=20),
        claim_expires=farm.now + timedelta(minutes=10),
    )


def heartbeat_loop(farm: Farm, task_id: str, run_id: int, calls: int = 20) -> None:
    for i in range(calls):
        farm.spool.tool_call(
            task_id, run_id, "heartbeat_check", {"target": "all"},
            at=farm.ago(seconds=30 * (calls - i)),
        )  # fmt: skip


def healthy_work(farm: Farm, task_id: str, run_id: int) -> None:
    for i, tool in enumerate(["read_file", "edit_file", "terminal", "read_file", "edit_file"]):
        farm.spool.tool_call(task_id, run_id, tool, {"step": i}, at=farm.ago(minutes=9 - 2 * i))


def test_an_action_is_routed_to_the_host_that_owns_the_task(hosts: MultiHostFarm) -> None:
    laptop, vm = hosts.hosts["laptop"].farm, hosts.hosts["vm-1"].farm
    # The same task id on both machines: a runaway loop on the laptop, healthy work on the VM.
    loop = live_run(laptop, "t_loop", "Nightly maintenance heartbeat")
    heartbeat_loop(laptop, "t_loop", loop)
    healthy = live_run(vm, "t_loop", "Paginate the audit log", minutes=30)
    healthy_work(vm, "t_loop", healthy)
    kelpie = hosts.serve(hosts.config({"loop_breaker": {"mode": "act"}}))

    wait_for("the stop", lambda: laptop.board.get("t_loop").status == "blocked", timeout=30)
    message = hosts.wait_message("loop_breaker", "laptop:t_loop")
    assert message.startswith("\U0001f534 Runaway run: ")
    assert " · laptop:t_loop" in message
    assert f"Stopped run laptop:hermes-run-{loop}" in message
    kelpie.wait_ticks(3)  # a stopped run is not stopped again; the healthy one never is

    (call,) = laptop.hermes_calls()
    assert call["argv"][:6] == ["kanban", "block", "--kind", "needs_input", "--", "t_loop"]
    assert call["board"] == str(laptop.board.path)
    assert vm.hermes_calls() == []
    assert vm.board.get("t_loop").status == "running"
    assert hosts.messages("vm-1:t_loop") == []


class Heartbeats:
    """Hermes keeps running on both machines: the live runs heartbeat every second, whether
    or not a machine's ``kelpie agent`` is up. A board the hub kept merging after its agent
    went silent would show these heartbeats ageing."""

    def __init__(self, runs: list[tuple[Farm, str, int]]) -> None:
        self.runs = runs
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._beat, daemon=True)

    def _beat(self) -> None:
        while not self.stop.wait(1.0):
            now = epoch(datetime.now(UTC))
            for farm, task_id, run_id in self.runs:
                with connect(farm.board.path) as conn:
                    conn.execute(
                        "UPDATE task_runs SET last_heartbeat_at = ? WHERE id = ?", (now, run_id)
                    )
                    conn.execute(
                        "UPDATE tasks SET last_heartbeat_at = ? WHERE id = ?", (now, task_id)
                    )

    def __enter__(self) -> Heartbeats:
        self.thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop.set()
        self.thread.join(5)


def test_a_silent_agent_does_not_make_its_tasks_look_abandoned(hosts: MultiHostFarm) -> None:
    runs = []
    for name in ("laptop", "vm-1"):
        farm = hosts.hosts[name].farm
        b = farm.board
        # A review nobody owns, with work waiting behind it: an open orphan alert.
        b.task("t_auth", "todo", farm.ago(hours=6), title="Shared auth", assignee="dev")
        b.claim("t_auth", "dev", farm.ago(hours=5))
        b.hand_off("t_auth", farm.ago(hours=3))
        b.update("t_auth", assignee=None)
        for i in range(2):
            b.task(f"t_dep{i}", "todo", farm.ago(hours=6), title=f"Protect endpoint {i}")
            b.link("t_auth", f"t_dep{i}")
        # A live run: fine while its heartbeats are seen; a stale copy of the board would
        # make it a dead session after 20s (exit_contract).
        runs.append((farm, "t_live", live_run(farm, "t_live", "Paginate", minutes=30)))
    stale = {"session_stale_after": "20s", "session_exit_grace": "0s"}
    with Heartbeats(runs):
        kelpie = hosts.serve(
            hosts.config(
                {
                    "orphan_status": {"mode": "suggest", **HERMES_STALE},
                    "exit_contract": {"mode": "suggest", **stale},
                },
                # Without the freeze, a state its rule stops reporting resolves within 2s.
                {"engine": {"state_resolve_after": "2s"}},
            )
        )
        hosts.wait_message("orphan_status", "laptop:t_auth")
        hosts.wait_message("orphan_status", "vm-1:t_auth")
        kelpie.wait_ticks(2)
        before = len(hosts.channels.telegram_texts())
        assert open_states(kelpie, "orphan_status") == 2

        killed = time.monotonic()
        hosts.hosts["laptop"].kill()
        silent = hosts.wait_message("No fresh report from agent laptop", timeout=40)
        assert "not evaluated" in silent
        assert not hosts.up("laptop") and hosts.up("vm-1")
        assert task_count(kelpie) == 4  # only vm-1's board is evaluated
        # The other machine carries on: vm-1's review gets an owner and its alert resolves.
        hosts.hosts["vm-1"].farm.board.update("t_auth", assignee="dev")
        resolved = hosts.wait_message("resolved after", "vm-1:t_auth")
        # Long enough for a stale laptop board to show a dead session (20s) and for any
        # laptop state to resolve (2s).
        wait_for("30s of silence", lambda: time.monotonic() - killed > 30, timeout=40)
        kelpie.wait_ticks(2)
        during = hosts.channels.telegram_texts()[before:]
        assert during == [silent, resolved], during  # nothing about the laptop's tasks
        assert open_states(kelpie, "orphan_status") == 1  # the laptop's, held

        hosts.hosts["laptop"].start(hosts.hub_url)
        hosts.wait_up("laptop")
        hosts.wait_message("Agent laptop reports again")
        kelpie.wait_ticks(5)
        after = hosts.channels.telegram_texts()[before:]
        assert [t for t in after if "laptop:t_" in t] == []
        assert len(hosts.messages("orphan_status", "laptop:t_auth")) == 1
        assert task_count(kelpie) == 8
        assert kelpie.metrics()['kelpie_agent_up{agent="laptop"}'] == 1


def open_states(kelpie: KelpieServe, rule: str) -> float:
    return kelpie.metrics().get(f'kelpie_open_states{{rule="{rule}"}}', 0)


def task_count(kelpie: KelpieServe) -> float:
    return sum(v for k, v in kelpie.metrics().items() if k.startswith("kelpie_tasks{"))
