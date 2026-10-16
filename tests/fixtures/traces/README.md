# Trace fixtures

Synthetic scenarios for trace analyses that are not rules (no actions), such as huge tool
outputs (`kelpie.traces.outputs`). Same conventions as `../incidents/README.md`: synthetic
ids only, the `snapshot` goes through `kelpie.core.serde.snapshot_from_dict`, spans may use
the fixture-only `repeat` key, and every fixture has a `not_expected` part with a distractor.
`config` holds the analysis settings (for outputs: `[traces.outputs]`; for tokens:
`prices`); `expected` and `not_expected` are read by the fixture's own test
(`tests/traces/test_<analysis>.py`). The tokens fixture describes model calls compactly
under a fixture-only `calls` key (see its header comment).

| File | Analysis | Expected |
|------|----------|----------|
| `field-05-build-log-flood.yaml` | huge tool outputs | `xcodebuild` (ios-developer) ranked first, `xcbeautify` recommended as a repeated offender to the role's owner; `npm test` gets `rtk` (Hermes is supported); `git diff` switched to `rtk` and is verified before/after (over 70% fewer tokens per call); nothing for small `git status` outputs, short file reads or calls without a size |
| `tokens-cache-context.yaml` | token optimization (`kelpie.traces.tokens`, report section `tokens`) | developer cache breaks twice after `load_skill` (126k missed tokens) and S-T01 runs away to 120k; S-T03 restarts T-T02 from zero (8 steps, 240k tokens); reviewer polling past the cache lifetime (expired) and triage without cache reads (partial); a compaction is neither a break nor a runaway, a resumed session is not a restart, a healthy reviewer has no misses |
