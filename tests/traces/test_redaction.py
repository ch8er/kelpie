"""The shared command redaction (#82): one implementation in four files, no leaks."""

from __future__ import annotations

import json
import os
import random
import stat
import time
from collections.abc import Callable
from pathlib import Path

import pytest

from kelpie.adapters.claude_code import hook
from kelpie.adapters.hermes import plugin
from kelpie.adapters.openclaw.plugin import kelpie_hook as openclaw_hook
from kelpie.cli import main
from kelpie.traces import command
from kelpie.traces import scrub as scrub_module
from kelpie.traces.command import command_prefix, detect_filter
from kelpie.traces.scrub import default_spools, scrub_file, scrub_line

COPIES: dict[str, Callable[[str], str | None]] = {
    "traces": command.redact_command,
    "hermes": plugin.redact_command,
    "claude_code": hook.redact_command,
    "openclaw": openclaw_hook.redact_command,
}
BEGIN, END = "# --- shared redaction: begin ---", "# --- shared redaction: end ---"


def _block(path: str) -> str:
    text = Path(path).read_text()
    return text[text.index(BEGIN) : text.index(END)]


def test_the_copies_are_identical() -> None:
    """The hooks run on agent hosts without Kelpie, so they carry the code; this keeps it
    the same code."""
    blocks = {
        name: _block(m.__file__ or "")
        for name, m in (
            ("traces", command),
            ("hermes", plugin),
            ("claude_code", hook),
            ("openclaw", openclaw_hook),
        )
    }
    assert blocks["traces"] == blocks["hermes"] == blocks["claude_code"] == blocks["openclaw"]
    assert "def redact_command" in blocks["traces"]


