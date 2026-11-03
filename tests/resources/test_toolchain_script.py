"""scripts/kelpie-toolchain.sh writes toolchain versions for node_exporter's textfile
collector (incident 20, #284)."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "kelpie-toolchain.sh"


def run(*args: str, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["sh", str(SCRIPT), *args], capture_output=True, text=True, check=False, env=env
    )


def metrics(out: Path) -> list[str]:
    text = (out / "kelpie_toolchain.prom").read_text()
    assert "# TYPE kelpie_toolchain_info gauge" in text
    return [x for x in text.splitlines() if x.startswith("kelpie_toolchain_info{")]


def test_one_line_per_tool_from_its_command(tmp_path: Path) -> None:
    result = run(
        str(tmp_path),
        "node=echo '  v20.11.1  '; echo second line",
        "odd=printf 'a \"b\" \\\\c\\n'",
        "Upper=echo 1",
        "silent=true",
        "broken=exit 3",
        "no-command",
    )
    assert result.returncode == 0, result.stderr
    assert metrics(tmp_path) == [
        'kelpie_toolchain_info{tool="node",version="v20.11.1"} 1',
        'kelpie_toolchain_info{tool="odd",version="a \\"b\\" \\\\c"} 1',
    ]
    for skipped in ("'Upper'", "silent", "broken", "'no-command'"):
        assert skipped in result.stderr
    assert not [p for p in tmp_path.iterdir() if p.name.startswith(".kelpie_toolchain.")]


def test_macos_defaults_read_the_selected_xcode(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    xcode = tmp_path / "Xcode.app" / "Contents"
    (xcode / "Developer").mkdir(parents=True)
    (bin_dir / "uname").write_text("#!/bin/sh\necho Darwin\n")
    (bin_dir / "xcode-select").write_text(f"#!/bin/sh\necho {xcode / 'Developer'}\n")
    # PlistBuddy -c 'Print :KEY' FILE: the fake answers by key, and fails for CoreSimulator
    buddy = bin_dir / "plistbuddy"
    buddy.write_text(
        "#!/bin/sh\n"
        'case "$2" in\n'
        "  *CFBundleShortVersionString) echo 16.1 ;;\n"
        "  *ProductBuildVersion) echo 16B40 ;;\n"
        "  *) exit 1 ;;\n"
        "esac\n"
    )
    for f in bin_dir.iterdir():
        f.chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "KELPIE_PLISTBUDDY": str(buddy)}
    result = run(str(out), env=env)
    assert result.returncode == 0, result.stderr
    # CoreSimulator's framework is not there: skipped, not an error
    assert metrics(out) == ['kelpie_toolchain_info{tool="xcode",version="16.1 (16B40)"} 1']
    assert "skipped coresimulator" in result.stderr


def test_elsewhere_without_tools_no_versions(tmp_path: Path) -> None:
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "uname").write_text("#!/bin/sh\necho Linux\n")
    (bin_dir / "uname").chmod(0o755)
    out = tmp_path / "out"
    out.mkdir()
    result = run(str(out), env={**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"})
    assert result.returncode == 0 and metrics(out) == []


def test_usage_errors(tmp_path: Path) -> None:
    assert run().returncode == 2
    assert run(str(tmp_path / "missing")).returncode == 2


def test_long_versions_are_cut_and_slow_commands_bounded(tmp_path: Path) -> None:
    env = {**os.environ, "KELPIE_TOOL_TIMEOUT": "1"}
    long = "é" + "x" * 300  # non-ASCII is dropped: a cut never splits a character
    result = run(str(tmp_path), f"long=echo {long}", "slow=sleep 5; echo late", env=env)
    assert result.returncode == 0, result.stderr
    assert metrics(tmp_path) == [f'kelpie_toolchain_info{{tool="long",version="{"x" * 100}"}} 1']
    assert "skipped slow" in result.stderr
    bad = run(str(tmp_path), env={**os.environ, "KELPIE_TOOL_TIMEOUT": "soon"})
    assert bad.returncode == 2
