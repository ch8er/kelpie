"""Per-agent ingest tokens (#302, audit M1): config, ingest scope checks, the snapshot guard
and the forged-session fixture."""

from __future__ import annotations

import io
import json
from collections import Counter
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import yaml

from kelpie.core.actions import ActionKind
from kelpie.core.model import Owner, Session, Snapshot, Span, SpanKind, Task
from kelpie.core.serde import snapshot_from_dict
from kelpie.core.spans import MemorySpanStore, span_key
from kelpie.rules import REGISTRY
from kelpie.traces.config import load_traces
from kelpie.traces.ingest import Reply, ScopedToken, TraceIngest
from kelpie.traces.otlp import PROTOBUF
from kelpie.traces.scope import (
    OUT_OF_SCOPE,
    SESSION,
    TOKEN_ATTRIBUTE,
    Place,
    ScopeGuard,
    ScopeIndex,
    TokenScope,
)
from tests.traces.otlp_builders import NOW, S, request, sid
from tests.traces.test_ingest import Headers, Observer

FIXTURE = Path(__file__).parents[1] / "fixtures" / "traces" / "m1-forged-session.yaml"
T1 = "dev-one-token-0123456789abcdef"
T2 = "dev-two-token-0123456789abcdef"
SHARED = "shared-token-0123456789abcdef"


def tool(n: int, session: str | None, task: str | None, args: str = "ls", **kw: Any) -> S:
    attrs: dict[str, Any] = {
        "gen_ai.operation.name": "execute_tool",
        "gen_ai.tool.name": "shell",
        "gen_ai.tool.call.arguments": json.dumps({"command": args}),
    }
    if session is not None:
        attrs["kelpie.session_id"] = session
    if task is not None:
        attrs["kelpie.task_id"] = task
    attrs.update(kw)
    return S(sid(n), name="execute_tool shell", attrs=attrs, start=NOW - timedelta(seconds=60 - n))


def board() -> Snapshot:
    return Snapshot(
        now=NOW,
        owners=(Owner("agent-dev-1", "developer"), Owner("agent-dev-2", "developer-b")),
        tasks=(
            Task("T-101", "a", "in_progress", NOW, owner_id="agent-dev-1", board="main"),  # type: ignore[arg-type]
            Task("T-102", "b", "in_progress", NOW, owner_id="agent-dev-2"),  # type: ignore[arg-type]
        ),
        sessions=(
            Session("S-101", "T-101", "agent-dev-1", NOW - timedelta(hours=1)),
            Session("S-102", "T-102", "agent-dev-2", NOW - timedelta(hours=1)),
        ),
    )


SCOPES = {
    "dev-1": TokenScope("dev-1", owners=frozenset({"agent-dev-1"})),
    "dev-2": TokenScope("dev-2", roles=frozenset({"developer-b"}), sessions=("new-2-*",)),
}


def setup(
    shared: str | None = None, scopes: dict[str, TokenScope] = SCOPES
) -> tuple[TraceIngest, MemorySpanStore, Observer, ScopeGuard]:
    store, observer = MemorySpanStore(), Observer()
    guard = ScopeGuard(scopes, store=store, rejected=observer.spans_rejected)
    guard.index.update(board())
    tokens = {"dev-1": T1, "dev-2": T2}
    ingest = TraceIngest(
        store=store,
        clock=lambda: NOW,
        observer=observer,
        token=shared,
        scoped=[ScopedToken(n, tokens[n]) for n in scopes],
        guard=guard,
    )
    return ingest, store, observer, guard


def post(target: TraceIngest, spans: list[S], token: str | None) -> Reply:
    body = request(spans)
    h = Headers({"Content-Type": PROTOBUF, "Content-Length": str(len(body))})
    if token is not None:
        h["Authorization"] = f"Bearer {token}"
    return target.handle(h, io.BytesIO(body).read)


def stored(store: MemorySpanStore) -> list[Span]:
    return list(store.spans_since(NOW - timedelta(days=1)))


# --- ingest --------------------------------------------------------------------------------


def test_forged_session_under_own_token_is_rejected_and_counted() -> None:
    ingest, store, observer, _ = setup()
    reply = post(ingest, [tool(1, "S-101", "T-101")], T2)
    assert reply.status == 200  # partial success: the export itself was fine
    assert stored(store) == []
    assert observer.rejected == Counter({OUT_OF_SCOPE: 1})


def test_own_session_is_accepted_and_stamped() -> None:
    ingest, store, observer, _ = setup()
    assert post(ingest, [tool(1, "S-101", "T-101")], T1).status == 200
    (span,) = stored(store)
    assert span.attributes[TOKEN_ATTRIBUTE] == "dev-1"
    assert not observer.rejected