# (command, secret that must never come out) — one or more per pattern in #82.
LEAKS = [
    # curl -u / --user
    ("curl -u admin:hunter2 https://api.example.test", "hunter2"),
    ("curl -u admin:hunter2 https://api.example.test", "admin"),
    ("curl --user admin:hunter2 https://x.test", "hunter2"),
    ("curl --user=admin:hunter2 https://x.test", "hunter2"),
    ("curl -sSu admin:hunter2 https://x.test", "hunter2"),
    # redis-cli -a, other short credential flags
    ("redis-cli -a hunter2 ping", "hunter2"),
    ("redis-cli -h cache.test -a hunter2 ping", "hunter2"),
    ("ldapsearch -x -w hunter2 -b dc=x", "hunter2"),
    ("zip -P hunter2 out.zip f", "hunter2"),
    # --password=, --password, other sensitive long flags
    ("tool --password=hunter2 run", "hunter2"),
    ("tool --password hunter2 run", "hunter2"),
    ("psql --db-password hunter2 db", "hunter2"),
    ("tool --api-token=tok123 run", "tok123"),
    ("npm login --otp 123456", "123456"),
    # -p<pass>
    ("mysql -phunter2 db", "hunter2"),
    ("mysql -p hunter2 db", "hunter2"),
    ("unzip -Phunter2 a.zip", "hunter2"),
    # URLs and host specs with user:pass@
    ("git clone https://user:hunter2@git.example.test/repo", "hunter2"),
    ("psql postgres://app:hunter2@db.test/main", "hunter2"),
    ("scp admin:hunter2@host.test:/f .", "hunter2"),
    ("redis-cli -u redis://default:hunter2@cache.test:6379", "hunter2"),
    # Authorization and other headers
    ('curl -H "Authorization: Bearer tok123" https://x.test', "tok123"),
    ("curl -H 'Authorization:Bearer tok123' https://x.test", "tok123"),
    ("curl -H Authorization:Bearer tok123 https://x.test", "tok123"),
    ('curl --header "Authorization: Basic dXNlcjpwYXNz" x', "dXNlcjpwYXNz"),
    ('wget --header="Authorization: Bearer tok123" x', "tok123"),
    ("curl -H 'X-Api-Key: shortkey1' https://x.test", "shortkey1"),
    ("curl -H 'Cookie: session=abc123' x", "abc123"),
    ("http GET x.test Authorization:'Bearer tok123'", "tok123"),
    # env-style KEY=secret
    ("GH_TOKEN=tok123 gh pr checks 5", "tok123"),
    ("export TOKEN=tok123; gh pr checks", "tok123"),
    ("env API_KEY=tok123 make test", "tok123"),
    ("env STRIPE=sk_live_abc node x.js", "sk_live_abc"),
    ("export DATABASE_URL=postgres://u:hunter2@db/x", "hunter2"),
    ("docker run -e DB_PASS=hunter2 img", "hunter2"),
    ("tool api_key=tok123 run", "tok123"),
    # quoted text is dropped whole, not just its first word
    ('echo "my password is hunter2 ok"', "hunter2"),
    ("git commit -m 'rotate token tok123'", "tok123"),
    ("x --name='a hunter2' y", "hunter2"),
    # query strings, long token-like runs, later lines
    ("curl https://api.test/v1?access_token=tok123&x=1", "tok123"),
    ("deploy Zx9Qe7Lm2Vb4Nc6Ty8Uh1Jk3ABC", "Zx9Qe7Lm2Vb4Nc6Ty8Uh1Jk3"),
    ("echo hi\nexport TOKEN=tok123", "tok123"),
    # review of #101: a value-less flag before the credential flag, glued values, headers
    ("curl -k -u admin:hunter2 x", "hunter2"),
    ("curl -sk -u admin:hunter2 x", "hunter2"),
    ("ls -a -u x:hunter2", "hunter2"),
    ("curl -uadmin:hunter2 x", "hunter2"),
    ("curl -sSuadmin:hunter2 x", "hunter2"),
    ("curl -u=admin:hunter2", "hunter2"),
    ("redis-cli -ahunter2 ping", "hunter2"),
    ('curl -H"X-Auth-Token: hunter2" x', "hunter2"),
    ("curl -H'X-Auth-Token: hunter2' x", "hunter2"),
    ("curl -HX-Auth-Token:hunter2 x", "hunter2"),
    ("http x.test X-API-Key:hunter2", "hunter2"),
    ("openssl enc -pass pass:hunter2", "hunter2"),
    ("keytool -storepass hunter2", "hunter2"),
    ("docker run --env=TOKEN=hunter2 img", "hunter2"),
    ("curl https://x.test/?pwd=hunter2", "hunter2"),
    ("curl https://x.test/#access_token=hunter2", "hunter2"),
    ("curl https://x.test/reset/hunter2", "hunter2"),
    ("x '--token' hunter2", "hunter2"),
    ("curl \u2013u admin:hunter2 x", "hunter2"),
    ("smbclient -U admin%hunter2 //h/s", "hunter2"),
    ("curl -E cert.pem:hunter2 x", "hunter2"),
    ("curl https://u:pa@hunter2@h.test", "hunter2"),
    ("x --pw hunter2", "hunter2"),
    ("tool password:hunter2", "hunter2"),
    ("terraform apply -var db_pw=hunter2", "hunter2"),
    ("npm config set //r/:_authToken=hunter2", "hunter2"),
    ("echo `cat secret` $'hunter2'", "hunter2"),
    ("cat /home/alice/.secrets/prod.env", ".secrets"),
    ("mysql -p -u root db", "root"),
    # a quoted leading assignment, values with spaces
    ("PGPASSWORD='correct horse battery staple' psql -h db", "horse"),
    ("PGPASSWORD='correct horse battery staple' psql -h db", "battery"),
    ('DB_PASS="my secret pass" ./migrate', "secret"),
    ("A='x' B='tok hunter2 y' make", "hunter2"),
    # wrappers: their values and assignments
    ("env TOKEN=hunter2 make test", "hunter2"),
    ("env TOKEN='a hunter2' make test", "hunter2"),
    ("sudo -u hunter2 make", "hunter2"),
    ("sudo -p 'pw hunter2' make", "hunter2"),
    ("sudo --user=hunter2 make", "hunter2"),
    ("env -S 'make hunter2' x", "hunter2"),
    ("sudo env A=1 nice -n 5 curl -u admin:hunter2 h", "hunter2"),
    # a wrapper's flags: unknown ones and clusters ending in one that takes a value
    ("sudo -T hunter2 make", "hunter2"),
    ("sudo -R hunter2 make", "hunter2"),
    ("sudo --chroot hunter2 make", "hunter2"),
    ("sudo -X hunter2 make", "hunter2"),
    ("sudo --frobnicate hunter2 make", "hunter2"),
    ("sudo -Xhunter2 make", "hunter2"),
    ("env -P hunter2 make", "hunter2"),
    ("rtk --token hunter2 git status", "hunter2"),
    ("sudo -iu hunter2", "hunter2"),
    ("sudo -Hu hunter2 make", "hunter2"),
    ("sudo -Eu hunter2 make", "hunter2"),
    ("env -iC /x/hunter2 make", "hunter2"),
    ("nice -123456 hunter2", "123456"),
    ("nice -123456 hunter2", "hunter2"),
    # only spaces and tabs split words; an escaped quote masks the rest
    ("cmd\u00a0hunter2", "hunter2"),
    ("tool x\u2003hunter2", "hunter2"),
    ("tool x\x1fhunter2", "hunter2"),
    ('"a\\" | cat hunter2 x"', "hunter2"),
    ('echo \\"hi hunter2\\"', "hunter2"),
]


