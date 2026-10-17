"""``kelpie demo``: see Kelpie work in two minutes, without a farm.

The farm simulator (:mod:`kelpie.sim`) plays a working day of real incidents
(:mod:`kelpie.demo.story`) while the real Kelpie watches it in this process
(:mod:`kelpie.demo.farm`), and the narration (:mod:`kelpie.demo.narrate`) says what happened,
what Kelpie noticed and what it does in each mode. Offline: no Hermes, no GitHub, no tokens,
nothing sent anywhere.

Kelpie's own log goes to ``kelpie-demo.log`` in the farm directory, not to the terminal; a
run that logged an error exits 1 and says where the log is (the farm is then kept).

With ``--web`` the day's report is also written as one self-contained HTML page
(:mod:`kelpie.report.html`), in a new private directory that stays after the demo (the
browser reads it later) or in the kept farm, and handed to a graphical browser when there is
one; the path is printed first, so a text-only session still has it. (With a display but no
graphical browser installed, Python may still fall back to a text browser in the terminal.)
"""

from __future__ import annotations

import logging
import os
import shutil
import sys
import tempfile
import webbrowser
from pathlib import Path
from typing import TextIO

from kelpie.demo.farm import LOG, DemoFarm
from kelpie.demo.narrate import Narrator
from kelpie.demo.story import DAY, Outcomes, Story
from kelpie.report import html

PAGE = "kelpie-demo-report.html"


def run(
    *,
    fast: bool = False,
    keep: bool = False,
    web: bool = False,
    root: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    log_level: int | str = logging.INFO,
) -> tuple[int, Outcomes | None]:
    """Tell the story; ``(exit code, what happened)``: 0, 1 when it failed or Kelpie
    logged an error, 2 on a platform it cannot run on, 130 when interrupted. The farm lives
    in a temporary directory (or ``root``), removed afterwards unless ``keep`` or something
    went wrong. With ``web`` the report is also written as a web page and opened."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    if os.name != "posix" or not Path("/bin/sh").exists():
        err.write("kelpie demo: runs on Linux or macOS (its simulated hermes CLI needs /bin/sh)\n")
        return 2, None
    try:
        farm = DemoFarm(DAY, root)
    except OSError as exc:
        err.write(f"kelpie demo: cannot set up the simulated farm: {exc}\n")
        return 1, None
    saved = _log_to(farm.root / LOG, log_level)
    narrator = Narrator(out, pace=0.0 if fast else None)
    outcomes: Outcomes | None = None
    failed: str | None = None
    try:
        outcomes = Story(farm, narrator).run()
    except KeyboardInterrupt:
        farm.close()
        err.write("\nkelpie demo: interrupted\n")
        return 130, None
    except Exception as exc:
        logging.getLogger(__name__).exception("the demo failed")
        failed = f"the demo failed: {exc!r}"
    finally:
        farm.close(keep=True)
        _restore_logging(saved)
    errors = _errors(farm.root / LOG)
    if failed or errors:
        err.write(f"kelpie demo: {failed or 'Kelpie logged errors'}; see {farm.root / LOG}\n")
        for line in [] if failed else errors[:5]:
            err.write(f"  {line}\n")
        return 1, outcomes
    if web and outcomes is not None:
        try:
            page = _page(outcomes, farm.root if keep or root is not None else None)
        except OSError as exc:
            err.write(f"kelpie demo: the report page could not be written: {exc}\n")
        else:
            out.write(f"\nThe report as a web page:\n  {page}\n")
            out.flush()
            if _open(page):
                out.write("(sent to your browser)\n")
    if keep or root is not None:
        out.write("\nKept the farm: its board, kelpie.toml, Kelpie's log and the full report:\n")
        out.write(f"  {farm.root}\n")
    else:
        farm.remove()
    return 0, outcomes


def _page(outcomes: Outcomes, farm: Path | None) -> Path:
    """The report as HTML: in the kept farm, else in a new private directory of its own
    (never a predictable path in a shared temporary directory)."""
    directory = farm if farm is not None else Path(tempfile.mkdtemp(prefix="kelpie-demo-web-"))
    path = directory / PAGE
    day = DAY.date().isoformat()
    try:
        path.write_text(
            html.render(
                outcomes.report,
                f"Kelpie daily report {day} (demo)",
                "The day of a simulated farm from kelpie demo: synthetic tasks, people and "
                "repository, no real farm data.",
            ),
            encoding="utf-8",
        )
    except OSError:
        if farm is None:  # ours: leave no empty directory behind
            shutil.rmtree(directory, ignore_errors=True)
        raise
    return path


def _open(path: Path) -> bool:
    """Hand ``path`` to a graphical browser; False when there is none. Without a display on
    Linux (a server, SSH, CI) nothing is opened: Python's fallback would be a text browser
    that takes over the terminal."""
    if sys.platform.startswith("linux") and not (
        os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")
    ):
        return False
    try:
        return webbrowser.open(path.resolve().as_uri())
    except webbrowser.Error:
        return False


_Saved = tuple[list[logging.Handler], int, logging.Handler]


def _log_to(path: Path, level: int | str) -> _Saved:
    """Kelpie's log goes to ``path`` only, from this run (the terminal is the story's);
    what the root logger had is given back by :func:`_restore_logging`."""
    root = logging.getLogger()
    handlers, previous = list(root.handlers), root.level
    for handler in handlers:
        root.removeHandler(handler)
    ours = logging.FileHandler(path, mode="w", encoding="utf-8")
    ours.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(ours)
    root.setLevel(level)
    return handlers, previous, ours


def _restore_logging(saved: _Saved) -> None:
    handlers, level, ours = saved
    root = logging.getLogger()
    if ours not in root.handlers:
        return  # restored already
    root.removeHandler(ours)
    ours.close()
    for handler in handlers:
        root.addHandler(handler)
    root.setLevel(level)


def _errors(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line for line in lines if " ERROR " in line or " CRITICAL " in line]
