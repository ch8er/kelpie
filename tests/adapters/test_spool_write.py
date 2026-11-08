"""``_spool.write_in_dir``: the stop-marker write relative to a directory handle (audit L4)."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

import pytest

from kelpie.adapters import _spool
from kelpie.adapters.claude_code.adapter import write_marker as cc_write_marker
from kelpie.adapters.openclaw import stop as oc_stop

SID = "0b3f6c1e-2d4a-4c55-9a77-1f0e2b3c4d5e"


def test_writes_atomically_with_mode_0600(tmp_path: Path) -> None:
    d = tmp_path / "stop"
    path = _spool.write_in_dir(d, "a.json", b"{}", prefix=".stop-")
    assert path == d / "a.json" and path.read_bytes() == b"{}"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(d.stat().st_mode) == 0o700
    _spool.write_in_dir(d, "a.json", b"[]")  # replaces the older file
    assert path.read_bytes() == b"[]"
    assert sorted(p.name for p in d.iterdir()) == ["a.json"]  # no temporary file left


def test_a_symlinked_directory_is_refused(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    d = tmp_path / "stop"
    d.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(OSError, match="not a plain directory"):
        _spool.write_in_dir(d, "a.json", b"{}")
    assert list(elsewhere.iterdir()) == []


def test_a_file_in_place_of_the_directory_is_refused(tmp_path: Path) -> None:
    d = tmp_path / "stop"
    d.write_text("x")
    with pytest.raises(OSError):
        _spool.write_in_dir(d, "a.json", b"{}")


def test_a_directory_swapped_for_a_symlink_after_the_check_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The race the old path-based write lost: the directory passes the check, then an agent
    swaps it for a symlink before the temporary file is created and renamed."""
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    d = tmp_path / "stop"
    d.mkdir()
    real_open = os.open
    swapped: list[bool] = []

    def racing_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        fd = real_open(path, flags, *args, **kwargs)  # type: ignore[arg-type]
        if path == d and not swapped:  # the directory handle is held: now swap the path
            swapped.append(True)
            d.rename(tmp_path / "moved")
            d.symlink_to(elsewhere, target_is_directory=True)
        return fd

    monkeypatch.setattr(_spool.os, "open", racing_open)
    _spool.write_in_dir(d, "a.json", b"{}")
    assert swapped
    assert list(elsewhere.iterdir()) == []  # nothing written through the symlink
    assert (tmp_path / "moved" / "a.json").read_bytes() == b"{}"  # the directory checked


def test_a_symlink_at_the_name_is_replaced_not_followed(tmp_path: Path) -> None:
    d = tmp_path / "stop"
    d.mkdir()
    target = tmp_path / "t.json"
    target.write_text("keep")
    (d / "a.json").symlink_to(target)
    _spool.write_in_dir(d, "a.json", b"{}")
    assert target.read_text() == "keep"
    assert not (d / "a.json").is_symlink()


def test_a_failed_write_leaves_no_temporary_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = tmp_path / "stop"

    def fail(*args: object, **kwargs: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(_spool.os, "replace", fail)
    with pytest.raises(OSError, match="disk full"):
        _spool.write_in_dir(d, "a.json", b"{}")
    assert list(d.iterdir()) == []


@pytest.mark.parametrize("name", ["", ".", "..", "a/b"])
def test_only_a_plain_name(tmp_path: Path, name: str) -> None:
    with pytest.raises(ValueError):
        _spool.write_in_dir(tmp_path, name, b"{}")


def test_both_adapters_refuse_a_symlinked_stop_directory(tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    d = tmp_path / "stop"
    d.symlink_to(elsewhere, target_is_directory=True)
    with pytest.raises(OSError):
        cc_write_marker(d, SID, "loop", 1.0)
    with pytest.raises(OSError):
        oc_stop.write_marker(d, "s-1", "loop", 1.0)
    assert list(elsewhere.iterdir()) == []


def test_both_adapters_write_through_the_helper(tmp_path: Path) -> None:
    cc = cc_write_marker(tmp_path / "cc", SID, "loop", 1.0)
    oc = oc_stop.write_marker(tmp_path / "oc", "s-1", "loop", 2.0, key="agent:main:main")
    assert json.loads(cc.read_text())["at"] == 1.0
    assert json.loads(oc.read_text()) == {"at": 2.0, "reason": "loop", "key": "agent:main:main"}