@pytest.mark.parametrize("copy", COPIES)
@pytest.mark.parametrize(("cmd", "secret"), LEAKS)
def test_no_secret_leaks(copy: str, cmd: str, secret: str) -> None:
    out = COPIES[copy](cmd) or ""
    assert secret not in out, out


@pytest.mark.parametrize(
    ("cmd", "expected"),
    [
        ("gh pr checks 5", "gh pr checks 5"),
        ("GH_TOKEN=abc gh pr checks 5", "gh pr checks 5"),
        ("  pytest -q tests/test_x.py -k foo\nsecond line", "pytest -q *** -k"),
        ("gh run view 123456 --log", "gh run view 123456"),
        ("npm run build && npm test", "npm run build &&"),
        ("pytest -q | tail -5", "pytest -q | tail"),
        ("xcodebuild test | xcbeautify", "xcodebuild test | xcbeautify"),
        ("python -m pytest -q", "python -m pytest -q"),
        ("/usr/bin/python3 -m pytest", "python3 -m pytest"),
        ("git status", "git status"),
        ("curl -u admin:pw https://x.test", "curl -u *** ***"),
        ("redis-cli -a pw ping", "redis-cli -a *** ***"),
        ("mysql -ppw db", "mysql -p*** ***"),
        ('curl -H "Authorization: Bearer x" https://h.test', "curl -H *** ***"),
        ("git clone https://u:p@host.test/repo", "git clone ***"),
        ("export TOKEN=x; gh pr checks", "export *** ; gh pr checks"),
        ("export DEVELOPER_DIR=/A/x.app A=1 && ./scripts/sim.sh test", "export *** && sim.sh test"),
        ("cd /home/a/repo && make test", "cd *** && make test"),
        ("cd build; cmake ..", "cd *** ; cmake ***"),
        ("set -euo pipefail; pytest -q", "set *** ; pytest -q"),
        ("source ~/.secrets/env && gh pr checks 5", "source *** && gh pr checks 5"),
        (". ./env.sh && make", ". *** && make"),
        ("export 'A=b; c' && make", "export *** && make"),
        ("export A=1 || exit 1", "export *** || exit 1"),
        # Setup tokens are capped like a wrapper's (_MAX_WRAPPED): a long chain loses the end.
        (
            "cd /x && cd /y && cd /z && export A=1 && make test",
            "cd *** && cd *** && cd *** && export *** &&",
        ),
        ('git commit -m "a b c" --amend', "git commit -m ***"),
        ("ssh user@host.test ls", "ssh *** ***"),
        ("ls -la /tmp", "ls -l*** ***"),
        ("tool --level=3 run", "tool --level=*** ***"),
        ("sleep 30 # wait for CI", "sleep 30"),
        ("sudo -u root make test", "sudo -u *** make test"),
        ("env FOO='a b' make test", "env FOO=*** make test"),
        ("sudo env A=1 B=2 nice -n 5 git status", "sudo env A=*** B=*** nice -n *** git status"),
        ("pytest\t-q", "pytest -q"),
    ],
)
def test_commands_stay_recognizable(cmd: str, expected: str) -> None:
    assert command.redact_command(cmd) == expected


