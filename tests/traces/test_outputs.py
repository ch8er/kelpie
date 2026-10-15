"""Huge tool outputs: command prefixes, ingest of output sizes, ranking, filter
recommendations and before/after (fixture ``field-05-build-log-flood``)."""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from kelpie.adapters.hermes import plugin
from kelpie.core.model import Span, SpanKind
from kelpie.core.serde import snapshot_from_dict
from kelpie.traces import command as traces_command
from kelpie.traces.command import command_prefix, detect_filter, redact_command
from kelpie.traces.config import load_outputs, load_traces
from kelpie.traces.outputs import (
    DEFAULT_FILTERS,
    Call,
    OutputSettings,
    before_after,
    call_key,
    calls,
    filter_introductions,
    output_tokens,
    rank,
    recommend,
    session_context,
)
from kelpie.traces.semconv import MappingOptions
from tests.incidents import expand_repeats
from tests.traces.otlp_builders import S, sid
from tests.traces.test_semconv import mapped

FIXTURE = Path(__file__).parents[1] / "fixtures" / "traces" / "field-05-build-log-flood.yaml"
T0 = datetime(2026, 3, 2, 9, tzinfo=UTC)


# --- fixture ---------------------------------------------------------------------------


def _load() -> dict[str, Any]:
    data = yaml.safe_load(FIXTURE.read_text())
    assert data["id"] == FIXTURE.stem
    data["snapshot"]["spans"] = expand_repeats(data["snapshot"]["spans"])
    return data


def _matches(item: Any, spec: dict[str, Any]) -> bool:
    return all(getattr(item, k) == v for k, v in spec.items())


def test_build_log_flood_fixture() -> None:
    data = _load()
    settings = load_outputs(data["config"])
    snapshot = snapshot_from_dict(data["snapshot"])
    roles, platforms = session_context(snapshot)
    found = calls(snapshot.spans, roles, platforms)
    groups = rank(found, settings)
    assert _matches(groups[0], data["expected"]["top"])
    recs = recommend(groups, settings)
    flat = [
        {
            "role": r.group.role,
            "key": r.group.key,
            "filter": r.filter.name,
            "repeated": r.repeated,
            "owner": r.owner,
        }
        for r in recs
    ]
    for spec in data["expected"]["recommendations"]:
        assert any(spec.items() <= f.items() for f in flat), (spec, flat)
    for spec in data["not_expected"]["recommendations"]:
        assert not any(spec.items() <= f.items() for f in flat), (spec, flat)
    verified = filter_introductions(found, settings)
    for spec in data["expected"]["before_after"]:
        hits = [
            b
            for b in verified
            if (b.role, b.key, b.filter) == (spec["role"], spec["key"], spec["filter"])
        ]
        assert len(hits) == 1, verified
        assert hits[0].change is not None and hits[0].change < spec["change_below"]
        assert hits[0].small_sample is spec["small_sample"]


def test_fixture_numbers() -> None:
    data = _load()
    snapshot = snapshot_from_dict(data["snapshot"])
    found = calls(snapshot.spans, *session_context(snapshot))
    top = rank(found, load_outputs(data["config"]))[0]
    # 6 calls of 180000 chars and 6 of 176000, at 4 characters per token
    assert (top.calls, top.tokens, top.max_tokens) == (12, 6 * 45000 + 6 * 44000, 45000)
    assert top.estimated and top.platform == "hermes" and top.tasks == ("T-F501",)
    assert len(top.hot_days) == 2
    assert not any(c.key == "make" for c in found)  # no size: not counted


# --- commands ----------------------------------------------------------------------------


def test_redaction_shares_source_with_the_hermes_plugin() -> None:
    assert inspect.getsource(plugin.redact_command) == inspect.getsource(redact_command)
    for name in ("_ASSIGN", "_KEY_VALUE", "_SENSITIVE_KEY", "_URL_USERINFO", "_QUERY_SECRET"):
        assert getattr(plugin, name).pattern == getattr(traces_command, name).pattern
    assert plugin._SECRET_FLAGS == traces_command._SECRET_FLAGS
    assert plugin._LONG_RUN.pattern == traces_command._LONG_RUN.pattern
    assert (plugin._MASK, plugin._MAX_TOKENS) == (traces_command._MASK, traces_command._MAX_TOKENS)


@pytest.mark.parametrize(
    ("command", "prefix"),
    [
        ("xcodebuild -scheme App test", "xcodebuild"),
        ("/usr/bin/xcodebuild test", "xcodebuild test"),
        ("rtk git status", "git status"),
        ("uv run pytest -q", "pytest"),
        ("python3 -m pytest tests", "python3 -m pytest"),
        ("GH_TOKEN=x gh pr checks 5", "gh pr"),
        ("./gradlew build", "gradlew build"),
        ("npm run build", "npm run build"),
        ("npm test", "npm test"),
        ("cat notes.txt", "cat"),
        ("sudo make install", "make install"),
        ("", None),
        ("env", None),
    ],
)
def test_command_prefix(command: str, prefix: str | None) -> None:
    assert command_prefix(command) == prefix


