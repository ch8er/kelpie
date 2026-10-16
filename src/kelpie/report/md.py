"""Markdown-safe rendering of text Kelpie does not control.

Task ids, titles and block reasons (quoted in alert summaries), command keys, tool and
filter names, roles and people's names come from agents, the board or exporters. In the
report they must stay text: no link, image, HTML, code-span breakout, table-cell split,
emphasis around them, or new line or block. Use :func:`text` for running text,
:func:`code` for a name shown as code, and ``cell=True`` inside a table row.

Bare URLs stay as they are: a renderer may make them clickable, but they show the real
address (block reasons carry PR links on purpose); what this prevents is text that hides
or changes where a link goes.
"""

from __future__ import annotations

import re
import unicodedata

MAX_NAME = 80  # characters of a name shown as code or as link text
PLACEHOLDER = "(empty)"
_SPECIAL = re.compile(r"([\\`\[\]<>|*])")


def text(value: str) -> str:
    """One line of escaped text (also safe in a table cell); ordinary words (``t_store``,
    ``T-101``) read unchanged."""
    flat = _flat(value)
    escaped = _SPECIAL.sub(r"\\\1", flat.replace("&", "&amp;"))
    return escaped or (PLACEHOLDER if flat == "" and value else "")


def code(value: str, *, cell: bool = False) -> str:
    """A name as a code span it cannot close, at most :data:`MAX_NAME` characters. In a
    table cell (``cell``) every ``|`` is escaped, since tables split cells on it even
    inside code spans."""
    flat = _flat(value)
    if not flat:
        return PLACEHOLDER
    if len(flat) > MAX_NAME:
        flat = flat[: MAX_NAME - 3] + "..."
    if cell:
        flat = _escape_pipes(flat)
    runs = [len(r) for r in re.findall(r"`+", flat)]
    fence = "`" * (max(runs, default=0) + 1)
    pad = " " if flat.startswith("`") or flat.endswith("`") else ""
    return f"{fence}{pad}{flat}{pad}{fence}"


def link_text(value: str) -> str:
    """Escaped text for ``[...](url)``, capped like a code span."""
    flat = _flat(value)
    if len(flat) > MAX_NAME:
        flat = flat[: MAX_NAME - 3] + "..."
    return text(flat) or PLACEHOLDER


def _escape_pipes(value: str) -> str:
    """Every ``|`` preceded by an odd number of backslashes: GFM's cell scanner reads
    ``\\\\`` as one escaped backslash, which would leave the pipe live."""
    return re.sub(
        r"(\\*)\|", lambda m: m.group(1) + ("" if len(m.group(1)) % 2 else "\\") + "|", value
    )


def _flat(value: str) -> str:
    """No control or format characters (bidi, zero-width), all whitespace one space."""
    kept = "".join(c for c in value if unicodedata.category(c) not in {"Cc", "Cf"} or c.isspace())
    return " ".join(kept.split())
