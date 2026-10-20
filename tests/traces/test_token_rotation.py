"""Rotating the traces ingest token (#199): the token file, the grace period, the command,
the doctor check. No test output may contain a token."""

from __future__ import annotations

import os
import stat
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie.app import ConfigError, build_app, load_settings, render_status
from kelpie.cli import main
from kelpie.doctor import TOKEN_CHECK, check_traces_token
from kelpie.traces.auth import (
    TokenCheck,
    TokenFile,
    TokenFileError,
    parse_token_file,
    rotate,
    token_copies,
)
from kelpie.traces.ingest import TraceIngest

NOW = datetime(2026, 10, 19, 12, tzinfo=UTC)
NAME = "KELPIE_TRACES_TOKEN"
OLD = "old-token-" + "a" * 32
NEW = "new-token-" + "b" * 32


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


def tokens(*made: str) -> object:
    it = iter(made)
    return lambda: next(it)


def mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


# --- the file -------------------------------------------------------------------------------


def test_parse_reads_its_lines_and_ignores_the_rest() -> None:
    text = f"# traces\nexport {NAME}='{OLD}'\nOTHER=1\n\n{NAME}_GRACE=\n"
    state = parse_token_file(text, NAME)
    assert state.token == OLD and state.grace == ()


@pytest.mark.parametrize(
    "text",
    [f"{NAME}=zq9", f"{NAME}=zq9zq9zq9 zq9zq9zq9", f"{NAME}={OLD}\n{NAME}_GRACE=zq9"],
)
def test_parse_refuses_without_echoing_the_token(text: str) -> None:
    with pytest.raises(TokenFileError) as exc:
        parse_token_file(text, NAME)
    assert "zq9" not in str(exc.value) and OLD not in str(exc.value)


def test_rotate_creates_then_keeps_the_old_one_as_a_hash(tmp_path: Path) -> None:
    path = tmp_path / "traces.env"
    first = rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    assert first.created and first.grace == 0 and mode(path) == 0o600
    path.write_text(f"# keep me\nOTHER=1\n{path.read_text()}")
    path.chmod(0o644)  # someone loosened it: the rotation writes 600 again
    done = rotate(path, NAME, timedelta(hours=2), NOW, tokens(NEW))
    assert not done.created and done.grace == 1 and done.grace_until == NOW + timedelta(hours=2)
    text = path.read_text()
    assert OLD not in text  # only its hash
    assert text.startswith("# keep me\nOTHER=1\n") and f"{NAME}={NEW}\n" in text
    assert mode(path) == 0o600
    assert not list(tmp_path.glob(".*.tmp"))


def test_expired_grace_entries_are_dropped_and_zero_grace_keeps_none(tmp_path: Path) -> None:
    path = tmp_path / "traces.env"
    rotate(path, NAME, timedelta(hours=1), NOW, tokens(OLD))
    rotate(path, NAME, timedelta(hours=1), NOW, tokens(NEW))
    later = rotate(path, NAME, timedelta(hours=1), NOW + timedelta(hours=2), tokens(OLD + "x"))
    assert later.grace == 1  # NEW's; OLD's expired
    none = rotate(path, NAME, timedelta(0), NOW + timedelta(hours=5), tokens(NEW + "y"))
    assert none.grace == 0 and none.grace_until is None
    assert f"{NAME}_GRACE" not in path.read_text()


def test_the_file_is_read_again_when_it_changes(tmp_path: Path) -> None:
    path = tmp_path / "traces.env"
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    clock = Clock()
    errors: list[str] = []
    auth = TokenFile(path, NAME, clock, on_error=errors.append)
    assert auth.check(OLD) is TokenCheck.CURRENT and auth.check(NEW) is TokenCheck.INVALID
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(NEW))
    clock.now += timedelta(seconds=1)
    assert auth.check(NEW) is TokenCheck.CURRENT
    assert auth.check(OLD) is TokenCheck.GRACE and auth.grace_count() == 1
    clock.now = NOW + timedelta(hours=2, seconds=1)
    assert auth.check(OLD) is TokenCheck.INVALID and auth.grace_count() == 0
    path.write_text("garbage without a token\n")  # broken: the last good tokens stay
    clock.now += timedelta(seconds=1)
    assert auth.check(NEW) is TokenCheck.CURRENT
    assert len(errors) == 1 and NEW not in errors[0] and OLD not in errors[0]
    clock.now += timedelta(seconds=1)
    auth.check(NEW)
    assert len(errors) == 1  # told once


