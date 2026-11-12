"""Hook/OTLP span pairing across ingest tokens (#374): another token's span carrying a hook
call's id must not fold the hook span into a span the scope check deletes. Rule 10's logic
and thresholds are unchanged; only the pairing is."""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta
from pathlib import Path

import pytest

from kelpie.core.actions import ActionKind
from kelpie.core.model import Snapshot, Span, SpanKind
from kelpie.core.spans import UNVERIFIED_ATTRIBUTE, span_key
from kelpie.rules import REGISTRY
from kelpie.traces.hooks import HOOKS, SOURCE, merge_hook_spans
from kelpie.traces.scope import TOKEN_ATTRIBUTE, ScopeGuard
from kelpie.traces.semconv import TOOL_CALL_ID
from tests.incidents import load
from tests.traces.test_token_scopes import NOW, SCOPES, board

FIXTURES = Path(__file__).parents[1] / "fixtures" / "incidents"
RULE_10 = sorted(p for p in FIXTURES.glob("*.yaml") if "rule: loop_breaker" in p.read_text())


def hook(n: int, session: str = "S-102", **attrs: object) -> Span:
    """A verified hook tool span of ``session`` (the platform's own record of the call)."""
    return Span(
        "hook",
        f"h-{n}",
        "execute_tool shell",
        SpanKind.TOOL,
        NOW - timedelta(minutes=9) + timedelta(seconds=30 * n),
        end=NOW - timedelta(minutes=9) + timedelta(seconds=30 * n + 1),
        session_id=session,
        tool="shell",
        args_hash="same-args",
        attributes={SOURCE: HOOKS, TOOL_CALL_ID: f"call-{n}", **attrs},
    )


def otlp(n: int, session: str, token: str | None, **kw: object) -> Span:
    """An OTLP tool span, stamped with the ingest token that sent it (None: unscoped)."""
    attrs: dict[str, object] = {TOOL_CALL_ID: f"call-{n}"}
    if token is not None:
        attrs[TOKEN_ATTRIBUTE] = token
    values: dict[str, object] = {
        "start": NOW - timedelta(minutes=9) + timedelta(seconds=30 * n),
        "session_id": session,
        "tool": "shell",
        "attributes": attrs,
    }
    values.update(kw)
    return Span(f"tr-{token}", f"o-{n}", "execute_tool shell", SpanKind.TOOL, **values)  # type: ignore[arg-type]


def guard() -> ScopeGuard:
    g = ScopeGuard(SCOPES)
    g.index.update(board())
    return g


def stops(snapshot: Snapshot) -> set[str]:
    rule = REGISTRY["loop_breaker"]({})
    return {a.params["session_id"] for a in rule.evaluate(snapshot) if a.kind is ActionKind.STOP}


def live_board() -> Snapshot:
    """The scoped board with both runs live (rule 10 stops only live sessions)."""
    b = board()
    return replace(b, sessions=tuple(replace(x, last_heartbeat=NOW) for x in b.sessions))


def checked(spans: tuple[Span, ...], g: ScopeGuard, *, pair: bool) -> Snapshot:
    merged = merge_hook_spans(spans, g.may_fold if pair else None).spans
    snap, _ = g.check(replace(live_board(), spans=merged))
    return snap


# --- the cross-token case ----------------------------------------------------------------


def test_another_tokens_span_cannot_erase_a_verified_hook_call() -> None:
    """dev-1 sends spans in its own session S-101 carrying the call ids of dev-2's run S-102
    (a loop of 10 identical calls). Folded, they would land in S-102 with dev-1's mark, be
    judged out of scope and deleted, and rule 10 would miss the loop."""
    hooks = tuple(hook(n) for n in range(10))
    forged = tuple(otlp(n, "S-101", "dev-1") for n in range(10))
    assert "S-102" in stops(replace(live_board(), spans=hooks))  # the loop, on its own
    g = guard()
    assert "S-102" not in stops(checked(hooks + forged, g, pair=False))  # the old pairing
    after = checked(hooks + forged, guard(), pair=True)
    assert "S-102" in stops(after)
    assert sum(1 for s in after.spans if s.session_id == "S-102") == 10
    # dev-1's own spans stay in its own session, judged on their own
    assert {s.session_id for s in after.spans if s.attributes.get(TOKEN_ATTRIBUTE)} == {"S-101"}


def test_a_token_still_folds_the_hook_calls_of_its_own_runs() -> None:
    hooks = tuple(hook(n) for n in range(3))
    own = tuple(otlp(n, "new-2-chat", "dev-2") for n in range(3))  # dev-2's own exporter
    merged = merge_hook_spans(hooks + own, guard().may_fold)
    assert merged.merged == 3
    assert {s.session_id for s in merged.spans} == {"S-102"}


