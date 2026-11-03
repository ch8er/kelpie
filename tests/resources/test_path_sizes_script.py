"""scripts/kelpie-path-sizes.sh writes directory sizes for node_exporter's textfile collector."""

from __future__ import annotations

import os
import plistlib
import re
import subprocess
import time
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "scripts" / "kelpie-path-sizes.sh"


def run(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True, check=False)


def test_writes_one_metric_per_directory(tmp_path: Path) -> None:
    out = tmp_path / "textfile"
    out.mkdir()
    big = tmp_path / 'sim "devices"'
    big.mkdir()
    (big / "cache.bin").write_bytes(b"x" * 300_000)
    result = run(str(out), str(big), str(tmp_path / "missing"))
    assert result.returncode == 0, result.stderr
    text = (out / "kelpie_paths.prom").read_text()
    assert "# TYPE kelpie_path_bytes gauge" in text
    [line] = [x for x in text.splitlines() if x.startswith("kelpie_path_bytes{")]
    match = re.fullmatch(r'kelpie_path_bytes\{path="(.*)",mountpoint="(.*)"\} (\d+)', line)
    assert match is not None
    assert match[1] == str(big).replace('"', '\\"')
    assert match[2].startswith("/")  # the filesystem it is on, as df names it
    assert int(match[3]) >= 300_000
    assert "skipped" in result.stderr and "missing" in result.stderr
    assert not [p for p in out.iterdir() if p.name.startswith(".kelpie_paths.")]


def test_usage_errors(tmp_path: Path) -> None:
    assert run(str(tmp_path)).returncode == 2
    assert run(str(tmp_path / "nope"), str(tmp_path)).returncode == 2


def test_odd_arguments_are_skipped_or_measured_once(tmp_path: Path) -> None:
    out = tmp_path / "textfile"
    out.mkdir()
    dash = tmp_path / "-dash"
    dash.mkdir()
    result = run(str(out), str(dash), str(dash), "relative/dir", f"{tmp_path}/a\nb")
    assert result.returncode == 0, result.stderr
    lines = [
        x
        for x in (out / "kelpie_paths.prom").read_text().splitlines()
        if x.startswith("kelpie_path_bytes{")
    ]
    assert len(lines) == 1 and f'path="{dash}"' in lines[0]  # once, though given twice
    assert "not an absolute path" in result.stderr and "newline" in result.stderr


def test_deploy_units_parse() -> None:
    """launchd refuses a plist that is not well-formed XML ("--" in a comment is not)."""
    deploy = SCRIPT.parents[1] / "deploy" / "textfile"
    plists = sorted(deploy.glob("*.plist"))
    assert plists
    for path in plists:
        with path.open("rb") as f:
            unit = plistlib.load(f)
        assert unit["ProgramArguments"][0].endswith(".sh")


def test_symlinks_and_trailing_slashes(tmp_path: Path) -> None:
    out = tmp_path / "textfile"
    out.mkdir()
    real = tmp_path / "real"
    real.mkdir()
    (real / "blob").write_bytes(os.urandom(200_000))  # incompressible: du sees it all
    link = tmp_path / "link"
    link.symlink_to(real)
    result = run(str(out), str(link), f"{link}/", f"{link}//")
    assert result.returncode == 0, result.stderr
    lines = [
        x
        for x in (out / "kelpie_paths.prom").read_text().splitlines()
        if x.startswith("kelpie_path_bytes{")
    ]
    assert len(lines) == 1  # the same directory once
    assert int(lines[0].rsplit(" ", 1)[1]) >= 200_000  # measured through the link


