"""The farm view as plain text (``kelpie status``). Text from agents is cleaned to one line
(:func:`kelpie.alerts.render.one_line`): no control characters reach the terminal."""

from __future__ import annotations

from datetime import datetime, timedelta

from kelpie.alerts.render import one_line
from kelpie.core.farmview import FarmView, StatusCount

REASON_CHARS = 100
SUMMARY_CHARS = 120

_SOURCES = {
    "board": "read from the board now",
    "server": "kelpie serve's last tick",
    "stored": "the last status kelpie serve observed per task (no board read)",
    "none": "no board read and none stored",
}


def _age(delta: timedelta) -> str:
    seconds = max(0, int(delta.total_seconds()))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h{seconds % 3600 // 60:02d}m"
    return f"{seconds // 86400}d{seconds % 86400 // 3600}h"


def _ago(now: datetime, at: datetime | None) -> str:
    return "never" if at is None else f"{_age(now - at)} ago"


def _counts(counts: list[StatusCount]) -> str:
    total = sum(c.count for c in counts)
    parts = ", ".join(f"{_t(c.status, 40)} {c.count}" for c in counts)
    return f"{total}" + (f" ({parts})" if parts else "")


def _t(text: str | None, limit: int = 80) -> str:
    return one_line(text or "", limit)


def render(view: FarmView) -> str:
    now = view.now
    source = _SOURCES.get(view.source) or _t(view.source, 40)
    if view.snapshot_at is not None:
        source += f", {_ago(now, view.snapshot_at)}"
    lines = [f"farm: {source}"]
    if view.boards:
        for board in (*view.boards, None):
            counts = [c for c in view.statuses if c.board == board]
            if counts:
                lines.append(f"  board {_t(board) if board else '(none)'}: {_counts(counts)}")
    else:
        lines.append(f"  tasks: {_counts(list(view.statuses))}")
    lines += ["", f"waiting conditions: {view.waiting_total}"]
    for w in view.waiting:
        state = "unknown" if w.satisfied is None else "unmet"
        where = f"{_t(w.board, 40)}/" if w.board else ""
        target = f" {_t(w.target)}" if w.target else ""
        status = f" [{_t(w.task_status, 40)}]" if w.task_status else ""
        lines.append(
            f"  {where}{_t(w.task_id, 60)}{status} {_t(w.kind, 40)}{target}: {state}, "
            f"checked {_ago(now, w.checked_at)}"
        )
    _more(lines, view.waiting_total, len(view.waiting))
    lines += ["", f"open alerts: {view.alerts_total}"]
    for a in view.alerts:
        task = f" {_t(a.task_id, 60)}" if a.task_id else ""
        lines.append(
            f"  {_t(a.severity, 20):<8} {_t(a.rule, 60)}{task} ({_t(a.mode, 20)}, "
            f"open {_age(now - a.opened_at)}): {_t(a.summary, SUMMARY_CHARS)}"
        )
    _more(lines, view.alerts_total, len(view.alerts))
    lines += ["", "last actions per rule:"]
    for r in view.rules:
        lines.append(f"  {_t(r.rule, 60)}:")
        for e in r.actions:
            task = f" {_t(e.task_id, 60)}" if e.task_id else ""
            lines.append(
                f"    {_ago(now, e.at):>9} {_t(e.kind, 30)} {_t(e.mode, 20)}/"
                f"{_t(e.outcome, 20)}{task}: {_t(e.reason, REASON_CHARS)}"
            )
    if not view.rules:
        lines.append("  (none recorded)")
    lines += [f"note: {_t(p, 300)}" for p in view.problems]
    return "\n".join(lines) + "\n"


def _more(lines: list[str], total: int, shown: int) -> None:
    if total > shown:
        lines.append(f"  ... and {total - shown} more")
