"""Known runners anywhere in a command (#197 part 2b): one shared block in four files, names
from the configured list only, never a word of the command."""

from __future__ import annotations

import io
import json
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.claude_code import adapter as claude_adapter
from kelpie.adapters.claude_code import hook
from kelpie.adapters.hermes import adapter as hermes_adapter
from kelpie.adapters.hermes import plugin
from kelpie.adapters.openclaw import adapter as openclaw_adapter
from kelpie.adapters.openclaw.plugin import kelpie_hook as openclaw_hook
from kelpie.core.model import Span, SpanKind
from kelpie.traces import command, otlp, semconv
from kelpie.traces.command import parse_runners, runners_of, spool_runners
from kelpie.traces.config import load_traces
from kelpie.traces.otlp import PROTOBUF
from kelpie.traces.outputs import call_key, calls, rank
from kelpie.traces.semconv import RUNNERS_ATTRIBUTE, MappingOptions, map_spans
from tests.traces.otlp_builders import S, request, sid

BEGIN, END = "# --- shared runners: begin ---", "# --- shared runners: end ---"
MODULES = {"traces": command, "hermes": plugin, "claude_code": hook, "openclaw": openclaw_hook}
COPIES: dict[str, Callable[[object, tuple[str, ...]], list[str]]] = {
    name: m.runners_of for name, m in MODULES.items()
}
KNOWN = ("ios_sim.sh", "xcodebuild", "npm test", "gradlew", "make test")
SECRETS = ("hunter2", "Secr3tPassw0rd", "ghp_Zx9Qe7Lm2Vb4Nc6Ty8Uh1Jk3Wq5Er7", "prod-db01")


def test_the_copies_are_identical() -> None:
    blocks = {}
    for name, m in MODULES.items():
        text = Path(m.__file__ or "").read_text()
        blocks[name] = text[text.index(BEGIN) : text.index(END)]
    assert len(set(blocks.values())) == 1
    assert "def runners_of" in blocks["traces"] and "def parse_runners" in blocks["traces"]


@pytest.mark.parametrize("copy", COPIES)
@pytest.mark.parametrize(
    ("cmd", "found"),
    [
        ("export DEVELOPER_DIR=/A/x.app && ./scripts/ios_sim.sh test", ["ios_sim.sh"]),
        (
            "export DEVELOPER_DIR=/A/x.app && ./scripts/ios_sim.sh test xcodebuild -x",
            ["ios_sim.sh", "xcodebuild"],
        ),
        ("cd /repo && xcodebuild -scheme App test", ["xcodebuild"]),
        ("cd /repo/app && npm test -- --watch=false", ["npm test"]),
        ("env CI=1 npm test", ["npm test"]),
        ("sudo -u builder env A=1 /opt/tools/gradlew build", ["gradlew"]),
        ("cat plan.txt | xcodebuild test 2>&1 | tail -50", ["xcodebuild"]),
        ("bash -c 'cd ios && ./ios_sim.sh run'", ["ios_sim.sh"]),
        ('sh -c "make test"', ["make test"]),
        ("timeout 600 ./ios_sim.sh", ["ios_sim.sh"]),
        ("set -e\ncd ios\n./ios_sim.sh test", ["ios_sim.sh"]),  # any line
        ("(cd x; xcodebuild)", ["xcodebuild"]),
        ("ios_sim.sh; ios_sim.sh; ios_sim.sh", ["ios_sim.sh"]),  # once
        # near misses: no runner
        ("npm run test", []),
        ("npm", []),
        ("npm tests", []),
        ("./ios_sim.sh.bak", []),
        ("xcodebuild2 test", []),
        ("XCODEBUILD test", []),
        ("make check", []),
        # a runner's words never match across a separator or a line
        ("npm\ntest", []),
        ("echo npm; test -f x", []),
        ("npm | test", []),
        ("npm && test", []),
        ("(npm) test", []),
        # review of #331: only spaces and tabs split words, as bash does
        ("npm\ttest", ["npm test"]),
        ("npm\rtest", []),
        ("npm\x0btest", []),
        ("npm\x0ctest", []),
        ("npm\xa0test", []),
        ("npm\u2003test", []),
        ("", []),
    ],
)
def test_runners_anywhere(copy: str, cmd: str, found: list[str]) -> None:
    assert COPIES[copy](cmd, KNOWN) == found


