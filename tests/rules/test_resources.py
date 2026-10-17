from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from kelpie.core.actions import Action, ActionKind, Severity
from kelpie.core.model import Resource, ResourceKind, Snapshot, Status, Task
from kelpie.rules import REGISTRY
from kelpie.rules.resources import Resources, Thresholds, build

NOW = datetime(2026, 3, 2, 12, tzinfo=UTC)
M = timedelta(minutes=1)
K = ResourceKind


def res(
    kind: ResourceKind,
    used: float | None,
    limit: float | None,
    host: str | None = "host-a",
    name: str | None = None,
    at: datetime | None = NOW,
    unit: str = "GB",
) -> Resource:
    return Resource(name or f"{host}:{kind}", kind, host, used, limit, unit, at=at)


def task(
    tid: str,
    status: Status = Status.IN_PROGRESS,
    labels: tuple[str, ...] = ("heavy",),
    host: str | None = None,
    role: str | None = None,
) -> Task:
    return Task(tid, tid, status, NOW - 60 * M, role=role, labels=labels, host=host)


def run(
    *resources: Resource, tasks: tuple[Task, ...] = (), rule: Resources | None = None
) -> list[Action]:
    snapshot = Snapshot(now=NOW, resources=resources, tasks=tasks)
    return list((rule or Resources()).evaluate(snapshot))


HEAVY = Resources(max_heavy_per_host=1)
BUSY = (res(K.LOAD, 20, 8, unit="load"), res(K.MEMORY, 28.8, 32))  # 2.5x, 90% -> warning
THREE_RUNNING = tuple(task(f"T-{i}", host="host-a") for i in range(3))


def heavy_alerts(actions: list[Action]) -> list[Action]:
    return [a for a in actions if a.state and a.state.endswith(("heavy", "unplaced"))]


def test_registered_and_defaults() -> None:
    rule = REGISTRY["resources"]({})
    assert isinstance(rule, Resources)
    assert rule.max_heavy_per_host is None
    assert rule.heavy_labels == ("heavy",)


# --- thresholds -------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("used", "severity"),
    [(84, None), (85, Severity.WARNING), (94, Severity.WARNING), (95, Severity.CRITICAL)],
)
def test_disk_thresholds_are_inclusive(used: float, severity: Severity | None) -> None:
    actions = run(res(K.DISK, used, 100, name="host-a:/"))
    if severity is None:
        assert actions == []
        return
    (alert,) = actions
    assert alert.kind is ActionKind.ALERT
    assert alert.severity is severity
    assert alert.params["host"] == "host-a"
    assert "disk host-a:/" in alert.reason


def test_state_and_key_do_not_depend_on_the_reading() -> None:
    (a,) = run(res(K.DISK, 86, 100, name="host-a:/"))
    (b,) = run(res(K.DISK, 90, 100, name="host-a:/"))
    (c,) = run(res(K.DISK, 97, 100, name="host-a:/"))
    assert a.state == b.state == c.state == "resources:disk:host-a:/"
    # A rule keeps no history, so no hysteresis band is possible: the key stays put, and
    # the state tracker escalates only when the severity rises above what it announced.
    assert a.key == b.key == c.key
    assert (a.severity, c.severity) == (Severity.WARNING, Severity.CRITICAL)
    assert a.since is None  # the state tracker has it


def test_each_mount_is_its_own_state() -> None:
    root, data = run(
        res(K.DISK, 90, 100, name="host-a:/"), res(K.DISK, 96, 100, name="host-a:/data")
    )
    assert root.state != data.state
    assert (root.severity, data.severity) == (Severity.WARNING, Severity.CRITICAL)


def test_memory_load_cpu_and_processes() -> None:
    rule = build({"cpu_warning": 0.95})
    readings = (
        res(K.MEMORY, 31.5, 32),  # 98% -> critical
        res(K.LOAD, 13, 8, unit="load"),  # 1.6x -> warning
        res(K.CPU, 96, 100, unit="percent"),  # 96% -> warning, if switched on
        res(K.PROCESSES, 30000, 32768, unit="processes"),  # 92% -> warning
    )
    by_kind = {a.params["kind"]: a.severity for a in run(*readings, rule=rule)}
    assert by_kind == {
        "memory": Severity.CRITICAL,
        "load": Severity.WARNING,
        "cpu": Severity.WARNING,
        "processes": Severity.WARNING,
    }
    # CPU is off by default: load covers overload.
    assert "cpu" not in {a.params["kind"] for a in run(*readings)}
    load = next(a for a in run(*readings) if a.params["kind"] == "load")
    assert "1.6x its cores" in load.reason