def test_scope_by_role_board_and_session_pattern() -> None:
    scopes = {
        "dev-1": TokenScope("dev-1", boards=frozenset({"main"})),
        "dev-2": TokenScope("dev-2", roles=frozenset({"developer-b"}), sessions=("new-2-*",)),
    }
    ingest, store, observer, _ = setup(scopes=scopes)
    post(ingest, [tool(1, "S-101", "T-101")], T1)  # T-101 is on board "main"
    post(ingest, [tool(2, "S-102", "T-102")], T2)  # the owner's role
    post(ingest, [tool(3, "new-2-x", None)], T2)  # a pattern, unknown to the board
    post(ingest, [tool(4, "S-102", "T-102")], T1)  # T-102 is on no board: rejected
    assert sorted(s.span_id for s in stored(store)) == [sid(1), sid(2), sid(3)]
    assert observer.rejected == Counter({OUT_OF_SCOPE: 1})


def test_task_without_session_is_checked_by_its_task() -> None:
    ingest, store, observer, _ = setup()
    post(ingest, [tool(1, None, "T-101")], T2)
    assert stored(store) == [] and observer.rejected[OUT_OF_SCOPE] == 1


def test_unknown_session_is_accepted_provisionally() -> None:
    ingest, store, _, _ = setup()
    post(ingest, [tool(1, "S-999", None)], T2)
    (span,) = stored(store)
    assert span.attributes[TOKEN_ATTRIBUTE] == "dev-2"


def test_with_scoped_tokens_ingest_needs_a_token() -> None:
    ingest, store, _, _ = setup()
    assert post(ingest, [tool(1, "S-101", "T-101")], None).status == 401
    assert post(ingest, [tool(1, "S-101", "T-101")], "wrong-token-0123456789").status == 401
    assert stored(store) == []


def test_shared_token_keeps_working_unscoped_and_cannot_claim_a_scope() -> None:
    ingest, store, observer, _ = setup(shared=SHARED)
    span = tool(1, "S-101", "T-101", **{TOKEN_ATTRIBUTE: "dev-2"})
    assert post(ingest, [span], SHARED).status == 200
    (kept,) = stored(store)
    assert TOKEN_ATTRIBUTE not in kept.attributes
    assert not observer.rejected


def test_scoped_token_needs_its_scope() -> None:
    with pytest.raises(ValueError, match="scope"):
        TraceIngest(MemorySpanStore(), lambda: NOW, scoped=[ScopedToken("x", T1)])


def test_scoped_token_from_its_file_rotates(tmp_path: Path) -> None:
    from kelpie.traces.auth import TokenFile, rotate

    path = tmp_path / "dev-1.env"
    rotate(path, "KELPIE_TRACES_TOKEN_DEV_1", timedelta(0), NOW)
    clock = [NOW]
    file = TokenFile(path, "KELPIE_TRACES_TOKEN_DEV_1", lambda: clock[0])
    old = path.read_text().split("=", 1)[1].strip()
    store, observer = MemorySpanStore(), Observer()
    guard = ScopeGuard(SCOPES)
    guard.index.update(board())
    ingest = TraceIngest(
        store, lambda: NOW, observer, scoped=[ScopedToken("dev-1", file=file)], guard=guard
    )
    assert post(ingest, [tool(1, "S-101", "T-101")], old).status == 200
    rotate(path, "KELPIE_TRACES_TOKEN_DEV_1", timedelta(hours=1), NOW)
    clock[0] = NOW + timedelta(seconds=5)
    new = next(
        line.split("=", 1)[1].strip()
        for line in path.read_text().splitlines()
        if line.startswith("KELPIE_TRACES_TOKEN_DEV_1=")
    )
    assert new != old
    assert post(ingest, [tool(2, "S-101", "T-101")], new).status == 200
    assert post(ingest, [tool(3, "S-101", "T-101")], old).status == 200  # in its grace
    assert len(stored(store)) == 3


# --- the snapshot guard --------------------------------------------------------------------


def test_guard_drops_provisional_spans_once_the_board_places_them_elsewhere() -> None:
    ingest, store, observer, guard = setup()
    post(ingest, [tool(1, "S-103", "T-103"), tool(2, "S-102", "T-102")], T2)
    assert len(stored(store)) == 2  # S-103 is not on the board yet
    later = board()
    later = replace(
        later,
        tasks=(*later.tasks, Task("T-103", "c", "in_progress", NOW, owner_id="agent-dev-1")),  # type: ignore[arg-type]
        sessions=(*later.sessions, Session("S-103", "T-103", "agent-dev-1", NOW)),
        spans=tuple(stored(store)),
    )
    checked, _ = guard.check(later)
    assert [s.session_id for s in checked.spans] == ["S-102"]
    assert [s.session_id for s in stored(store)] == ["S-102"]  # deleted from the store
    assert observer.rejected == Counter({OUT_OF_SCOPE: 1})


