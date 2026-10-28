"""The CI outage contract between rule 9, the engine and the report (incident 20)."""

from __future__ import annotations

from kelpie.core.incidents import (
    MAX_BUILDS,
    CiOutage,
    is_outage_state,
    outage_signature,
    outage_state_id,
)


def outage(**kw: object) -> CiOutage:
    base: dict[str, object] = {
        "state": outage_state_id("ci.yml", "build", "Select toolchain"),
        "signature": outage_signature("ci.yml", "build", "Select toolchain"),
        "workflow": "ci.yml",
        "job": "build",
        "step": "Select toolchain",
    }
    return CiOutage(**{**base, **kw})  # type: ignore[arg-type]


def test_params_round_trip() -> None:
    full = outage(
        repos=("example/app",),
        workflows=("ci.yml",),
        prs=("example/app#12", "example/app#14"),
        tasks=("T-1", "T-2"),
        runners=("runner-1", "self-hosted"),
        builds=("example/app:7",),
    )
    assert full.signature == "ci.yml > build > Select toolchain"
    assert CiOutage.from_params(full.to_params()) == full
    bare = outage(workflow=None, step=None, signature=outage_signature(None, "build", None))
    assert bare.signature == "build" and CiOutage.from_params(bare.to_params()) == bare


def test_reading_is_tolerant() -> None:
    good = outage().to_params()
    for bad in (None, [], "text", {}, {**good, "state": "ci_economy:waste"}, {**good, "job": ""}):
        assert CiOutage.from_params(bad) is None, bad
    assert CiOutage.from_params({**good, "signature": 5}) is None
    assert CiOutage.from_params({**good, "signature": "  "}) is None
    odd = {**good, "step": "", "repos": "example/app", "prs": [1, " ", "a#1"], "tasks": {"T": 1}}
    read = CiOutage.from_params({**odd, "builds": [f"b{i}" for i in range(30)], "new": True})
    assert read == outage(step=None, prs=("a#1",), builds=tuple(f"b{i}" for i in range(MAX_BUILDS)))


def test_the_state_id_is_stable_and_names_the_signature() -> None:
    state = outage_state_id("ci.yml", "build", "Select toolchain")
    assert state == outage_state_id("ci.yml", "build", "Select toolchain")
    assert is_outage_state(state) and len(state.rsplit(":", 1)[1]) == 12
    assert state != outage_state_id("ci.yml", "build", None)
    assert state != outage_state_id(None, "build", "Select toolchain")
    assert outage_state_id("a", "b", None) != outage_state_id(None, "a", "b")  # same signature
    assert not is_outage_state("ci_economy:waste")


def test_outages_have_kinds_and_any_rule_can_report_one() -> None:
    from kelpie.core.incidents import BLOCK_STORM, CI_FAILURES, Outage, incident_state_id

    state = incident_state_id("stale_blocks", "waiting for ceo (xcode first launch)")
    assert state.startswith("stale_blocks:outage:") and is_outage_state(state)
    storm = Outage(
        state=state,
        signature="waiting for CEO (Xcode first launch)",
        kind=BLOCK_STORM,
        cause='7 tasks blocked within 52 min: "waiting for CEO (Xcode first launch)"',
        person="ceo",
        tasks=("T-1", "T-2"),
    )
    assert Outage.from_params(storm.to_params()) == storm
    assert storm.job == "" and CiOutage is Outage
    # Params written before kinds existed read as failing CI jobs.
    old = {k: v for k, v in outage().to_params().items() if k not in ("kind", "cause", "person")}
    assert Outage.from_params(old) == outage() and outage().kind == CI_FAILURES
    for bad in ("unknown", ["block_storm"], 3, None):
        assert Outage.from_params({**storm.to_params(), "kind": bad}) is None, bad
    assert Outage.from_params({**outage().to_params(), "job": ""}) is None  # CI needs a job


def test_only_outage_states_are_outage_states() -> None:
    from kelpie.core.incidents import incident_state_id

    assert is_outage_state(incident_state_id("loop_breaker", "ios_sim.sh"))
    for not_one in ("outage:abc", ":outage:abc", "stale_blocks:outage:", "stale_blocks:T-1:b"):
        assert not is_outage_state(not_one), not_one