def test_a_missing_file_does_not_start(tmp_path: Path) -> None:
    with pytest.raises(TokenFileError, match="cannot read it"):
        TokenFile(tmp_path / "none.env", NAME, Clock())


# --- ingest -----------------------------------------------------------------------------------


class Observer:
    def __init__(self) -> None:
        self.codes: list[int] = []
        self.grace = 0

    def traces_request(self, code: int) -> None:
        self.codes.append(code)

    def spans_received(self, kind: str, count: int) -> None:
        pass

    def spans_rejected(self, reason: str, count: int) -> None:
        pass

    def traces_grace_request(self) -> None:
        self.grace += 1


def post(ingest: TraceIngest, token: str | None) -> int:
    headers = {"Content-Type": "application/json", "Content-Length": "2"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    return ingest.handle(headers, lambda n: b"{}").status  # type: ignore[arg-type]


def test_ingest_takes_the_current_and_grace_tokens(tmp_path: Path) -> None:
    from kelpie.core.spans import MemorySpanStore

    path = tmp_path / "traces.env"
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(NEW))
    clock = Clock()
    observer = Observer()
    ingest = TraceIngest(MemorySpanStore(), clock, observer, tokens=TokenFile(path, NAME, clock))
    assert post(ingest, NEW) == 200 and observer.grace == 0
    assert post(ingest, OLD) == 200 and observer.grace == 1
    assert post(ingest, "wrong-" + "c" * 32) == 401 and post(ingest, None) == 401
    clock.now += timedelta(hours=3)
    assert post(ingest, OLD) == 401 and observer.grace == 1


# --- config, serve, status ----------------------------------------------------------------


def config(tmp_path: Path, extra: str = "") -> Path:
    path = tmp_path / "kelpie.toml"
    path.write_text(
        f'[storage]\npath = "{tmp_path / "kelpie.db"}"\n'
        f'[traces]\ntoken_file = "{tmp_path / "traces.env"}"\n{extra}'
    )
    return path


@pytest.mark.parametrize(
    ("extra", "message"),
    [
        ('token_grace = "8d"', "between 0s and 7d"),
        ('token_grace = "soon"', "token_grace"),
        ("token_file = 3", "token_file must be a path"),
    ],
)
def test_bad_config(tmp_path: Path, extra: str, message: str) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text(f"[traces]\n{extra}\n")
    with pytest.raises(ConfigError, match=message):
        load_settings(path)