@pytest.mark.parametrize(
    ("command", "used"),
    [
        ("rtk git diff", "rtk"),
        ("~/.local/bin/rtk cargo test", "rtk"),
        ("xcodebuild test 2>&1 | xcbeautify --quiet", "xcbeautify"),
        ("make 2>&1 | /usr/bin/tail -n 50", "tail"),
        ("npm test | grep FAIL", "grep"),
        ("npm test | tee out.log", None),
        ("git status", None),
        ("", None),
        (None, None),
    ],
)
def test_detect_filter(command: str | None, used: str | None) -> None:
    assert detect_filter(command) == used


# --- ingest ------------------------------------------------------------------------------


def _tool(attrs: dict[str, Any], name: str = "execute_tool shell", **kw: Any) -> Span:
    result = mapped([S(sid(1), name=name, attrs=attrs)], **kw)
    assert len(result.spans) == 1
    return result.spans[0]


def test_ingest_measures_output_and_keeps_only_the_prefix() -> None:
    span = _tool(
        {
            "gen_ai.operation.name": "execute_tool",
            "gen_ai.tool.name": "shell",
            "gen_ai.tool.call.arguments": json.dumps(
                {"command": "API_TOKEN=s3cr3tvalue xcodebuild -scheme Secret test | xcbeautify"}
            ),
            "gen_ai.tool.call.result": "x" * 40_000,
        }
    )
    assert span.attributes["kelpie.tool.output_chars"] == 40_000
    assert span.attributes["kelpie.tool.command_prefix"] == "xcodebuild"
    assert span.attributes["kelpie.tool.filter"] == "xcbeautify"
    assert "s3cr3t" not in repr(span) and "Secret" not in repr(span)
    assert output_tokens(span) == (10_000, True)


def test_ingest_structured_result_and_openinference_output() -> None:
    span = _tool({"gen_ai.tool.name": "search", "output.value": "y" * 10})
    assert span.attributes["kelpie.tool.output_chars"] == 10
    assert call_key(span) == "tool:search"


def test_claude_code_result_tokens() -> None:
    span = _tool(
        {"tool_name": "Bash", "full_command": "rtk cargo test --all", "result_tokens": 1234},
        name="claude_code.tool",
    )
    assert span.attributes["kelpie.tool.output_tokens"] == 1234
    assert span.attributes["kelpie.tool.command_prefix"] == "cargo test"
    assert span.attributes["kelpie.tool.filter"] == "rtk"
    assert output_tokens(span) == (1234, False)


def test_exporter_cannot_set_kelpie_tool_details() -> None:
    span = _tool(
        {
            "gen_ai.tool.name": "shell",
            "kelpie.tool.output_tokens": 1,
            "kelpie.tool.filter": "rtk",
            "kelpie.tool.command_prefix": "spoofed",
        }
    )
    for key in ("kelpie.tool.output_tokens", "kelpie.tool.filter", "kelpie.tool.command_prefix"):
        assert key not in span.attributes


def test_commands_can_be_turned_off() -> None:
    attrs = {"gen_ai.tool.name": "shell", "gen_ai.tool.call.arguments": '{"cmd": "npm test"}'}
    assert _tool(attrs).attributes["kelpie.tool.command_prefix"] == "npm test"
    off = _tool(attrs, options=MappingOptions(commands=False))
    assert "kelpie.tool.command_prefix" not in off.attributes
    assert load_traces({"commands": False}).options.commands is False


def test_llm_spans_get_no_tool_details() -> None:
    span = _tool({"gen_ai.operation.name": "chat", "gen_ai.tool.call.result": "zzz"})
    assert span.kind is SpanKind.LLM and "kelpie.tool.output_chars" not in span.attributes


# --- analysis ----------------------------------------------------------------------------


def _call(
    key: str,
    tokens: int,
    at: datetime,
    filt: str | None = None,
    role: str | None = "developer",
    platform: str | None = None,
) -> Call:
    return Call(at, role, key, tokens, False, filt, platform, "T-1", "S-1")


def test_rtk_needs_a_supported_platform() -> None:
    found = [
        _call("cargo test", 9000, T0 + timedelta(minutes=i), platform="paperclip") for i in range(4)
    ]
    [rec] = recommend(rank(found))
    assert rec.filter.name == "errors-only" and rec.platform_known
    found = [_call("cargo test", 9000, T0 + timedelta(minutes=i)) for i in range(4)]
    [rec] = recommend(rank(found))
    assert rec.filter.name == "rtk" and not rec.platform_known  # "if your platform is supported"
    assert rec.saved_tokens == round(4 * 9000 * 0.6)


