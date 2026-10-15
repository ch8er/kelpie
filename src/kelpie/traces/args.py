"""The tool-arguments hash used for loop detection (rule 10).

Kept byte-for-byte identical to ``args_hash`` in :mod:`kelpie.adapters.hermes.plugin`,
which must stay standard-library only and import nothing from Kelpie. A test asserts that
both produce the same hash, so a tool call reported by OTLP and by a hook hashes the same.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
from typing import Any


def args_hash(args: Any) -> str:
    """First 16 hex digits of the SHA-256 of the canonical JSON of ``args``."""
    try:
        canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        canonical = repr(args)
    return hashlib.sha256(canonical.encode("utf-8", "replace")).hexdigest()[:16]


def args_hash_of_attribute(value: Any) -> str:
    """Hash of a ``gen_ai.tool.call.arguments`` value.

    Instrumentations record arguments as a JSON string or as a structured value. A JSON
    string is parsed first, so ``'{"b": 1, "a": 2}'`` hashes like the dict a hook sees.
    """
    if isinstance(value, str):
        with contextlib.suppress(ValueError):  # not JSON: hash the text itself
            value = json.loads(value)
    return args_hash(value)