def test_serve_needs_the_file_and_status_names_it(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    settings = load_settings(config(tmp_path, 'token_grace = "30m"'))
    with pytest.raises(ConfigError, match="rotate-token"):
        build_app(settings)
    rotate(tmp_path / "traces.env", NAME, timedelta(hours=2), NOW, tokens(OLD))
    (tmp_path / "traces.env").chmod(0o640)
    app = build_app(settings)
    try:
        assert app.ingest is not None and app.ingest.tokens is not None
        assert "kelpie_traces_grace_tokens 0" in app.metrics.render()
    finally:
        app.close()
    assert any("chmod 600" in r.message for r in caplog.records)
    from kelpie.app import load_rules

    text = render_status(settings, load_rules(settings.engine))
    assert f"bearer token from {tmp_path / 'traces.env'} (rotatable, grace 0:30:00)" in text
    assert OLD not in text and all(OLD not in r.message for r in caplog.records)


# --- the command ----------------------------------------------------------------------------


def test_rotate_token_prints_no_token(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = config(tmp_path)
    assert main(["--config", str(path), "traces", "rotate-token"]) == 0
    first = (tmp_path / "traces.env").read_text()
    assert main(["--config", str(path), "traces", "rotate-token", "--grace", "1h"]) == 0
    out = capsys.readouterr()
    second = (tmp_path / "traces.env").read_text()
    for text in (first, second):
        token = parse_token_file(text, NAME).token
        assert token is not None and token not in out.out + out.err
    assert "there was none before" in out.out and "previous tokens accepted: 1" in out.out
    assert "the old one is accepted until" in out.out


def test_rotate_token_needs_a_token_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text(f'[storage]\npath = "{tmp_path / "kelpie.db"}"\n')
    assert main(["--config", str(path), "traces", "rotate-token"]) == 2
    assert "set [traces] token_file first" in capsys.readouterr().err
    assert main(["--config", str(config(tmp_path)), "traces", "rotate-token", "--grace", "9d"]) == 2


# --- doctor ----------------------------------------------------------------------------------


def test_doctor_finds_backups_and_readable_copies(tmp_path: Path) -> None:
    home = tmp_path / "home"
    profile = home / ".hermes" / "profiles" / "dev"
    profile.mkdir(parents=True)
    rotate(tmp_path / "traces.env", NAME, timedelta(hours=2), NOW, tokens(OLD))
    settings = load_settings(config(tmp_path))
    env = {"HOME": str(home)}
    check = check_traces_token(settings, env)
    assert check.status == "PASS" and check.name == TOKEN_CHECK
    inline = profile / "hermes_otel.yaml"
    inline.write_text(f'headers:\n  Authorization: "Bearer {OLD}"\n')
    inline.chmod(0o600)
    check = check_traces_token(settings, env)
    assert check.status == "PASS" and str(inline) in check.detail  # listed for the rotation
    (profile / "hermes_otel.yaml.bak.1").write_text(f"Bearer {OLD}\n")
    inline.chmod(0o644)
    check = check_traces_token(settings, env)
    assert check.status == "WARN"
    assert "1 backup file(s)" in check.detail and "1 file(s) others may read" in check.detail
    assert OLD not in check.detail + check.fix and "rotate-token" in check.fix


def test_doctor_on_a_loose_token_file_and_without_a_token(tmp_path: Path) -> None:
    rotate(tmp_path / "traces.env", NAME, timedelta(hours=2), NOW, tokens(OLD))
    (tmp_path / "traces.env").chmod(0o644)
    check = check_traces_token(load_settings(config(tmp_path)), {"HOME": str(tmp_path)})
    assert check.status == "WARN" and "chmod 600" in check.fix
    bare = tmp_path / "bare.toml"
    bare.write_text("")
    check = check_traces_token(load_settings(bare), {"HOME": str(tmp_path)})
    assert check.status == "SKIP"


def test_scan_skips_symlinks_and_big_files(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import kelpie.traces.auth as auth

    monkeypatch.setattr(auth, "MAX_SCAN_FILE_BYTES", 1024)
    root = tmp_path / "root"
    root.mkdir()
    (root / "big.bak").write_bytes(OLD.encode() + b"x" * 1024)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "copy.bak").write_text(OLD)
    (root / "link").symlink_to(target)
    (root / "file-link.bak").symlink_to(target / "copy.bak")
    found, limit = token_copies(OLD, [root])
    assert found == [] and limit is None


# --- review: files, owners, revocation, locking --------------------------------------------


def test_a_symlinked_token_file_is_refused(tmp_path: Path) -> None:
    real = tmp_path / "etc" / "traces.env"
    rotate(real, NAME, timedelta(hours=2), NOW, tokens(OLD))
    link = tmp_path / "traces.env"
    link.symlink_to(real)
    with pytest.raises(TokenFileError, match="a symlink"):
        rotate(link, NAME, timedelta(hours=2), NOW, tokens(NEW))
    with pytest.raises(TokenFileError, match="a symlink"):
        TokenFile(link, NAME, Clock())
    assert f"{NAME}={OLD}" in real.read_text()  # untouched


def test_the_folder_is_created_private(tmp_path: Path) -> None:
    path = tmp_path / "new" / "deeper" / "traces.env"
    assert rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD)).created
    assert mode(path.parent) == 0o700 and mode(path) == 0o600


def test_a_folder_others_may_write_is_refused(tmp_path: Path) -> None:
    folder = tmp_path / "shared"
    folder.mkdir()
    folder.chmod(0o777)
    with pytest.raises(TokenFileError, match="others may write"):
        rotate(folder / "traces.env", NAME, timedelta(hours=2), NOW, tokens(OLD))


