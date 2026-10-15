"""Traces: OTLP/HTTP ingest following the OpenTelemetry GenAI semantic conventions.

- :mod:`kelpie.traces.otlp` decodes ``ExportTraceServiceRequest`` (protobuf and OTLP/JSON);
- :mod:`kelpie.traces.semconv` maps spans onto :class:`kelpie.core.model.Span` and drops
  message content;
- :mod:`kelpie.traces.ingest` serves ``POST /v1/traces`` into a span store
  (:mod:`kelpie.core.spans`);
- :mod:`kelpie.traces.store` feeds stored spans into snapshots and prunes them;
- :mod:`kelpie.traces.args` is the tool-arguments hash shared with platform hooks.

This package builds on ``kelpie.core`` only; the app wires it to the server and metrics.
"""
