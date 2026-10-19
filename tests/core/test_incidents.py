"""The CI outage contract between rule 9 and the daily report (incident 20)."""

from __future__ import annotations

from kelpie.core.incidents import OUTAGE_STATE_PREFIX, CiOutage, is_outage_state, outage_key


def test_params_round_trip() -> None:
    outage = CiOutage(
        signature=CiOutage.describe("ios.yml", "build-ios", "Select toolchain"),
        job="build-ios",
        step="Select toolchain",
        repos=("example/app",),
        workflows=("ios.yml",),
        prs=("example/app#12", "example/app#14"),
        tasks=("T-1", "T-2"),
        runners=("mac-runner-1", "self-hosted"),
        builds=("example/app:7",),
    )
    assert outage.signature == "ios.yml > build-ios > Select toolchain"
    assert CiOutage.from_params(outage.to_params()) == outage


def test_reading_is_tolerant() -> None:
    assert CiOutage.from_params({}) is None
    assert CiOutage.from_params({"signature": ""}) is None
    odd = {"signature": "s", "job": 3, "step": "", "repos": "example/app", "prs": [1, "a#1"]}
    read = CiOutage.from_params({**odd, "tasks": {"T-1": 1}, "new_key": True})
    assert read == CiOutage(signature="s", prs=("a#1",))


def test_the_key_is_stable_and_names_the_signature() -> None:
    key = outage_key("ios.yml", "build-ios", "Select toolchain")
    assert key == outage_key("ios.yml", "build-ios", "Select toolchain") and len(key) == 12
    assert key != outage_key("ios.yml", "build-ios", None)
    assert key != outage_key(None, "build-ios", "Select toolchain")
    assert is_outage_state(OUTAGE_STATE_PREFIX + key)
    assert not is_outage_state("ci_economy:waste")
    assert CiOutage.describe(None, "build", None) == "build"