def test_a_folder_of_another_user_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    folder = tmp_path / "theirs"
    folder.mkdir(mode=0o700)
    me = folder.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: me + 1)  # root too: never in another's folder
    with pytest.raises(TokenFileError, match=r"run as that user|not by this user"):
        rotate(folder / "traces.env", NAME, timedelta(hours=2), NOW, tokens(OLD))
    assert not (folder / "traces.env").exists()


def test_a_symlinked_lock_file_is_refused(tmp_path: Path) -> None:
    victim = tmp_path / "victim"
    folder = tmp_path / "kelpie"
    folder.mkdir(mode=0o700)
    (folder / ".traces.env.lock").symlink_to(victim)
    with pytest.raises(TokenFileError, match="lock file is a symlink"):
        rotate(folder / "traces.env", NAME, timedelta(hours=2), NOW, tokens(OLD))
    assert not victim.exists()


def test_a_fifo_never_blocks(tmp_path: Path) -> None:
    path = tmp_path / "traces.env"
    os.mkfifo(path, 0o600)
    with pytest.raises(TokenFileError, match="not a regular file"):
        TokenFile(path, NAME, Clock())
    with pytest.raises(TokenFileError, match="not a regular file"):
        rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    rotate_path = tmp_path / "ok.env"
    rotate(rotate_path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    clock = Clock()
    auth = TokenFile(rotate_path, NAME, clock)
    rotate_path.unlink()
    os.mkfifo(rotate_path, 0o600)  # swapped in at runtime
    clock.now += timedelta(seconds=2)
    assert auth.check(OLD) is TokenCheck.CURRENT and not auth.status()[0]


def test_serve_trusts_only_a_file_it_could_have_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "traces.env"
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    path.chmod(0o620)
    with pytest.raises(TokenFileError, match="others may write to it"):
        TokenFile(path, NAME, Clock())
    path.chmod(0o600)
    tmp_path.chmod(0o777)
    try:
        with pytest.raises(TokenFileError, match="others may write to this folder"):
            TokenFile(path, NAME, Clock())
    finally:
        tmp_path.chmod(0o700)
    me = path.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: me + 1 if me else 4242)
    if me != 0:  # a file root owns is trusted
        with pytest.raises(TokenFileError, match="not by this user"):
            TokenFile(path, NAME, Clock())


def test_the_file_swapped_during_a_rotation_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.traces.auth as auth

    path = tmp_path / "traces.env"
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    real_open = auth._open

    def swap(p: Path) -> object:
        opened = real_open(p)
        p.unlink()
        p.write_text(f"{NAME}={NEW}\n")  # replaced after it was read
        p.chmod(0o600)
        return opened

    monkeypatch.setattr(auth, "_open", swap)
    with pytest.raises(TokenFileError, match="changed while it was rotated"):
        rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD + "q"))
    assert path.read_text() == f"{NAME}={NEW}\n"
    assert not list(tmp_path.glob(".*.tmp"))


def test_commented_copies_of_the_token_lines_go(tmp_path: Path) -> None:
    path = tmp_path / "traces.env"
    path.write_text(f"# {NAME}={OLD}\n# about the token\n{NAME}={OLD}\n")
    path.chmod(0o600)
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(NEW))
    text = path.read_text()
    assert OLD not in text and "# about the token" in text


def test_the_grace_list_is_capped(tmp_path: Path) -> None:
    from kelpie.traces.auth import MAX_GRACE

    path = tmp_path / "traces.env"
    for i in range(MAX_GRACE + 5):
        rotate(path, NAME, timedelta(hours=2), NOW, tokens(f"tok-{i:03d}-" + "d" * 32))
    assert len(parse_token_file(path.read_text(), NAME).grace) == MAX_GRACE


def test_metrics_show_the_token_file(tmp_path: Path) -> None:
    from kelpie.metrics import Metrics

    path = tmp_path / "traces.env"
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(NEW))
    clock = Clock()
    auth = TokenFile(path, NAME, clock)
    metrics = Metrics()
    metrics.set_traces(True)
    metrics.token_file = auth.status
    metrics.traces_grace_request()
    text = metrics.render()
    assert "kelpie_traces_token_file_ok 1" in text and "kelpie_traces_grace_tokens 1" in text
    assert "kelpie_traces_grace_requests_total 1" in text
    path.write_text("broken\n")
    clock.now += timedelta(seconds=2)
    assert "kelpie_traces_token_file_ok 0" in metrics.render()


