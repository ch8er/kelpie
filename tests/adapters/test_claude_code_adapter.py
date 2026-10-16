"""Claude Code adapter, read side: task lists and the hooks spool against synthetic files."""

from __future__ import annotations

import io
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.claude_code import ClaudeCodeAdapter, hook
from kelpie.adapters.claude_code.adapter import ClaudeCodeError, list_dir_name
from kelpie.core.actions import ActionKind
from kelpie.core.config import EngineConfig, Mode
from kelpie.core.engine import Engine
from kelpie.core.ledger import Outcome
from kelpie.core.model import Dependency, SpanKind, Status
from kelpie.core.ports import SnapshotSource, SupportsStop, UnsupportedAction
from kelpie.rules.exit_contract import ExitContract
from kelpie.traces.hooks import merge_hook_spans
from kelpie.traces.semconv import OUTPUT_CHARS_ATTRIBUTE
from tests.fakes import FakeAlerter

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
S1 = "0b5c6f7e-0000-4000-8000-000000000001"
S2 = "0b5c6f7e-0000-4000-8000-000000000002"


def ago(**kw: float) -> float:
    return (NOW - timedelta(**kw)).timestamp()


class Farm:
    """A synthetic ``~/.claude``: task lists and a spool."""

    def __init__(self, root: Path) -> None:
        self.tasks_dir = root / "tasks"
        self.spool = root / "kelpie" / "spool.jsonl"
        self.spool.parent.mkdir(parents=True)

    def task(
        self,
        task_list: str,
        task_id: str,
        status: str,
        *,
        mtime: float | None = None,
        **fields: Any,
    ) -> Path:
        folder = self.tasks_dir / list_dir_name(task_list)
        folder.mkdir(parents=True, exist_ok=True)
        (folder / ".lock").touch()
        path = folder / f"{task_id}.json"
        data = {
            "id": task_id,
            "subject": f"Task {task_id}",
            "description": "PRIVATE description",
            "status": status,
            "blocks": [],
            "blockedBy": [],
            **fields,
        }
        path.write_text(json.dumps(data))
        at = ago(hours=1) if mtime is None else mtime
        os.utime(path, (at, at))
        return path

    def event(self, type_: str, at: float, session: str = S1, **fields: Any) -> None:
        rec = {"type": type_, "at": at, "session_id": session, **fields}
        with self.spool.open("a") as f:
            f.write(json.dumps(rec) + "\n")

    def update(self, at: float, task: str, status: str | None = None, **fields: Any) -> None:
        self.event(
            "tool",
            at,
            tool="TaskUpdate",
            tool_call_id=f"toolu_{at}",
            args_hash="0" * 16,
            outcome="ok",
            task=task,
            task_status=status,
            **fields,
        )

    def adapter(self, *lists: str, **kw: Any) -> ClaudeCodeAdapter:
        kw.setdefault("spool", self.spool)
        return ClaudeCodeAdapter(tasks_dir=self.tasks_dir, task_lists=lists, **kw)


@pytest.fixture
def farm(tmp_path: Path) -> Farm:
    return Farm(tmp_path)


def test_capabilities(farm: Farm) -> None:
    adapter = farm.adapter()
    assert isinstance(adapter, SnapshotSource)
    assert not isinstance(adapter, SupportsStop)  # a later step
    assert adapter.name == "claude-code"


# --- tasks --------------------------------------------------------------------------------


def test_status_mapping(farm: Farm) -> None:
    farm.task("farm", "1", "completed")
    farm.task("farm", "2", "in_progress", owner="builder-1")
    farm.task("farm", "3", "pending", blockedBy=["1"])  # blocker done: ready
    farm.task("farm", "4", "pending", blockedBy=["2"])  # blocker open: backlog
    farm.task("farm", "5", "pending", blockedBy=["99"])  # blocker gone: ready
    farm.task("farm", "6", "exploded")  # unknown: skipped
    snap = farm.adapter("farm").snapshot(NOW)
    assert {t.id: t.status for t in snap.tasks} == {
        "farm/1": Status.DONE,
        "farm/2": Status.IN_PROGRESS,
        "farm/3": Status.READY,
        "farm/4": Status.BACKLOG,
        "farm/5": Status.READY,
    }
    t2 = snap.task("farm/2")
    assert t2 is not None
    assert (t2.title, t2.owner_id, t2.role, t2.source) == (
        "Task 2",
        "builder-1",
        "builder-1",
        "claude-code",
    )
    assert t2.block is None and t2.status_since == datetime.fromtimestamp(ago(hours=1), UTC)
    assert snap.dependencies == (
        Dependency("farm/3", "farm/1"),
        Dependency("farm/4", "farm/2"),
    )
    assert [o.id for o in snap.owners] == ["builder-1"]


