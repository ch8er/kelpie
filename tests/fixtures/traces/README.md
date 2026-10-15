# Trace fixtures

Synthetic scenarios for trace analyses that are not rules (no actions), such as huge tool
outputs (`kelpie.traces.outputs`). Same conventions as `../incidents/README.md`: synthetic
ids only, the `snapshot` goes through `kelpie.core.serde.snapshot_from_dict`, spans may use
the fixture-only `repeat` key, and every fixture has a `not_expected` part with a distractor.
`config` holds the analysis settings (for outputs: `[traces.outputs]`); `expected` and
`not_expected` are read by the fixture's own test (`tests/traces/test_outputs.py`).

| File | Analysis | Expected |
|------|----------|----------|
| `field-05-build-log-flood.yaml` | huge tool outputs | `xcodebuild` (ios-developer) ranked first, `xcbeautify` recommended as a repeated offender to the role's owner; `npm test` gets `rtk` (Hermes is supported); `git diff` switched to `rtk` and is verified before/after (over 70% fewer tokens per call); nothing for small `git status` outputs, short file reads or calls without a size |