def test_a_pasted_token_as_token_file_is_not_echoed(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"
    pasted = "3f9a" * 12  # as `openssl rand -hex 24` prints one
    path.write_text(f'[traces]\ntoken_file = "{pasted}"\n')
    with pytest.raises(ConfigError) as exc:
        load_settings(path)
    assert pasted not in str(exc.value) and "looks like a token" in str(exc.value)


def test_rotate_token_cli_write_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.traces.auth as auth

    def fail(*args: object) -> None:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(auth, "_write_private", fail)
    assert main(["--config", str(config(tmp_path)), "traces", "rotate-token"]) == 1
    assert "No space left on device" in capsys.readouterr().err


def test_doctor_flags_older_backups_and_untrusted_files(tmp_path: Path) -> None:
    rotate(tmp_path / "traces.env", NAME, timedelta(hours=2), NOW, tokens(OLD))
    (tmp_path / "traces.env.bak").write_text(f"{NAME}=an-older-one-00000000000\n")
    settings = load_settings(config(tmp_path))
    check = check_traces_token(settings, {"HOME": str(tmp_path)})
    assert check.status == "WARN" and "maybe an older token" in check.detail
    (tmp_path / "traces.env.bak").unlink()
    (tmp_path / "traces.env").chmod(0o620)
    check = check_traces_token(settings, {"HOME": str(tmp_path)})
    assert check.status in ("WARN", "FAIL") and "will not trust it" in (check.detail + check.fix)


def test_another_users_file_is_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "traces.env"
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    me = path.stat().st_uid
    monkeypatch.setattr(os, "geteuid", lambda: me + 1)  # someone who is not root
    with pytest.raises(TokenFileError, match=r"run as that user|not by this user"):
        rotate(path, NAME, timedelta(hours=2), NOW, tokens(NEW))


def test_zero_grace_revokes_every_previous_token(tmp_path: Path) -> None:
    path = tmp_path / "traces.env"
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(NEW))  # OLD in grace
    done = rotate(path, NAME, timedelta(0), NOW, tokens(OLD + "z"))
    assert done.grace == 0 and f"{NAME}_GRACE" not in path.read_text()
    auth = TokenFile(path, NAME, Clock())
    assert auth.check(OLD) is TokenCheck.INVALID and auth.check(NEW) is TokenCheck.INVALID


def test_concurrent_rotations_lose_no_token(tmp_path: Path) -> None:
    import threading

    path = tmp_path / "traces.env"
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    made = iter(f"tok-{i:02d}-" + "c" * 32 for i in range(8))
    lock = threading.Lock()

    def one() -> str:
        with lock:
            return next(made)

    threads = [
        threading.Thread(target=rotate, args=(path, NAME, timedelta(hours=2), NOW, one))
        for _ in range(8)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    state = parse_token_file(path.read_text(), NAME)
    assert len(state.grace) == 8  # OLD and the seven replaced ones


def test_no_repr_carries_a_token(tmp_path: Path) -> None:
    from kelpie.core.spans import MemorySpanStore

    path = tmp_path / "traces.env"
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(NEW))
    auth = TokenFile(path, NAME, Clock())
    ingest = TraceIngest(MemorySpanStore(), Clock(), token=NEW, tokens=auth)
    text = repr(auth) + repr(auth._state) + repr(ingest)
    assert NEW not in text and OLD not in text
    assert all(g.sha256 not in text for g in auth._state.grace)


def test_a_clock_that_goes_back_does_not_stall_the_reread(tmp_path: Path) -> None:
    path = tmp_path / "traces.env"
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(OLD))
    clock = Clock()
    auth = TokenFile(path, NAME, clock)
    auth.check(OLD)
    clock.now -= timedelta(hours=1)
    rotate(path, NAME, timedelta(hours=2), NOW, tokens(NEW))
    assert auth.check(NEW) is TokenCheck.CURRENT