def test_healthy_readings_make_no_alert() -> None:
    assert (
        run(
            res(K.DISK, 200, 500),
            res(K.MEMORY, 10, 32),
            res(K.LOAD, 2, 8, unit="load"),
            res(K.CPU, 20, 100, unit="percent"),
        )
        == []
    )


def test_readings_that_cannot_be_judged_are_ignored() -> None:
    assert (
        run(
            res(K.DISK, None, 100),  # no value
            res(K.DISK, 99, None),  # no limit: no utilization
            res(K.DISK, 99, 0),
            res(K.PROCESSES, 30000, None, unit="processes"),
            res(K.DISK, 99, 100, host=None),  # not a host resource
            res(K.LLM_QUOTA, 50, 100, host="host-a"),  # half a quota left is fine
            res(K.LLM_QUOTA, None, 100, host="host-a"),  # nothing known
            res(K.AUTH, 1, 1, host="host-a"),  # no expiry
        )
        == []
    )


def test_stale_readings_are_ignored_and_untimed_ones_are_current() -> None:
    assert run(res(K.DISK, 99, 100, at=NOW - 11 * M)) == []
    assert len(run(res(K.DISK, 99, 100, at=NOW - 10 * M))) == 1
    assert len(run(res(K.DISK, 99, 100, at=None))) == 1
    assert run(res(K.DISK, 99, 100, at=NOW - 3 * M), rule=Resources(max_reading_age=2 * M)) == []


def test_the_newest_of_duplicate_readings_wins() -> None:
    old = res(K.DISK, 99, 100, name="host-a:/", at=NOW - 5 * M)
    new = res(K.DISK, 10, 100, name="host-a:/", at=NOW - 1 * M)
    assert run(old, new) == []
    assert run(new, old) == []
    assert len(run(res(K.DISK, 99, 100, name="host-a:/"), old)) == 1


def test_custom_thresholds() -> None:
    rule = Resources(thresholds={K.DISK: Thresholds(0.5, None)})
    (alert,) = run(res(K.DISK, 60, 100), rule=rule)
    assert alert.severity is Severity.WARNING
    assert (
        run(res(K.DISK, 99, 100, name="host-a:/"), res(K.MEMORY, 31.9, 32), rule=rule)[0].severity
        is Severity.WARNING
    )  # no critical level configured
    assert run(res(K.MEMORY, 31.9, 32), rule=rule) == []  # kinds without thresholds: ignored


# --- heavy-task concurrency -------------------------------------------------------------


def test_overloaded_host_with_too_many_heavy_tasks() -> None:
    queued = (task("T-8", Status.READY), task("T-9", Status.READY), task("T-7", Status.REVIEW))
    actions = run(*BUSY, tasks=(*THREE_RUNNING, *queued), rule=HEAVY)
    (alert,) = heavy_alerts(actions)
    assert alert.severity is Severity.WARNING
    assert alert.task_id is None
    assert alert.state == "resources:host-a:heavy"
    assert alert.params["host"] == "host-a"
    assert alert.params["max_heavy"] == 1
    assert alert.params["running_heavy"] == ["T-0", "T-1", "T-2"]
    assert alert.params["queued_heavy"] == ["T-8", "T-9"]  # review tasks are not queued
    assert "heavy" in alert.reason
    assert "T-8, T-9" in alert.reason


def test_pressure_alerts_stay_open_next_to_the_heavy_alert() -> None:
    # Dropping them would let the state tracker report them as resolved while they last.
    actions = run(*BUSY, tasks=THREE_RUNNING, rule=HEAVY)
    assert sorted(a.state or "" for a in actions) == [
        "resources:host-a:heavy",
        "resources:load:host-a:load",
        "resources:memory:host-a:memory",
    ]
    covered = [a for a in actions if a not in heavy_alerts(actions)]
    assert all("covered by the heavy-task alert for host-a" in a.reason for a in covered)
    plain = run(*BUSY)
    assert all("heavy-task alert" not in a.reason for a in plain)


