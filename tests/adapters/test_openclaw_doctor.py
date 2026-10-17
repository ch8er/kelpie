"""``kelpie doctor`` checks for ``[adapters.openclaw]``."""

from __future__ import annotations

import json
import os
import shutil
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.adapters.openclaw import OpenClawAdapter
from kelpie.adapters.openclaw import doctor as oc_doctor
from kelpie.doctor import run_checks
from tests.adapters.test_openclaw_adapter import Spool
from tests.adapters.test_openclaw_stop import answers, calls, fake_cli
from tests.sim.workboard import Workboard

NOW = datetime.now(UTC).replace(microsecond=0)


@pytest.fixture
def cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return fake_cli(tmp_path, monkeypatch)


@pytest.fixture
def state(tmp_path: Path) -> Path:
    path = tmp_path / "openclaw"
    path.mkdir()
    return path


@pytest.fixture
def spool(state: Path) -> Spool:
    return Spool(state / "kelpie" / "spool.jsonl")


def adapter(state: Path, spool: Spool, **kw: Any) -> OpenClawAdapter:
    kw.setdefault("workboard_db", state / "plugins" / "workboard" / "workboard.sqlite")
    kw.setdefault("stop_dir", state / "kelpie" / "stop")
    kw.setdefault("cli", "openclaw")
    return OpenClawAdapter(state_dir=state, spool=spool.path, **kw)


def by_name(checks: list[tuple[str, str, str, str]]) -> dict[str, tuple[str, str, str]]:
    return {name: (status, detail, fix) for status, name, detail, fix in checks}


def installed(state: Path, spool: Spool, **entry: Any) -> Path:
    """A plugin copy and an openclaw.json that loads it, as the quickstart sets them up."""
    plugin = state / "kelpie" / "plugin"
    shutil.copytree(oc_doctor.PLUGIN_DIR, plugin, ignore=shutil.ignore_patterns("__pycache__"))
    kelpie: dict[str, Any] = {
        "enabled": True,
        "hooks": {"allowConversationAccess": True},
        "config": {"spool": str(spool.path), "stopDir": str(state / "kelpie" / "stop")},
        **entry,
    }
    config = {
        "gateway": {"auth": {"token": "SECRET-GATEWAY-TOKEN"}},
        "plugins": {"entries": {"kelpie": kelpie}, "load": {"paths": [str(plugin)]}},
    }
    (state / "openclaw.json").write_text(json.dumps(config))
    return plugin


# --- workboard -----------------------------------------------------------------------------


def test_workboard(state: Path, spool: Spool) -> None:
    a = adapter(state, spool)
    check, running = oc_doctor.check_workboard(a, NOW)
    assert check[0] == "SKIP" and "not found" in check[2] and running is None
    board = Workboard(state / "plugins" / "workboard" / "workboard.sqlite")
    board.card("c1", "running", NOW - timedelta(hours=1))
    board.card("c2", "ready", NOW - timedelta(hours=1))
    check, running = oc_doctor.check_workboard(a, NOW)
    assert (check[0], running) == ("PASS", 1) and "2 cards, 1 running" in check[2]
    off, _ = oc_doctor.check_workboard(adapter(state, spool, workboard_db=None), NOW)
    assert off[0] == "SKIP"
    (state / "plugins" / "workboard" / "workboard.sqlite").write_bytes(b"x" * 1000)
    broken, _ = oc_doctor.check_workboard(adapter(state, spool), NOW)
    assert broken[0] == "FAIL" and broken[3]


# --- plugin fires --------------------------------------------------------------------------


def test_hooks(state: Path, spool: Spool) -> None:
    a = adapter(state, spool)
    assert oc_doctor.check_hooks(a, NOW, 0)[0] == "SKIP"  # nothing runs: no line expected
    assert oc_doctor.check_hooks(a, NOW, 2)[0] == "FAIL"
    spool.tool("s1", NOW - timedelta(minutes=3))
    check = oc_doctor.check_hooks(a, NOW, 2)
    assert check[0] == "PASS" and "3m old" in check[2] and "without" in check[2]
    spool.write("agent_end", NOW - timedelta(minutes=1), {"success": True}, sessionId="s1")
    old = oc_doctor.check_hooks(a, NOW + timedelta(hours=2), 2)
    assert old[0] == "FAIL" and "old" in old[2]
    no_spool = OpenClawAdapter(state_dir=state, spool=None)
    assert oc_doctor.check_hooks(no_spool, NOW, 2)[0] == "SKIP"


def test_hooks_need_a_plugin_that_stops(state: Path, spool: Spool) -> None:
    spool.path.parent.mkdir(parents=True)
    line = {"type": "tool", "at": NOW.timestamp(), "session_id": "s1", "v": 2}
    spool.path.write_text(json.dumps(line) + "\n")
    check = oc_doctor.check_hooks(adapter(state, spool), NOW, 1)
    assert check[0] == "FAIL" and "not loaded yet" in check[2]
    assert oc_doctor.check_hooks(adapter(state, spool, stop_dir=None), NOW, 1)[0] == "PASS"


# --- plugin configured ---------------------------------------------------------------------


def test_configured(state: Path, spool: Spool) -> None:
    a = adapter(state, spool)
    assert oc_doctor.check_configured(a)[0] == "SKIP"  # no openclaw.json
    installed(state, spool)
    check = oc_doctor.check_configured(a)
    assert check[0] == "PASS" and "this Kelpie's copy" in check[2]
    assert "SECRET" not in repr(check)