@pytest.mark.parametrize(
    ("cmd", "prefix"),
    [
        ("GH_TOKEN=x gh pr checks 5", "gh pr"),
        ("npm run build -- --token=x", "npm run build"),
        ("python -m pytest -q", "python -m pytest"),
        ("uv run pytest -q", "pytest"),
        ("sudo -u root make test", "make test"),
        ("sudo -uroot npm test", "npm test"),
        ("nice -n 5 make", "make"),
        ("env FOO=1 make", "make"),
        ("env -u X make", "make"),
        ("sudo env A=1 nice -n 5 git status", "git status"),
        ("rtk git status --short", "git status"),
        ("sudo -iu root make test", "make test"),
        ("sudo -E -- make test", "make test"),
        ("env -i PATH=x make test", "make test"),
        ("nice -5 make", "make"),
        ("sudo -u root -g wheel nice -n 5 make test", "make test"),
        ("export DEVELOPER_DIR=/A/x.app && ./scripts/sim.sh test", "sim.sh"),
        ("cd /repo && xcodebuild test -scheme App", "xcodebuild test"),
        ("export A=1; cd /r && npm test", "npm test"),
        ("cd /repo", "cd"),
        ("export A=1 || exit 1", "export"),
    ],
)
def test_prefixes_still_work(cmd: str, prefix: str) -> None:
    assert command_prefix(command.redact_command(cmd)) == prefix


def test_detect_filter_still_sees_a_pipe_in_the_prefix() -> None:
    assert detect_filter(command.redact_command("xcodebuild test | xcbeautify")) == "xcbeautify"


def test_long_and_many_arguments_stay_fast() -> None:
    """Runs on every tool call: linear, whatever the line."""
    for cmd in ("A=1 " * 200_000 + "echo", "x " + "'a " * 200_000, "y" * 5_000_000):
        start = time.monotonic()
        command.redact_command(cmd)
        assert time.monotonic() - start < 1, cmd[:20]


def test_redaction_is_stable_on_random_commands() -> None:
    rng = random.Random(82)
    pool = ["curl", "-u", "-k", "-H", "'", '"', "a:b", "x=y", "TOKEN=z", "|", "&&", "#",
            "gh", "pr", "checks", "5", "--token", "--a=b", "-pq", "Authorization:", "Bearer",
            "https://u:p@h", "`x`", "$'q'", "-", "--", "***", "…", "a'b", "/p/q",
            "sudo", "env", "nice", "rtk", "-iu", "-iA=1", "-E", "-n", "-1234", "-X", "A=1",
            "--user=r", "--chroot", "make"]  # fmt: skip
    for _ in range(20000):
        cmd = " ".join(rng.choice(pool) for _ in range(rng.randint(1, 9)))
        once = command.redact_command(cmd)
        assert once is None or command.redact_command(once) == once, cmd


def test_redaction_is_stable() -> None:
    """Scrubbing an old spool redacts the already redacted text again: nothing changes the
    second time."""
    for cmd, _ in LEAKS:
        once = command.redact_command(cmd)
        assert once is None or command.redact_command(once) == once, cmd


@pytest.mark.parametrize("cmd", ["", "   ", "FOO=bar", "\n\n"])
def test_nothing_left(cmd: str) -> None:
    assert all(f(cmd) is None for f in COPIES.values())


# --- kelpie spool scrub --------------------------------------------------------------------