def test_heavy_alert_key_changes_only_with_the_limit() -> None:
    (a,) = heavy_alerts(run(*BUSY, tasks=THREE_RUNNING, rule=HEAVY))
    (b,) = heavy_alerts(run(*BUSY, tasks=THREE_RUNNING[:2], rule=HEAVY))
    (c,) = heavy_alerts(run(*BUSY, tasks=THREE_RUNNING, rule=Resources(max_heavy_per_host=2)))
    assert a.key == b.key != c.key


def test_disk_alerts_are_unrelated_to_the_heavy_alert() -> None:
    actions = run(*BUSY, res(K.DISK, 90, 100, name="host-a:/"), tasks=THREE_RUNNING, rule=HEAVY)
    disk = next(a for a in actions if a.params.get("kind") == "disk")
    assert "heavy" not in disk.reason


def test_at_the_limit_there_is_no_heavy_alert_but_the_pressure_is_still_reported() -> None:
    actions = run(*BUSY, tasks=THREE_RUNNING[:1], rule=HEAVY)
    assert heavy_alerts(actions) == []
    assert {a.params["kind"] for a in actions} == {"load", "memory"}


def test_healthy_host_with_many_heavy_tasks_is_left_alone() -> None:
    healthy = (res(K.LOAD, 2, 8, unit="load"), res(K.MEMORY, 10, 32))
    assert run(*healthy, tasks=THREE_RUNNING, rule=HEAVY) == []


def test_light_tasks_do_not_count() -> None:
    light = tuple(task(f"T-{i}", labels=(), host="host-a") for i in range(5))
    assert heavy_alerts(run(*BUSY, tasks=light, rule=HEAVY)) == []


def test_heavy_by_role() -> None:
    rule = Resources(max_heavy_per_host=1, heavy_roles=("builder",))
    tasks = tuple(task(f"T-{i}", labels=(), role="builder", host="host-a") for i in range(2))
    assert len(heavy_alerts(run(*BUSY, tasks=tasks, rule=rule))) == 1
    other = tuple(task(f"T-{i}", labels=(), role="developer", host="host-a") for i in range(2))
    assert heavy_alerts(run(*BUSY, tasks=other, rule=rule)) == []


def test_tasks_with_a_known_host_count_only_there() -> None:
    elsewhere = tuple(task(f"T-{i}", host="host-b") for i in range(3))
    assert heavy_alerts(run(*BUSY, tasks=elsewhere, rule=HEAVY)) == []
    assert len(heavy_alerts(run(*BUSY, tasks=THREE_RUNNING, rule=HEAVY))) == 1


def test_hostless_tasks_belong_to_the_only_host_with_readings() -> None:
    tasks = tuple(task(f"T-{i}") for i in range(3))
    (alert,) = heavy_alerts(run(*BUSY, tasks=tasks, rule=HEAVY))
    assert alert.params["host"] == "host-a"


def test_hostless_tasks_on_several_hosts_get_one_alert_that_claims_no_host() -> None:
    other = (res(K.MEMORY, 31, 32, host="host-b"), res(K.DISK, 10, 100, host="host-c"))
    tasks = tuple(task(f"T-{i}") for i in range(4))  # 4 > 1 per host x 3 hosts
    actions = run(*BUSY, *other, tasks=tasks, rule=HEAVY)
    (alert,) = heavy_alerts(actions)
    assert alert.state == "resources:heavy:unplaced"
    assert "host" not in alert.params
    assert alert.params["overloaded_hosts"] == ["host-a", "host-b"]
    assert alert.params["running_heavy"] == ["T-0", "T-1", "T-2", "T-3"]
    assert "do not say which host" in alert.reason
    assert "Task.host" in alert.reason


def test_hostless_tasks_within_what_the_hosts_allow_are_left_alone() -> None:
    other = (res(K.MEMORY, 31, 32, host="host-b"),)
    tasks = tuple(task(f"T-{i}") for i in range(2))  # 2 <= 1 per host x 2 hosts
    assert heavy_alerts(run(*BUSY, *other, tasks=tasks, rule=HEAVY)) == []


