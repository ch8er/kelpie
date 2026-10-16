"""One farm on two machines (``tests/sim/hosts.py``): ``kelpie agent`` on each, a central
``kelpie serve`` with a hub.

- Both boards are evaluated, with host-namespaced ids, and an action on one host's task is
  carried out by that host's agent only, through that host's Hermes CLI.
- An agent going silent is told once; its tasks are not treated as abandoned and the alerts
  open on them are neither resolved nor repeated, while the other host carries on.
"""

from __future__ import annotations

from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path

import pytest

from tests.sim import channels, github
from tests.sim.farm import HERMES_STALE, Farm
from tests.sim.hosts import MultiHostFarm
from tests.sim.kelpie import wait_for


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
    assert message.startswith("[CRITICAL] loop_breaker: alert laptop:t_loop")
    assert f"Stopped run laptop:hermes-run-{loop}" in message
    kelpie.wait_ticks(3)  # a stopped run is not stopped again; the healthy one never is

    (call,) = laptop.hermes_calls()
    assert call["argv"][:6] == ["kanban", "block", "--kind", "needs_input", "--", "t_loop"]
    assert call["board"] == str(laptop.board.path)
    assert vm.hermes_calls() == []
    assert vm.board.get("t_loop").status == "running"
    assert hosts.messages("vm-1:t_loop") == []


def test_a_silent_agent_does_not_make_its_tasks_look_abandoned(hosts: MultiHostFarm) -> None:
    for name in ("laptop", "vm-1"):
        b = hosts.hosts[name].farm.board
        farm = hosts.hosts[name].farm
        # A review nobody owns, with work waiting behind it: an open orphan alert.
        b.task("t_auth", "todo", farm.ago(hours=6), title="Shared auth", assignee="dev")
        b.claim("t_auth", "dev", farm.ago(hours=5))
        b.hand_off("t_auth", farm.ago(hours=3))
        b.update("t_auth", assignee=None)
        for i in range(2):
            b.task(f"t_dep{i}", "todo", farm.ago(hours=6), title=f"Protect endpoint {i}")
            b.link("t_auth", f"t_dep{i}")
        # A live run, heartbeating: fine while its host is watched.
        live_run(farm, "t_live", "Paginate the audit log", minutes=30)
    kelpie = hosts.serve(
        hosts.config(
            {"orphan_status": {"mode": "suggest", **HERMES_STALE}},
            # Without the freeze, a state its rule stops reporting resolves within 2s.
            {"engine": {"state_resolve_after": "2s"}},
        )
    )
    hosts.wait_message("orphan_status", "laptop:t_auth")
    hosts.wait_message("orphan_status", "vm-1:t_auth")
    kelpie.wait_ticks(2)
    before = len(hosts.channels.telegram_texts())

    hosts.hosts["laptop"].kill()
    silent = hosts.wait_message("agent laptop silent", timeout=40)
    assert "not evaluated" in silent
    kelpie.wait_ticks(5)
    assert not hosts.up("laptop") and hosts.up("vm-1")
    during = hosts.channels.telegram_texts()[before:]
    assert during == [silent], during  # no resolution, no "abandoned" task, nothing else

    hosts.hosts["laptop"].start(hosts.hub_url)
    hosts.wait_up("laptop")
    hosts.wait_message("agent laptop silent", "reports again")
    kelpie.wait_ticks(5)
    after = hosts.channels.telegram_texts()[before:]
    assert [t for t in after if "t_auth" in t or "t_live" in t] == []
    assert len(hosts.messages("orphan_status", "laptop:t_auth")) == 1
    metrics = kelpie.metrics()
    assert metrics['kelpie_agent_up{agent="laptop"}'] == 1