def test_threshold_and_min_calls() -> None:
    few = [_call("npm test", 50_000, T0 + timedelta(minutes=i)) for i in range(2)]
    small = [_call("npm run lint", 3999, T0 + timedelta(minutes=i)) for i in range(10)]
    assert recommend(rank(few + small)) == []


def test_non_shell_tools_get_the_range_filter() -> None:
    found = [_call("tool:read_file", 20_000, T0 + timedelta(minutes=i)) for i in range(3)]
    [rec] = recommend(rank(found))
    assert rec.filter.name == "range"


def test_groups_are_per_role() -> None:
    found = [
        _call("npm test", 9000, T0 + timedelta(minutes=i), role=role)
        for i in range(3)
        for role in ("developer", "reviewer")
    ]
    assert {g.role for g in rank(found)} == {"developer", "reviewer"}


def test_before_after_labels_small_samples_and_windows() -> None:
    fix = T0 + timedelta(days=1)
    found = [
        _call("npm test", 8000, fix - timedelta(days=2)),  # outside the window
        *[_call("npm test", 8000, fix - timedelta(hours=i + 1)) for i in range(6)],
        *[_call("npm test", 2000, fix + timedelta(hours=i)) for i in range(2)],
    ]
    result = before_after(found, "npm test", fix)
    assert (result.before.calls, result.after.calls) == (6, 2)
    assert result.change == pytest.approx(-0.75) and result.small_sample
    assert before_after([], "npm test", fix).change is None


def test_filter_introduction_needs_earlier_unfiltered_calls() -> None:
    only_filtered = [_call("git diff", 500, T0 + timedelta(minutes=i), "rtk") for i in range(6)]
    assert filter_introductions(only_filtered) == []


def test_default_filter_table_order() -> None:
    assert [f.name for f in DEFAULT_FILTERS] == ["rtk", "xcbeautify", "errors-only", "range"]
    assert not DEFAULT_FILTERS[0].matches("xcodebuild")
    assert DEFAULT_FILTERS[0].matches("python3 -m pytest")


# --- config ------------------------------------------------------------------------------


def test_outputs_config() -> None:
    settings = load_outputs(
        {
            "threshold_tokens": 2000,
            "window": "12h",
            "owners": {"developer": " human-owner "},
            "filters": [
                {
                    "name": "custom",
                    "how": "pipe through our formatter",
                    "commands": ["make"],
                    "reduction": 0.4,
                    "platforms": ["Claude_Code"],
                }
            ],
        }
    )
    assert settings.threshold_tokens == 2000 and settings.window == timedelta(hours=12)
    assert settings.owners == {"developer": "human-owner"}
    assert settings.filters[0].platforms == ("claude-code",)
    assert load_outputs(None) == OutputSettings()


@pytest.mark.parametrize(
    "section",
    [
        [],
        {"bogus": 1},
        {"threshold_tokens": 0},
        {"min_calls": True},
        {"window": "0m"},
        {"window": "soon"},
        {"owners": {"developer": ""}},
        {"filters": []},
        {"filters": [{"name": "x", "how": "y", "commands": ["make"], "reduction": 1}]},
        {"filters": [{"name": "x", "how": "y", "commands": [], "reduction": 0.5}]},
        {"filters": [{"name": "", "how": "y", "commands": ["make"], "reduction": 0.5}]},
        {"filters": [{"name": "x", "how": "y", "commands": ["make"], "reduction": "half"}]},
        {"filters": [{"name": "x", "how": "y", "commands": ["make"], "reduction": 0.5, "z": 1}]},
    ],
)
def test_outputs_config_errors(section: Any) -> None:
    with pytest.raises(ValueError, match=r"traces\.outputs"):
        load_outputs(section)


def test_traces_commands_flag_is_validated() -> None:
    with pytest.raises(ValueError, match="commands"):
        load_traces({"commands": "yes"})


# --- Hermes hook -------------------------------------------------------------------------


def test_hermes_plugin_records_output_size_not_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spool = tmp_path / "spool.jsonl"
    monkeypatch.setenv(plugin.SPOOL_ENV, str(spool))
    plugin.post_tool_call(tool_name="terminal", args={"command": "npm test"}, result="s3cret" * 10)
    plugin.post_tool_call(tool_name="read", args={}, result={"lines": ["a", "b"]})
    plugin.post_tool_call(tool_name="noop", args={})
    first, second, third = (json.loads(x) for x in spool.read_text().splitlines())
    assert first["output_chars"] == 60 and "s3cret" not in spool.read_text()
    assert second["output_chars"] == len('{"lines":["a","b"]}')
    assert third["output_chars"] is None
