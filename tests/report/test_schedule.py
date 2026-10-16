"""The daily report scheduled inside ``kelpie serve`` ([report] at)."""

from __future__ import annotations

from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from time import monotonic, sleep
from typing import Any

import pytest

from kelpie.app import load_settings
from kelpie.core.storage import SqliteLedgerStore
from kelpie.report.cli import report_schedule, scheduled_run
from kelpie.report.config import ReportSettings, load_report
from kelpie.report.schedule import MAX_ATTEMPTS, RETRY_AFTER, ReportScheduler, RunLog

BERLIN = ReportSettings(timezone="Europe/Berlin", at=time(0, 15))


class Clock:
    def __init__(self, at: datetime) -> None:
        self.now = at

    def __call__(self) -> datetime:
        return self.now


def scheduler(
    clock: Clock, results: list[bool] | None = None, db: Path | None = None
) -> tuple[ReportScheduler, list[date]]:
    ran: list[date] = []
    answers = list(results or [])

    def run_day(day: date) -> bool:
        ran.append(day)
        return answers.pop(0) if answers else True

    s = ReportScheduler(BERLIN, time(0, 15), run_day, RunLog(db), clock)
    s.monotonic = lambda: clock.now.timestamp()  # retries follow the test's clock
    return s, ran


def test_runs_yesterday_once_after_the_local_time() -> None:
    clock = Clock(datetime(2026, 3, 2, 23, 10, tzinfo=UTC))  # 00:10 in Berlin on the 3rd
    s, ran = scheduler(clock)
    assert s.tick() is None
    clock.now = datetime(2026, 3, 2, 23, 16, tzinfo=UTC)  # 00:16 in Berlin
    assert s.tick() == date(2026, 3, 2)
    clock.now += timedelta(hours=5)
    assert s.tick() is None and ran == [date(2026, 3, 2)]
    clock.now += timedelta(days=1)
    assert s.tick() == date(2026, 3, 3)


def test_catches_up_the_same_day_but_not_older_days() -> None:
    clock = Clock(datetime(2026, 3, 3, 9, tzinfo=UTC))  # serve was down at 00:15
    s, ran = scheduler(clock)
    assert s.tick() == date(2026, 3, 2) and ran == [date(2026, 3, 2)]


