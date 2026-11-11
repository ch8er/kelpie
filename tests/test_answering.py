"""Carrying out an answer from the chat (#300): the ledger's audit line around the write,
the re-checks, what gives a question back, the opt-ins in config, ``kelpie status`` and
``kelpie doctor``, and the act lock."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.alerts.answers import AnswerPress
from kelpie.alerts.buttons import ButtonStore
from kelpie.answering import AnswerBoards, AnswerRunner
from kelpie.app import (
    ConfigError,
    ControlLoop,
    answer_boards,
    build_app,
    chat_answers_on,
    load_rules,
    load_settings,
    render_status,
)
from kelpie.core.actions import Action, ActionKind
from kelpie.core.engine import Engine
from kelpie.core.executors import PlatformExecutor
from kelpie.core.ledger import Outcome
from kelpie.core.model import Block, Comment, Snapshot, Status, Task
from kelpie.core.offers import Expect, Offer, answers_from_ledger, parse_offer, question_key
from kelpie.core.ports import StaleAnswer
from kelpie.core.storage import SqliteLedgerStore
from kelpie.doctor import check_chat_answers
from kelpie.metrics import Metrics
from tests.fakes import FakeAdapter

NOW = datetime(2026, 11, 2, 12, tzinfo=UTC)
SINCE = NOW - timedelta(hours=1)
QUESTION = "Which schema?\nOptions:\nA) target schema v3\nB) keep v2 and add a shim"


@dataclass
class CheckingAdapter(FakeAdapter):
    """A board that re-checks an answer: ``stale`` says why it changed (None: it holds)."""

    name: str = "hermes"
    stale: str | None = None
    unblock_fails: bool = False
    bodies: list[str] = field(default_factory=list)
    checked: list[Expect] = field(default_factory=list)

    def check_answer(self, task_id: str, expect: Expect) -> None:
        self.checked.append(expect)
        if self.stale is not None:
            raise StaleAnswer(self.stale)

    def comment(self, task_id: str, body: str) -> None:
        super().comment(task_id, body)
        self.bodies.append(body)

    def unblock(self, task_id: str, reason: str) -> None:
        if self.unblock_fails:
            raise RuntimeError("hermes kanban unblock failed")
        super().unblock(task_id, reason)


@dataclass
class Desk:
    """The chat side, faked: what it was told."""

    told: list[tuple[str, bool, str]] = field(default_factory=list)

    @property
    def rendering(self) -> Any:
        from kelpie.alerts.render import DEFAULT_RENDERING

        return DEFAULT_RENDERING

    def finished(self, press: AnswerPress, written: bool, text: str) -> None:
        self.told.append((press.task_id, written, text))


def blocked(text: str = QUESTION, **kw: Any) -> Task:
    fields: dict[str, Any] = {
        "assignee": "dev-1",
        "block": Block("waiting_human: needs_input: which schema?", SINCE),
        "comments": (Comment("dev-1", text, SINCE),),
        "source": "hermes",
    }
    fields.update(kw)
    return Task("T-1", "Migrate", Status.BLOCKED, SINCE, **fields)


def press(offer: Offer | None = None, index: int = 1) -> AnswerPress:
    offer = offer or parse_offer(QUESTION)  # type: ignore[assignment]
    assert isinstance(offer, Offer)
    return AnswerPress(
        channel="telegram", chat_id="-100", task_id="T-1", board=None,
        question=question_key("T-1", "dev-1", SINCE, offer.digest), offer=offer, index=index,
        expect=Expect("dev-1", SINCE, offer.digest), user_id=7, who="Telegram user 7",
        name="Alice", ref_id=1, message_id=11, digest_message_id=10, item=1,
    )  # fmt: skip


@dataclass
class World:
    runner: AnswerRunner
    adapter: CheckingAdapter
    store: SqliteLedgerStore
    buttons: ButtonStore
    desk: Desk

    def answer(self, snapshot: Snapshot, p: AnswerPress | None = None) -> AnswerPress:
        p = p or press()
        assert self.buttons.claim_question(p.question, p.label, p.who, 7, p.ref_id, NOW) is None
        self.runner.submit(p, NOW)
        self.runner.drain(snapshot)
        return p

    def entry(self) -> Any:
        (found,) = [
            e for e in self.store.since(NOW - timedelta(days=1)) if e.kind is ActionKind.ANSWER
        ]
        return found


@pytest.fixture
def world(tmp_path: Path) -> World:
    adapter = CheckingAdapter()
    store = SqliteLedgerStore(tmp_path / "kelpie.db")
    buttons = ButtonStore(tmp_path / "kelpie.buttons.db")
    engine = Engine(rules=[], adapters=[adapter])
    runner = AnswerRunner(
        store, engine, AnswerBoards(frozenset({"hermes"})), False, lambda: NOW, buttons
    )
    desk = Desk()
    runner.desks["telegram"] = desk  # type: ignore[assignment]
    return World(runner, adapter, store, buttons, desk)


def snap(*tasks: Task) -> Snapshot:
    return Snapshot(NOW, tasks=tasks)


def test_an_answer_writes_the_comment_then_unblocks_and_records_both(world: World) -> None:
    woken: list[bool] = []
    world.runner.wake = lambda: woken.append(True)
    p = world.answer(snap(blocked()))
    assert woken == [True]
    assert world.adapter.calls == [("comment", "T-1"), ("unblock", "T-1")]
    (body,) = world.adapter.bodies
    assert "option B, of the options you offered" in body and "Telegram user 7" in body
    assert "    B) keep v2 and add a shim" in body and "Alice" not in body
    assert world.adapter.checked == [p.expect, p.expect]  # re-checked before each write
    entry = world.entry()
    assert entry.outcome is Outcome.EXECUTED and entry.rule == "chat"
    assert entry.params["comment"] == "executed" and entry.params["unblock"] == "executed"
    assert entry.params["label"] == "B" and entry.params["user_id"] == 7
    assert entry.params["name"] == "Alice" and entry.subject == "chat:telegram"
    assert world.desk.told == [("T-1", True, "Option B sent to T-1, and its block cleared.")]
    (answer,) = answers_from_ledger(world.store.answers(NOW - timedelta(days=1)))
    assert (answer.question, answer.label, answer.outcome) == (p.question, "B", "executed")


def test_a_pending_answer_is_in_the_ledger_before_any_write(world: World) -> None:
    world.runner.submit(press(), NOW)
    assert world.entry().outcome is Outcome.PENDING
    assert world.adapter.calls == [] and world.runner.queued == 1
    (answer,) = world.runner.answers(NOW)
    assert answer.outcome == "pending"


def test_a_question_that_changed_writes_nothing_and_is_given_back(world: World) -> None:
    p = world.answer(snap(blocked(QUESTION.replace("shim", "bridge"))))
    assert world.adapter.calls == []
    entry = world.entry()
    assert entry.outcome is Outcome.NOT_EXECUTED and "options changed" in entry.detail
    assert world.buttons.claim(p.question) is None
    assert world.desk.told[0][1] is False


def test_the_adapters_own_re_check_refuses_and_gives_the_question_back(world: World) -> None:
    world.adapter.stale = "task T-1: the task was blocked again; nothing written"
    p = world.answer(snap(blocked()))
    assert world.adapter.calls == [] and world.entry().outcome is Outcome.NOT_EXECUTED
    assert world.buttons.claim(p.question) is None


def test_a_failed_comment_keeps_the_claim_for_a_person(world: World) -> None:
    world.adapter.fail_on = "comment"
    p = world.answer(snap(blocked()))
    entry = world.entry()
    assert entry.outcome is Outcome.FAILED and "comment failed" in entry.detail
    assert world.buttons.claim(p.question) is not None
    ((_, written, text),) = world.desk.told
    assert not written and "comment failed" not in text and "Kelpie log" in text
    (answer,) = world.runner.answers(NOW)  # still claimed: rule 13 offers no buttons
    assert answer.outcome == "failed"


def test_no_snapshot_yet_is_a_changed_question(world: World) -> None:
    from kelpie.core.offers import Expect

    assert world.runner.control().stale("T-1", Expect("dev-1", SINCE, "d" * 32)) is not None
    assert world.runner.control().enabled("T-1") is False


def test_an_executor_for_another_board_gets_nothing(world: World) -> None:
    world.runner.engine = Engine(rules=[], adapters=[world.adapter], fallback=True)
    task = blocked(source="hermes")
    world.runner.boards = AnswerBoards(frozenset({"hermes", "elsewhere"}))
    other = Task("T-1", "x", Status.BLOCKED, SINCE, assignee="dev-1", source="elsewhere",
                 block=task.block, comments=task.comments)  # fmt: skip
    world.answer(snap(other))
    assert world.adapter.calls == [] and world.entry().outcome is Outcome.NOT_EXECUTED


def test_a_failed_unblock_leaves_the_answer_written(world: World) -> None:
    world.adapter.unblock_fails = True
    world.answer(snap(blocked()))
    entry = world.entry()
    assert entry.outcome is Outcome.EXECUTED and entry.params["unblock"] == "failed: RuntimeError"
    assert world.desk.told == [("T-1", True, "Option B sent to T-1.")]


def test_a_board_that_did_not_opt_in_gets_nothing(world: World) -> None:
    world.answer(snap(blocked(source="other")))
    assert world.adapter.calls == [] and world.entry().outcome is Outcome.NOT_EXECUTED


def test_an_adapter_that_cannot_re_check_never_gets_the_answer(tmp_path: Path) -> None:
    adapter = FakeAdapter(name="hermes")
    store = SqliteLedgerStore(tmp_path / "kelpie.db")
    runner = AnswerRunner(
        store, Engine(rules=[], adapters=[adapter]), AnswerBoards(frozenset({"hermes"})),
        False, lambda: NOW,
    )  # fmt: skip
    runner.submit(press(), NOW)
    runner.drain(snap(blocked()))
    assert adapter.calls == []
    (entry,) = store.answers(NOW - timedelta(days=1))
    assert entry.outcome is Outcome.NOT_EXECUTED


def test_dry_run_records_and_writes_nothing(world: World) -> None:
    world.runner.record_dry_run(press(), NOW)
    entry = world.entry()
    assert entry.outcome is Outcome.DRY_RUN and entry.params["dry_run"] is True
    assert answers_from_ledger([entry]) == ()  # the question is not used up


def test_the_queue_is_bounded(world: World) -> None:
    for _ in range(100):
        world.runner.submit(press(), NOW)
    with pytest.raises(RuntimeError):
        world.runner.submit(press(), NOW)


def test_the_answer_outcome_is_set_once(tmp_path: Path) -> None:
    store = SqliteLedgerStore(tmp_path / "kelpie.db")
    with pytest.raises(ValueError):
        store.answer_outcome("x", Outcome.PENDING, None, {})
    assert not store.answer_outcome("missing", Outcome.EXECUTED, None, {})


# --- the executor's re-check ---------------------------------------------------------------


def test_an_action_with_expect_needs_an_adapter_that_re_checks() -> None:
    expect = {"expect": Expect("dev-1", SINCE, "d" * 32).params()}
    plain = FakeAdapter()
    executor = PlatformExecutor(plain)
    comment = Action(ActionKind.COMMENT, "chat", "body", task_id="T-1", params=expect)
    assert executor.execute(comment) is False and plain.calls == []
    checking = CheckingAdapter()
    assert PlatformExecutor(checking).execute(comment) is True
    bad = Action(ActionKind.COMMENT, "chat", "b", task_id="T-1", params={"expect": {"x": 1}})
    assert PlatformExecutor(checking).execute(bad) is False
    requeue = Action(ActionKind.REQUEUE, "chat", "b", task_id="T-1", params=expect)
    assert PlatformExecutor(checking).execute(requeue) is False
    checking.stale = "changed"
    with pytest.raises(StaleAnswer):
        PlatformExecutor(checking).execute(comment)


# --- the control loop ----------------------------------------------------------------------


def test_the_control_loop_carries_answers_out_on_a_wake(tmp_path: Path) -> None:
    seen: list[Snapshot] = []
    engine = Engine(rules=[])
    loop = ControlLoop(engine, lambda: snap(blocked()), Metrics(), answers=seen.append)
    loop.tick_once()
    assert len(seen) == 1  # on the fresh snapshot, before the rules
    loop.wake()

    done = threading.Event()
    loop.answers = lambda s: (seen.append(s), done.set())  # type: ignore[assignment,func-returns-value]
    loop.start()
    try:
        loop.wake()
        assert done.wait(10)
    finally:
        loop.stop()


# --- config, status, doctor, the act lock --------------------------------------------------


def write(
    tmp_path: Path,
    hermes: str = "chat_answers = true",
    channel: str = "",
    extra: str = "",
    db: bool = True,
    dry: bool = False,
) -> Path:
    board = f'db_path = "{tmp_path / "kanban.db"}"' if db else ""
    path = tmp_path / "kelpie.toml"
    path.write_text(f"""