def test_only_named_lists_are_read(farm: Farm) -> None:
    farm.task("farm", "1", "pending")
    farm.task(S1, "1", "pending")  # a session's own default list
    assert [t.id for t in farm.adapter("farm").snapshot(NOW).tasks] == ["farm/1"]
    assert farm.adapter().snapshot(NOW).tasks == ()


def test_list_names_are_sanitized_like_claude_code(farm: Farm) -> None:
    farm.task("team alpha/1", "1", "pending")
    assert (farm.tasks_dir / "team-alpha-1" / "1.json").exists()
    assert [t.id for t in farm.adapter("team alpha/1").snapshot(NOW).tasks] == ["team alpha/1/1"]


def test_old_completed_tasks_are_left_out(farm: Farm) -> None:
    farm.task("farm", "1", "completed", mtime=ago(hours=30))
    farm.task("farm", "2", "in_progress", mtime=ago(hours=30))
    assert [t.id for t in farm.adapter("farm").snapshot(NOW).tasks] == ["farm/2"]


def test_bad_task_files_are_skipped(farm: Farm) -> None:
    folder = farm.tasks_dir / "farm"
    farm.task("farm", "1", "pending")
    (folder / "2.json").write_text("{not json")
    (folder / "3.json").write_text(json.dumps({"id": "4", "status": "pending"}))  # id mismatch
    (folder / "5.json").write_text(json.dumps(["list"]))
    (folder / "6.json").write_text(json.dumps({"id": "6"}))  # no status
    (folder / "7.json").mkdir()
    (folder / "notes.txt").write_text("x")
    (folder / "..json").write_text("{}")
    assert [t.id for t in farm.adapter("farm").snapshot(NOW).tasks] == ["farm/1"]


def test_large_task_files_and_task_cap(farm: Farm, monkeypatch: pytest.MonkeyPatch) -> None:
    farm.task("farm", "1", "pending", subject="x" * 2000)
    farm.task("farm", "2", "pending")
    farm.task("farm", "3", "pending")
    monkeypatch.setattr("kelpie.adapters.claude_code.adapter.MAX_TASKS_PER_LIST", 2)
    tasks = farm.adapter("farm").snapshot(NOW).tasks
    assert [t.id for t in tasks] == ["farm/1", "farm/2"]
    assert len(tasks[0].title) == 200
    monkeypatch.setattr("kelpie.adapters.claude_code.adapter.MAX_TASK_FILE", 10)
    assert farm.adapter("farm").snapshot(NOW).tasks == ()


def test_missing_list_is_not_fatal(farm: Farm, caplog: pytest.LogCaptureFixture) -> None:
    adapter = farm.adapter("nowhere")
    assert adapter.snapshot(NOW).tasks == ()
    adapter.snapshot(NOW)
    assert caplog.text.count("not readable") == 1  # warned once
    assert adapter.check() is not None and "not found" in adapter.check()  # type: ignore[operator]
    with pytest.raises(ClaudeCodeError):
        adapter.list_counts()


def test_status_since_from_the_spool(farm: Farm) -> None:
    farm.task("farm", "1", "in_progress", mtime=ago(minutes=1))  # an edit moved the mtime
    farm.update(ago(minutes=40), "1", "in_progress", task_list="farm")
    farm.update(ago(minutes=5), "1", None, task_list="farm", task_owner="b")  # no status
    task = farm.adapter("farm").snapshot(NOW).task("farm/1")
    assert task is not None and task.status_since == datetime.fromtimestamp(ago(minutes=40), UTC)


# --- sessions -----------------------------------------------------------------------------