def test_a_restart_does_not_send_twice(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    SqliteLedgerStore(db).close()
    clock = Clock(datetime(2026, 3, 3, 9, tzinfo=UTC))
    first, _ = scheduler(clock, db=db)
    assert first.tick() == date(2026, 3, 2)
    again, ran = scheduler(clock, db=db)  # a new process
    assert again.tick() is None and ran == []


def test_failures_retry_then_give_up_for_the_day() -> None:
    clock = Clock(datetime(2026, 3, 3, 9, tzinfo=UTC))
    s, ran = scheduler(clock, results=[False] * MAX_ATTEMPTS)
    assert s.tick() is None
    assert s.tick() is None and len(ran) == 1  # waits RETRY_AFTER
    for _ in range(MAX_ATTEMPTS):
        clock.now += RETRY_AFTER
        s.tick()
    assert len(ran) == MAX_ATTEMPTS


def test_a_raising_run_is_a_failure() -> None:
    def boom(day: date) -> bool:
        raise RuntimeError("adapter down")

    s = ReportScheduler(
        BERLIN, time(0, 15), boom, RunLog(None), Clock(datetime(2026, 3, 3, 9, tzinfo=UTC))
    )
    assert s.tick() is None


def test_thread_starts_and_stops() -> None:
    s, ran = scheduler(Clock(datetime(2026, 3, 3, 9, tzinfo=UTC)))
    s.start()
    deadline = monotonic() + 5
    while not ran and monotonic() < deadline:
        sleep(0.01)
    s.stop()
    assert ran == [date(2026, 3, 2)]


def test_at_config() -> None:
    assert load_report({"at": "00:15"}).at == time(0, 15)
    assert load_report(None).at is None
    for bad in ("0:15", "24:00", "noon", 15, "00:15:00"):
        with pytest.raises(ValueError, match="at must be"):
            load_report({"at": bad})


class FakeSender:
    def __init__(self) -> None:
        self.sent: list[tuple[str, str, Any]] = []

    def send(self, text: str, severity: str, channels: Any = None) -> None:
        self.sent.append((text, severity, channels))


def test_scheduled_run_writes_and_sends(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    config = tmp_path / "kelpie.toml"
    config.write_text(
        f'[storage]\npath = "{tmp_path / "kelpie.db"}"\n'
        f'[report]\nat = "00:15"\ndir = "{tmp_path / "reports"}"\n'
    )
    SqliteLedgerStore(tmp_path / "kelpie.db").close()
    settings = load_settings(config)
    sender = FakeSender()
    assert scheduled_run(settings, sender)(date(2026, 3, 2))
    assert (tmp_path / "reports" / "kelpie-report-2026-03-02.md").exists()
    [(text, severity, _)] = sender.sent
    assert text.startswith("Kelpie daily report 2026-03-02") and severity == "warning"
    assert scheduled_run(settings, None)(date(2026, 3, 2))  # no alerts: written only
    (tmp_path / "kelpie.db").unlink()
    (tmp_path / "kelpie.db").write_bytes(b"not a database")
    assert not scheduled_run(settings, sender)(date(2026, 3, 2))  # retried later


def test_report_schedule_only_with_at(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("KELPIE_DB", raising=False)
    config = tmp_path / "kelpie.toml"
    config.write_text('[storage]\npath = ":memory:"\n')
    assert report_schedule(load_settings(config), None) is None
    config.write_text('[storage]\npath = ":memory:"\n[report]\nat = "23:59"\n')
    assert report_schedule(load_settings(config), None) is not None


@pytest.mark.parametrize("bad", ["0015Z", "1230Z", "T0015", "00:15Z", "0:15", "00.15"])
def test_at_is_strictly_hh_mm(bad: str) -> None:
    with pytest.raises(ValueError, match="at must be"):
        load_report({"at": bad})


def test_a_failing_tick_does_not_end_the_schedule() -> None:
    class Broken(RunLog):
        def done(self, day: date) -> bool | None:
            raise PermissionError("denied")

    calls: list[date] = []
    s = ReportScheduler(BERLIN, time(0, 15), lambda d: calls.append(d) or True, Broken(None),
                        Clock(datetime(2026, 3, 3, 9, tzinfo=UTC)))  # fmt: skip
    s.start()
    sleep(0.05)
    assert s._thread is not None and s._thread.is_alive()
    s.stop()


def test_unreadable_run_log_skips_instead_of_resending(tmp_path: Path) -> None:
    db = tmp_path / "kelpie.db"
    db.write_bytes(b"not a database")
    s, ran = scheduler(Clock(datetime(2026, 3, 3, 9, tzinfo=UTC)), db=db)
    assert s.tick() is None and ran == []


def test_a_run_interrupted_by_shutdown_is_not_recorded() -> None:
    clock = Clock(datetime(2026, 3, 3, 9, tzinfo=UTC))
    runs = RunLog(None)
    holder: list[ReportScheduler] = []

    def run_day(day: date) -> bool:
        holder[0]._stop.set()  # shutdown requested while the report runs
        return True

    s = ReportScheduler(BERLIN, time(0, 15), run_day, runs, clock)
    holder.append(s)
    assert s.tick() is None and runs.done(date(2026, 3, 2)) is False


def test_serve_starts_and_stops_the_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.server
    from kelpie.app import build_app

    monkeypatch.delenv("KELPIE_DB", raising=False)
    config = tmp_path / "kelpie.toml"
    config.write_text(f'[storage]\npath = "{tmp_path / "k.db"}"\n[traces]\nenabled = false\n'
                      '[report]\nat = "00:15"\n')  # fmt: skip
    order: list[str] = []
    app = build_app(load_settings(config))

    class Recording:
        def start(self) -> None:
            order.append("start")

        def stop(self, timeout: float = 5.0) -> None:
            order.append("stop")

    app.services.append(Recording())

    class Server:
        def serve_forever(self) -> None:
            order.append("serving")

        def server_close(self) -> None:
            pass

    kelpie.server.run(app, Server())  # type: ignore[arg-type]
    assert order == ["start", "serving", "stop"]


def test_kelpie_serve_registers_the_schedule(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.server
    from kelpie.cli import main
    from kelpie.report.schedule import ReportScheduler as Scheduler

    monkeypatch.delenv("KELPIE_DB", raising=False)
    seen: list[Any] = []
    monkeypatch.setattr(kelpie.server, "serve", lambda app, *a, **k: seen.append(app))
    config = tmp_path / "kelpie.toml"
    config.write_text(f'[storage]\npath = "{tmp_path / "k.db"}"\n[traces]\nenabled = false\n'
                      '[report]\nat = "00:15"\n')  # fmt: skip
    assert main(["--config", str(config), "serve"]) == 0
    [app] = seen
    assert [type(s) for s in app.services] == [Scheduler]
    app.close()