def test_placed_and_hostless_tasks_are_counted_together_farm_wide() -> None:
    other = (res(K.MEMORY, 31, 32, host="host-b"),)
    tasks = (task("T-0", host="host-a"), task("T-1"), task("T-2"))
    (alert,) = heavy_alerts(run(*BUSY, *other, tasks=tasks, rule=HEAVY))
    assert alert.state == "resources:heavy:unplaced"
    assert alert.params["running_heavy"] == ["T-0", "T-1", "T-2"]


def test_no_unplaced_alert_without_pressure() -> None:
    healthy = (res(K.MEMORY, 10, 32), res(K.MEMORY, 10, 32, host="host-b"))
    tasks = tuple(task(f"T-{i}") for i in range(5))
    assert run(*healthy, tasks=tasks, rule=HEAVY) == []


def test_without_a_limit_only_resource_alerts_are_raised() -> None:
    actions = run(*BUSY, tasks=THREE_RUNNING)
    assert {a.params["kind"] for a in actions} == {"load", "memory"}


def test_many_queued_tasks_are_summarized() -> None:
    queued = tuple(task(f"Q-{i}", Status.READY) for i in range(8))
    (alert,) = heavy_alerts(run(*BUSY, tasks=(*THREE_RUNNING, *queued), rule=HEAVY))
    assert "and 3 more" in alert.reason
    assert len(alert.params["queued_heavy"]) == 8


# --- configuration ----------------------------------------------------------------------


def test_build_reads_every_parameter() -> None:
    rule = build(
        {
            "disk_warning": 0.7,
            "disk_critical": 0.9,
            "memory_critical": False,
            "load_warning": 2,
            "cpu_warning": 0.9,
            "heavy_label": "heavy",
            "heavy_labels": ["build", "e2e"],
            "heavy_roles": "builder",
            "max_heavy_per_host": 2,
            "max_reading_age": "5m",
        }
    )
    assert isinstance(rule, Resources)
    assert rule.thresholds[K.DISK] == Thresholds(0.7, 0.9)
    assert rule.thresholds[K.MEMORY] == Thresholds(0.90, None)
    assert rule.thresholds[K.LOAD] == Thresholds(2.0, 3.0)
    assert rule.thresholds[K.CPU] == Thresholds(0.9, None)
    assert build({}).thresholds[K.CPU] == Thresholds(None, None)
    assert rule.heavy_labels == ("heavy", "build", "e2e")
    assert rule.heavy_roles == ("builder",)
    assert rule.max_heavy_per_host == 2
    assert rule.max_reading_age == 5 * M


def test_max_heavy_can_be_switched_off() -> None:
    assert build({"max_heavy_per_host": False}).max_heavy_per_host is None


@pytest.mark.parametrize(
    "config",
    [
        {"nope": 1},
        {"disk_warning": 0},
        {"disk_warning": -1},
        {"disk_warning": 1.5},
        {"disk_warning": True},
        {"disk_warning": "high"},
        {"disk_warning": 0.9, "disk_critical": 0.9},
        {"memory_warning": 0.99},  # above the default critical level
        {"max_heavy_per_host": 0},
        {"max_heavy_per_host": 1.5},
        {"max_heavy_per_host": True},
        {"heavy_label": 3},
        {"heavy_roles": [""]},
        {"max_reading_age": "0m"},
        {"max_reading_age": "soon"},
        {"max_reading_age": None},
    ],
)
def test_build_rejects_bad_config(config: dict) -> None:
    with pytest.raises(ValueError):
        build(config)


def test_load_may_exceed_one_per_core() -> None:
    assert build({"load_warning": 4, "load_critical": 8}).thresholds[K.LOAD] == Thresholds(4, 8)


# --- disk time-to-full (incident 19) -----------------------------------------------------


def disk(used: float, growth: float | None, limit: float = 500) -> Resource:
    return replace(res(K.DISK, used, limit, name="host-a:/"), growth_per_day=growth)


def path(directory: str, growth: float, size: float = 10, host: str = "host-a") -> Resource:
    return Resource(f"{host}:path:{directory}", K.PATH, host, size, None, "GB", at=NOW,
                    growth_per_day=growth)  # fmt: skip


def full_alerts(actions: list[Action]) -> list[Action]:
    """Alerts with a forecast: their own, or a utilization alert that carries it."""
    return [a for a in actions if "hours_to_full" in a.params]


