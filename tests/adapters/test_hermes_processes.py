"""Hermes tool runs and tool slots from the host's process table (processes.py)."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.adapters.hermes import processes
from kelpie.adapters.hermes.processes import SlotPool, ToolProcesses, read_host

NOW = datetime(2026, 5, 12, 12, tzinfo=UTC)
PATTERNS = (".worktrees/{task}", "workspaces/{task}")

# pid ppid etime command: an agent's bash in its worktree runs xcodebuild; another agent's
# bash runs ios_sim.sh under lockf; a CI runner's lockf holds a slot; an xcodebuild outside
# any workspace (a person's terminal) belongs to no task.
PS = """\
  100     1  2-01:00:00 /usr/local/bin/hermes-agent --profile dev-1
  101   100    01:00:00 /bin/bash -lc build
  102   101       12:30 /Applications/Xcode.app/usr/bin/xcodebuild -scheme App test
  103   102       10:00 /usr/bin/xcrun simctl boot SECRET=token
  200     1  1-00:00:00 /usr/local/bin/hermes-agent --profile dev-2
  201   200       40:00 /bin/bash -lc sim
  202   201       39:00 lockf -s -t 0 /slots/sim-1.lock ./ios_sim.sh
  203   202       38:59 /bin/bash ./ios_sim.sh --device A
  300     1    05:00:00 /actions-runner/bin/Runner.Worker
  301   300       03:00 lockf -s -t 0 /slots/sim-2.lock make ui-test
  400     1       01:00 xcodebuild -version
  bad line
