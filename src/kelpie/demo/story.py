"""The story ``kelpie demo`` tells: one working day on a small Hermes farm.

Four incidents from real farms (``docs/plan.md``), replayed with synthetic names on the
simulated farm while the real Kelpie watches, then the daily report on that day:

1. a block that outlived its cause: a reviewer blocked its own card "waiting for CI/merge"
   and the PR merged (incident 15);
2. a runaway loop: a maintenance agent repeats the same tool call (field evidence, rule 10);
3. a forgotten hold: a merge hold promised for 2.5 hours, forgotten (incident 14);
4. a human task gating agent work: a person's task holds a chain of agent tasks
   (incident 16);
5. the daily "cost of chaos" report.

For each, the narration says what happened on the farm, what Kelpie noticed, and what it
does in each mode. What Kelpie sent, recorded, ran and reported comes from this run (the
alerter's events, the engine's results, the hermes CLI calls, the report); the farm's side
is scripted. The per-mode lines describe how the engine routes each mode on Hermes, as
``tests/demo`` checks against a run in that mode.
"""

from __future__ import annotations

import os
import re
import subprocess
import textwrap
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from kelpie.core.actions import ActionKind
from kelpie.core.alerts import intent, suggested_line
from kelpie.core.ledger import Dispatched, Outcome
from kelpie.demo.farm import REPO, DemoFarm, Message
from kelpie.demo.narrate import WIDTH, Narrator
from kelpie.report.cli import run as run_report
from kelpie.sim.otlp import ModelCall

DAY = datetime(2026, 3, 3, 8, 0, tzinfo=UTC)  # a Tuesday; times are UTC
HOLD = "merge-first"
MODEL = "synthetic-model"
PRICES = {MODEL: {"input": 3.0, "output": 15.0}}  # dollars per million tokens
CONTEXT = 80_000  # input tokens the loop resends on every model call
REPORT_SECTIONS = (
    "## Cost of chaos",
    "## Fixed by Kelpie",
    "## Needs a human",
    "## Top 3 recommendations",
)

RULES = {
    # Hermes lets Kelpie stop a run; the other actions here are suggestions on Hermes.
    "loop_breaker": {"mode": "act"},
    "stale_blocks": {"mode": "suggest"},
    "bottlenecks": {
        "mode": "suggest",
        "min_idle_time": "30m",
        "idle_alert_after": "30m",
        "human_gate_min_tasks": 3,
        "human_gate_after": "1h",
        "holds": [{"label": HOLD, "owner": "dana", "expires_after": "2h30m"}],
    },
    "needs_you": {"mode": "suggest", "human_wait_alert_after": "1h"},
    # Watching in observe mode; Hermes tolerates heartbeats up to an hour old.
    "exit_contract": {"session_stale_after": "60m"},
    "orphan_status": {"session_stale_after": "60m"},
}
BLOCK_CONDITIONS = [
    {"pattern": r"waiting for (?:ci/)?merge of (?P<pr>\S+)", "kind": "pr_merged", "repo": REPO}
]


@dataclass(frozen=True, slots=True)
class Outcomes:
    """What the day produced, for the caller (and the tests)."""

    messages: tuple[Message, ...]
    results: tuple[Dispatched, ...]
    report: str
    report_path: Path | None


