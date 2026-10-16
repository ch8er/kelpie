"""The simulator parts the demo added: PR lists by state, label events, the hermes CLI's
clock file and the OTLP exporter."""

from __future__ import annotations

import os
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from opentelemetry.proto.collector.trace.v1.trace_service_pb2 import ExportTraceServiceRequest

from kelpie.sim import hermes_cli, otlp
from kelpie.sim.board import Board
from kelpie.sim.github import FakeGitHub

NOW = datetime(2026, 3, 3, 9, tzinfo=UTC)


def test_pull_lists_by_state_with_labels_and_their_events() -> None:
    gh = FakeGitHub()
    gh.repo("example/shop")
    held = gh.pull("example/shop", 61, branch="hermes/t_1", created=NOW, labels={"hold": NOW})
    gh.pull("example/shop", 62, state="closed", merged_at=NOW)
    gh.pull("example/shop", 63, state="closed", closed_at=NOW - timedelta(hours=1))

    def numbers(state: str) -> list[int]:
        status, body = gh.answer("GET", f"/repos/example/shop/pulls?state={state}&per_page=100")
        assert status == 200
        return [p["number"] for p in body]

    assert numbers("open") == [61]
    assert numbers("closed") == [63, 62]
    assert numbers("all") == [63, 62, 61]
    _, (pr,) = gh.answer("GET", "/repos/example/shop/pulls")  # open by default
    assert pr["labels"] == [{"name": "hold"}] and pr["head"]["ref"] == "hermes/t_1"
    assert pr["head"]["repo"] == {"full_name": "example/shop"} and pr["closed_at"] is None
    _, closed = gh.answer("GET", "/repos/example/shop/pulls/63")
    assert closed["closed_at"] == "2026-03-03T08:00:00Z" and closed["merged"] is False
    _, events = gh.answer("GET", "/repos/example/shop/issues/61/events")
    assert events == [
        {"event": "labeled", "label": {"name": "hold"}, "created_at": "2026-03-03T09:00:00Z"}
    ]
    assert held.body("example/shop")["created_at"] == "2026-03-03T09:00:00Z"


def test_the_hermes_cli_takes_its_time_from_the_clock_file(tmp_path: Path) -> None:
    board = Board(tmp_path / "kanban.db")
    board.task("t_1", "todo", NOW - timedelta(hours=1))
    board.claim("t_1", "dev", NOW - timedelta(minutes=30))
    clock = tmp_path / "clock"
    clock.write_text(f"{NOW.timestamp():.0f}\n")
    log = tmp_path / "calls.jsonl"
    cli = hermes_cli.install(tmp_path, log, clock)
    env = {"HERMES_KANBAN_DB": str(board.path), "PATH": os.defpath}
    subprocess.run([str(cli), "kanban", "block", "t_1", "stop"], env=env, check=True)
    (_, payload), *_ = [e for e in board.events("t_1") if e[0] == "blocked"]
    assert payload["reason"] == "stop"
    (call,) = hermes_cli.calls(log)
    assert call["at"] == NOW.timestamp() and call["code"] == 0

    clock.write_text("not a time\n")  # a broken clock fails the command, never the log
    done = subprocess.run([str(cli), "kanban", "unblock", "t_1"], env=env, check=False)
    assert done.returncode != 0
    assert len(hermes_cli.calls(log)) == 2


def test_the_otlp_exporter_writes_genai_model_calls() -> None:
    call = otlp.ModelCall("t_1", "hermes-run-4", "synthetic-model", 80_000, 300, NOW)
    req = ExportTraceServiceRequest.FromString(otlp.request([call, call]))
    spans = req.resource_spans[0].scope_spans[0].spans
    assert len(spans) == 2 and spans[0].span_id == spans[1].span_id  # same call, same id
    attrs = {a.key: a.value for a in spans[0].attributes}
    assert attrs["gen_ai.operation.name"].string_value == "chat"
    assert attrs["gen_ai.usage.input_tokens"].int_value == 80_000
    assert attrs["kelpie.session_id"].string_value == "hermes-run-4"
    assert spans[0].start_time_unix_nano == int(NOW.timestamp() * 1e9)