def test_guard_leaves_shared_and_hook_spans_alone() -> None:
    guard = ScopeGuard(SCOPES)
    plain = Span("t", "a", "x", SpanKind.TOOL, NOW, session_id="S-101")
    gone = replace(plain, span_id="b", attributes={TOKEN_ATTRIBUTE: "gone"})  # removed token
    unplaced = replace(gone, span_id="c", session_id="S-999")
    forged = replace(plain, span_id="d", attributes={TOKEN_ATTRIBUTE: "dev-2"})
    checked, _ = guard.check(replace(board(), spans=(plain, gone, unplaced, forged)))
    assert [span_key(s) for s in checked.spans] == [("t", "a"), ("t", "c")]


def test_a_removed_tokens_spans_go_once_the_board_places_them() -> None:
    store = MemorySpanStore()
    mark = {TOKEN_ATTRIBUTE: "old"}
    forged = Span("t", "a", "x", SpanKind.TOOL, NOW, session_id="S-9", attributes=mark)
    chat = replace(forged, span_id="b", session_id="chat-1")
    store.append_spans([forged, chat], NOW)
    guard = ScopeGuard(SCOPES, store=store)  # a restart without the "old" entry
    assert guard.restore(NOW - timedelta(days=7), NOW) == 2
    guard.check(later_board(("S-9", "T-9", "agent-dev-1")))
    assert [s.session_id for s in stored(store)] == ["chat-1"]  # history while unplaced


def test_index_keeps_sessions_the_board_dropped_for_a_day() -> None:
    index = ScopeIndex()
    index.update(board())
    gone = Snapshot(now=NOW + timedelta(hours=2))
    index.update(gone)
    assert index.session("S-101") == (Place("agent-dev-1", "developer", "main"), "T-101")
    index.update(Snapshot(now=NOW + timedelta(hours=25)))
    assert index.session("S-101") is None


# --- config --------------------------------------------------------------------------------


def test_config_parses_scoped_tokens() -> None:
    traces = load_traces(
        {
            "tokens": [
                {"name": "dev-1", "token_env": "KELPIE_TRACES_TOKEN_DEV_1", "owners": ["a"]},
                {
                    "name": "dev-2",
                    "token_env": "KELPIE_TRACES_TOKEN_DEV_2",
                    "token_file": "~/dev-2.env",
                    "roles": ["r"],
                    "sessions": ["s-*"],
                    "boards": ["b"],
                },
            ]
        }
    )
    one, two = traces.tokens
    assert one.scope == TokenScope("dev-1", owners=frozenset({"a"}))
    assert two.token_file is not None and two.token_file.name == "dev-2.env"
    assert two.scope.sessions == ("s-*",) and two.scope.boards == frozenset({"b"})
    assert traces.scoped("dev-2") is two and traces.scoped("x") is None
    assert one.token({"KELPIE_TRACES_TOKEN_DEV_1": " t "}) == "t"
    with pytest.raises(ValueError, match=r"\$KELPIE_TRACES_TOKEN_DEV_1 is not set"):
        one.token({})


@pytest.mark.parametrize(
    ("entry", "match"),
    [
        ({"name": "a", "token_env": "A"}, "owners, roles, sessions or boards"),
        ({"name": "a", "owners": ["x"]}, "token_env is required"),
        ({"name": "a", "token_env": "KELPIE_TRACES_TOKEN", "owners": ["x"]}, "shared"),
        ({"name": "a", "token_env": "A", "sessions": ["*"]}, "every session"),
        ({"name": "a", "token_env": "A", "owners": "x"}, "list"),
        ({"name": "a", "token_env": "A", "owners": ["x"], "extra": 1}, "unknown"),
        ({"name": "", "token_env": "A", "owners": ["x"]}, "name"),
        ({"name": "a", "token_env": "a8f3k2m9q7x1z5w0p4r6", "owners": ["x"]}, "looks like"),
    ],
)
def test_config_rejects_bad_entries(entry: dict[str, Any], match: str) -> None:
    with pytest.raises(ValueError, match=match):
        load_traces({"tokens": [entry]})


def test_config_rejects_duplicates() -> None:
    a = {"name": "a", "token_env": "A", "owners": ["x"]}
    with pytest.raises(ValueError, match="unique"):
        load_traces({"tokens": [a, {**a, "token_env": "B"}]})
    with pytest.raises(ValueError, match="own token_env"):
        load_traces({"tokens": [a, {**a, "name": "b"}]})


def test_exposure_needs_strong_scoped_tokens() -> None:
    traces = load_traces({"tokens": [{"name": "a", "token_env": "A", "owners": ["x"]}]})
    traces.check_exposure("0.0.0.0", {"A": T1})  # no shared token: still authenticated
    with pytest.raises(ValueError, match="at least 16"):
        traces.check_exposure("0.0.0.0", {"A": "short"})


# --- the fixture ---------------------------------------------------------------------------