class Story:
    def __init__(self, farm: DemoFarm, out: Narrator) -> None:
        self.farm = farm
        self.out = out
        self.board = farm.board
        self.github = farm.github
        self._seen = 0  # messages already narrated

    # --- the day -------------------------------------------------------------------------

    def run(self) -> Outcomes:
        self.intro()
        self.farm.start(self.config())
        self.farm.tick()
        self.stale_block()
        self.runaway_loop()
        self.forgotten_hold()
        self.human_gate()
        report, path = self.daily_report()
        self.outro()
        return Outcomes(tuple(self.farm.inbox.messages), tuple(self.farm.results), report, path)

    def config(self) -> dict[str, object]:
        return self.farm.config(
            RULES,
            hermes={"human_owners": ["dana", "sam"]},
            github={"pulls": {"poll_interval": "1m"}},
            block_conditions=BLOCK_CONDITIONS,
            report={"timezone": "UTC", "hour_usd": 2.5, "dir": str(self.farm.root / "reports")},
            prices=PRICES,
            # the farm pays its synthetic model per token: dollars, not a plan's limit
            spend={"providers": {"synthetic": {"mode": "api", "models": [MODEL]}}},
        )

    def intro(self) -> None:
        o = self.out
        o.title("Kelpie demo: one working day on a simulated agent farm")
        o.line()
        o.paragraph(
            "A Hermes board with three agent profiles (dev, reviewer, ops), two people (dana, "
            f"sam) and a repository ({REPO}). Four incidents from real farms happen on it, "
            "with made-up names; the real Kelpie watches. Nothing leaves this machine: the "
            "board, the hermes CLI and GitHub are simulated, and a day passes in seconds."
        )
        o.paragraph(
            "Kelpie runs every rule in observe mode unless told otherwise. Here loop_breaker "
            "acts (Hermes lets Kelpie stop a run) and the rules of this story suggest. Each "
            "scene ends with what Kelpie does in each mode."
        )
        self._history()

    def _history(self) -> None:
        """The morning so far: the dev profile finished a task."""
        b, f = self.board, self.farm
        b.task("t_login", "todo", f.ago(hours=2), title="Fix the login flake", assignee="dev")
        rid = b.claim("t_login", "dev", f.ago(hours=1), heartbeat=f.ago(minutes=12))
        b.end_run(rid, f.ago(minutes=10), "completed")
        b.update("t_login", status="done", current_run_id=None, completed_at=f.ago(minutes=10))
        b.event("t_login", "completed", f.ago(minutes=10), run_id=rid)

    # --- 1. a block that outlived its cause ----------------------------------------------

    def stale_block(self) -> None:
        o, f, b = self.out, self.farm, self.board
        o.scene(
            1,
            "A block that outlived its cause",
            'On a real farm (incident 15) a reviewer blocked its own card "waiting for '
            'CI/merge" while the PR sat in the merge queue. The PR merged; the card stayed '
            "blocked. It was most of the block alerts of the first live run.",
        )
        self.github.pull(
            REPO,
            57,
            title="Checkout refactor",
            branch="hermes/t_checkout",
            created=f.ago(hours=1),
            updated=f.ago(minutes=30),
        )
        b.task("t_review", "todo", f.ago(minutes=5), title="Review the checkout refactor")
        b.update("t_review", assignee="reviewer")
        f.at(DAY.replace(hour=8, minute=20))
        self._claim("t_review", "reviewer")
        self._tick_until(DAY.replace(hour=8, minute=30))
        b.block("t_review", "waiting for CI/merge of #57", f.now)
        o.event(f.now, 'reviewer blocks t_review: "waiting for CI/merge of #57".')
        self._tick_until(DAY.replace(hour=9, minute=5))
        pr = self.github.repos[REPO].pulls[57]
        pr.state, pr.merged_at = "closed", f.now
        o.event(f.now, "PR #57 merges on GitHub. Nothing tells the card.")
        found = self._tick_until_action("stale_blocks", ActionKind.UNBLOCK)
        o.noticed(f.now, "Kelpie notices (stale_blocks): the block's condition is met.")
        self._narrate_messages("stale_blocks")
        o.modes(
            [
                ("observe", f'records "{intent(found.action)}" in its ledger; sends nothing.'),
                ("suggest", "sends the message above; a person clears the block in one command."),
                (
                    "act",
                    "not on Hermes yet: its adapter cannot clear a block, so Kelpie refuses "
                    "to start with stale_blocks in act mode there. Where the adapter can, "
                    "Kelpie clears the block itself and says so.",
                ),
            ]
        )
        f.advance(minutes=10)
        self._hermes(["kanban", "unblock", "t_review"])
        o.event(f.now, "dana runs `hermes kanban unblock t_review`; the review goes on.")
        self._claim("t_review", "reviewer")
        self._tick_until(f.now + timedelta(minutes=30))
        self._complete("t_review")
        o.event(f.now, "t_review is done.")
        o.line()

    # --- 2. a runaway loop ------------------------------------------------------------------

    def runaway_loop(self) -> None:
        o, f, b = self.out, self.farm, self.board
        o.scene(
            2,
            "A runaway loop",
            "On another farm a maintenance agent's heartbeat loop called the same tool with "
            "the same arguments about 900 times overnight, resending about 80k input tokens "
            "each time: roughly 72M tokens for no work.",
        )
        f.at(DAY.replace(hour=10, minute=0))
        b.task("t_heartbeat", "todo", f.ago(minutes=30), title="Nightly maintenance heartbeat")
        b.update("t_heartbeat", assignee="ops")
        rid = self._claim("t_heartbeat", "ops")
        o.event(
            f.now,
            "ops starts t_heartbeat. Every 30 seconds it calls the model, resending "
            f"{CONTEXT // 1000}k tokens of context, and runs `heartbeat_check`.",
        )
        session = f"hermes-run-{rid}"
        model_calls = 0
        for _ in range(12):  # Kelpie looks every 5 minutes; the agent calls every 30 seconds
            for _ in range(10):
                f.advance(seconds=30)
                call = ModelCall("t_heartbeat", session, MODEL, CONTEXT, 300, f.ago(seconds=5))
                f.export([call])
                model_calls += 1
                f.spool.tool_call("t_heartbeat", rid, "heartbeat_check", {"target": "all"})
                b.update("t_heartbeat", last_heartbeat_at=f.now)
            if any(d.action.rule == "loop_breaker" for d in f.tick()):
                break
        stop = self._first("loop_breaker", ActionKind.STOP)
        o.noticed(
            f.now,
            "Kelpie notices (loop_breaker): identical consecutive calls, past the limit.",
        )
        self._narrate_messages("loop_breaker")
        ran = f.hermes_calls()
        argv = ran[-1]["argv"] if ran else []
        command = " ".join(str(a) for a in argv[:6]) if isinstance(argv, list) else "nothing"
        o.event(
            f.now,
            f"Kelpie ran `hermes {command} ...`: t_heartbeat is {b.get('t_heartbeat').status}.",
        )
        o.modes(
            [
                (
                    "observe",
                    f'records "{intent(stop.action)}"; the loop keeps burning tokens '
                    "until someone looks.",
                ),
                (
                    "suggest",
                    f'sends the alert, ending "{suggested_line(stop.action)}"; the loop runs '
                    "until a person stops it.",
                ),
                (
                    "act",
                    "stops the run through the platform (above) and says so: one "
                    "critical message, to every channel.",
                ),
            ]
        )
        price = PRICES[MODEL]
        spent = model_calls * (CONTEXT * price["input"] + 300 * price["output"]) / 1e6
        overnight = 900 * CONTEXT * price["input"] / 1e6
        o.paragraph_at(
            f"The stop came after {model_calls} model calls: "
            f"{model_calls * CONTEXT / 1e6:.1f}M tokens, ${spent:.2f} (${price['input']:g} "
            f"per million input tokens, ${price['output']:g} per million output). Left alone "
            f"for a night like on that farm (900 calls) it is {900 * CONTEXT / 1e6:.0f}M input "
            f"tokens, about ${overnight:,.0f}."
        )
        f.advance(minutes=20)
        b.update("t_heartbeat", status="done", completed_at=f.now)
        b.event("t_heartbeat", "completed", f.now)
        o.event(f.now, "sam fixes the heartbeat's schedule and closes t_heartbeat.")
        o.line()

    # --- 3. a forgotten hold ----------------------------------------------------------------

    def forgotten_hold(self) -> None:
        o, f, b = self.out, self.farm, self.board
        o.scene(
            3,
            "A forgotten hold",
            "On a real farm (incident 14) a deliberate merge hold was promised to be lifted "
            "in 2.5 hours and was forgotten. Ready PRs waited and agents had no work: about "
            "5 hours of idle farm, 1 task closed.",
        )
        f.at(DAY.replace(hour=10, minute=30))
        for tid, title in (("t_settings", "Settings page"), ("t_export", "Export job")):
            b.task(tid, "todo", f.ago(hours=1), title=title, branch=f"hermes/{tid}")
            b.update(tid, assignee="dev")
        self._claim("t_settings", "dev")
        self._tick_until(DAY.replace(hour=10, minute=50))
        self._hand_off("t_settings", 61, "Settings page")
        self._claim("t_export", "dev")
        self._tick_until(DAY.replace(hour=11, minute=0))
        self._hand_off("t_export", 62, "Export job")
        for number in (61, 62):
            self.github.repos[REPO].pulls[number].labels[HOLD] = f.now
        o.event(
            f.now,
            f'dana puts the "{HOLD}" label on PRs #61 and #62: "for 2.5 hours, until the '
            'release is out". dev has nothing else to do.',
        )
        self._tick_until_action("bottlenecks", ActionKind.ALERT, every=15)
        o.noticed(f.now, "Kelpie notices (bottlenecks): the hold is past its expiry.")
        self._narrate_messages("bottlenecks")
        o.modes(
            [
                ("observe", "records the forgotten hold and the idle agent; sends nothing."),
                (
                    "suggest",
                    "sends the alert above, naming the hold's owner, to the configured "
                    "channels. The alert is the whole action: lifting a hold is a human "
                    "decision.",
                ),
                ("act", "the same: Kelpie never lifts a hold."),
            ]
        )
        self._tick_until(DAY.replace(hour=15, minute=0), every=15)
        for number, tid in ((61, "t_settings"), (62, "t_export")):
            pr = self.github.repos[REPO].pulls[number]
            pr.labels.pop(HOLD, None)
            pr.state, pr.merged_at = "closed", f.now
            b.update(tid, status="done", completed_at=f.now)
            b.event(tid, "completed", f.now)
        o.event(f.now, "dana lifts the hold, 1.5 hours late; both PRs merge.")
        self._tick_until_message("bottlenecks", "resolved after")
        o.noticed(f.now, "Kelpie sees the held tasks move and closes its alert.")
        self._narrate_messages("bottlenecks")
        o.line()

    # --- 4. a human task gating agent work ----------------------------------------------------

    def human_gate(self) -> None:
        o, f, b = self.out, self.farm, self.board
        o.scene(
            4,
            "A human task gating agent work",
            "On a real farm (incident 16) an agent task was linked to a human task (store "
            "products in the app store console); 28 agent tasks depended on it through a "
            "chain. The farm ran 3 of 5 slots, about 4 tasks an hour instead of 9.",
        )
        f.at(DAY.replace(hour=15, minute=30))
        b.task(
            "t_store",
            "ready",
            f.now,
            title="Store the products in the app store console",
            assignee="sam",
        )
        b.event("t_store", "promoted", f.now)
        parent = "t_store"
        for i in range(1, 6):
            b.task(f"t_iap{i}", "todo", f.now, title=f"In-app purchases, step {i}")
            b.update(f"t_iap{i}", assignee="dev")
            b.link(parent, f"t_iap{i}")
            parent = f"t_iap{i}"
        o.event(
            f.now,
            "sam gets t_store; five agent tasks wait behind it in a chain (t_iap1 to "
            "t_iap5). Every agent is idle.",
        )
        self._tick_until_action("needs_you", ActionKind.ESCALATE, every=15)
        f.tick()
        o.noticed(
            f.now,
            "Kelpie notices (needs_you, bottlenecks): a person's task holds the agents' work.",
        )
        self._narrate_messages("needs_you", "bottlenecks")
        o.modes(
            [
                ("observe", "records who holds what and for how long; sends nothing."),
                (
                    "suggest",
                    "sends sam's inbox, ordered by cost of delay, and what the farm is idle "
                    "on to the configured channels.",
                ),
                ("act", "the same: Kelpie never changes dependencies or does a person's task."),
            ]
        )
        self._tick_until(DAY.replace(hour=18, minute=0), every=30)
        o.event(f.now, "The day ends with t_store still open.")
        o.line()

    # --- 5. the daily report -------------------------------------------------------------------

    def daily_report(self) -> tuple[str, Path | None]:
        o, f = self.out, self.farm
        o.scene(5, "The daily report: cost of chaos")
        assert f.app is not None
        day = f.now.date().isoformat()
        markdown, err, code = run_report(f.app.settings, day, False, clock=lambda: f.now)
        if code != 0:
            raise RuntimeError(f"kelpie report failed ({code}): {err.strip()}")
        path = f.root / "reports" / f"kelpie-report-{day}.md"
        o.paragraph(
            "Tokens burned counts the model calls of a stuck or looping session after "
            "Kelpie reported it. Kelpie stopped the runaway in that same tick, so nothing "
            "burned here; its calls before the stop are in the day's spend."
        )
        o.line(f"$ kelpie report --date {day}", "dim")
        o.line()
        o.verbatim(summary(markdown))
        return markdown, path if path.exists() else None

    def outro(self) -> None:
        o = self.out
        o.line()
        o.paragraph(
            "That was Kelpie on a simulated farm. On yours it starts safe: dry-run on, every "
            "rule in observe mode, nothing sent until you add a channel. Start with "
            "`kelpie doctor` and the README's quickstart."
        )

    # --- the farm's side --------------------------------------------------------------------

    def _claim(self, task_id: str, profile: str) -> int:
        f = self.farm
        return self.board.claim(
            task_id, profile, f.now, heartbeat=f.now, claim_expires=f.now + timedelta(hours=1)
        )

    def _complete(self, task_id: str) -> None:
        f, b = self.farm, self.board
        rid = b.get(task_id).current_run_id
        if rid is not None:
            b.end_run(rid, f.now, "completed")
        b.update(task_id, status="done", current_run_id=None, completed_at=f.now)
        b.event(task_id, "completed", f.now, run_id=rid)

    def _hand_off(self, task_id: str, number: int, title: str) -> None:
        f = self.farm
        self.board.hand_off(task_id, f.now)
        self.github.pull(
            REPO,
            number,
            title=f"{task_id}: {title}",
            branch=f"hermes/{task_id}",
            mergeable_state="blocked",
            created=f.now,
            updated=f.now,
        )

    def _hermes(self, argv: Sequence[str]) -> None:
        """A person runs the hermes CLI on the board."""
        env = {"HERMES_KANBAN_DB": str(self.board.path), "PATH": os.defpath}
        done = subprocess.run(
            [str(self.farm.cli), *argv], env=env, capture_output=True, text=True, check=False
        )
        if done.returncode != 0:
            raise RuntimeError(f"hermes {' '.join(argv)} failed: {done.stderr.strip()}")

    # --- Kelpie's side ------------------------------------------------------------------------

    def _tick_until(self, when: datetime, every: int = 5) -> None:
        """Let time pass until ``when``, Kelpie looking every ``every`` minutes."""
        f = self.farm
        while f.now + timedelta(minutes=every) <= when:
            f.advance(minutes=every)
            f.tick()
        if f.now < when:
            f.at(when)

    def _tick_until_action(
        self, rule: str, kind: ActionKind, every: int = 5, limit: int = 6 * 60
    ) -> Dispatched:
        """Tick until ``rule`` emits a ``kind`` action that was not suppressed."""
        f = self.farm
        for _ in range(limit // every):
            f.advance(minutes=every)
            for d in f.tick():
                if (
                    d.action.rule == rule
                    and d.action.kind is kind
                    and d.outcome is not Outcome.SUPPRESSED
                ):
                    return d
        raise RuntimeError(f"{rule} did not {kind} within {limit} minutes")

    def _tick_until_message(self, rule: str, needle: str, limit: int = 60) -> None:
        """Tick every 5 minutes until ``rule`` sends a message containing ``needle``."""
        inbox = self.farm.inbox.messages
        for _ in range(limit // 5):
            self.farm.advance(minutes=5)
            self.farm.tick()
            if any(m.rule == rule and m.mentions(needle) for m in inbox[self._seen :]):
                return
        raise RuntimeError(f"{rule} sent no {needle!r} message within {limit} minutes")

    def _first(self, rule: str, kind: ActionKind) -> Dispatched:
        for d in self.farm.results:
            if d.action.rule == rule and d.action.kind is kind:
                return d
        raise RuntimeError(f"{rule} never emitted {kind}")

    def _narrate_messages(self, *rules: str) -> None:
        messages = self.farm.inbox.messages
        for message in messages[self._seen :]:
            if message.rule in rules:
                self.out.message(message)
        self._seen = len(messages)


def summary(markdown: str) -> str:
    """The report's headline and the sections a visitor cares about first, as Markdown
    wrapped for a terminal (tables are left whole); the full report has the rest."""
    lines = markdown.splitlines()
    headline = next((line for line in lines if line.startswith("Cost of chaos:")), "")
    parts = [*_fold(headline), ""]
    for section in REPORT_SECTIONS:
        if section not in markdown:
            continue
        body = markdown.split(section, 1)[1].split("\n## ", 1)[0].strip()
        parts += [section, ""]
        for line in body.splitlines():
            if line.startswith("Hours lost:"):  # how the hours are counted: full report only
                continue
            parts += [line] if line.startswith("|") else _fold(line)
        parts.append("")
    text = "\n".join(parts)
    while "\n\n\n" in text:
        text = text.replace("\n\n\n", "\n\n")
    return text.strip() + "\n"


def _fold(line: str) -> list[str]:
    """A Markdown line wrapped at the narration's width, list items hanging."""
    if not line:
        return [""]
    marker = re.match(r"(\s*(?:[-*]|\d+\.) )", line)
    indent = " " * len(marker[1]) if marker else ""
    return textwrap.wrap(
        line, WIDTH, subsequent_indent=indent, break_on_hyphens=False, break_long_words=False
    )