def test_a_disk_that_fills_soon_alerts_with_what_grows() -> None:
    sims = "/Users/agent/Library/Developer/CoreSimulator/Devices"
    paths = (
        path(sims, 8.7, 34),
        path("/Users/agent/.hermes", 0.3),
        path("/tmp", -1.0),
        path("/var/cache", 0.2),
        path("/srv/x", 0.1),
        path("/other", 50, host="host-b"),
    )
    # 30 GB left at 9 GB a day: about 80 hours -> no alert; 18 GB left: 48h -> warning.
    assert full_alerts(run(disk(470, 9.0), *paths)) == []
    lax = Resources(thresholds={K.DISK: Thresholds(0.99, 0.999)})  # not past utilization
    [a] = full_alerts(run(disk(482, 9.0), *paths, rule=lax))
    assert a.severity is Severity.WARNING and a.state == "resources:disk:host-a:/"
    assert a.params["hours_to_full"] == 48.0
    assert [p["path"] for p in a.params["growing_paths"]] == [
        sims,
        "/Users/agent/.hermes",
        "/var/cache",
    ]
    assert f"{sims} (+8.7 GB/day)" in a.reason and "2.0 days (48h)" in a.reason
    [c] = full_alerts(run(disk(491, 9.0), *paths, rule=lax))
    assert c.severity is Severity.CRITICAL and c.key == a.key  # one state; it escalates
    [bare] = full_alerts(run(disk(495, 9.0)))
    assert bare.state == a.state  # past its threshold: one alert, the same condition
    assert "scripts/kelpie-path-sizes.sh" in bare.reason and bare.params["growing_paths"] == []


def test_no_forecast_without_real_growth() -> None:
    assert full_alerts(run(disk(495, None))) == []
    assert full_alerts(run(disk(495, 0.05))) == []  # under min_growth_per_day: noise
    assert full_alerts(run(disk(495, -3.0))) == []  # shrinking
    off = Resources(time_to_full_warning=None, time_to_full_critical=None)
    assert full_alerts(run(disk(495, 9.0), rule=off)) == []
    stale = Resource("host-a:/", K.DISK, "host-a", 495, 500, "GB", at=NOW - 60 * M,
                     growth_per_day=9.0)  # fmt: skip
    assert full_alerts(run(stale)) == []


def test_build_time_to_full() -> None:
    r = build({"time_to_full_warning": "48h", "time_to_full_critical": "12h",
               "min_growth_per_day": 1, "growing_paths_shown": 5})  # fmt: skip
    assert (r.time_to_full_warning, r.time_to_full_critical) == (48 * 60 * M, 12 * 60 * M)
    assert r.min_growth_per_day == 1.0 and r.growing_paths_shown == 5
    assert build({"time_to_full_warning": False}).time_to_full_warning is None
    for bad, message in [
        ({"time_to_full_critical": "80h"}, "shorter than time_to_full_warning"),
        ({"min_growth_per_day": 0}, "positive number"),
        ({"growing_paths_shown": 50}, "0 to 20"),
    ]:
        with pytest.raises(ValueError, match=message):
            build(bad)


def test_one_alert_per_disk_at_the_worse_severity() -> None:
    # 90 % full is a utilization warning; 50 GB left at 60 GB a day is a critical forecast.
    actions = run(disk(450, 60.0), path("/srv/cache", 55))
    [a] = [x for x in actions if x.params.get("resource") == "host-a:/"]
    assert a.state == "resources:disk:host-a:/" and a.severity is Severity.CRITICAL
    assert "full in about 20h" in a.reason and "/srv/cache (+55 GB/day)" in a.reason


def test_a_huge_filesystem_forecasts_nothing_and_does_not_crash() -> None:
    # A network share of about 8 EiB: days to full overflow a timedelta.
    share = replace(res(K.DISK, 12, 9.22e9, name="host-a:/mnt/share"), growth_per_day=0.5)
    assert full_alerts(run(share)) == []