"""
CWDS = {
    100: "/home/a",
    101: "/repo/.worktrees/t_17",
    102: "/repo/.worktrees/t_17/App",
    201: "/data/hermes/ws/t_99",  # Hermes' own workspace path for t_99
    202: "/data/hermes/ws/t_99",
    203: "/data/hermes/ws/t_99",
    300: "/actions-runner/_work",
    301: "/actions-runner/_work/app",
    400: "/home/a",
}


@pytest.fixture
def host(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    slots = tmp_path / "slots"
    slots.mkdir()
    for i in (1, 2, 3):
        (slots / f"sim-{i}.lock").touch()
    real = os.path.realpath
    holders = {real(slots / "sim-1.lock"): 202, real(slots / "sim-2.lock"): 301}

    def run(argv: list[str], timeout: float, ok: tuple[int, ...] = (0,)) -> str | None:
        if argv[0] == "ps":
            return PS
        if "-d" in argv:  # working directories
            pids = [int(p) for p in argv[argv.index("-p") + 1].split(",")]
            return "".join(f"p{p}\nfcwd\nn{CWDS[p]}\n" for p in pids if p in CWDS)
        files = argv[argv.index("--") + 1 :]
        return "".join(f"p{holders[f]}\nf3\nn{f}\n" for f in files if f in holders)

    monkeypatch.setattr(processes, "_run", run)
    return slots


FARM = {"names": ("xcodebuild", "ios_sim.sh"), "workspace_patterns": PATTERNS}


def config(slots: Path, **kw: object) -> ToolProcesses:
    pool = SlotPool("sim", (str(slots / "sim-*.lock"),))
    return ToolProcesses(slots=(pool,), **{**FARM, **kw})  # type: ignore[arg-type]


def test_tool_runs_by_task_and_slots_by_holder(host: Path) -> None:
    tasks = {"t_17", "t_99", "t_5"}
    found = read_host(config(host), NOW, tasks, {"/data/hermes/ws/t_99": "t_99"})
    assert found is not None
    assert found.runs["t_17"].tool == "xcodebuild"
    assert found.runs["t_17"].since == NOW - timedelta(minutes=12, seconds=30)
    assert found.runs["t_99"].tool == "ios_sim.sh"  # a script: its interpreter's argument
    assert found.runs["t_99"].since == NOW - timedelta(minutes=38, seconds=59)  # not lockf
    assert set(found.runs) == {"t_17", "t_99"}  # pid 400 works in no task workspace
    use = found.pools["sim"]
    assert (use.total, use.agent, use.ci) == (3, 1, 1)


def test_a_workspace_pattern_must_name_a_task_on_the_board(host: Path) -> None:
    found = read_host(config(host), NOW, {"t_99"}, {})
    assert found is not None and set(found.runs) == set()  # t_17 not on the board
    assert found.pools["sim"].agent == 0 and found.pools["sim"].ci == 2


def test_names_and_patterns_are_config(host: Path) -> None:
    custom = config(host, names=("simctl",), workspace_patterns=("worktrees/{task}",))
    found = read_host(custom, NOW, {"t_17"}, {})
    assert found is not None and found.runs == {}  # ".worktrees" is not "worktrees"
    custom = config(host, names=("simctl",))
    found = read_host(custom, NOW, {"t_17"}, {})
    assert found is not None and found.runs["t_17"].tool == "simctl"


def test_nothing_when_ps_or_lsof_fails(host: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(processes, "_run", lambda *a, **k: None)
    assert read_host(config(host), NOW, {"t_17"}, {}) is None


def test_workspace_patterns_need_task_once() -> None:
    with pytest.raises(ValueError, match="must name"):
        ToolProcesses(workspace_patterns=(".worktrees/",))
    with pytest.raises(ValueError, match="must name"):
        ToolProcesses(workspace_patterns=("{task}/{task}",))
    t = ToolProcesses(workspace_patterns=PATTERNS)
    assert t.task_of("/r/.worktrees/t_1/sub", {"t_1"}, {}) == "t_1"
    # A second match of the same pattern: repositories under ~/workspaces.
    assert t.task_of("/u/workspaces/proj/workspaces/t_1", {"t_1"}, {}) == "t_1"
    # The longest of Hermes' paths that holds it.
    assert t.task_of("/ws/a/b", set(), {"/ws": "A", "/ws/a": "B"}) == "B"
    assert ToolProcesses().task_of("/r/.worktrees/t_1", {"t_1"}, {}) is None  # no patterns
    assert t.task_of("/r/x.worktrees/t_1", {"t_1"}, {}) is None  # a whole component
    assert t.task_of("/r/workspaces/t_1", {"t_1"}, {}) == "t_1"
    assert t.task_of("/ws/t_2x", {"t_2"}, {"/ws/t_2": "t_2"}) is None  # not inside it


def test_elapsed_and_ancestry() -> None:
    assert processes._elapsed("05") is None
    assert processes._elapsed("01:02") == timedelta(minutes=1, seconds=2)
    assert processes._elapsed("3-04:05:06") == timedelta(days=3, hours=4, minutes=5, seconds=6)
    procs = {
        5: processes.Process(5, 6, NOW, ("a",)),
        6: processes.Process(6, 5, NOW, ("b",)),  # a cycle
    }
    assert processes._ancestry(5, procs) == [5, 6]


def test_run_survives_a_missing_command() -> None:
    assert processes._run(["kelpie-no-such-command-xyz"], processes._Budget(1.0)) is None


def test_run_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    def done(code: int, out: str = "", err: str = "") -> object:
        return lambda *a, **k: subprocess.CompletedProcess(a[0], code, out, err)

    budget = processes._Budget(1.0)
    monkeypatch.setattr(subprocess, "run", done(1, "", ""))
    assert processes._run(["lsof"], budget, ok=(0, 1)) == ""  # nothing open: fine
    monkeypatch.setattr(subprocess, "run", done(1, "", "lsof: WARNING: can't stat() /proc"))
    assert processes._run(["lsof"], budget, ok=(0, 1)) is None  # an error, no output
    monkeypatch.setattr(subprocess, "run", done(2, "p1\n"))
    assert processes._run(["lsof"], budget, ok=(0, 1)) is None
    monkeypatch.setattr(subprocess, "run", done(0, "x" * (processes.MAX_OUTPUT + 1)))
    assert processes._run(["ps"], budget) is None  # truncated output is no read
    spent = processes._Budget(1.0)
    spent.deadline = 0.0
    assert processes._run(["ps"], spent) is None  # the snapshot's budget is spent


def test_an_interpreter_script_after_options(monkeypatch: pytest.MonkeyPatch) -> None:
    out = "  10     1    01:00 /bin/bash -x ./ios_sim.sh --device A\n  11  1  01:00 zsh -l\n"
    monkeypatch.setattr(processes, "_run", lambda *a, **k: out)
    procs = processes.read_processes(NOW, 1.0)
    assert procs is not None
    assert procs[10].names == ("bash", "ios_sim.sh") and procs[11].names == ("zsh",)


def test_a_pool_size_counts_slots_whose_files_lockf_removed(host: Path) -> None:
    sized = SlotPool("sim", (str(host / "sim-*.lock"),), size=6)
    found = read_host(ToolProcesses(slots=(sized,), **FARM), NOW, {"t_99"}, {})
    assert found is not None and found.pools["sim"] == processes.PoolUse(6, 0, 2)


def test_a_shared_path_stops_a_shorter_workspace_claiming_it() -> None:
    t = ToolProcesses()
    assert t.task_of("/w/a/sub/x", set(), {"/w/a": "A", "/w/a/sub": None}) is None
    assert t.task_of("/w/a/other", set(), {"/w/a": "A", "/w/a/sub": None}) == "A"
    # A shared path still lets a workspace pattern name the task.
    p = ToolProcesses(workspace_patterns=PATTERNS)
    assert p.task_of("/repo/workspaces/t_1", {"t_1"}, {"/repo": None}) == "t_1"
    with pytest.raises(ValueError, match="not a ~ path"):
        ToolProcesses(workspace_patterns=("~/workspaces/{task}",))


def test_only_an_interpreter_runs_a_script(monkeypatch: pytest.MonkeyPatch) -> None:
    out = (
        "  10  1  01:00 vim ios_sim.sh\n"
        "  11  1  01:00 /usr/bin/env -S bash ios_sim.sh\n"
        "  12  1  01:00 grep -rn xcodebuild .\n"
        "  13  1  01:00 env FOO=1 bash x.sh\n"
        "  14  1  01:00 /opt/py/bin/python3.12 -u ios_sim.py\n"
    )
    monkeypatch.setattr(processes, "_run", lambda *a, **k: out)
    procs = processes.read_processes(NOW, 1.0)
    assert procs is not None
    assert procs[10].names == ("vim",) and procs[12].names == ("grep",)
    assert procs[11].names == ("env", "bash")  # env's script is the shell
    assert procs[13].names == ("env", "bash") and procs[14].names == ("python3.12", "ios_sim.py")


def test_a_slot_file_gone_before_lsof_is_no_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    gone = "lsof: status error on /slots/sim-3.lock: No such file or directory\n"
    monkeypatch.setattr(
        subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a[0], 1, "", gone)
    )
    assert processes._run(["lsof"], processes._Budget(1.0), ok=(0, 1)) == ""