def test_unscoped_spans_fold_as_before() -> None:
    hooks = tuple(hook(n) for n in range(3))
    plain = tuple(otlp(n, "chat-1", None) for n in range(3))
    with_guard = merge_hook_spans(hooks + plain, guard().may_fold)
    without = merge_hook_spans(hooks + plain)
    assert with_guard == without and without.merged == 3


def test_a_removed_tokens_span_takes_no_hook() -> None:
    hooks = (hook(0),)
    old = (otlp(0, "S-102", "gone"),)
    assert merge_hook_spans(hooks + old, guard().may_fold).merged == 0


def test_an_unverified_hook_line_pairs_as_before_within_scope() -> None:
    line = hook(0, **{UNVERIFIED_ATTRIBUTE: True})
    own = otlp(0, "S-102", "dev-2")
    merged = merge_hook_spans((line, own), guard().may_fold)
    assert merged.merged == 1 and merged.spans[0].session_id == "S-102"


# --- rule 10's existing evidence is unchanged --------------------------------------------


@pytest.mark.parametrize("path", RULE_10, ids=[p.stem for p in RULE_10])
def test_every_rule_10_fixture_gives_the_same_result(path: Path) -> None:
    incident = load(path)
    snap = incident.snapshot
    rule = REGISTRY["loop_breaker"](incident.config)
    g = ScopeGuard(SCOPES)
    g.index.update(snap)
    old = merge_hook_spans(snap.spans)
    new = merge_hook_spans(snap.spans, g.may_fold)
    assert [span_key(s) for s in new.spans] == [span_key(s) for s in old.spans]
    assert new == old
    before = rule.evaluate(replace(snap, spans=old.spans))
    after = rule.evaluate(replace(snap, spans=new.spans))
    assert before == after


def test_the_cross_token_fixture() -> None:
    import yaml

    from kelpie.core.serde import snapshot_from_dict
    from kelpie.traces.scope import TokenScope

    path = Path(__file__).parents[1] / "fixtures" / "traces" / "m1-hook-fold-across-tokens.yaml"
    fixture = yaml.safe_load(path.read_text())
    assert fixture["id"] == path.stem
    snap = snapshot_from_dict(fixture["snapshot"])
    scopes = {
        t["name"]: TokenScope(
            t["name"], owners=frozenset(t.get("owners", ())), roles=frozenset(t.get("roles", ()))
        )
        for t in fixture["config"]["tokens"]
    }
    g = ScopeGuard(scopes)
    g.index.update(snap)
    merged = merge_hook_spans(snap.spans, g.may_fold)
    assert merged.merged == fixture["expected"]["folded"]
    board_, _ = g.check(replace(snap, spans=merged.spans))
    stopped = stops(board_)
    assert set(fixture["expected"]["stopped_sessions"]) <= stopped
    assert not set(fixture["not_expected"]["stopped_sessions"]) & stopped
    old = merge_hook_spans(snap.spans)  # the pairing before #374 lost the loop
    assert old.merged == 8
    before, _ = ScopeGuard(scopes).check(replace(snap, spans=old.spans))
    assert "S-102" not in stops(before)


# --- the board is read before folding ----------------------------------------------------


def test_a_fresh_guard_or_a_new_session_folds_no_other_sessions_call() -> None:
    """The first tick after a start (empty index) or a session new to the board: a span
    never takes a hook call of another session the board does not place yet."""
    hooks = tuple(hook(n) for n in range(10))
    forged = tuple(otlp(n, "S-101", "dev-1") for n in range(10))
    fresh = ScopeGuard(SCOPES)  # nothing read yet
    assert merge_hook_spans(hooks + forged, fresh.may_fold).merged == 0
    fresh.refresh(live_board())
    snap, _ = fresh.check(
        replace(live_board(), spans=merge_hook_spans(hooks + forged, fresh.may_fold).spans)
    )
    assert "S-102" in stops(snap)


def test_the_report_folds_with_the_same_pairing() -> None:
    from kelpie.core.prices import PriceTable
    from kelpie.report.config import ReportSettings
    from kelpie.report.run import ReportSources, load_inputs
    from kelpie.traces.outputs import OutputSettings

    hooks = tuple(hook(n) for n in range(10))
    forged = tuple(otlp(n, "S-101", "dev-1") for n in range(10))
    sources = ReportSources(
        db_path=None,
        report=ReportSettings(),
        prices=PriceTable(),
        outputs=OutputSettings(),
        estimated=False,
        modes={},
        token_scopes=SCOPES,
    )
    board_ = replace(live_board(), spans=hooks + forged)
    inputs = load_inputs(sources, NOW.date(), board_)
    assert sum(1 for s in inputs.spans if s.session_id == "S-102") == 10
    assert {s.session_id for s in inputs.spans if s.attributes.get(TOKEN_ATTRIBUTE)} == {"S-101"}
    unscoped = load_inputs(replace(sources, token_scopes={}), NOW.date(), board_)
    assert sum(1 for s in unscoped.spans if s.session_id == "S-102") == 10  # folded as before