@pytest.mark.parametrize(
    ("change", "problem"),
    [
        ({"enabled": False}, "not enabled"),
        ({"hooks": {}}, "no conversation access"),
        ({"config": {"spool": "/elsewhere.jsonl"}}, "spool is not Kelpie's"),
        ({"config": {"spool": "~/.openclaw/kelpie/spool.jsonl"}}, "stopDir is not Kelpie's"),
    ],
)
def test_configured_problems(
    state: Path, spool: Spool, change: dict[str, Any], problem: str
) -> None:
    installed(state, spool, **change)
    check = oc_doctor.check_configured(adapter(state, spool))
    assert check[0] == "FAIL" and problem in check[2]
    assert "SECRET" not in repr(check)


def test_configured_plugin_lists_and_copies(state: Path, spool: Spool) -> None:
    plugin = installed(state, spool)
    path = state / "openclaw.json"
    config = json.loads(path.read_text())
    config["plugins"] |= {"allow": ["workboard"], "deny": ["kelpie"]}
    path.write_text(json.dumps(config))
    detail = oc_doctor.check_configured(adapter(state, spool))[2]
    assert "plugins.deny" in detail and "plugins.allow" in detail
    config["plugins"] |= {"allow": ["kelpie"], "deny": []}
    path.write_text(json.dumps(config))
    (plugin / "index.js").write_text("// an older copy\n")
    check = oc_doctor.check_configured(adapter(state, spool))
    assert check[0] == "FAIL" and "not this Kelpie's copy" in check[2]
    shutil.rmtree(plugin)
    check = oc_doctor.check_configured(adapter(state, spool))
    assert "no plugins.load.paths entry" in check[2]


def test_configured_unreadable_configs(state: Path, spool: Spool) -> None:
    path = state / "openclaw.json"
    path.write_text("{ plugins: { entries: {} }, // JSON5\n}")
    assert "not plain JSON" in oc_doctor.check_configured(adapter(state, spool))[2]
    path.unlink()
    target = state / "real.json"
    target.write_text("{}")
    path.symlink_to(target)
    check = oc_doctor.check_configured(adapter(state, spool))
    assert check[0] == "SKIP" and "symlink" in check[2]


# --- gateway -------------------------------------------------------------------------------


def test_gateway(
    state: Path,
    spool: Spool,
    cli: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answers(
        monkeypatch,
        {"ok": True, "plugins": {"loaded": ["workboard", "kelpie"]}},
        {"ok": True, "plugins": {"loaded": ["workboard"]}},
        "fail",
    )
    a = adapter(state, spool)
    assert oc_doctor.check_gateway(a)[0] == "PASS"
    missing = oc_doctor.check_gateway(a)
    assert missing[0] == "FAIL" and "not loaded" in missing[2]
    down = oc_doctor.check_gateway(a)
    assert down[0] == "FAIL" and "SECRET" not in down[2]
    assert [c["argv"][:3] for c in calls(cli)] == [["gateway", "call", "health"]] * 3
    assert oc_doctor.check_gateway(adapter(state, spool, cli=None))[0] == "SKIP"


# --- stop markers --------------------------------------------------------------------------


def test_stop_dir(state: Path, spool: Spool, tmp_path: Path) -> None:
    a = adapter(state, spool)
    assert oc_doctor.check_stop_dir(a)[2].endswith("will be created on the first stop")
    (state / "kelpie" / "stop").mkdir(parents=True)
    assert oc_doctor.check_stop_dir(a)[0] == "PASS"
    assert oc_doctor.check_stop_dir(adapter(state, spool, stop_dir=None))[0] == "SKIP"
    link = tmp_path / "link"
    link.symlink_to(state / "kelpie" / "stop")
    assert oc_doctor.check_stop_dir(adapter(state, spool, stop_dir=link))[0] == "FAIL"
    afile = tmp_path / "afile"
    afile.write_text("")
    assert oc_doctor.check_stop_dir(adapter(state, spool, stop_dir=afile))[0] == "FAIL"


# --- kelpie doctor -------------------------------------------------------------------------


def test_doctor_lists_openclaw_only_when_configured(
    tmp_path: Path, state: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KELPIE_DB", str(tmp_path / "kelpie.db"))
    plain = tmp_path / "plain.toml"
    plain.write_text("[engine]\ndry_run = true\n")
    names = [c.name for c in run_checks(plain, prometheus="http://127.0.0.1:9")]
    assert not any(n.startswith("openclaw") for n in names)
    config = tmp_path / "oc.toml"
    config.write_text(
        f'[engine]\ndry_run = true\n[adapters.openclaw]\nstate_dir = "{state}"\n'
        f'cli = "{tmp_path / "no-cli"}"\n'
    )
    checks = {c.name: c for c in run_checks(config, prometheus="http://127.0.0.1:9")}
    assert {n for n in checks if n.startswith("openclaw")} == {
        oc_doctor.WORKBOARD,
        oc_doctor.HOOKS,
        oc_doctor.CONFIGURED,
        oc_doctor.GATEWAY,
        oc_doctor.STOP,
    }
    assert checks[oc_doctor.GATEWAY].status == "FAIL"
    config.write_text(config.read_text() + "enabled = false\n")
    checks = {c.name: c for c in run_checks(config, prometheus="http://127.0.0.1:9")}
    assert checks[oc_doctor.WORKBOARD].status == "SKIP"
    assert os.environ["KELPIE_DB"].endswith("kelpie.db")
