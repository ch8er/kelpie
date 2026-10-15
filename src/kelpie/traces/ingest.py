"""OTLP/HTTP span ingest (stub).

Maps ``gen_ai.*`` attributes (``gen_ai.usage.input_tokens``, ``gen_ai.usage.output_tokens``,
``gen_ai.request.model``, ...) onto :class:`kelpie.core.model.Span`.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from kelpie.core.model import Span


def spans_from_otlp(payload: dict[str, Any]) -> Sequence[Span]:
    raise NotImplementedError