def test_session_lifecycle(farm: Farm) -> None:
    farm.event("session_start", ago(minutes=50), source="startup", agent_type=None)
    farm.event("prompt", ago(minutes=49))
    farm.event("tool", ago(minutes=30), tool="Bash", tool_call_id="toolu_1", outcome="ok")
    farm.event("turn_end", ago(minutes=29))
    farm.event("session_end", ago(minutes=28), reason="prompt_input_exit")
    farm.event("prompt", ago(minutes=10), session=S2)
    (s1, s2) = sorted(farm.adapter().snapshot(NOW).sessions, key=lambda s: s.id)
    assert s1.started_at == datetime.fromtimestamp(ago(minutes=50), UTC)
    assert s1.ended_at == s1.last_heartbeat == datetime.fromtimestamp(ago(minutes=28), UTC)
    assert (s1.task_id, s1.owner_id, s1.final_status) == (f"session:{S1}", "claude-code", None)
    assert s1.exit_reason == "prompt_input_exit"
    assert s2.ended_at is None and s2.last_heartbeat == datetime.fromtimestamp(ago(minutes=10), UTC)


def test_resumed_session_is_not_ended(farm: Farm) -> None:
    farm.event("session_end", ago(minutes=20), reason="other")
    farm.event("session_start", ago(minutes=10), source="resume")
    (session,) = farm.adapter().snapshot(NOW).sessions
    assert session.ended_at is None and session.exit_reason is None


def test_failed_turn_is_the_exit_reason(farm: Farm) -> None:
    farm.event("turn_end", ago(minutes=20), error="rate_limit")
    farm.event("session_end", ago(minutes=19), reason="other")
    (session,) = farm.adapter().snapshot(NOW).sessions
    assert session.exit_reason == "other rate_limit"


def test_claimed_task_and_declared_status(farm: Farm) -> None:
    farm.task("farm", "1", "completed")
    farm.update(ago(minutes=50), "1", "in_progress", task_list="farm", task_owner="builder-1")
    farm.update(ago(minutes=20), "1", "completed", task_list="farm")
    farm.event("session_end", ago(minutes=19), reason="other")
    (session,) = farm.adapter("farm").snapshot(NOW).sessions
    assert (session.task_id, session.owner_id) == ("farm/1", "builder-1")
    assert session.final_status is Status.DONE


def test_completion_counts_only_after_the_end(farm: Farm) -> None:
    farm.update(ago(minutes=50), "1", "in_progress", task_list="farm")
    farm.update(ago(minutes=20), "1", "completed", task_list="farm")
    (session,) = farm.adapter("farm").snapshot(NOW).sessions
    assert session.final_status is None  # still running: nothing declared yet


def test_a_later_claim_resets_the_declaration(farm: Farm) -> None:
    farm.update(ago(minutes=50), "1", "in_progress", task_list="farm")
    farm.update(ago(minutes=40), "1", "completed", task_list="farm")
    farm.update(ago(minutes=30), "2", "in_progress", task_list="farm")
    farm.update(ago(minutes=25), "1", "pending", task_list="farm")  # another task: ignored
    farm.event("session_end", ago(minutes=20), reason="other")
    (session,) = farm.adapter("farm").snapshot(NOW).sessions
    assert (session.task_id, session.final_status) == ("farm/2", None)


def test_private_task_updates_never_touch_farm_tasks(farm: Farm) -> None:
    """A session outside the farm list (its updates name its own list, the session id) must
    not pass for the worker that claimed farm/1 and crashed."""
    farm.task("farm", "1", "in_progress", owner="builder-1", mtime=ago(hours=2))
    farm.update(ago(hours=2), "1", "in_progress", task_list="farm", session=S1)
    farm.update(ago(minutes=30), "1", "in_progress", task_list=S2, session=S2)
    farm.update(ago(minutes=20), "1", "completed", task_list=S2, session=S2)
    farm.event("session_end", ago(minutes=19), session=S2, reason="other")
    snapshot = farm.adapter("farm").snapshot(NOW)
    sessions = {s.id: s for s in snapshot.sessions}
    assert sessions[S2].task_id == f"session:{S2}" and sessions[S2].final_status is None
    assert sessions[S1].task_id == "farm/1"
    task = snapshot.task("farm/1")
    assert task is not None and task.status_since == datetime.fromtimestamp(ago(hours=2), UTC)
    actions = ExitContract().evaluate(snapshot)
    assert [(a.kind, a.task_id) for a in actions] == [(ActionKind.REQUEUE, "farm/1")]