@pytest.mark.parametrize(
    ("data", "message"),
    [(b"x" * (64 * 1024 + 1), "larger than"), (b"\xff\xfe", "not UTF-8")],
)
def test_unreadable_files(tmp_path: Path, data: bytes, message: str) -> None:
    path = tmp_path / "traces.env"
    path.write_bytes(data)
    with pytest.raises(TokenFileError, match=message):
        TokenFile(path, NAME, Clock())


def test_parse_quotes_and_export(tmp_path: Path) -> None:
    assert parse_token_file(f"export\t{NAME}={OLD}\n", NAME).token == OLD
    with pytest.raises(TokenFileError, match="unmatched quote"):
        parse_token_file(f"{NAME}='{OLD}\n", NAME)


def test_a_pasted_token_as_token_env_is_not_echoed(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text(f'[traces]\ntoken_env = "{OLD}-x y"\n')
    with pytest.raises(ConfigError) as exc:
        load_settings(path)
    assert OLD not in str(exc.value) and "token_env" in str(exc.value)


def test_rotate_token_cli_errors(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = config(tmp_path)
    assert main(["--config", str(path), "traces", "rotate-token", "--grace", "soon"]) == 2
    assert "--grace must be a duration" in capsys.readouterr().err
    (tmp_path / "traces.env").mkdir()  # a folder where the file should be
    assert main(["--config", str(path), "traces", "rotate-token"]) in (1, 2)
    assert "kelpie:" in capsys.readouterr().err


def test_the_scan_reads_in_chunks_and_flags_older_backups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.traces.auth as auth

    monkeypatch.setattr(auth, "_CHUNK", 16)
    (tmp_path / "far.yaml").write_text("x" * 37 + OLD + "y" * 9)  # across chunks
    (tmp_path / "older.env.bak").write_text(f"{NAME}=an-older-token-0000000\n")
    (tmp_path / "plain.yaml").write_text(f"{NAME}=something\n")  # not a backup: ignored
    found, limit = token_copies(OLD, [tmp_path], marker=f"{NAME}=")
    assert limit is None
    assert {(c.path.name, c.marker) for c in found} == {
        ("far.yaml", False),
        ("older.env.bak", True),
    }


def test_doctor_skips_with_a_hub(tmp_path: Path) -> None:
    path = tmp_path / "kelpie.toml"
    path.write_text(
        '[hub]\nlisten = "127.0.0.1:9465"\nallow_plain_http = true\n'
        '[[hub.agents]]\nname = "a"\ntoken_env = "KELPIE_TEST_AGENT_TOKEN"\n'
        f'[traces]\ntoken_file = "{tmp_path / "none.env"}"\n'
    )
    check = check_traces_token(load_settings(path), {"HOME": str(tmp_path)})
    assert check.status == "SKIP" and "[hub]" in check.detail


def test_root_never_writes_through_a_folder_another_user_may_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.traces.auth as auth

    theirs = tmp_path / "alice"
    theirs.mkdir(mode=0o755)
    me = theirs.stat().st_uid
    real_lstat = os.lstat

    def owned_by_alice(p: object) -> os.stat_result:
        st = real_lstat(p)  # type: ignore[arg-type]
        if Path(p) == theirs:  # type: ignore[arg-type]
            fields = list(st)
            fields[4] = me + 1000  # st_uid
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(auth.os, "lstat", owned_by_alice)
    with pytest.raises(TokenFileError, match="not by this user"):
        rotate(theirs / "kelpie" / "token", NAME, timedelta(hours=2), NOW, tokens(OLD))
    assert not (theirs / "kelpie").exists()  # checked before anything is created


def test_a_writable_folder_above_is_refused(tmp_path: Path) -> None:
    above = tmp_path / "shared"
    above.mkdir()
    above.chmod(0o777)
    try:
        with pytest.raises(TokenFileError, match="others may write"):
            rotate(above / "kelpie" / "token", NAME, timedelta(hours=2), NOW, tokens(OLD))
        assert not (above / "kelpie").exists()
        sticky = tmp_path / "sticky"
        sticky.mkdir()
        sticky.chmod(0o1777)  # as /tmp: others cannot replace what is ours
        rotate(sticky / "kelpie" / "token", NAME, timedelta(hours=2), NOW, tokens(OLD))
    finally:
        above.chmod(0o700)


def test_one_huge_folder_is_skipped_not_the_scan(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.traces.auth as auth

    monkeypatch.setattr(auth, "MAX_SCAN_ENTRIES", 3)
    big = tmp_path / "a-big"
    big.mkdir()
    for i in range(5):
        (big / f"f{i}").write_text(OLD)
    (tmp_path / "z.bak").write_text(OLD)
    found, limit = token_copies(OLD, [tmp_path])
    assert [c.path.name for c in found] == ["z.bak"]
    assert limit is not None and "skipped" in limit and "over 3 entries" in limit


@pytest.mark.parametrize(
    ("name", "token"),
    [
        ("kelpie_traces_token_v2", False),
        ("my_traces_token_2024", False),
        ("KELPIE_TRACES_TOKEN", False),
        ("3f9a" * 12, True),
        ("a1_b2_c3_d4_e5_f6_g7_h8", True),
    ],
)
def test_looks_like_token(name: str, token: bool) -> None:
    from kelpie.traces.auth import looks_like_token

    assert looks_like_token(name) is token


def test_a_symlink_another_user_planted_is_not_followed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.traces.auth as auth

    shared = tmp_path / "tmp"
    shared.mkdir()
    shared.chmod(0o1777)  # as /tmp
    target = tmp_path / "rootonly"
    target.mkdir(mode=0o755)
    planted = shared / "kelpie"
    planted.symlink_to(target)
    me = os.lstat(planted).st_uid
    real_lstat = os.lstat

    def planted_by_someone(p: object) -> os.stat_result:
        st = real_lstat(p)  # type: ignore[arg-type]
        if Path(p) == planted:  # type: ignore[arg-type]
            fields = list(st)
            fields[4] = me + 1000  # st_uid: another user's link
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(auth.os, "lstat", planted_by_someone)
    with pytest.raises(TokenFileError, match="a symlink of uid"):
        rotate(planted / "token", NAME, timedelta(hours=2), NOW, tokens(OLD))
    assert not (target / "token").exists()


def test_own_symlinks_on_the_way_are_followed(tmp_path: Path) -> None:
    real = tmp_path / "real"
    real.mkdir(mode=0o700)
    (tmp_path / "alias").symlink_to(real)  # ours: fine
    rotate(tmp_path / "alias" / "token", NAME, timedelta(hours=2), NOW, tokens(OLD))
    assert (real / "token").exists()


def test_a_link_planted_between_the_check_and_mkdir_is_refused(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.traces.auth as auth

    shared = tmp_path / "tmp"
    shared.mkdir()
    shared.chmod(0o1777)
    target = tmp_path / "rootonly"
    target.mkdir(mode=0o755)
    planted = shared / "kelpie"
    me = os.lstat(shared).st_uid
    real_check, real_lstat = auth.path_problem, os.lstat
    calls = []

    def check_then_plant(folder: Path) -> str | None:
        found = real_check(folder)
        calls.append(found)
        if len(calls) == 1:
            planted.symlink_to(target)  # another user wins the race
        return found

    def theirs(p: object) -> os.stat_result:
        st = real_lstat(p)  # type: ignore[arg-type]
        if Path(p) == planted:  # type: ignore[arg-type]
            fields = list(st)
            fields[4] = me + 1000
            return os.stat_result(fields)
        return st

    monkeypatch.setattr(auth, "path_problem", check_then_plant)
    monkeypatch.setattr(auth.os, "lstat", theirs)
    with pytest.raises(TokenFileError, match="a symlink of uid"):
        rotate(planted / "token", NAME, timedelta(hours=2), NOW, tokens(OLD))
    assert not (target / "token").exists()


def test_doctor_says_when_it_cannot_check(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import kelpie.doctor as doctor

    rotate(tmp_path / "traces.env", NAME, timedelta(hours=2), NOW, tokens(OLD))

    def denied(path: Path, st: object = None) -> str | None:
        raise PermissionError(13, "Permission denied")

    monkeypatch.setattr(doctor, "trust_problem", denied)
    check = check_traces_token(load_settings(config(tmp_path)), {"HOME": str(tmp_path)})
    assert check.status == "WARN" and "cannot check" in check.detail