def test_m1_forged_session_fixture() -> None:
    fixture = yaml.safe_load(FIXTURE.read_text())
    assert fixture["id"] == FIXTURE.stem
    snapshot = snapshot_from_dict(fixture["snapshot"])
    now: datetime = snapshot.now
    scopes = {}
    for entry in fixture["config"]["tokens"]:
        scopes[entry["name"]] = TokenScope(
            entry["name"],
            owners=frozenset(entry.get("owners", ())),
            roles=frozenset(entry.get("roles", ())),
        )
    secrets = {name: f"{name}-secret-0123456789abcdef" for name in scopes}
    store, observer = MemorySpanStore(), Observer()
    guard = ScopeGuard(scopes, store=store, rejected=observer.spans_rejected)
    guard.index.update(snapshot)
    ingest = TraceIngest(
        store,
        lambda: now,
        observer,
        scoped=[ScopedToken(n, secrets[n]) for n in scopes],
        guard=guard,
    )
    accepted: Counter[str] = Counter()
    n = 0
    for req in fixture["requests"]:
        spans = []
        for i in range(req["calls"]):
            n += 1
            span = tool(n, req["session_id"], req["task_id"], args=f"cat notes-{req['token']}.txt")
            spans.append(replace(span, start=now - timedelta(minutes=5) + timedelta(seconds=i)))
        before = len(stored(store))
        assert post(ingest, spans, secrets[req["token"]]).status == 200
        accepted[req["token"]] += len(stored(store)) - before
    assert dict(accepted) == fixture["expected"]["accepted"]
    assert dict(observer.rejected) == fixture["expected"]["rejected"]

    board_, _ = guard.check(replace(snapshot, spans=tuple(stored(store))))
    rule = REGISTRY["loop_breaker"]({})
    stops = {a.params["session_id"] for a in rule.evaluate(board_) if a.kind is ActionKind.STOP}
    assert set(fixture["expected"]["stopped_sessions"]) <= stops
    assert not set(fixture["not_expected"]["stopped_sessions"]) & stops


def test_m1_fixture_discriminates_under_one_shared_token() -> None:
    """The same requests under one shared token stop the victim's run: the fixture's
    negative case fails without per-agent tokens."""
    fixture = yaml.safe_load(FIXTURE.read_text())
    snapshot = snapshot_from_dict(fixture["snapshot"])
    store = MemorySpanStore()
    ingest = TraceIngest(store, lambda: snapshot.now, token=SHARED)
    n = 0
    for req in fixture["requests"]:
        spans = []
        for i in range(req["calls"]):
            n += 1
            span = tool(n, req["session_id"], req["task_id"], args=f"cat notes-{req['token']}.txt")
            spans.append(replace(span, start=snapshot.now - timedelta(minutes=5, seconds=-i)))
        post(ingest, spans, SHARED)
    rule = REGISTRY["loop_breaker"]({})
    board_ = replace(snapshot, spans=tuple(stored(store)))
    stops = {a.params["session_id"] for a in rule.evaluate(board_) if a.kind is ActionKind.STOP}
    assert "S-101" in stops


# --- serve, status, doctor, rotation --------------------------------------------------------


def scoped_config(tmp_path: Path, extra: str = "", rules: str = "") -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(
        f'[storage]\npath = "{tmp_path / "kelpie.db"}"\n{rules}'
        f"[traces]\n{extra}"
        '[[traces.tokens]]\nname = "dev-1"\ntoken_env = "KELPIE_TRACES_TOKEN_DEV_1"\n'
        'owners = ["agent-dev-1"]\n'
        '[[traces.tokens]]\nname = "dev-2"\ntoken_env = "KELPIE_TRACES_TOKEN_DEV_2"\n'
        f'token_file = "{tmp_path / "dev-2.env"}"\nroles = ["developer-b"]\n'
    )
    return path


def test_serve_builds_scoped_ingest_and_status_lists_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from kelpie.app import ConfigError, build_app, load_rules, load_settings, render_status
    from kelpie.cli import main

    path = scoped_config(tmp_path)
    settings = load_settings(path)
    with pytest.raises(ConfigError, match="dev-1"):  # its variable is not set
        build_app(settings)
    monkeypatch.setenv("KELPIE_TRACES_TOKEN_DEV_1", T1)
    with pytest.raises(ConfigError, match="rotate-token --token dev-2"):
        build_app(settings)
    assert main(["--config", str(path), "traces", "rotate-token", "--token", "dev-2"]) == 0
    assert main(["--config", str(path), "traces", "rotate-token", "--token", "dev-1"]) == 2
    assert main(["--config", str(path), "traces", "rotate-token", "--token", "nope"]) == 2
    err = capsys.readouterr().err
    assert "set [[traces.tokens]] dev-1 token_file first" in err and "'nope'" in err
    token_2 = (tmp_path / "dev-2.env").read_text().split("=", 1)[1].strip()
    assert token_2 not in err
    app = build_app(settings)
    try:
        assert app.ingest is not None and [t.name for t in app.ingest.scoped] == [
            "dev-1",
            "dev-2",
        ]
        assert app.ingest.guard is not None and app.ingest.token is None
    finally:
        app.close()
    text = render_status(settings, load_rules(settings.engine))
    assert "no shared token, 2 per-agent token(s) (dev-1, dev-2)" in text
    assert T1 not in text and token_2 not in text
    monkeypatch.setenv("KELPIE_TRACES_TOKEN_DEV_1", token_2)
    with pytest.raises(ConfigError, match="same as"):
        build_app(settings)