[storage]
path = "{tmp_path / "kelpie.db"}"

[traces]
enabled = false

[engine]
dry_run = {"true" if dry else "false"}

[adapters.hermes]
{board}
spool = false
state_home = false
git_state = false
{hermes}

[[alerts.channels]]
type = "telegram"
token_env = "KELPIE_TEST_TG"
chat_id = "-100777"
api_url = "http://127.0.0.1:9"
answer_buttons = true
allowed_user_ids = [4242]
{channel}
{extra}
""")
    return path


def test_chat_answers_are_opted_in_per_board(tmp_path: Path) -> None:
    on = load_settings(write(tmp_path))
    assert answer_boards(on) == AnswerBoards(frozenset({"hermes"}))
    assert chat_answers_on(on)
    off = load_settings(write(tmp_path, hermes=""))
    assert not answer_boards(off) and not chat_answers_on(off)
    boards = (
        "chat_answers = true\n"
        f'[[adapters.hermes.boards]]\nname = "qa"\ndb_path = "{tmp_path / "a.db"}"\n'
        f'[[adapters.hermes.boards]]\nname = "ops"\ndb_path = "{tmp_path / "b.db"}"\n'
        "chat_answers = false\n"
    )
    several = load_settings(write(tmp_path, hermes=boards, db=False))
    assert answer_boards(several) == AnswerBoards(boards=frozenset({("hermes", "qa")}))
    qa = AnswerBoards(boards=frozenset({("hermes", "qa")}))
    assert qa.on(blocked(board="qa")) and not qa.on(blocked(board="ops"))
    assert not qa.on(blocked()) and not qa.on(None)
    dry = load_settings(write(tmp_path, dry=True))
    assert not chat_answers_on(dry)


def test_the_opt_ins_must_be_booleans(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="chat_answers"):
        load_settings(write(tmp_path, hermes='chat_answers = "yes"'))
    bad = write(tmp_path)
    bad.write_text(bad.read_text().replace("answer_buttons = true", 'answer_buttons = "y"'))
    with pytest.raises(ConfigError, match="answer_buttons"):
        load_settings(bad)


def test_with_a_hub_answers_do_not_start(tmp_path: Path) -> None:
    from dataclasses import replace

    from kelpie.app import _answer_runner

    settings = replace(load_settings(write(tmp_path)), hub=object())  # type: ignore[arg-type]
    assert not chat_answers_on(settings)
    store = SqliteLedgerStore(tmp_path / "kelpie.db")
    with pytest.raises(ConfigError, match="hub"):
        _answer_runner(settings, store, Engine(rules=[]), lambda: NOW)
    assert [c.status for c in check_chat_answers(settings)] == ["FAIL"]


def test_status_and_doctor_say_whether_answers_are_on(tmp_path: Path) -> None:
    on = load_settings(write(tmp_path))
    status = render_status(on, load_rules(on.engine))
    assert "chat answers: on for hermes, from telegram (any allowed user)" in status
    checks = check_chat_answers(on)
    assert [c.status for c in checks] == ["PASS", "WARN"]  # no answer_users: a warning
    owned = load_settings(write(tmp_path, channel='answer_users = {"4242" = "ceo"}'))
    assert "only the person each item is for" in render_status(owned, load_rules(owned.engine))
    assert [c.status for c in check_chat_answers(owned)] == ["PASS"]
    off = load_settings(write(tmp_path, hermes=""))
    assert "chat answers: off (no board has chat_answers)" in render_status(
        off, load_rules(off.engine)
    )
    assert [c.status for c in check_chat_answers(off)] == ["SKIP"]


def test_a_pending_answer_is_reported_by_doctor(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, channel='answer_users = {"4242" = "ceo"}'))
    store = SqliteLedgerStore(tmp_path / "kelpie.db")
    adapter = CheckingAdapter()
    runner = AnswerRunner(
        store, Engine(rules=[], adapters=[adapter]), AnswerBoards(frozenset({"hermes"})),
        False, lambda: NOW,
    )  # fmt: skip
    runner.submit(press(), NOW)
    store.close()
    (_, pending) = check_chat_answers(settings)
    assert pending.status == "WARN" and "T-1" in pending.detail


def test_build_app_wires_the_runner_into_the_loop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("KELPIE_TEST_TG", "987654:ZZtop_SECRET-value-0123456789abcdef")
    app = build_app(load_settings(write(tmp_path)), clock=lambda: NOW)
    try:
        assert app.loop.answers is not None
        assert app.alerts is not None and app.alerts.pollers
        assert app.alerts.pollers[0].desk is not None  # type: ignore[attr-defined]
    finally:
        app.close()
    plain = build_app(load_settings(write(tmp_path, hermes="")), clock=lambda: NOW)
    try:
        assert plain.loop.answers is None
        assert plain.alerts is not None and plain.alerts.pollers[0].desk is None  # type: ignore[attr-defined]
    finally:
        plain.close()