@pytest.mark.parametrize("copy", COPIES)
def test_names_only_never_the_command(copy: str) -> None:
    """Whatever the command holds, the result is names from the configured list."""
    rng = random.Random(197)
    pool = ["ios_sim.sh", "./ios_sim.sh", "xcodebuild", "npm", "test", "make", "&&", "|",
            ";", "'", '"', "$(", ")", "=", "cd", "/x/", "-u", "\n", "\\", "`",
            *SECRETS]  # fmt: skip
    for _ in range(3000):
        cmd = " ".join(rng.choice(pool) for _ in range(rng.randint(1, 12)))
        out = COPIES[copy](cmd, KNOWN)
        assert set(out) <= set(KNOWN) and len(out) == len(set(out)), cmd
        assert not any(s in " ".join(out) for s in SECRETS)


def test_without_known_runners_nothing_is_found() -> None:
    assert all(f("./ios_sim.sh", ()) == [] for f in COPIES.values())
    assert runners_of(None, KNOWN) == [] and runners_of(["ios_sim.sh"], KNOWN) == []


def test_at_most_max_runners_and_fast() -> None:
    known = tuple(f"r{i}" for i in range(20))
    assert runners_of(" ".join(known), known) == list(known[: command.MAX_RUNNERS])
    import time

    start = time.monotonic()
    runners_of("ios_sim.sh " * 2_000_000, KNOWN)
    runners_of("a" * 5_000_000, KNOWN)
    assert time.monotonic() - start < 1


@pytest.mark.parametrize(
    ("value", "parsed"),
    [
        ("ios_sim.sh,xcodebuild, npm  test ,", ("ios_sim.sh", "xcodebuild", "npm test")),
        ("", ()),
        ("xcodebuild,xcodebuild", ("xcodebuild",)),
        # skipped: not a runner name (a path, a value, quotes, too many words, capitals)
        ("/usr/bin/x,A=hunter2,'npm',npm run build x,npm Test", ()),
        (["ios_sim.sh", 3, None], ("ios_sim.sh",)),
        (None, ()),
    ],
)
def test_parse_runners(value: object, parsed: tuple[str, ...]) -> None:
    assert all(m.parse_runners(value) == parsed for m in MODULES.values())


def test_parse_runners_caps_the_list() -> None:
    many = ",".join(f"r{i}" for i in range(100))
    assert len(parse_runners(many)) == command.MAX_KNOWN_RUNNERS


# --- the hooks: a "runners" field, only when configured --------------------------------------


@pytest.fixture
def spools(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Path]:
    paths = {name: tmp_path / name / "spool.jsonl" for name in ("hermes", "claude", "openclaw")}
    monkeypatch.setenv(plugin.SPOOL_ENV, str(paths["hermes"]))
    monkeypatch.setenv(hook.SPOOL_ENV, str(paths["claude"]))
    monkeypatch.setenv(openclaw_hook.SPOOL_ENV, str(paths["openclaw"]))
    monkeypatch.setattr(hook, "claude_process", dict)
    return paths


CMD = "export DEVELOPER_DIR=/Applications/X.app TOKEN=hunter2 && ./scripts/ios_sim.sh test"


def _write_all(cmd: str) -> None:
    plugin.post_tool_call(tool_name="terminal", args={"command": cmd})
    hook.main(
        io.StringIO(
            json.dumps(
                {
                    "hook_event_name": "PostToolUse",
                    "session_id": "0b5c6f7e-0000-4000-8000-000000000001",
                    "tool_name": "Bash",
                    "tool_input": {"command": cmd},
                    "tool_use_id": "toolu_01",
                }
            )
        )
    )
    openclaw_hook.main(
        io.StringIO(
            json.dumps(
                {
                    "hook": "after_tool_call",
                    "at": 1792191669.5,
                    "event": {"toolName": "exec", "params": {"command": cmd}},
                    "ctx": {"sessionId": "1ce1fbe0-c1fc-4572-91c4-394ad62e4a40"},
                }
            )
        )
    )


def _tool_lines(paths: dict[str, Path]) -> dict[str, dict[str, Any]]:
    out = {}
    for name, path in paths.items():
        recs = [json.loads(x) for x in path.read_text().splitlines()]
        (out[name],) = [r for r in recs if r.get("type") == "tool"]
    return out