def test_a_term_leaves_no_temporary_file(tmp_path: Path) -> None:
    out = tmp_path / "textfile"
    out.mkdir()
    slow = tmp_path / "bin"
    slow.mkdir()
    started, release = tmp_path / "started", tmp_path / "release"
    # du says it started, then waits to be released: TERM lands while it runs.
    (slow / "du").write_text(
        f"#!/bin/sh\ntouch '{started}'\n"
        f"i=0; while [ ! -e '{release}' ] && [ $i -lt 100 ]; do sleep 0.1; i=$((i+1)); done\n"
    )
    (slow / "du").chmod(0o755)
    env = {**os.environ, "PATH": f"{slow}:{os.environ['PATH']}", "KELPIE_DU_TIMEOUT": "60"}
    proc = subprocess.Popen(["sh", str(SCRIPT), str(out), str(tmp_path)], env=env)
    deadline = time.monotonic() + 10
    while not started.exists() and time.monotonic() < deadline:
        time.sleep(0.02)
    proc.terminate()
    release.touch()  # the shell handles TERM once du returns
    proc.wait(timeout=30)
    assert proc.returncode != 0
    assert not list(out.iterdir())


def test_the_mountpoint_of_a_macos_df(tmp_path: Path) -> None:
    """macOS df pads its columns: the mountpoint is what follows the capacity."""
    out = tmp_path / "textfile"
    out.mkdir()
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "df").write_text(
        "#!/bin/sh\n"
        "echo 'Filesystem   1024-blocks      Used Available Capacity  Mounted on'\n"
        "echo '/dev/disk3s5  1942700360 900000000 1000000000    48%    /System/Volumes/Data'\n"
    )
    (fake / "df").chmod(0o755)
    env = {**os.environ, "PATH": f"{fake}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["sh", str(SCRIPT), str(out), str(tmp_path)], env=env, capture_output=True, text=True
    )
    assert result.returncode == 0, result.stderr
    assert 'mountpoint="/System/Volumes/Data"}' in (out / "kelpie_paths.prom").read_text()


def test_scan_seconds_per_directory_even_without_a_size(tmp_path: Path) -> None:
    """#281: how long each du took, so doctor can name a directory that is too big."""
    out = tmp_path / "textfile"
    out.mkdir()
    one, two = tmp_path / "one", tmp_path / 'two "q"'
    one.mkdir()
    two.mkdir()
    result = run(str(out), str(one), str(two))
    assert result.returncode == 0, result.stderr
    text = (out / "kelpie_paths.prom").read_text()
    assert "# TYPE kelpie_path_scan_seconds gauge" in text
    scans = re.findall(r'^kelpie_path_scan_seconds\{path="(.*)"\} (\d+)$', text, re.M)
    assert [p for p, _ in scans] == [str(one), str(two).replace('"', '\\"')]
    # Each metric is one group (Prometheus text format): sizes first, then scan times.
    lines = [x.split("{")[0] for x in text.splitlines() if not x.startswith("#")]
    assert lines == ["kelpie_path_bytes"] * 2 + ["kelpie_path_scan_seconds"] * 2

    # A du that gives no size (a hung mount cut by the timeout) still has its scan time.
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "du"
    fake.write_text("#!/bin/sh\nexit 1\n")
    fake.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["sh", str(SCRIPT), str(out), str(one)], capture_output=True, text=True, env=env
    )
    assert result.returncode == 0 and "du gave no size" in result.stderr
    text = (out / "kelpie_paths.prom").read_text()
    assert "kelpie_path_bytes{" not in text
    assert re.search(
        rf'^kelpie_path_scan_seconds\{{path="{re.escape(str(one))}"\}} \d+$', text, re.M
    )


def test_shipped_units_run_hourly_and_skip_whole_agent_homes() -> None:
    """#281: the shipped launchd agent and systemd timer run hourly, over narrow paths."""
    deploy = SCRIPT.parents[1] / "deploy" / "textfile"
    with (deploy / "com.kelpie.path-sizes.plist").open("rb") as f:
        unit = plistlib.load(f)
    assert unit["StartInterval"] == 3600
    timer = (deploy / "kelpie-path-sizes.timer").read_text()
    assert re.search(r"^OnUnitActiveSec=1h$", timer, re.M)
    service = (deploy / "kelpie-path-sizes.service").read_text()
    [exec_line] = [x for x in service.splitlines() if x.startswith("ExecStart=")]
    for paths in (unit["ProgramArguments"][2:], exec_line.split()[2:]):
        assert paths
        assert not [p for p in paths if p.rstrip("/").endswith((".hermes", ".claude", "worktrees"))]
        assert not [p for p in paths if p.rstrip("/") in ("/Users/YOU", "/home/YOU")]