def test_snapshot_drops_out_of_scope_spans(tmp_path: Path) -> None:
    from kelpie.app import merged_snapshot
    from kelpie.metrics import Metrics
    from kelpie.traces.store import SpanWindow

    store = MemorySpanStore()
    metrics = Metrics()
    guard = ScopeGuard(SCOPES, store=store, rejected=metrics.spans_rejected)
    metrics.set_traces(True)
    ingest = TraceIngest(store, lambda: NOW, scoped=[ScopedToken("dev-2", T2)], guard=guard)
    post(ingest, [tool(1, "S-101", "T-101")], T2)  # the board is not read yet: provisional

    class Board:
        name = "fake"

        def snapshot(self, now: datetime) -> Snapshot:
            return replace(board(), now=now)

    window = SpanWindow(store, timedelta(minutes=30), timedelta(hours=24), 1000)
    source = merged_snapshot([Board()], metrics, lambda: NOW, window, scopes=guard)  # type: ignore[list-item]
    snap = source()
    assert snap.spans == () and stored(store) == []
    assert 'kelpie_spans_rejected_total{reason="out_of_scope"} 1' in metrics.render()


def test_doctor_warns_about_the_shared_token_when_a_rule_acts(tmp_path: Path) -> None:
    from kelpie.app import load_rules, load_settings
    from kelpie.doctor import SCOPES_CHECK, check_traces_scopes

    acting = '[engine.rules.loop_breaker]\nmode = "act"\n'
    path = tmp_path / "kelpie.toml"
    path.write_text(f'[storage]\npath = "{tmp_path / "kelpie.db"}"\n{acting}')
    settings = load_settings(path)
    check = check_traces_scopes(settings, load_rules(settings.engine), {})
    assert (check.status, check.name) == ("WARN", SCOPES_CHECK)
    assert "without a token" in check.detail and "loop_breaker" in check.detail
    check = check_traces_scopes(
        settings, load_rules(settings.engine), {"KELPIE_TRACES_TOKEN": SHARED}
    )
    assert check.status == "WARN" and "shared token" in check.detail
    assert SHARED not in check.detail + check.fix

    env = {"KELPIE_TRACES_TOKEN_DEV_1": T1, "KELPIE_TRACES_TOKEN_DEV_2": T2}
    settings = load_settings(scoped_config(tmp_path, rules=acting))
    check = check_traces_scopes(settings, load_rules(settings.engine), env)
    assert check.status == "FAIL" and "dev-2" in check.detail  # its token file is missing
    path = scoped_config(tmp_path, rules=acting)
    path.write_text(path.read_text().replace(f'token_file = "{tmp_path / "dev-2.env"}"\n', ""))
    settings = load_settings(path)
    check = check_traces_scopes(settings, load_rules(settings.engine), env)
    assert check.status == "PASS", check
    check = check_traces_scopes(
        settings, load_rules(settings.engine), {**env, "KELPIE_TRACES_TOKEN_DEV_2": "short"}
    )
    assert check.status == "WARN" and "dev-2" in check.detail
    check = check_traces_scopes(
        settings, load_rules(settings.engine), {**env, "KELPIE_TRACES_TOKEN": SHARED}
    )
    assert check.status == "WARN" and "shared token" in check.detail


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_store_discard(tmp_path: Path, kind: str) -> None:
    from kelpie.core.storage import SqliteSpanStore

    store: Any = MemorySpanStore() if kind == "memory" else SqliteSpanStore(tmp_path / "k.db")
    a = Span("t", "a", "x", SpanKind.TOOL, NOW, session_id="S-1")
    b = replace(a, span_id="b")
    store.append_spans([a, b], NOW)
    epoch = store.epoch
    assert store.discard([("t", "a"), ("t", "zz")]) == 1
    assert [s.span_id for s in store.spans_since(NOW - timedelta(hours=1))] == ["b"]
    assert store.count() == 1 and store.epoch > epoch
    assert store.discard([]) == 0
    if kind == "sqlite":
        store.close()


# --- bypasses closed (first review of #340) ------------------------------------------------


def later_board(*extra: tuple[str, str, str]) -> Snapshot:
    """The board, plus (session, task, owner) runs that started since."""
    b = board()
    return replace(
        b,
        tasks=(*b.tasks, *(Task(t, "x", "in_progress", NOW, owner_id=o) for _, t, o in extra)),  # type: ignore[arg-type]
        sessions=(*b.sessions, *(Session(s, t, o, NOW) for s, t, o in extra)),
    )


