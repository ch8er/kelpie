"""The daily report after a day on the farm: ``kelpie report --date <today> --send``.

``kelpie serve`` watches a farm where a runaway run gets stopped (act mode), a review sits
ownerless with work behind it (an open condition) and a task was completed. Then the report
command reads the same database and board: the Markdown says what Kelpie fixed and what
still needs a human, the file lands in ``[report] dir``, and the summary reaches both
channels.
"""

from __future__ import annotations

import re
from datetime import timedelta
from zoneinfo import ZoneInfo

from tests.sim.farm import HERMES_STALE, Farm
from tests.sim.kelpie import problems_in, wait_for


def midday_zone(farm: Farm) -> str:
    """A fixed-offset zone where the farm's now is around noon, so every event of the
    scenario (hours ago, minutes from now) falls on the same local day whenever it runs.
    ``Etc/GMT-N`` is UTC+N (the sign is inverted by POSIX convention)."""
    offset = 12 - farm.now.hour  # -11 .. +12
    return "Etc/UTC" if offset == 0 else f"Etc/GMT{-offset:+d}"


def test_daily_report_is_written_and_sent(farm: Farm) -> None:
    b = farm.board
    # A runaway run, stopped by Kelpie.
    b.task("t_loop", "todo", farm.ago(hours=2), title="Nightly heartbeat", assignee="ops")
    loop = b.claim(
        "t_loop",
        "ops",
        farm.ago(minutes=40),
        heartbeat=farm.ago(seconds=20),
        claim_expires=farm.now + timedelta(minutes=10),
    )
    for i in range(12):
        farm.spool.tool_call(
            "t_loop", loop, "heartbeat_check", {"target": "all"}, at=farm.ago(seconds=30 * (12 - i))
        )
    # A review nobody owns, four tasks behind it: open when the report runs.
    b.task("t_auth", "todo", farm.ago(hours=6), title="Shared auth middleware", assignee="dev")
    b.claim("t_auth", "dev", farm.ago(hours=5))
    b.hand_off("t_auth", farm.ago(hours=3))
    b.update("t_auth", assignee=None)
    for i in range(4):
        b.task(f"t_dep{i}", "todo", farm.ago(hours=6), title=f"Protect endpoint {i}")
        b.link("t_auth", f"t_dep{i}")
    # A task done earlier today.
    b.task("t_done", "todo", farm.ago(hours=4), title="Fix the login flake", assignee="dev")
    done = b.claim("t_done", "dev", farm.ago(hours=3))
    b.end_run(done, farm.ago(hours=2), "completed")
    b.update("t_done", status="done", current_run_id=None, completed_at=farm.ago(hours=2))
    b.event("t_done", "completed", farm.ago(hours=2), run_id=done)

    zone = midday_zone(farm)
    reports = farm.root / "reports"
    config = farm.config(
        {"loop_breaker": {"mode": "act"}, "orphan_status": {"mode": "suggest", **HERMES_STALE}},
        extra={"report": {"timezone": zone, "dir": str(reports), "hour_usd": 2.5}},
    )
    kelpie = farm.serve(config)
    farm.wait_message("orphan_status", "t_auth")
    wait_for("the stop", lambda: farm.board.get("t_loop").status == "blocked", timeout=20)
    kelpie.wait_ticks(2)
    farm.stop()
    alerts = len(farm.channels.telegram_texts())

    day = farm.now.astimezone(ZoneInfo(zone)).date().isoformat()
    result = kelpie.cli("--log-level", "INFO", "report", "--date", day, "--send")

    assert result.code == 0, result.stderr
    assert problems_in(result.stderr.splitlines()) == []
    markdown = result.stdout
    for heading in ("## Cost of chaos", "## Fixed by Kelpie", "## Needs a human"):
        assert heading in markdown
    fixed = markdown.split("## Fixed by Kelpie", 1)[1].split("\n## ", 1)[0]
    assert "- `loop_breaker`: stop x1 `t_loop`" in fixed  # done, not "would have done"
    needs_human = markdown.split("## Needs a human", 1)[1].split("\n## ", 1)[0]
    assert "**critical** `orphan_status` `t_auth`" in needs_human
    chaos = markdown.split("## Cost of chaos", 1)[1].split("\n## ", 1)[0]
    assert re.search(r"\| ownerless tasks \(`orphan_status`\) \|.*`t_auth`", chaos)
    assert "Stuck hours at $2.50/hour" in chaos
    assert (reports / f"kelpie-report-{day}.md").read_text(encoding="utf-8") == markdown

    sent = farm.channels.telegram_texts()[alerts:]
    assert len(sent) == 1
    summary = sent[0]
    assert summary.startswith(f"Kelpie daily report {day}")
    assert re.search(r"\b1 tasks? completed", summary)
    # The ownerless review and the stopped run (a human resumes it), at least; rule 6, in
    # observe mode, adds its own observation today.
    found = re.search(r"Needs a human: (\d+) open condition\(s\)\.", summary)
    assert found and int(found[1]) >= 2
    # The alert layout: the title, then the headline under the severity's icon, a blank line,
    # one line per item. [report] severity is warning: Slack gets the same, in blocks.
    title, headline, blank, *items = summary.splitlines()
    assert headline.startswith("\U0001f7e1 Cost of chaos: ") and blank == ""
    assert items[0].startswith("Needs a human: ") or items[0].startswith("Biggest wait: ")
    assert "Open report" not in summary  # no setting holds the report's URL yet
    assert "\n".join([title, headline, *items]) in farm.channels.slack_texts()


def test_report_send_fails_loudly_when_a_channel_rejects_it(farm: Farm) -> None:
    """Slack answers ``no_service`` (the webhook was removed): cron must see exit 1."""
    farm.board.task("t_1", "ready", farm.ago(hours=1), title="Anything", assignee="dev")
    kelpie = farm.serve(farm.config(extra={"report": {"timezone": midday_zone(farm)}}))
    kelpie.wait_ticks(1)
    farm.stop()
    farm.channels.slack.default = (404, "no_service", {})  # removed after the day ran
    alerts = len(farm.channels.telegram_texts())

    day = farm.now.astimezone(ZoneInfo(midday_zone(farm))).date().isoformat()
    result = kelpie.cli("report", "--date", day, "--send")

    assert result.code == 1
    assert "did not reach every channel" in result.stderr
    assert [t.splitlines()[0] for t in farm.channels.telegram_texts()[alerts:]] == [
        f"Kelpie daily report {day}"
    ]
    problems = problems_in(result.stderr.splitlines())
    assert problems and all("slack" in p and "delivery failed" in p for p in problems)
