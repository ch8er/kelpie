"""``kelpie demo``: see Kelpie work in two minutes, without a farm.

The farm simulator (:mod:`kelpie.sim`) plays a working day of real incidents
(:mod:`kelpie.demo.story`) while the real Kelpie watches it in this process
(:mod:`kelpie.demo.farm`), and the narration (:mod:`kelpie.demo.narrate`) says what happened,
what Kelpie noticed and what it does in each mode. Offline: no Hermes, no GitHub, no tokens,
nothing sent anywhere.

Kelpie's own log goes to ``kelpie-demo.log`` in the farm directory, not to the terminal; a
run that logged an error exits 1 and says where the log is (the farm is then kept).
"""

from __future__ import annotations

import logging
import os
import sys
from pathlib import Path
from typing import TextIO

from kelpie.demo.farm import LOG, DemoFarm
from kelpie.demo.narrate import Narrator
from kelpie.demo.story import DAY, Outcomes, Story


def run(
    *,
    fast: bool = False,
    keep: bool = False,
    root: Path | None = None,
    out: TextIO | None = None,
    err: TextIO | None = None,
    log_level: int | str = logging.INFO,
) -> tuple[int, Outcomes | None]:
    """Tell the story; ``(exit code, what happened)``: 0, 1 when it failed or Kelpie
    logged an error, 2 on a platform it cannot run on, 130 when interrupted. The farm lives
    in a temporary directory (or ``root``), removed afterwards unless ``keep`` or something
    went wrong."""
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
    if keep or root is not None:
        out.write("\nKept the farm: its board, kelpie.toml, Kelpie's log and the full report:\n")
        out.write(f"  {farm.root}\n")
    else:
        farm.remove()
    return 0, outcomes


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
