"""``kelpie demo``: the story runs offline on the packaged simulator and says what Kelpie
actually did."""

from __future__ import annotations

import io
import logging
import os
import re
import socket
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from kelpie.app import ConfigError
from kelpie.cli import main
from kelpie.demo import run, story
from kelpie.demo.farm import DemoFarm
from kelpie.demo.narrate import Narrator
from kelpie.demo.story import Story, summary

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
    """The README's first screen quotes the demo: each quoted run of lines (between the
    "..." that mark cuts) must still be printed, whole and in order."""
    text, _ = demo(tmp_path)
    printed = text.splitlines()
    readme = README.read_text(encoding="utf-8")
    block = readme.split("```console\n$ kelpie demo\n", 1)[1].split("```", 1)[0]
    chunks: list[list[str]] = [[]]
    for line in block.splitlines():
        if line == "...":
            chunks.append([])
        else:
            chunks[-1].append(line)
    chunks = [c for c in chunks if any(line.strip() for line in c)]
    assert len(chunks) >= 3
    at = 0
    for chunk in chunks:
        found = next(
            (i for i in range(at, len(printed)) if printed[i : i + len(chunk)] == chunk), None
        )
        assert found is not None, f"not printed in order: {chunk[:3]}"
        at = found + len(chunk)


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


# --- a real install is never touched --------------------------------------------------------


def test_the_demo_never_writes_a_real_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real = tmp_path / "real" / "kelpie.db"
    monkeypatch.setenv("KELPIE_DB", str(real))
    monkeypatch.delenv("KELPIE_DEMO_GITHUB_TOKEN", raising=False)
    demo(tmp_path)
    assert not real.exists()
    assert os.environ["KELPIE_DB"] == str(real)  # given back as it was
    assert "KELPIE_DEMO_GITHUB_TOKEN" not in os.environ


def test_logging_is_given_back(tmp_path: Path) -> None:
    root = logging.getLogger()
    handler = logging.NullHandler()
    root.addHandler(handler)
    level = root.level
    try:
        before = list(root.handlers)
        demo(tmp_path)
        assert root.handlers == before and root.level == level
    finally:
        root.removeHandler(handler)


# --- what each mode does, as the narration says it -----------------------------------------


def scene(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, **modes: str) -> Story:
    rules = {name: dict(section) for name, section in story.RULES.items()}
    for name, mode in modes.items():
        rules[name]["mode"] = mode
    monkeypatch.setattr(story, "RULES", rules)
    farm = DemoFarm(story.DAY, tmp_path / "farm")
    tale = Story(farm, Narrator(io.StringIO(), pace=0.0))
    farm.start(tale.config())
    return tale


def test_act_on_a_stale_block_is_refused_on_hermes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Scene 1's act line: Kelpie will not start with stale_blocks in act mode on Hermes."""
    with pytest.raises(ConfigError, match=r"stale_blocks"):
        scene(tmp_path, monkeypatch, stale_blocks="act")


@pytest.mark.parametrize("mode", ["observe", "suggest"])
def test_a_runaway_is_only_stopped_in_act_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    tale = scene(tmp_path, monkeypatch, loop_breaker=mode)
    try:
        tale.runaway_loop()
        farm = tale.farm
        assert farm.hermes_calls() == []
        loops = [m for m in farm.inbox.messages if m.rule == "loop_breaker"]
        stops = [r for r in farm.results if r.action.rule == "loop_breaker"]
        outcomes = {r.outcome.value for r in stops}
        if mode == "observe":  # recorded (repeats held back by the ledger), nothing sent
            assert loops == [] and "observed" in outcomes <= {"observed", "suppressed"}
        else:  # sent, ending "Suggested: stop run ..."
            assert loops and loops[0].did.startswith("Suggested: stop run hermes-run-")
    finally:
        tale.farm.close()


def test_a_hold_alert_is_only_recorded_in_observe_mode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tale = scene(tmp_path, monkeypatch, bottlenecks="observe")
    try:
        with pytest.raises(RuntimeError, match="no 'Resolved' message"):
            tale.forgotten_hold()  # observed, never sent: nothing to resolve
        farm = tale.farm
        assert [m for m in farm.inbox.messages if m.rule == "bottlenecks"] == []
        held = {r.outcome.value for r in farm.results if r.action.rule == "bottlenecks"}
        assert "observed" in held <= {"observed", "suppressed"}
    finally:
        tale.farm.close()


# --- failures ------------------------------------------------------------------------------


