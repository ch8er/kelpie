import gzip
import http.client
import json
import shutil
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie.core.actions import ActionKind
from kelpie.core.model import Session, Snapshot, Status, Task
from kelpie.core.ports import UnsupportedAction
from kelpie.remote.agent import Agent, AgentError, tls_context
from kelpie.remote.config import AgentSettings, HubAgent, HubSettings
from kelpie.remote.hub import AgentUnavailable, Hub, HubServer
from kelpie.remote.protocol import (
    ACTIONS_PATH,
    REPORT_PATH,
    Outcome,
    WireAction,
    encode_action,
)
from tests.fakes import StoppingAdapter

TOKEN = "t" * 40
OTHER = "o" * 40
NOW = datetime(2026, 10, 16, 12, 0, tzinfo=UTC)


class Clock:
    def __init__(self) -> None:
        self.now = NOW

    def __call__(self) -> datetime:
        return self.now


@dataclass
class BoardAdapter(StoppingAdapter):
    name: str = "fake"
    unsupported: bool = False
    stops: int = 0

    def snapshot(self, now: datetime) -> Snapshot:
        task = Task("t_1a", "Fix login", Status.IN_PROGRESS, now, owner_id="dev-1")
        return Snapshot(now=now, tasks=(task,), sessions=(Session("s1", "t_1a", "dev-1", now),))

    def stop(self, session_id: str, reason: str) -> str:
        if self.unsupported:
            raise UnsupportedAction("stop is turned off")
        self.stops += 1
        super().stop(session_id, reason)
        return "the worker exits within about 2 minutes"


def hub_settings(**kw: Any) -> HubSettings:
    base = HubSettings(
        agents=(HubAgent("laptop", "T1"), HubAgent("vm-1", "T2")),
        host="127.0.0.1",
        port=0,
        allow_plain_http=True,
        max_body_bytes=64 * 1024,
    )
    return replace(base, **kw)


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def hub(clock: Clock) -> Hub:
    return Hub(hub_settings(), {"laptop": TOKEN, "vm-1": OTHER}, clock)


@pytest.fixture
def server(hub: Hub) -> Iterator[HubServer]:
    srv = HubServer(hub)
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def agent_for(server: HubServer, adapter: BoardAdapter, clock: Clock, **kw: Any) -> Agent:
    port = server.server_address[1]
    settings = AgentSettings(
        name="laptop",
        server=f"http://127.0.0.1:{port}",
        allow_plain_http=True,
        interval=timedelta(seconds=1),
        timeout=timedelta(seconds=5),
        **kw,
    )
    return Agent(settings, [adapter], TOKEN, clock=clock)


def stop_action(session: str = "s1") -> WireAction:
    return WireAction(
        "", "fake", ActionKind.STOP, "loop_breaker", "looping", None, {"session_id": session}
    )


def send_async(hub: Hub, action: WireAction, timeout: float) -> tuple[threading.Thread, dict]:
    out: dict[str, Any] = {}

    def run() -> None:
        try:
            out["result"] = hub.send("laptop", action, timeout)
        except AgentUnavailable as exc:
            out["error"] = exc

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    return thread, out


def wait_queued(hub: Hub, agent: str = "laptop") -> None:
    deadline = time.monotonic() + 5
    while not hub._agents[agent].queue and time.monotonic() < deadline:
        time.sleep(0.01)


# --- identity ---------------------------------------------------------------------------


def test_the_token_is_the_identity(hub: Hub) -> None:
    assert hub.identify(f"Bearer {TOKEN}") == "laptop"
    assert hub.identify(f"bearer {OTHER}") == "vm-1"
    assert hub.identify(f"Bearer {TOKEN}x") is None
    assert hub.identify(f"Basic {TOKEN}") is None
    assert hub.identify(None) is None


# --- reports ------------------------------------------------------------------------------


def test_report_reaches_the_hub(server: HubServer, hub: Hub, clock: Clock) -> None:
    agent = agent_for(server, BoardAdapter(), clock)
    agent.report()
    view = next(v for v in hub.agents() if v.name == "laptop")
    assert view.report is not None and view.report_at == NOW
    (part,) = view.report.adapters
    assert part.name == "fake"
    assert ActionKind.STOP in part.actions and ActionKind.COMMENT in part.actions
    assert part.snapshot is not None and part.snapshot.tasks[0].id == "t_1a"
    other = next(v for v in hub.agents() if v.name == "vm-1")
    assert other.report is None


def test_an_older_report_is_refused(server: HubServer, clock: Clock) -> None:
    agent = agent_for(server, BoardAdapter(), clock)
    agent.report()
    with pytest.raises(AgentError, match="409"):
        agent.report()  # same sent_at
    clock.now += timedelta(seconds=1)
    agent.report()


def test_an_adapter_that_fails_is_reported_as_failed(
    server: HubServer, hub: Hub, clock: Clock
) -> None:
    adapter = BoardAdapter()
    adapter.snapshot = lambda now: (_ for _ in ()).throw(OSError("disk"))  # type: ignore[method-assign]
    agent_for(server, adapter, clock).report()
    (part,) = next(v for v in hub.agents() if v.name == "laptop").report.adapters  # type: ignore[union-attr]
    assert part.snapshot is None and part.error == "OSError"


