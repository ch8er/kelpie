"""Issue 141 on the simulated farm: a run Kelpie stopped waits for a human. Rule 4 leaves it
alone, rule 13 lists it, and the report counts it as people's time, not as a block that
outlived its cause."""

from __future__ import annotations

import io
from datetime import timedelta
from pathlib import Path

from kelpie.core.model import KelpieMark
from kelpie.demo import story
from kelpie.demo.farm import DemoFarm
from kelpie.demo.narrate import Narrator
from kelpie.report.cli import run as run_report


def test_a_stopped_run_is_a_human_wait_not_a_stale_block(tmp_path: Path) -> None:
    farm = DemoFarm(story.DAY, tmp_path / "farm")
    tale = story.Story(farm, Narrator(io.StringIO(), pace=0.0))
    try:
        farm.start(tale.config())
        b = farm.board
        b.task("t_loop", "todo", farm.ago(minutes=30), title="Heartbeat", assignee="ops")
        rid = b.claim("t_loop", "ops", farm.now, heartbeat=farm.now)
        for _ in range(6):  # 5 minutes a tick, a call every 30 seconds, until Kelpie stops it
            for _ in range(10):
                farm.advance(seconds=30)
                farm.spool.tool_call("t_loop", rid, "heartbeat_check", {"target": "all"})
                b.update("t_loop", last_heartbeat_at=farm.now)
            farm.tick()
            if b.get("t_loop").status == "blocked":
                break
        assert b.get("t_loop").status == "blocked"
        assert farm.app is not None
        (task,) = [t for t in farm.app.loop.snapshot_source().tasks if t.id == "t_loop"]
        assert task.block is not None and task.block.kelpie is KelpieMark.STOP

        tale._tick_until(farm.now + timedelta(hours=6), every=15)  # nobody resumes it

        stale = [r for r in farm.results if r.action.rule == "stale_blocks"]
        assert [r for r in stale if r.action.task_id == "t_loop"] == []
        inbox = [m for m in farm.inbox.messages if m.rule == "needs_you"]
        assert any(
            m.mentions("t_loop", "was stopped by Kelpie and needs unblocking") for m in inbox
        )

        day = farm.now.date().isoformat()
        markdown, err, code = run_report(farm.app.settings, day, False, clock=lambda: farm.now)
        assert code == 0, err
        assert "blocks that outlived their cause" not in markdown
        assert "[[block_conditions]]" not in markdown  # no block conditions for it
        assert "agents waiting on people" in markdown
    finally:
        farm.close()