def test_hooks_record_known_runners(
    spools: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(command.RUNNERS_ENV, "ios_sim.sh,xcodebuild,npm test")
    _write_all(CMD)
    for name, rec in _tool_lines(spools).items():
        assert rec["runners"] == ["ios_sim.sh"], name
        assert rec["command"] == "export *** && ios_sim.sh test", name
    text = "".join(p.read_text() for p in spools.values())
    assert "hunter2" not in text and "Applications" not in text


def test_hooks_write_no_runners_unless_configured(spools: dict[str, Path]) -> None:
    _write_all(CMD)
    assert all("runners" not in rec for rec in _tool_lines(spools).values())


def test_hooks_write_no_runners_when_none_is_found(
    spools: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(command.RUNNERS_ENV, "xcodebuild")
    _write_all(CMD)
    assert all("runners" not in rec for rec in _tool_lines(spools).values())


def test_hooks_write_no_runners_when_commands_are_off(
    spools: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(command.RUNNERS_ENV, "ios_sim.sh")
    for env in (plugin.COMMANDS_ENV, hook.COMMANDS_ENV, openclaw_hook.COMMANDS_ENV):
        monkeypatch.setenv(env, "0")
    _write_all(CMD)
    for rec in _tool_lines(spools).values():
        assert "runners" not in rec and rec.get("command") is None


def test_a_bad_known_runners_value_never_breaks_a_hook(
    spools: dict[str, Path], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv(command.RUNNERS_ENV, "\x01,'x',/bin/sh," + "a" * 10_000)
    _write_all(CMD)
    assert all("runners" not in rec for rec in _tool_lines(spools).values())


# --- the readers ------------------------------------------------------------------------------

NOW = datetime(2026, 11, 3, 12, tzinfo=UTC)
LONG_AGO = NOW - timedelta(days=1)


def _line(**fields: Any) -> str:
    rec = {"type": "tool", "at": NOW.timestamp(), "session_id": "s-1", "tool": "terminal"}
    return json.dumps({**rec, **fields})


@pytest.mark.parametrize(
    ("runners", "kept"),
    [
        (["ios_sim.sh", "npm test"], ["ios_sim.sh", "npm test"]),
        (["ios_sim.sh", "ios_sim.sh"], ["ios_sim.sh"]),
        # forged lines (agents can write the spool): only runner names survive
        (["TOKEN=hunter2", "/home/a/x", "a b c d", 5, None, "x" * 300], []),
        ("ios_sim.sh", []),
        ({"a": 1}, []),
        ([f"r{i}" for i in range(20)], [f"r{i}" for i in range(8)]),
        # review of #331: a name is kept only as its command prefix, nothing more
        (["mysql password hunter2"], ["mysql"]),
        (["ssh prod-db01 x"], ["ssh"]),
        (["mytool sub", "mytool"], ["mytool"]),
        (["npm test\n"], []),
        (["npm test", "make test", "npm run build"], ["npm test", "make test", "npm run build"]),
    ],
)
def test_readers_keep_runner_names_only(runners: object, kept: list[str]) -> None:
    assert list(spool_runners(runners)) == kept
    line = _line(runners=runners, command="export *** && sim.sh test")
    hermes = hermes_adapter._event_from_line(line, LONG_AGO)
    claude = claude_adapter.parse_line(line, LONG_AGO)
    openclaw = openclaw_adapter.parse_line(line, LONG_AGO)
    assert hermes is not None and claude is not None and openclaw is not None
    assert hermes.attributes.get(RUNNERS_ATTRIBUTE, []) == kept
    assert list(claude.runners) == kept and list(openclaw.runners) == kept


def test_hermes_reader_without_runners_has_no_attribute() -> None:
    event = hermes_adapter._event_from_line(_line(command="make test"), LONG_AGO)
    assert event is not None and RUNNERS_ATTRIBUTE not in event.attributes


# --- OTLP ingest: [traces] known_runners -----------------------------------------------------


def _tool_span(cmd: str, **extra: Any) -> list[otlp.RawSpan]:
    attrs = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "shell",
        "gen_ai.tool.call.arguments": json.dumps({"command": cmd}),
        **extra,
    }
    return otlp.decode(request([S(sid(1), name="execute_tool shell", attrs=attrs)]), PROTOBUF).spans


def test_ingest_records_known_runners() -> None:
    opts = MappingOptions(known_runners=("ios_sim.sh", "xcodebuild"))
    (span,) = map_spans(_tool_span(CMD), opts).spans
    assert span.attributes[RUNNERS_ATTRIBUTE] == ["ios_sim.sh"]
    assert span.attributes["kelpie.tool.command_prefix"] == "ios_sim.sh"
    assert "hunter2" not in repr(span) and "Applications" not in repr(span)


def test_ingest_without_known_runners_or_commands_records_none() -> None:
    (plain,) = map_spans(_tool_span(CMD)).spans
    assert RUNNERS_ATTRIBUTE not in plain.attributes
    off = MappingOptions(commands=False, known_runners=("ios_sim.sh",))
    (span,) = map_spans(_tool_span(CMD), off).spans
    assert RUNNERS_ATTRIBUTE not in span.attributes


def test_ingest_never_takes_runners_from_the_sender() -> None:
    raw = _tool_span("ls", **{RUNNERS_ATTRIBUTE: "hunter2"})
    (span,) = map_spans(raw, MappingOptions(known_runners=("ios_sim.sh",))).spans
    assert RUNNERS_ATTRIBUTE not in span.attributes
    assert {RUNNERS_ATTRIBUTE} <= semconv.RESERVED_KEYS


def test_config() -> None:
    assert load_traces({}).known_runners == ()
    loaded = load_traces({"known_runners": ["ios_sim.sh", "npm  test", "ios_sim.sh"]})
    assert loaded.known_runners == ("ios_sim.sh", "npm test")
    assert loaded.options.known_runners == ("ios_sim.sh", "npm test")


@pytest.mark.parametrize(
    "value",
    [
        "ios_sim.sh",
        [1],
        ["/usr/bin/x"],
        ["A=hunter2"],
        ["npm run build x"],
        ["r"] * 65,
        ["mytool sub"],
        ["ssh prod-db01"],
        ["npm test\n x"],
    ],
)
def test_config_rejects_bad_runners(value: object) -> None:
    with pytest.raises(ValueError, match=r"\[traces\] known_runners"):
        load_traces({"known_runners": value})


# --- the outputs ranking ---------------------------------------------------------------------


def _call(at: int, **attributes: Any) -> Span:
    return Span(
        trace_id="t",
        span_id=f"s{at}",
        name="execute_tool terminal",
        kind=SpanKind.TOOL,
        start=NOW + timedelta(minutes=at),
        end=NOW + timedelta(minutes=at, seconds=1),
        session_id="sess",
        tool="terminal",
        attributes={"kelpie.tool.output_chars": 40_000, **attributes},
    )


def test_call_key_prefers_a_known_runner() -> None:
    assert (
        call_key(_call(0, command="export *** && cd ***", **{RUNNERS_ATTRIBUTE: ["ios_sim.sh"]}))
        == "ios_sim.sh"
    )
    # the prefix already is the runner, or more specific: kept
    assert call_key(_call(0, command="npm test", **{RUNNERS_ATTRIBUTE: ["npm"]})) == "npm test"
    assert (
        call_key(_call(0, command="xcodebuild test", **{RUNNERS_ATTRIBUTE: ["xcodebuild"]}))
        == "xcodebuild test"
    )
    # nothing changes without runners, or with a forged value
    assert call_key(_call(0, command="export *** && cd ***")) == "cd"
    assert call_key(_call(0, command="bash ***", **{RUNNERS_ATTRIBUTE: "ios_sim.sh"})) == "bash"
    assert call_key(_call(0, command="bash ***", **{RUNNERS_ATTRIBUTE: ["a b c d e"]})) == "bash"
    # a span from a remote agent arrives as sent: a name counts only as its command prefix
    forged = {RUNNERS_ATTRIBUTE: ["mysql password hunter2"]}
    assert call_key(_call(0, command="bash ***", **forged)) == "mysql"
    assert call_key(_call(0, command="bash ***", **{RUNNERS_ATTRIBUTE: ["uv run"]})) == "bash"
    assert call_key(_call(0, command="bash ***", **{RUNNERS_ATTRIBUTE: ["npm test\n"]})) == "bash"


def test_ranking_groups_a_runner_wherever_it_runs() -> None:
    spans = [
        _call(0, command="export *** && sim.sh test", **{RUNNERS_ATTRIBUTE: ["ios_sim.sh"]}),
        _call(1, command="bash -c ***", **{RUNNERS_ATTRIBUTE: ["ios_sim.sh"]}),
        _call(2, **{"kelpie.tool.command_prefix": "timeout", RUNNERS_ATTRIBUTE: ["ios_sim.sh"]}),
        _call(3, command="ls ***"),
    ]
    groups = rank(calls(spans))
    assert [(g.key, g.calls) for g in groups] == [("ios_sim.sh", 3), ("ls", 1)]
    plain = rank(calls([replace_runners(s) for s in spans]))
    assert {g.key for g in plain} == {"sim.sh", "bash", "timeout", "ls"}


def replace_runners(span: Span) -> Span:
    from dataclasses import replace

    kept = {k: v for k, v in span.attributes.items() if k != RUNNERS_ATTRIBUTE}
    return replace(span, attributes=kept)