def test_updates_without_a_list_are_not_linked(farm: Farm) -> None:
    farm.update(ago(minutes=50), "4", "in_progress")
    (session,) = farm.adapter("farm").snapshot(NOW).sessions
    assert session.task_id == f"session:{S1}"


def test_a_lead_assigning_work_keeps_its_own_name(farm: Farm) -> None:
    for task, owner in (("2", "researcher"), ("3", "tester")):
        farm.update(ago(minutes=50), task, None, task_list="farm", task_owner=owner)
    (session,) = farm.adapter("farm").snapshot(NOW).sessions
    assert session.owner_id == "claude-code"
    farm.update(ago(minutes=40), "1", "in_progress", task_list="farm", task_owner="lead")
    farm.update(ago(minutes=30), "4", None, task_list="farm", task_owner="tester")
    (session,) = farm.adapter("farm").snapshot(NOW).sessions
    assert (session.task_id, session.owner_id) == ("farm/1", "lead")


def test_owner_set_after_the_claim(farm: Farm) -> None:
    farm.update(ago(minutes=40), "1", "in_progress", task_list="farm")
    farm.update(ago(minutes=39), "1", None, task_list="farm", task_owner="builder-2")
    (session,) = farm.adapter("farm").snapshot(NOW).sessions
    assert session.owner_id == "builder-2"


def test_future_spool_lines_are_dropped(farm: Farm) -> None:
    farm.event("prompt", ago(minutes=10))
    farm.event("prompt", (NOW + timedelta(days=30)).timestamp())
    (session,) = farm.adapter().snapshot(NOW).sessions
    assert session.last_heartbeat == datetime.fromtimestamp(ago(minutes=10), UTC)


def test_task_files_are_read_safely(farm: Farm, tmp_path: Path) -> None:
    """Agents write the list directory: symlinks are not followed, a FIFO does not block,
    fields are capped."""
    farm.task("farm", "1", "pending", owner="o" * 500, blockedBy=[str(i) for i in range(500)])
    folder = farm.tasks_dir / "farm"
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"id": "2", "status": "pending", "subject": "LEAK"}))
    (folder / "2.json").symlink_to(outside)
    os.mkfifo(folder / "3.json")
    tasks = farm.adapter("farm").snapshot(NOW).tasks
    assert [t.id for t in tasks] == ["farm/1"]
    assert tasks[0].owner_id == "o" * 128


def test_launcher_task_and_agent_win(farm: Farm) -> None:
    farm.event(
        "session_start", ago(minutes=50), kelpie_task="T-42", agent="night-1", agent_type="x"
    )
    farm.update(ago(minutes=40), "1", "in_progress", task_list="farm", task_owner="builder-1")
    farm.update(ago(minutes=30), "1", "completed", task_list="farm")
    farm.event("session_end", ago(minutes=20), reason="other")
    (session,) = farm.adapter("farm").snapshot(NOW).sessions
    assert (session.task_id, session.owner_id) == ("T-42", "night-1")
    assert session.final_status is None  # its task lives elsewhere: the list says nothing


def test_agent_type_of_the_main_thread_names_the_owner(farm: Farm) -> None:
    farm.event("session_start", ago(minutes=50), agent_type="reviewer")
    farm.event("tool", ago(minutes=40), agent_id="sub-1", agent_type="explorer", tool="Read")
    (session,) = farm.adapter().snapshot(NOW).sessions
    assert session.owner_id == "reviewer"


def test_lookback_and_rotated_spool(farm: Farm) -> None:
    farm.event("prompt", ago(hours=30), session=S2)
    rotated = farm.spool.with_name("spool.jsonl.1")
    farm.spool.rename(rotated)
    farm.event("prompt", ago(hours=2))
    with rotated.open("a") as f:
        f.write(json.dumps({"type": "prompt", "at": ago(hours=3), "session_id": S2}) + "\n")
    sessions = farm.adapter().snapshot(NOW).sessions
    assert {s.id: s.started_at for s in sessions} == {
        S2: datetime.fromtimestamp(ago(hours=3), UTC),
        S1: datetime.fromtimestamp(ago(hours=2), UTC),
    }
    os.utime(rotated, (ago(hours=30), ago(hours=30)))  # nothing in it can be recent
    assert [s.id for s in farm.adapter().snapshot(NOW).sessions] == [S1]