def test_a_report_over_the_limit_is_not_sent(server: HubServer, clock: Clock) -> None:
    agent = agent_for(server, BoardAdapter(), clock, max_body_bytes=64 * 1024)
    big = BoardAdapter()
    tasks = tuple(Task(f"t_{i}", "x" * 200, Status.READY, NOW) for i in range(1000))
    big.snapshot = lambda now: Snapshot(now=now, tasks=tasks)  # type: ignore[method-assign]
    agent.adapters = [big]
    with pytest.raises(AgentError, match="max_body_bytes"):
        agent.report()


# --- actions ------------------------------------------------------------------------------


def test_action_round_trip(server: HubServer, hub: Hub, clock: Clock) -> None:
    adapter = BoardAdapter()
    agent = agent_for(server, adapter, clock)
    thread, out = send_async(hub, stop_action(), timeout=5)
    wait_queued(hub)
    assert agent.poll(wait=1) == 1
    assert adapter.calls == [("stop", "s1")]
    agent.poll(wait=0)  # delivers the result
    thread.join(5)
    assert out["result"].outcome is Outcome.DONE
    assert out["result"].detail == "the worker exits within about 2 minutes"


def test_kinds_not_offered_are_refused_by_the_agent(server: HubServer, clock: Clock) -> None:
    adapter = BoardAdapter()
    agent = agent_for(server, adapter, clock, actions=frozenset({ActionKind.STOP}))
    comment = WireAction("a1", "fake", ActionKind.COMMENT, "rule", "hi", "t_1a")
    result = agent.execute(encode_action(comment))
    assert result is not None and result.outcome is Outcome.NOT_EXECUTED
    assert "refused" in (result.detail or "")
    assert adapter.calls == []


def test_unsupported_and_failing_actions(server: HubServer, clock: Clock) -> None:
    agent = agent_for(server, BoardAdapter(unsupported=True), clock)
    result = agent.execute(encode_action(replace(stop_action(), id="a1")))
    assert result is not None and result.outcome is Outcome.NOT_EXECUTED
    broken = BoardAdapter(fail_on="stop")
    agent = agent_for(server, broken, clock)
    result = agent.execute(encode_action(replace(stop_action(), id="a2")))
    assert result is not None and result.outcome is Outcome.FAILED
    assert result.detail == "RuntimeError (details in the Kelpie log)"  # never the message


def test_an_action_is_carried_out_once_per_id(server: HubServer, clock: Clock) -> None:
    adapter = BoardAdapter()
    agent = agent_for(server, adapter, clock)
    raw = encode_action(replace(stop_action(), id="a1"))
    first, again = agent.execute(raw), agent.execute(raw)
    assert first == again and adapter.stops == 1


def test_an_expired_action_is_not_started(server: HubServer, clock: Clock) -> None:
    adapter = BoardAdapter()
    agent = agent_for(server, adapter, clock)
    raw = encode_action(replace(stop_action(), id="a1", ttl=0.5))
    result = agent.execute(raw, received=time.monotonic() - 1)
    assert result is not None and result.outcome is Outcome.NOT_EXECUTED
    assert adapter.stops == 0


def test_an_action_not_picked_up_is_withdrawn(server: HubServer, hub: Hub, clock: Clock) -> None:
    adapter = BoardAdapter()
    agent = agent_for(server, adapter, clock)
    with pytest.raises(AgentUnavailable, match="not carried out"):
        hub.send("laptop", stop_action(), timeout=0.1)
    assert agent.poll(wait=0) == 0
    assert adapter.stops == 0


def test_a_late_result_answers_the_retry(server: HubServer, hub: Hub, clock: Clock) -> None:
    adapter = BoardAdapter()
    agent = agent_for(server, adapter, clock)
    thread, out = send_async(hub, stop_action(), timeout=0.5)
    wait_queued(hub)
    agent.poll(wait=1)  # carried out, but the result stays with the agent
    thread.join(5)
    assert "may still carry it out" in out["error"].public_reason
    agent.poll(wait=0)  # the late result arrives
    result = hub.send("laptop", replace(stop_action(), reason="still looping"), timeout=1)
    assert result.outcome is Outcome.DONE
    assert adapter.stops == 1


def test_a_retry_while_unconfirmed_reuses_the_id(server: HubServer, hub: Hub, clock: Clock) -> None:
    adapter = BoardAdapter()
    agent = agent_for(server, adapter, clock)
    thread, out = send_async(hub, stop_action(), timeout=0.5)
    wait_queued(hub)
    agent.poll(wait=1)
    thread.join(5)
    thread, out = send_async(hub, stop_action(), timeout=5)  # the ledger retries
    wait_queued(hub)
    agent.poll(wait=1)  # sent again under the same id: answered from memory
    agent.poll(wait=0)
    thread.join(5)
    assert out["result"].outcome is Outcome.DONE
    assert adapter.stops == 1


