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
) -> tuple[int, Outcomes | None]:
    """Tell the story; ``(exit code, what happened)``. The farm lives in a temporary
    directory (or ``root``), removed afterwards unless ``keep`` or something went wrong."""
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    farm = DemoFarm(DAY, root)
    handler = _log_to(farm.root / LOG)
    narrator = Narrator(out, pace=0.0 if fast else None)
    outcomes: Outcomes | None = None
    failed: str | None = None
    try:
        outcomes = Story(farm, narrator).run()
    except Exception as exc:
        logging.getLogger(__name__).exception("the demo failed")
        failed = f"the demo failed: {exc!r}"
    finally:
        farm.close(keep=True)
        _stop_logging(handler)
    errors = _errors(farm.root / LOG)
    if failed or errors:
        err.write(f"kelpie demo: {failed or 'Kelpie logged errors'}; see {farm.root / LOG}\n")
        for line in errors[:5]:
            err.write(f"  {line}\n")
        return 1, outcomes
    if keep or root is not None:
        out.write("\nKept the farm: its board, kelpie.toml, Kelpie's log and the full report:\n")
        out.write(f"  {farm.root}\n")
    else:
        farm.remove()
    return 0, outcomes


def _log_to(path: Path) -> logging.Handler:
    """Kelpie's log goes to ``path`` only: the terminal is the story's."""
    root = logging.getLogger()
    for handler in list(root.handlers):
        root.removeHandler(handler)
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s"))
    root.addHandler(handler)
    root.setLevel(logging.INFO)
    return handler


def _stop_logging(handler: logging.Handler) -> None:
    logging.getLogger().removeHandler(handler)
    handler.close()


def _errors(path: Path) -> list[str]:
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line for line in lines if " ERROR " in line or " CRITICAL " in line]