OLD_LINES = [  # what the old shared redaction let through
    {"type": "tool", "at": 1.0, "command": "curl -u admin:hunter2 https", "tool": "terminal"},
    {"type": "tool", "at": 2.0, "command": "curl -H '… shortkey1'", "tool": "terminal"},
    {"type": "tool", "at": 3.0, "command": 'echo "… password is', "tool": "terminal"},
    {"type": "tool", "at": 4.0, "command": "gh pr checks 5", "tool": "terminal"},
    {"type": "tool", "at": 4.5, "command": "curl -H *** tok123", "tool": "Bash"},  # old hook
    {"type": "session_end", "at": 5.0, "session_id": "s"},
]


def _spool(tmp_path: Path, lines: list[object], extra: str = "") -> Path:
    path = tmp_path / "spool.jsonl"
    path.write_text("".join(json.dumps(x) + "\n" for x in lines) + extra)
    path.chmod(0o600)
    return path


def test_scrub_rewrites_old_lines(tmp_path: Path) -> None:
    path = _spool(tmp_path, OLD_LINES, extra="{cut off half of a line with tok123")
    result = scrub_file(path)
    assert (result.lines, result.changed, result.dropped) == (7, 4, 1)  # incl. the cut-off line
    text = path.read_text()
    for secret in ("hunter2", "shortkey1", "password", "tok123"):
        assert secret not in text
    recs = [json.loads(x) for x in text.splitlines()]
    assert [r.get("command") for r in recs] == [
        "curl -u *** ***",
        "curl -H ***",
        "echo ***",
        "gh pr checks 5",
        "curl -H *** ***",
        None,
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert sorted(p.name for p in tmp_path.iterdir()) == ["spool.jsonl", "spool.jsonl.lock"]


def test_scrub_drops_unreadable_lines_and_is_idempotent(tmp_path: Path) -> None:
    path = _spool(tmp_path, OLD_LINES, extra="not json tok123\n[1, 2]\n\n")
    first = scrub_file(path)
    assert first.dropped == 2 and "tok123" not in path.read_text()
    again = scrub_file(path)
    assert (again.changed, again.dropped) == (0, 0)


def test_scrub_dry_run_changes_nothing(tmp_path: Path) -> None:
    path = _spool(tmp_path, OLD_LINES)
    before = path.read_text()
    assert scrub_file(path, dry_run=True).changed == 4
    assert path.read_text() == before


def test_scrub_line_keeps_other_fields() -> None:
    line = json.dumps({"type": "tool", "command": "redis-cli -a pw ping", "x": [1]}) + "\n"
    new, changed = scrub_line(line)
    assert changed and json.loads(new or "") == {
        "type": "tool",
        "command": "redis-cli -a *** ***",
        "x": [1],
    }


def test_scrub_refuses_symlinks_and_fifos(tmp_path: Path) -> None:
    target = _spool(tmp_path, OLD_LINES)
    link = tmp_path / "link.jsonl"
    link.symlink_to(target)
    with pytest.raises(OSError):
        scrub_file(link)
    fifo = tmp_path / "fifo.jsonl"
    os.mkfifo(fifo)
    with pytest.raises(OSError):
        scrub_file(fifo)


def test_scrub_runs_only_as_the_spool_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """As root it would leave a root-owned file and lock the hooks cannot write, and it would
    write in a directory the agent controls: refused (sudo -u <agent> is the way)."""
    path = _spool(tmp_path, OLD_LINES)
    before = path.read_text()
    monkeypatch.setattr(scrub_module.os, "geteuid", lambda: os.stat(path).st_uid + 1)
    with pytest.raises(OSError, match="sudo -u"):
        scrub_file(path)
    assert path.read_text() == before
    assert not (tmp_path / "spool.jsonl.lock").exists()
    assert scrub_file(path, dry_run=True).changed == 4  # reading is fine


def test_scrub_takes_the_rotation_lock_of_the_spool(tmp_path: Path) -> None:
    _spool(tmp_path, OLD_LINES)
    rotated = tmp_path / "spool.jsonl.1"
    rotated.write_text(json.dumps(OLD_LINES[1]) + "\n")
    scrub_file(rotated)
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "spool.jsonl", "spool.jsonl.1", "spool.jsonl.lock"
    ]  # fmt: skip
    (tmp_path / "spool.jsonl.lock").unlink()
    (tmp_path / "spool.jsonl.lock").symlink_to(tmp_path / "elsewhere")
    with pytest.raises(OSError):
        scrub_file(rotated)
    assert not (tmp_path / "elsewhere").exists()