def test_spool_is_read_safely(farm: Farm, tmp_path: Path) -> None:
    """Agents can write next to the spool: a FIFO must not hang the control loop, a symlink
    is not followed, overlong lines are skipped."""
    farm.event("prompt", ago(minutes=5))
    with farm.spool.open("a") as f:
        f.write("x" * 100_000 + "\n")
    farm.event("prompt", ago(minutes=4), session=S2)
    os.mkfifo(farm.spool.with_name("spool.jsonl.1"))
    adapter = farm.adapter()
    assert sorted(s.id for s in adapter.snapshot(NOW).sessions) == [S1, S2]
    target = tmp_path / "elsewhere.jsonl"
    target.write_text(json.dumps({"type": "prompt", "at": ago(minutes=1), "session_id": "x"}))
    farm.spool.unlink()
    farm.spool.symlink_to(target)
    assert adapter.snapshot(NOW).sessions == ()
    assert adapter.last_spool_at() is None
    farm.spool.unlink()
    os.mkfifo(farm.spool)
    assert adapter.last_spool_at() is None and adapter.snapshot(NOW).sessions == ()


def test_spool_read_is_bounded(farm: Farm, monkeypatch: pytest.MonkeyPatch) -> None:
    for i in range(10):
        farm.event("prompt", ago(minutes=10 - i), session=f"s-{i}")
    monkeypatch.setattr("kelpie.adapters.claude_code.adapter.MAX_SPOOL_READ", 300)
    assert 0 < len(farm.adapter().snapshot(NOW).sessions) < 10


@pytest.mark.parametrize(
    "line",
    [
        "garbage",
        "[]",
        json.dumps({"type": "other", "at": 1, "session_id": S1}),
        json.dumps({"type": "prompt", "at": "yesterday", "session_id": S1}),
        json.dumps({"type": "prompt", "at": True, "session_id": S1}),
        json.dumps({"type": "prompt", "at": 1e300, "session_id": S1}),
        json.dumps({"type": "prompt", "at": 1772452800}),
        json.dumps({"type": "prompt", "at": 1772452800, "session_id": ["x"]}),
    ],
)
def test_bad_spool_lines_are_skipped(farm: Farm, line: str) -> None:
    farm.spool.write_text(line + "\n")
    assert farm.adapter().snapshot(NOW).sessions == ()


def test_no_spool(farm: Farm) -> None:
    adapter = farm.adapter(spool=None)
    farm.event("prompt", ago(minutes=1))
    assert adapter.snapshot(NOW).sessions == ()
    assert adapter.status_detail(NOW)[-1] == "spool: disabled"


def test_naive_now_is_refused(farm: Farm) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        farm.adapter().snapshot(NOW.replace(tzinfo=None))


# --- spans --------------------------------------------------------------------------------


def test_tool_spans(farm: Farm) -> None:
    farm.update(ago(minutes=50), "1", "in_progress", task_list="farm")
    farm.event(
        "tool",
        ago(minutes=30),
        tool="Bash",
        tool_call_id="toolu_9",
        args_hash="ab" * 8,
        command="pytest -q",
        outcome="error",
        duration_ms=1500,
        output_chars=1200,
    )
    farm.event("session_end", ago(minutes=29), reason="other")
    spans = farm.adapter("farm").snapshot(NOW).spans
    tool = next(s for s in spans if s.tool == "Bash")
    assert (tool.session_id, tool.task_id, tool.outcome, tool.args_hash) == (
        S1,
        "farm/1",
        "error",
        "ab" * 8,
    )
    assert tool.duration == timedelta(milliseconds=1500)
    assert tool.attributes["command"] == "pytest -q"
    assert tool.attributes[OUTPUT_CHARS_ATTRIBUTE] == 1200
    session = next(s for s in spans if s.kind is SpanKind.SESSION)
    assert session.end == datetime.fromtimestamp(ago(minutes=29), UTC)


def test_unlinked_session_spans_have_no_task(farm: Farm) -> None:
    farm.event("tool", ago(minutes=30), tool="Read", tool_call_id="toolu_1")
    spans = farm.adapter().snapshot(NOW).spans
    assert all(s.task_id is None for s in spans)
    assert not any(s.kind is SpanKind.TASK for s in spans)


