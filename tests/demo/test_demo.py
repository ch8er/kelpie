"""``kelpie demo``: the story runs offline on the packaged simulator and says what Kelpie
actually did."""

from __future__ import annotations

import io
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kelpie.cli import main
from kelpie.demo import run
from kelpie.demo.story import summary

README = Path(__file__).resolve().parents[2] / "README.md"


@pytest.fixture
def offline(monkeypatch: pytest.MonkeyPatch) -> list[Any]:
    """Refuse every connection that does not stay on this machine."""
    attempts: list[Any] = []
    connect = socket.socket.connect

    def guarded(self: socket.socket, address: Any) -> Any:
        host = address[0] if isinstance(address, tuple) else address
        if self.family in (socket.AF_INET, socket.AF_INET6) and host not in ("127.0.0.1", "::1"):
            attempts.append(address)
            raise OSError(f"the demo must stay offline: {address}")
        return connect(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded)
    return attempts


def demo(tmp_path: Path) -> tuple[str, Any]:
    out, err = io.StringIO(), io.StringIO()
    code, outcomes = run(fast=True, root=tmp_path / "farm", out=out, err=err)
    assert code == 0, err.getvalue()
    assert outcomes is not None
    return out.getvalue(), outcomes


def test_the_story_runs_offline_and_kelpie_does_what_it_says(
    tmp_path: Path, offline: list[Any]
) -> None:
    text, outcomes = demo(tmp_path)
    assert offline == []

    # 1. the stale block: suggested, not done (Hermes does not let Kelpie clear it)
    stale = [m for m in outcomes.messages if m.rule == "stale_blocks" and m.mentions("t_review")]
    assert stale and stale[0].did == (
        "Suggested: clear the block on t_review (not done, rule in suggest mode)."
    )
    # 2. the runaway: stopped through the hermes CLI, and the alert says so
    stops = [
        r
        for r in outcomes.results
        if r.action.rule == "loop_breaker" and r.outcome.value == "executed"
    ]
    assert [r.action.kind.value for r in stops] == ["stop"]
    (alert,) = [m for m in outcomes.messages if m.severity == "critical"]
    assert alert.mentions("Runaway run", "t_heartbeat") and alert.did is not None
    assert alert.did.startswith("Stopped run hermes-run-")
    assert "Kelpie ran `hermes kanban block --kind needs_input -- t_heartbeat" in text
    # 3. the forgotten hold, then its resolution
    assert any(
        m.mentions("Hold 'merge-first' looks forgotten", "Owner: dana") for m in outcomes.messages
    )
    assert any(m.mentions("Resolved after", "merge-first") for m in outcomes.messages)
    # 4. the human gate
    assert any(
        m.mentions("Needs you (sam)", "t_store", "5 agent task(s)") for m in outcomes.messages
    )
    # 5. the report: cost of chaos in hours and dollars, the stop as fixed
    report = outcomes.report
    assert re.search(r"Cost of chaos: [\d.]+ hours lost, .*\$[\d.]+ in stuck hours", report)
    assert "idle agents behind forgotten holds" in report
    assert "- `loop_breaker`: stop x1 `t_heartbeat`" in report
    assert "**Needs you, sam**" in report
    for heading in ("── 1.", "── 2.", "── 3.", "── 4.", "── 5.", "## Cost of chaos"):
        assert heading in text
    # each scene says what each mode does
    assert text.count("observe  ") == 4 and text.count("act      ") == 4
    assert all(len(line) <= 78 or line.startswith("|") for line in text.splitlines())
    log = (tmp_path / "farm" / "kelpie-demo.log").read_text()
    assert " ERROR " not in log and "Traceback" not in log


def test_the_story_is_the_same_every_time(tmp_path: Path) -> None:
    first, _ = demo(tmp_path / "a")
    second, _ = demo(tmp_path / "b")
    assert first.replace(str(tmp_path / "a"), "") == second.replace(str(tmp_path / "b"), "")


def test_the_readme_shows_what_the_demo_prints(tmp_path: Path) -> None:
    """The README's first screen quotes the demo; every quoted line must still be printed."""
    text, _ = demo(tmp_path)
    block = README.read_text().split("$ kelpie demo\n", 1)[1].split("```", 1)[0]
    quoted = [line for line in block.splitlines() if line.strip() and line.strip() != "..."]
    assert quoted
    missing = [line for line in quoted if line not in text.splitlines()]
    assert missing == []


def test_the_farm_is_removed_unless_kept(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    out = io.StringIO()
    code, _ = run(fast=True, out=out, err=io.StringIO())
    assert code == 0 and list(tmp_path.glob("kelpie-demo-*")) == []
    code, _ = run(fast=True, keep=True, out=out, err=io.StringIO())
    (kept,) = tmp_path.glob("kelpie-demo-*")
    assert code == 0 and (kept / "kelpie.toml").exists() and list((kept / "reports").glob("*.md"))
    assert f"the full report:\n  {kept}\n" in out.getvalue()


def test_a_failing_story_exits_1_and_keeps_the_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def broken(self: object) -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr("kelpie.demo.story.Story.runaway_loop", broken)
    err = io.StringIO()
    code, _ = run(fast=True, root=tmp_path / "farm", out=io.StringIO(), err=err)
    assert code == 1
    assert "the demo failed" in err.getvalue() and "boom" in err.getvalue()
    assert (tmp_path / "farm" / "kelpie-demo.log").exists()


def test_the_cli_runs_the_demo(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["demo", "--fast"]) == 0
    assert "Kelpie demo: one working day" in capsys.readouterr().out


def test_kelpie_itself_does_not_load_the_simulator() -> None:
    code = (
        "import sys, kelpie.cli, kelpie.app, kelpie.server; "
        "print(any(m.startswith(('kelpie.sim', 'kelpie.demo')) for m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == "False"


def test_the_report_summary_keeps_tables_whole_and_folds_the_rest() -> None:
    markdown = (
        "# Kelpie daily report: 2026-03-03\n\nCost of chaos: 1.0 hours lost.\n\n"
        "## Cost of chaos\n\n"
        "| Cause | Hours lost |\n|---|---:|\n| " + "x" * 90 + " | 1.0 |\n\nHours lost: how.\n\n"
        "## Spend\n\nnot shown\n\n## Needs a human\n\n- " + "word " * 30 + "\n"
    )
    text = summary(markdown)
    assert "| " + "x" * 90 + " | 1.0 |" in text
    assert "Hours lost: how." not in text and "not shown" not in text
    assert all(len(line) <= 78 for line in text.splitlines() if not line.startswith("|"))
    assert "\n  word" in text  # a list item hangs