def test_own_session_with_another_agents_task_is_rejected() -> None:
    ingest, store, observer, _ = setup()
    post(ingest, [tool(1, "S-101", "T-102")], T1)  # its own session, the victim's task
    assert stored(store) == [] and observer.rejected[OUT_OF_SCOPE] == 1


def test_bound_ids_are_checked_across_the_store_not_only_the_window() -> None:
    ingest, store, observer, guard = setup()
    old = replace(tool(1, "S-103", None), start=NOW - timedelta(hours=2))
    post(ingest, [old], T2)  # unplaced: accepted and bound; outside any 30m window
    assert len(stored(store)) == 1 and guard.bound(SESSION, "S-103") == "dev-2"
    guard.check(later_board(("S-103", "T-103", "agent-dev-1")))  # no spans in the snapshot
    assert stored(store) == [] and guard.bound(SESSION, "S-103") is None
    assert observer.rejected == Counter({OUT_OF_SCOPE: 1})
    post(ingest, [tool(2, "S-103", None)], T2)  # now placed elsewhere: rejected at ingest
    assert stored(store) == []


def test_bound_ids_in_scope_settle() -> None:
    ingest, store, _, guard = setup()
    post(ingest, [tool(1, "S-104", None)], T2)
    guard.check(later_board(("S-104", "T-104", "agent-dev-2")))
    assert len(stored(store)) == 1 and guard.bound(SESSION, "S-104") == "dev-2"


def test_unplaced_session_with_own_task_is_bound_and_revoked() -> None:
    ingest, store, observer, guard = setup()
    forged = replace(tool(1, "S-105", "T-102"), start=NOW - timedelta(hours=3))
    post(ingest, [forged], T2)  # its own task, a future session id
    assert len(stored(store)) == 1
    post(ingest, [tool(2, "S-105", "T-101")], T1)  # S-105 is bound to dev-2 already
    assert observer.rejected[OUT_OF_SCOPE] == 1
    guard.check(later_board(("S-105", "T-105", "agent-dev-1")))
    assert stored(store) == []


def test_check_waits_for_requests_between_judging_and_storing() -> None:
    import threading

    guard = setup()[3]
    done = threading.Event()
    with guard.barrier.shared():  # an ingest request between judging and storing
        worker = threading.Thread(target=lambda: (guard.check(board()), done.set()))
        worker.start()
        assert not done.wait(0.2)  # the check waits
        with pytest.raises(TimeoutError):  # and no new request starts meanwhile
            _try_shared(guard, 0.1)
    worker.join(2)
    assert done.is_set()


def _try_shared(guard: ScopeGuard, timeout: float) -> None:
    import threading

    got = threading.Event()

    def run() -> None:
        with guard.barrier.shared():
            got.set()

    threading.Thread(target=run, daemon=True).start()
    if not got.wait(timeout):
        raise TimeoutError


def test_restart_rebinds_ids_to_the_token_stored_first() -> None:
    ingest, store, observer, _ = setup()
    post(ingest, [tool(1, "S-103", None)], T2)
    fresh = ScopeGuard(SCOPES, store=store, rejected=observer.spans_rejected)  # a restart
    assert fresh.restore(NOW - timedelta(days=7), NOW) == 1
    assert fresh.bound(SESSION, "S-103") == "dev-2"
    fresh.check(later_board(("S-103", "T-103", "agent-dev-1")))
    assert stored(store) == []


def test_remembered_owner_places_a_session_the_board_dropped() -> None:
    from kelpie.core.spans import Attribution

    ingest, store, observer, guard = setup()
    guard.index.update(
        Snapshot(now=NOW + timedelta(hours=25)),
        Attribution(owners={"S-102": "agent-dev-2"}, session_tasks={"S-102": "T-102"}),
    )
    post(ingest, [tool(1, "S-102", None)], T1)
    assert stored(store) == [] and observer.rejected[OUT_OF_SCOPE] == 1


def test_an_unplaced_session_is_bound_to_its_first_token() -> None:
    ingest, store, observer, _ = setup()
    post(ingest, [tool(1, "S-900", None)], T2)
    post(ingest, [tool(2, "S-900", None)], T1)
    assert [s.span_id for s in stored(store)] == [sid(1)]
    assert observer.rejected[OUT_OF_SCOPE] == 1


def test_sessions_only_scope_needs_a_matching_pattern_while_unplaced() -> None:
    scopes = {**SCOPES, "dev-2": TokenScope("dev-2", sessions=("new-2-*",))}
    ingest, store, observer, _ = setup(scopes=scopes)
    post(ingest, [tool(1, "new-2-a", None), tool(2, "other", None)], T2)
    assert [s.span_id for s in stored(store)] == [sid(1)]
    assert observer.rejected[OUT_OF_SCOPE] == 1


