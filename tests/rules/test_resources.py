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
            res(K.LLM_QUOTA, 99, 100, host="host-a"),  # provider quotas are another step
            res(K.AUTH, 1, 1, host="host-a"),
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
