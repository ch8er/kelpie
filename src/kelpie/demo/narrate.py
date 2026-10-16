"""The demo's terminal output: a timeline, the messages Kelpie sends, what each mode does.

Plain text at a fixed width, so the output is the same on every terminal (the README shows
it). Colour only on a terminal that wants it (``NO_COLOR`` unset), and a short pause between
lines there, so the story can be read as it runs; piped output (CI) is never paused.
"""

from __future__ import annotations

import os
import sys
import textwrap
import time
from collections.abc import Sequence
from datetime import datetime
from typing import TextIO

from kelpie.demo.farm import Message

WIDTH = 78
TIME = 7  # "09:10  "
MODE = 9  # "observe  "

_RESET = "\033[0m"
_STYLES = {
    "title": "\033[1m",
    "scene": "\033[1;36m",
    "dim": "\033[2m",
    "kelpie": "\033[1;32m",
    "critical": "\033[1;31m",
    "warning": "\033[33m",
    "info": "\033[34m",
    "mode": "\033[1m",
}


class Narrator:
    def __init__(self, out: TextIO = sys.stdout, *, pace: float | None = None) -> None:
        self.out = out
        tty = hasattr(out, "isatty") and out.isatty()
        self.colour = tty and not os.environ.get("NO_COLOR")
        # Seconds after each line on a terminal; none when piped or asked to go fast.
        self.pace = (0.18 if tty else 0.0) if pace is None else pace

    # --- building blocks ---------------------------------------------------------------

    def _style(self, text: str, style: str | None) -> str:
        if not self.colour or style is None:
            return text
        return f"{_STYLES[style]}{text}{_RESET}"

    def line(self, text: str = "", style: str | None = None, pause: float = 1.0) -> None:
        self.out.write(self._style(text, style) + "\n")
        self.out.flush()
        if self.pace:
            time.sleep(self.pace * pause)

    def wrapped(self, text: str, indent: int = 0, style: str | None = None) -> None:
        for text_line in _wrap(text, WIDTH - indent):
            self.line(" " * indent + self._style(text_line, style))

    # --- the story -----------------------------------------------------------------------

    def title(self, text: str) -> None:
        self.line(text, "title")

    def paragraph(self, text: str) -> None:
        self.wrapped(text)
        self.line()

    def scene(self, number: int, title: str, real: str | None = None) -> None:
        """A scene's heading and, with ``real``, what happened on a real farm."""
        head = f"── {number}. {title} "
        self.line(head + "─" * max(0, WIDTH - len(head)), "scene", pause=8)
        if real is not None:
            self.wrapped(real, style="dim")
            self.line()

    def event(self, at: datetime, text: str) -> None:
        self._timed(at, text, None)

    def noticed(self, at: datetime, text: str) -> None:
        self._timed(at, text, "kelpie")

    def _timed(self, at: datetime, text: str, style: str | None) -> None:
        for i, part in enumerate(_wrap(text, WIDTH - TIME)):
            prefix = f"{at:%H:%M}  " if i == 0 else " " * TIME
            self.line(prefix + self._style(part, style), pause=3 if i == 0 else 1)

    def message(self, message: Message, channel: str = "alert, as Telegram shows it") -> None:
        style = message.severity if message.severity in _STYLES else None
        self.line(" " * TIME + "┌ " + self._style(channel, style))
        for part in message.text.splitlines():
            for text_line in _wrap(part, WIDTH - TIME - 2):
                self.line(" " * TIME + "│ " + text_line)
        self.line(" " * TIME + "└", pause=4)

    def modes(self, rows: Sequence[tuple[str, str]]) -> None:
        for mode, text in rows:
            for i, part in enumerate(_wrap(text, WIDTH - TIME - MODE)):
                label = mode.ljust(MODE) if i == 0 else " " * MODE
                self.line(" " * TIME + self._style(label, "mode" if i == 0 else None) + part)
        self.line()

    def paragraph_at(self, text: str) -> None:
        """A note under the timeline, indented like its events."""
        self.wrapped(text, indent=TIME)
        self.line()

    def verbatim(self, text: str, style: str | None = None) -> None:
        for text_line in text.splitlines():
            self.line(text_line, style, pause=0.5)


def _wrap(text: str, width: int) -> list[str]:
    return textwrap.wrap(text, width, break_on_hyphens=False, break_long_words=False) or [""]