def test_default_spools(tmp_path: Path) -> None:
    hermes = _spool(tmp_path, OLD_LINES)
    rotated = tmp_path / "spool.jsonl.1"
    rotated.write_text("")
    assert default_spools(hermes, tmp_path / "missing.jsonl") == [hermes, rotated]
    assert default_spools(None, None) == []


def test_cli_scrubs_configured_spools(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = _spool(tmp_path, OLD_LINES)
    config = tmp_path / "kelpie.toml"
    config.write_text(
        f"[storage]\npath = ':memory:'\n[adapters.hermes]\ndb_path = '{tmp_path}/k.db'\n"
        f"spool = '{path}'\n"
    )
    assert main(["--config", str(config), "spool", "scrub", "--dry-run"]) == 0
    assert "would change 4 commands" in capsys.readouterr().out
    assert main(["--config", str(config), "spool", "scrub"]) == 0
    assert "changed 4 commands" in capsys.readouterr().out
    assert "hunter2" not in path.read_text()


def test_cli_without_spools_fails(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text("[storage]\npath = ':memory:'\n")
    assert main(["--config", str(config), "spool", "scrub"]) == 1
    assert "no spool found" in capsys.readouterr().err
    assert main(["--config", str(config), "spool", "scrub", str(tmp_path / "nope")]) == 1


def test_scrub_carries_over_lines_appended_meanwhile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _spool(tmp_path, OLD_LINES)
    real = scrub_module._scrub_text
    calls = []

    def appending(text: str):  # type: ignore[no-untyped-def]
        if not calls:  # a hook appends right after the first read
            with path.open("a") as f:
                f.write(json.dumps({"type": "tool", "command": "redis-cli -a late ping"}) + "\n")
        calls.append(text)
        return real(text)

    monkeypatch.setattr(scrub_module, "_scrub_text", appending)
    result = scrub_file(path)
    commands = [json.loads(x).get("command") for x in path.read_text().splitlines()]
    assert commands[-1] == "redis-cli -a *** ***" and "late" not in path.read_text()
    assert result.lines == len(OLD_LINES) + 1


def test_scrub_starts_over_when_the_spool_is_rotated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _spool(tmp_path, OLD_LINES)
    real = scrub_module._scrub_text
    rotated = []

    def rotating(text: str):  # type: ignore[no-untyped-def]
        if not rotated:  # a hook rotates the spool while scrub works on the old file
            rotated.append(True)
            path.rename(tmp_path / "spool.jsonl.1")
            path.write_text(json.dumps({"type": "tool", "command": "curl -u a:b x"}) + "\n")
        return real(text)

    monkeypatch.setattr(scrub_module, "_scrub_text", rotating)
    result = scrub_file(path)
    assert result.lines == 1  # the new spool, not the old content written over it
    assert json.loads(path.read_text())["command"] == "curl -u *** ***"
    assert "hunter2" in (tmp_path / "spool.jsonl.1").read_text()  # scrubbed by its own run


def test_scrub_failure_leaves_the_spool_and_no_temp_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _spool(tmp_path, OLD_LINES)
    before = path.read_text()

    def broken(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(scrub_module.os, "replace", broken)
    with pytest.raises(OSError, match="disk full"):
        scrub_file(path)
    assert path.read_text() == before
    assert sorted(p.name for p in tmp_path.iterdir()) == ["spool.jsonl", "spool.jsonl.lock"]


def test_scrub_keeps_a_whole_last_line_without_newline(tmp_path: Path) -> None:
    path = _spool(tmp_path, [], extra=json.dumps({"type": "tool", "command": "curl -u a:b x"}))
    result = scrub_file(path)
    assert (result.lines, result.changed, result.dropped) == (1, 1, 0)
    assert json.loads(path.read_text())["command"] == "curl -u *** ***"