def test_unclaimed_late_results_are_handed_over(server: HubServer, hub: Hub, clock: Clock) -> None:
    agent = agent_for(server, BoardAdapter(), clock)
    thread, _ = send_async(hub, stop_action(), timeout=0.5)
    wait_queued(hub)
    agent.poll(wait=1)
    thread.join(5)
    agent.poll(wait=0)
    assert hub.late_results(timedelta(minutes=5)) == []
    clock.now += timedelta(minutes=5)
    (late,) = hub.late_results(timedelta(minutes=5))
    assert late.agent == "laptop" and late.result.outcome is Outcome.DONE
    assert hub.late_results(timedelta(0)) == []


def test_agent_run_stops_promptly(server: HubServer, clock: Clock) -> None:
    agent = agent_for(server, BoardAdapter(), clock)
    thread = threading.Thread(target=agent.run, kwargs={"grace": 1.0}, daemon=True)
    thread.start()
    time.sleep(0.3)
    agent.stop()
    thread.join(3)
    assert not thread.is_alive()


# --- HTTP limits --------------------------------------------------------------------------


def post(
    server: HubServer,
    path: str,
    body: bytes,
    headers: dict[str, str] | None = None,
    token: str | None = TOKEN,
) -> tuple[int, dict[str, Any]]:
    conn = http.client.HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    hdrs = {"Content-Type": "application/json", **(headers or {})}
    if token:
        hdrs["Authorization"] = f"Bearer {token}"
    conn.request("POST", path, body=body, headers=hdrs)
    response = conn.getresponse()
    data = response.read()
    conn.close()
    return response.status, json.loads(data) if data else {}


def test_requests_without_a_valid_token(server: HubServer) -> None:
    assert post(server, REPORT_PATH, b"{}", token=None)[0] == 401
    assert post(server, REPORT_PATH, b"{}", token="x" * 40)[0] == 401
    assert post(server, "/v1/other", b"{}")[0] == 404


def test_bodies_are_limited(server: HubServer) -> None:
    assert post(server, REPORT_PATH, b"x" * (64 * 1024 + 1))[0] == 413
    bomb = gzip.compress(b"[" + b" " * (2 * 1024 * 1024) + b"]")
    assert post(server, REPORT_PATH, bomb, {"Content-Encoding": "gzip"})[0] == 413
    assert post(server, REPORT_PATH, b"{}", {"Content-Type": "text/plain"})[0] == 415
    assert post(server, REPORT_PATH, b"{}", {"Content-Encoding": "br"})[0] == 415
    assert post(server, REPORT_PATH, b"{nope")[0] == 400
    assert post(server, REPORT_PATH, b"[" * 50_000)[0] == 400
    status, body = post(server, REPORT_PATH, b'{"version": 9}')
    assert status == 400 and "same Kelpie release" in body["error"]


def test_a_second_poll_with_one_token_is_refused(server: HubServer, hub: Hub) -> None:
    poll = json.dumps({"version": 1, "wait": 2, "results": []}).encode()
    first = threading.Thread(target=post, args=(server, ACTIONS_PATH, poll), daemon=True)
    first.start()
    deadline = time.monotonic() + 5
    while not hub._agents["laptop"].polling and time.monotonic() < deadline:
        time.sleep(0.01)
    assert post(server, ACTIONS_PATH, poll)[0] == 409
    assert post(server, ACTIONS_PATH, poll, token=OTHER)[0] == 200  # another agent
    first.join(5)


# --- TLS ----------------------------------------------------------------------------------


@pytest.fixture(scope="module")
def certificate(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    if shutil.which("openssl") is None:
        pytest.skip("openssl not installed")
    root = tmp_path_factory.mktemp("tls")
    cert, key = root / "tls.crt", root / "tls.key"
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
            "-subj", "/CN=localhost", "-addext", "subjectAltName=DNS:localhost",
            "-keyout", str(key), "-out", str(cert),
        ],
        check=True,
        capture_output=True,
    )  # fmt: skip
    return cert, key


def test_report_over_tls(certificate: tuple[Path, Path], clock: Clock) -> None:
    cert, key = certificate
    settings = hub_settings(tls_cert=cert, tls_key=key, allow_plain_http=False)
    hub = Hub(settings, {"laptop": TOKEN, "vm-1": OTHER}, clock)
    srv = HubServer(hub)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        url = f"https://localhost:{srv.server_address[1]}"
        trusted = AgentSettings(name="laptop", server=url, ca_file=cert)
        Agent(trusted, [BoardAdapter()], TOKEN, clock=clock, context=tls_context(trusted)).report()
        assert hub.agents()[0].report is not None
        untrusted = AgentSettings(name="laptop", server=url)
        agent = Agent(untrusted, [BoardAdapter()], TOKEN, clock=clock)
        clock.now += timedelta(seconds=1)
        with pytest.raises(AgentError, match="connection failed"):
            agent.report()  # self-signed, not in the system store: refused
        # A plain-HTTP client on the TLS port gets nowhere.
        status = None
        conn = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
        with pytest.raises((http.client.HTTPException, OSError)):
            conn.request("POST", REPORT_PATH, body=b"{}")
            status = conn.getresponse().status
        assert status is None
    finally:
        srv.shutdown()
        srv.server_close()