def test_directories_are_listed_under_the_disk_they_are_on() -> None:
    root = replace(res(K.DISK, 498, 500, name="host-a:/"), growth_per_day=9.0, mount="/")
    docker = replace(
        res(K.DISK, 10, 500, name="host-a:/var/lib/docker"), growth_per_day=0.0,
        mount="/var/lib/docker",
    )  # fmt: skip
    on_root = replace(path("/home/agent/.cache", 8), mount="/")
    on_docker = replace(path("/var/lib/docker/overlay2", 20), mount="/var/lib/docker")
    unknown = path("/srv/x", 30)  # no mountpoint, and the host has two disks: not listed
    [a] = full_alerts(run(root, docker, on_root, on_docker, unknown))
    assert [p["path"] for p in a.params["growing_paths"]] == ["/home/agent/.cache"]
    [b] = full_alerts(run(root, docker, on_docker))  # measured, but on the other disk
    assert b.params["growing_paths"] == [] and "None of the directories measured" in b.reason
    [u] = full_alerts(run(root, docker, unknown))  # measured, filesystem unknown
    assert "do not say which filesystem" in u.reason


def test_directory_sizes_from_a_stopped_script_are_said_to_be_old() -> None:
    old = replace(path("/srv/cache", 5), at=NOW - 3 * 60 * M)
    [a] = full_alerts(run(disk(495, 9.0), old))
    assert a.params["growing_paths"] == [] and "over an hour old" in a.reason


def test_build_path_max_age() -> None:
    assert build({"path_max_age": "2h"}).path_max_age == 120 * M
    with pytest.raises(ValueError, match="path_max_age"):
        build({"path_max_age": "0s"})


def test_an_open_forecast_is_held_until_it_clears_the_release() -> None:
    """72h opens it; it stays open, a warning, until the disk would fill after 84h."""
    held = ("resources:disk:host-a:/",)
    lax = Resources(thresholds={K.DISK: Thresholds(0.99, 0.999)})  # not past utilization

    def forecast(left: float, open_: tuple[str, ...] = ()) -> list[Action]:
        r = disk(500 - left, 24.0)  # 24 GB a day: hours to full == GB left
        snap = Snapshot(now=NOW, resources=(r,), open_states=open_)
        return full_alerts(list(lax.evaluate(snap)))

    assert forecast(80) == []  # 80h: not opened
    [a] = forecast(80, held)  # 80h, already open: held
    assert a.severity is Severity.WARNING and a.state == "resources:disk:host-a:/"
    assert forecast(90, held) == []  # past 84h: let go
    assert forecast(20, held)[0].severity is Severity.CRITICAL
    custom = Resources(thresholds=lax.thresholds, time_to_full_release=timedelta(hours=100))
    snap = Snapshot(now=NOW, resources=(disk(410, 24.0),), open_states=held)
    assert full_alerts(list(custom.evaluate(snap)))  # 90h, under a 100h release


def test_build_time_to_full_release() -> None:
    assert build({"time_to_full_release": "96h"}).time_to_full_release == timedelta(hours=96)
    with pytest.raises(ValueError, match="not be shorter than time_to_full_warning"):
        build({"time_to_full_release": "48h"})


def test_the_hold_boundary_and_a_utilization_alert_held_by_the_forecast() -> None:
    held = ("resources:disk:host-a:/",)
    lax = Resources(thresholds={K.DISK: Thresholds(0.99, 0.999)})
    at_84h = Snapshot(now=NOW, resources=(disk(416, 24.0),), open_states=held)  # 84 GB left
    assert full_alerts(list(lax.evaluate(at_84h)))  # <= 84h: still held
    # Opened by its utilization (86 %), now at 84 % but filling within 84h: one condition, held.
    big = replace(res(K.DISK, 840, 1000, name="host-a:/"), growth_per_day=48.0)  # 80h
    [a] = full_alerts(
        list(Resources().evaluate(Snapshot(now=NOW, resources=(big,), open_states=held)))
    )
    assert a.state == "resources:disk:host-a:/" and a.severity is Severity.WARNING
    assert full_alerts(list(Resources().evaluate(Snapshot(now=NOW, resources=(big,))))) == []


def test_build_release_off_and_against_the_critical() -> None:
    assert build({"time_to_full_release": False}).time_to_full_release == timedelta(hours=72)
    with pytest.raises(ValueError, match="shorter than time_to_full_critical"):
        build({"time_to_full_warning": False, "time_to_full_release": "12h"})
    off = Resources(time_to_full_warning=None, thresholds={K.DISK: Thresholds(0.99, 0.999)})
    snap = Snapshot(now=NOW, resources=(disk(474, 24.0),), open_states=("resources:disk:host-a:/",))
    assert full_alerts(list(off.evaluate(snap)))  # 26h, under the critical's 28h release