def test_children_that_inherit_a_forged_session_go_with_it() -> None:
    ingest, store, _, guard = setup()
    parent = tool(1, "S-103", None)
    child = tool(2, None, None, parent=None)
    child.parent = sid(1)
    post(ingest, [parent, child], T2)
    assert {s.session_id for s in stored(store)} == {"S-103"}  # the child inherited it
    guard.check(later_board(("S-103", "T-103", "agent-dev-1")))
    assert stored(store) == []


def test_a_span_folded_into_another_session_is_deleted() -> None:
    ingest, store, _, guard = setup()
    post(ingest, [tool(1, "S-102", "T-102")], T2)  # in scope as sent
    (span,) = stored(store)
    folded = replace(span, session_id="S-101", task_id="T-101")  # a hook call's session
    checked, _ = guard.check(replace(board(), spans=(folded,)))
    assert checked.spans == () and stored(store) == []


def test_hermes_task_found_by_its_id_on_its_board() -> None:
    b = board()
    hermes = Task("qr:t_1a", "x", "in_progress", NOW, owner_id="agent-dev-2", board="qr")  # type: ignore[arg-type]
    b = replace(b, tasks=(*b.tasks, hermes))
    index = ScopeIndex()
    index.update(b)
    assert index.task("t_1a") == (Place("agent-dev-2", "developer-b", "qr"), "qr:t_1a")
    ingest, store, observer, guard = setup()
    guard.index.update(b)
    post(ingest, [tool(1, None, "t_1a")], T1)
    assert stored(store) == [] and observer.rejected[OUT_OF_SCOPE] == 1


def test_role_falls_back_to_the_tasks_role() -> None:
    b = replace(
        board(),
        owners=(),
        tasks=(Task("T-201", "x", "in_progress", NOW, owner_id="agent-x", role="developer-b"),),  # type: ignore[arg-type]
        sessions=(Session("S-201", "T-201", "agent-x", NOW),),
    )
    index = ScopeIndex()
    index.update(b)
    assert index.session("S-201") == (Place("agent-x", "developer-b", None), "T-201")


def test_unplaced_ids_per_token_are_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    import kelpie.traces.scope as scope

    monkeypatch.setattr(scope, "MAX_UNPLACED", 1)
    ingest, store, observer, _ = setup()
    post(ingest, [tool(1, "S-901", None), tool(2, "S-902", None)], T2)
    assert len(stored(store)) == 1 and observer.rejected[scope.SCOPE_BACKLOG] == 1


def test_window_reads_totals_again_after_a_discard() -> None:
    from kelpie.traces.store import SpanWindow

    store = MemorySpanStore()
    window = SpanWindow(store, timedelta(minutes=30), timedelta(hours=24), 1000)
    store.append_spans([Span("t", "a", "x", SpanKind.TOOL, NOW, session_id="S-1")], NOW)
    assert len(window.totals(NOW)) == 1
    store.discard([("t", "a")])
    assert len(window.totals(NOW)) == 1  # cached for a minute
    window.invalidate()
    assert window.totals(NOW) == ()


def test_children_inherit_only_from_a_parent_of_the_same_token() -> None:
    ingest, store, _, _ = setup(shared=SHARED)
    post(ingest, [tool(1, "S-101", "T-101")], SHARED)  # the victim's span, shared token
    child = tool(2, None, None)
    child.parent = sid(1)
    post(ingest, [child], T2)  # names the victim's span as its parent
    kept = {s.span_id: s for s in stored(store)}
    assert kept[sid(2)].session_id is None and kept[sid(2)].task_id is None


def test_local_task_id_of_a_placed_session_is_its_own_on_two_boards() -> None:
    b = board()
    mine = Task("qr:t_1a", "x", "in_progress", NOW, owner_id="agent-dev-2", board="qr")  # type: ignore[arg-type]
    other = Task("zz:t_1a", "y", "in_progress", NOW, owner_id="agent-dev-1", board="zz")  # type: ignore[arg-type]
    b = replace(
        b,
        tasks=(*b.tasks, mine, other),
        sessions=(*b.sessions, Session("hermes-run-7", "qr:t_1a", "agent-dev-2", NOW)),
    )
    ingest, store, observer, guard = setup()
    guard.index.update(b)
    post(ingest, [tool(1, "hermes-run-7", "t_1a")], T2)
    assert len(stored(store)) == 1 and not observer.rejected