def test_hook_and_otlp_calls_are_kept_once(farm: Farm) -> None:
    """Claude Code's OTLP ``claude_code.tool`` span names the same ``tool_use_id``."""
    from kelpie.core.model import Span

    farm.event("tool", ago(minutes=30), tool="Bash", tool_call_id="toolu_9", args_hash="cd" * 8)
    hooks = farm.adapter().snapshot(NOW).spans
    otlp = Span(
        trace_id="t",
        span_id="s",
        name="claude_code.tool",
        kind=SpanKind.TOOL,
        start=datetime.fromtimestamp(ago(minutes=31), UTC),
        end=datetime.fromtimestamp(ago(minutes=30), UTC),
        session_id=S1,
        tool="Bash",
        attributes={"gen_ai.tool.call.id": "toolu_9"},
    )
    merged = merge_hook_spans((otlp, *hooks))
    tools = [s for s in merged.spans if s.kind is SpanKind.TOOL]
    assert merged.merged == 1 and len(tools) == 1 and tools[0].args_hash == "cd" * 8


# --- status and actions -------------------------------------------------------------------


def test_status_detail_and_counts(farm: Farm) -> None:
    farm.task("farm", "1", "in_progress")
    farm.task("farm", "2", "pending")
    adapter = farm.adapter("farm")
    assert adapter.check() is None
    assert adapter.list_counts() == {"farm": (2, 1)}
    assert adapter.status_detail(NOW)[1] == f"spool: missing ({farm.spool})"
    farm.spool.write_text("junk\n")
    assert adapter.status_detail(NOW)[1] == f"spool: no readable lines ({farm.spool})"
    farm.event("prompt", ago(minutes=5))
    assert adapter.status_detail(NOW) == [
        f"task lists: farm ({farm.tasks_dir})",
        f"spool: last line 5m ago ({farm.spool})",
    ]
    assert adapter.last_spool_at() == datetime.fromtimestamp(ago(minutes=5), UTC)


@pytest.mark.parametrize("call", ["requeue", "unblock", "comment", "wip_commit"])
def test_unsupported_actions(farm: Farm, call: str) -> None:
    with pytest.raises(UnsupportedAction, match="alerts a human"):
        getattr(farm.adapter(), call)("farm/1", "why")


def test_end_to_end_exit_contract_alerts_instead_of_requeue(
    farm: Farm, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The real hook writes the spool; a session that claimed a task and exited without
    finishing it trips rule 1, whose requeue Claude Code cannot do: a human is alerted."""
    farm.task("farm", "1", "in_progress", owner="builder-1", mtime=ago(minutes=50))
    monkeypatch.setenv(hook.SPOOL_ENV, str(farm.spool))
    monkeypatch.setenv("CLAUDE_CODE_TASK_LIST_ID", "farm")
    for var in ("KELPIE_TASK_ID", "KELPIE_AGENT"):
        monkeypatch.delenv(var, raising=False)
    inputs = [
        (ago(minutes=50), {"hook_event_name": "SessionStart", "source": "startup"}),
        (
            ago(minutes=49),
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "TaskUpdate",
                "tool_input": {"taskId": "1", "status": "in_progress", "owner": "builder-1"},
                "tool_response": {"success": True},
                "tool_use_id": "toolu_1",
            },
        ),
        (ago(minutes=40), {"hook_event_name": "SessionEnd", "reason": "other"}),
    ]
    for at, data in inputs:
        monkeypatch.setattr(hook.time, "time", lambda at=at: at)
        hook.main(
            io.StringIO(json.dumps({"session_id": S1, "transcript_path": "x", "cwd": "/", **data}))
        )
    snapshot = farm.adapter("farm").snapshot(NOW)
    (session,) = snapshot.sessions
    assert (session.task_id, session.owner_id, session.final_status) == (
        "farm/1",
        "builder-1",
        None,
    )
    alerter = FakeAlerter()
    engine = Engine(
        [ExitContract()],
        adapters=[farm.adapter("farm")],
        alerter=alerter,
        config=EngineConfig(dry_run=False, default_mode=Mode.ACT),
    )
    results = engine.execute(engine.evaluate(snapshot), snapshot)
    assert [(r.action.kind, r.outcome) for r in results] == [
        (ActionKind.REQUEUE, Outcome.NOT_EXECUTED)
    ]
    assert alerter.sent[0][0] == "[exit_contract] requeue farm/1 (not executed, needs a human)"
