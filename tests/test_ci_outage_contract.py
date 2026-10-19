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