@pytest.mark.parametrize("kind", ["memory", "sqlite"])
def test_store_marked_ids_discard_marked_and_same_mark_links(tmp_path: Path, kind: str) -> None:
    from kelpie.core.storage import SqliteSpanStore

    store: Any = MemorySpanStore() if kind == "memory" else SqliteSpanStore(tmp_path / "k.db")
    mark = {TOKEN_ATTRIBUTE: "dev-2"}
    a = Span("t", "a", "x", SpanKind.TOOL, NOW, session_id="S-1", task_id="T-1", attributes=mark)
    b = Span("t", "b", "x", SpanKind.TOOL, NOW, parent_id="a", attributes=mark)  # inherits
    c = Span("t", "c", "x", SpanKind.TOOL, NOW, parent_id="a")  # unmarked: does not
    d = Span("u", "d", "x", SpanKind.TOOL, NOW, session_id="S-1")  # unmarked, same session
    store.append_spans([a, b, c, d], NOW)
    store.inherit_links(["t"])
    got = {s.span_id: s for s in store.spans_since(NOW - timedelta(hours=1))}
    assert (got["b"].session_id, got["b"].task_id) == ("S-1", "T-1")
    assert got["c"].session_id is None
    assert list(store.marked_ids(NOW - timedelta(days=1), TOKEN_ATTRIBUTE)) == [
        ("S-1", "T-1", "dev-2")
    ]
    assert store.discard_marked("session_id", "S-1", TOKEN_ATTRIBUTE, "dev-2") == 2
    assert sorted(s.span_id for s in store.spans_since(NOW - timedelta(hours=1))) == ["c", "d"]
    with pytest.raises(ValueError):
        store.discard_marked("name", "x", TOKEN_ATTRIBUTE, "dev-2")
    if kind == "sqlite":
        store.close()


def test_the_unplaced_cap_counts_ids_used_within_a_day(monkeypatch: pytest.MonkeyPatch) -> None:
    import kelpie.traces.scope as scope

    monkeypatch.setattr(scope, "MAX_UNPLACED", 1)
    store, observer = MemorySpanStore(), Observer()
    clock = [NOW]
    guard = ScopeGuard(SCOPES, store=store, rejected=observer.spans_rejected)
    ingest = TraceIngest(
        store, lambda: clock[0], observer, scoped=[ScopedToken("dev-2", T2)], guard=guard
    )
    post(ingest, [tool(1, "chat-1", None)], T2)
    post(ingest, [tool(2, "chat-2", None)], T2)
    assert observer.rejected[scope.SCOPE_BACKLOG] == 1
    clock[0] = NOW + timedelta(hours=25)
    later = replace(tool(3, "chat-2", None), start=clock[0] - timedelta(minutes=1))
    post(ingest, [later], T2)
    assert observer.rejected[scope.SCOPE_BACKLOG] == 1 and guard.bound(SESSION, "chat-1") == "dev-2"


def test_use_refreshes_a_binding_and_requests_stay_fast() -> None:
    import time

    import kelpie.traces.scope as scope

    ingest, store, _, guard = setup()
    post(ingest, [tool(1, "chat-1", None)], T2)
    clock = NOW + timedelta(days=6)
    guard.ingest_filter("dev-2", [stored(store)[0]], clock)  # used again on day 6
    guard.check(replace(Snapshot(now=NOW + timedelta(days=8))))  # 7d retention after first use
    assert guard.bound(SESSION, "chat-1") == "dev-2"
    spans = [
        Span("t", f"{n:x}", "x", SpanKind.TOOL, NOW, session_id=f"c-{n}")
        for n in range(scope.MAX_UNPLACED + 2000)
    ]
    started = time.monotonic()
    kept, refused = guard.ingest_filter("dev-2", spans, NOW)
    assert time.monotonic() - started < 5
    assert refused[scope.SCOPE_BACKLOG] > 0 and len(kept) <= scope.MAX_UNPLACED


def test_old_unplaced_bindings_count_toward_the_cap_again(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Ids bound more than a day ago and still unplaced count again when used again, so a
    token cannot hold several days' worth of ids at once (#374)."""
    import kelpie.traces.scope as scope

    monkeypatch.setattr(scope, "MAX_UNPLACED", 3)
    guard = ScopeGuard(SCOPES, store=MemorySpanStore(), retention=timedelta(days=7))
    guard.index.update(board())
    for day in range(2):
        at = NOW + timedelta(hours=25 * day)
        for i in range(3):
            assert guard._bind("dev-1", [(SESSION, f"new-{day}-{i}")], at) is None
    later = NOW + timedelta(hours=26)
    ok = [guard._bind("dev-1", [(SESSION, f"new-0-{i}")], later) is None for i in range(3)]
    assert ok == [False, False, False]  # day 1's three ids are the token's active ones


def test_unconfigured_token_names_share_one_log_slot(caplog: pytest.LogCaptureFixture) -> None:
    """Spans whose mark names a token that is not configured (removed, or forged in a hub
    report) are deleted once the board places them, logged once a minute in all (#374)."""
    guard = ScopeGuard(SCOPES, store=MemorySpanStore())
    spans = tuple(
        Span(
            "t",
            f"s{i}",
            "x",
            SpanKind.TOOL,
            NOW,
            session_id="S-101",
            attributes={TOKEN_ATTRIBUTE: f"made-up-{i}"},
        )
        for i in range(500)
    )
    with caplog.at_level("WARNING", logger="kelpie.traces.scope"):
        snap, _ = guard.check(replace(board(), spans=spans))
    assert snap.spans == ()
    assert len(guard._logged) == 1
    assert len([r for r in caplog.records if "outside its scope" in r.message]) == 1