def test_ctrl_c_cleans_up(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def interrupted(self: object) -> None:
        raise KeyboardInterrupt

    monkeypatch.setattr("kelpie.demo.story.Story.runaway_loop", interrupted)
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    err = io.StringIO()
    code, _ = run(fast=True, out=io.StringIO(), err=err)
    assert code == 130 and "interrupted" in err.getvalue()
    assert list(tmp_path.glob("kelpie-demo-*")) == []


def test_a_platform_without_a_shell_is_told_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "nt")
    err = io.StringIO()
    assert run(fast=True, out=io.StringIO(), err=err) == (2, None)
    assert "runs on Linux or macOS" in err.getvalue()


def test_a_farm_that_cannot_be_set_up_is_told_plainly(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(*args: object) -> None:
        raise OSError("no space left on device")

    monkeypatch.setattr("kelpie.demo.DemoFarm", broken)
    err = io.StringIO()
    assert run(fast=True, out=io.StringIO(), err=err) == (1, None)
    assert "cannot set up the simulated farm: no space left" in err.getvalue()


# --- the terminal --------------------------------------------------------------------------


class Terminal(io.StringIO):
    def isatty(self) -> bool:
        return True


def test_colour_and_pauses_only_on_a_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    slept: list[float] = []
    monkeypatch.setattr("time.sleep", slept.append)
    monkeypatch.delenv("NO_COLOR", raising=False)
    tty = Terminal()
    Narrator(tty).title("Kelpie")
    assert "\033[" in tty.getvalue() and slept
    monkeypatch.setenv("NO_COLOR", "1")
    plain = Terminal()
    Narrator(plain, pace=0.0).title("Kelpie")
    assert plain.getvalue() == "Kelpie\n"
    piped = io.StringIO()
    slept.clear()
    Narrator(piped).title("Kelpie")
    assert piped.getvalue() == "Kelpie\n" and slept == []


def test_web_writes_the_report_as_a_page_and_opens_it(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    monkeypatch.setattr("webbrowser.open", lambda url: opened.append(url) or True)
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    monkeypatch.setenv("DISPLAY", ":0")
    out = io.StringIO()
    code, outcomes = run(fast=True, web=True, out=out, err=io.StringIO())
    assert code == 0 and outcomes is not None
    (page,) = tmp_path.glob("kelpie-demo-web-*/kelpie-demo-report.html")
    assert opened == [page.resolve().as_uri()]
    html = page.read_text(encoding="utf-8")
    assert "<h2>Cost of chaos</h2>" in html and "<code>t_heartbeat</code>" in html
    assert f"The report as a web page:\n  {page}\n(sent to your browser)\n" in out.getvalue()
    assert not list(tmp_path.glob("kelpie-demo-[!w]*"))  # the farm itself is gone


def test_web_without_a_display_only_prints_the_page(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a server or over SSH, Python would fall back to a text browser in the terminal."""
    monkeypatch.setattr("sys.platform", "linux")
    monkeypatch.delenv("DISPLAY", raising=False)
    monkeypatch.delenv("WAYLAND_DISPLAY", raising=False)
    monkeypatch.setattr("webbrowser.open", lambda url: pytest.fail("no browser to open"))
    out = io.StringIO()
    code, _ = run(fast=True, web=True, keep=True, root=tmp_path / "farm", out=out)
    assert code == 0
    page = tmp_path / "farm" / "kelpie-demo-report.html"
    assert page.exists()  # kept with the farm
    assert f"The report as a web page:\n  {page}\n" in out.getvalue()
    assert "sent to your browser" not in out.getvalue()


def test_web_survives_a_browser_error_and_a_page_it_cannot_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import webbrowser

    def broken(url: str) -> bool:
        raise webbrowser.Error("no runnable browser")

    monkeypatch.setenv("DISPLAY", ":0")
    monkeypatch.setattr("webbrowser.open", broken)
    out = io.StringIO()
    code, _ = run(fast=True, web=True, root=tmp_path / "a", out=out)
    assert code == 0 and "sent to your browser" not in out.getvalue()

    def unwritable(*args: object, **kwargs: object) -> None:
        raise OSError("read-only file system")

    monkeypatch.setattr("kelpie.demo.html.render", unwritable)
    err = io.StringIO()
    code, _ = run(fast=True, web=True, root=tmp_path / "b", out=io.StringIO(), err=err)
    assert code == 0
    assert "the report page could not be written: read-only file system" in err.getvalue()
