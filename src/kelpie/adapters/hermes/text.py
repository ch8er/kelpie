"""Hermes adapter: the one cleaner for every text Kelpie writes into Hermes.

A stop's ``BLOCKED:`` reason, a park's reason, a wake's ``--reason`` and a comment all pass
through :func:`clean_text` before they reach the Hermes CLI. Rule reasons quote what agents
did (tool arguments, command lines, block reasons), so the text is agent-controlled and must
not carry:

- a GitHub pull request URL: Hermes' ``active_pr`` guard holds a ready task back for 24 hours
  once one appears in a comment or reason, so a URL becomes ``owner/repo#N``;
- control and format characters (C0 but tab and newline, DEL, C1, bidirectional overrides,
  zero-width characters, lone surrogates, line and paragraph separators), which could hide
  or reorder text for the human reading the board; with ``one_line`` tab and newline go too;
- anything that reads as one of Kelpie's signed markers (``kelpie-wait``, ``kelpie-stop``):
  the marker is the last word of a park or stop, and text quoted in front of it carries none.

Each step repeats until nothing changes, so removing one piece cannot assemble another.
"""

from __future__ import annotations

import re
import unicodedata

# As Hermes' active_pr guard (case-insensitive, any owner/repo segment), with its tail.
_PR_URL = re.compile(
    r"https?://[^\s/]+/([^/\s]+)/([^/\s]+)/pull/(\d+)(?:[/#?][^\s)\]>\"']*)?", re.IGNORECASE
)
_ANY_MARKER = re.compile(r"\[?kelpie-(wait|stop)", re.IGNORECASE)
# Control, format, lone surrogate (cannot be passed to the CLI), line and paragraph separator.
_UNSAFE = frozenset({"Cc", "Cf", "Cs", "Zl", "Zp"})
_LINES = frozenset("\t\n")


def _fold_unsafe(text: str, keep: frozenset[str]) -> str:
    """Each run of unsafe characters (but ``keep``) becomes one space."""
    out: list[str] = []
    folding = False
    for c in text:
        if c not in keep and unicodedata.category(c) in _UNSAFE:
            if not folding:
                out.append(" ")
            folding = True
        else:
            out.append(c)
            folding = False
    return "".join(out)


def rewrite_pr_urls(text: str) -> str:
    """GitHub pull request URLs as ``owner/repo#N`` (one pass; :func:`clean_text` repeats)."""
    return _PR_URL.sub(lambda m: f"{m[1]}/{m[2]}#{m[3]}", text)


def clean_text(text: str, limit: int, *, one_line: bool = False) -> str:
    """Text safe to write into Hermes, stripped and at most ``limit`` characters (cut with
    ``...``). ``one_line`` also folds tabs and newlines into spaces."""
    keep = frozenset() if one_line else _LINES
    # Bounded first: each pass of the loop walks the whole text, and nested URLs can make it
    # take one pass per URL. A cut here cannot assemble anything: the loop runs after it.
    text = text[: 4 * limit]
    while True:
        before = text
        text = _fold_unsafe(text, keep)
        text = _ANY_MARKER.sub(lambda m: f"kelpie {m[1].lower()}", text)
        text = rewrite_pr_urls(text)
        if text == before:
            break
    text = text.strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."
