import contextlib
import gzip
import http.client
import json
import shutil
import socket
import subprocess
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from kelpie import net
from kelpie.core.actions import ActionKind
from kelpie.core.model import Session, Snapshot, Status, Task
from kelpie.core.ports import UnsupportedAction
from kelpie.remote.agent import Agent, AgentError, tls_context
from kelpie.remote.config import AgentSettings, HubAgent, HubSettings
from kelpie.remote.hub import AgentUnavailable, Hub, HubServer, OutOfOrder
from kelpie.remote.protocol import (
    ACTIONS_PATH,
    REPORT_PATH,
    Outcome,
    Report,
    Result,
    WireAction,
    encode_action,
)
from tests.fakes import StoppingAdapter

TOKEN = "0123456789abcdef" * 3
OTHER = "fedcba9876543210" * 3
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


def test_reports_are_ordered_by_process_and_counter_not_clock(hub: Hub, clock: Clock) -> None:
    def report(boot: str, seq: int, sent_at: datetime) -> Report:
        return Report("0.0.1", sent_at, timedelta(seconds=15), (), boot, seq)

    hub.receive_report("laptop", report("aaaaaaaa", 2, NOW + timedelta(days=1)))  # clock ahead
    with pytest.raises(OutOfOrder):
        hub.receive_report("laptop", report("aaaaaaaa", 2, NOW + timedelta(days=2)))
    hub.receive_report("laptop", report("aaaaaaaa", 3, NOW))  # NTP fixed the clock: fine
    hub.receive_report("laptop", report("bbbbbbbb", 1, NOW))  # a restarted agent


def test_an_agent_counts_its_reports(server: HubServer, hub: Hub, clock: Clock) -> None:
    agent = agent_for(server, BoardAdapter(), clock)
    agent.report()
    agent.report()  # same clock, next seq
    view = next(v for v in hub.agents() if v.name == "laptop")
    assert view.report is not None and view.report.seq == 2


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
    assert result is not None and result.outcome is Outcome.EXPIRED
    assert adapter.stops == 0
    assert agent.execute(raw) is not None and adapter.stops == 1  # not remembered: may run


def test_an_expired_action_is_retried_afresh(server: HubServer, hub: Hub, clock: Clock) -> None:
    thread, out = send_async(hub, stop_action(), timeout=2)
    wait_queued(hub)
    (action,) = hub.poll("laptop", [], 0)
    hub.poll("laptop", [Result(action.id, Outcome.EXPIRED)], 0)
    thread.join(5)
    assert "not carried out" in out["error"].public_reason
    assert hub._agents["laptop"].pending == {}
    assert hub.late_results(timedelta(0)) == []


def test_different_comments_are_different_actions(
    server: HubServer, hub: Hub, clock: Clock
) -> None:
    def comment(text: str) -> WireAction:
        return WireAction("", "fake", ActionKind.COMMENT, "rule", text, "t_1a")

    with pytest.raises(AgentUnavailable):
        hub.send("laptop", comment("text A"), timeout=0.1)
    thread, _ = send_async(hub, comment("text A"), timeout=0.3)
    wait_queued(hub)
    (first,) = hub.poll("laptop", [], 0)
    thread.join(5)
    hub.poll("laptop", [Result(first.id, Outcome.DONE, "posted A")], 0)
    thread, _ = send_async(hub, comment("completely different text B"), timeout=0.3)
    wait_queued(hub)
    (second,) = hub.poll("laptop", [], 0)
    assert second.id != first.id and second.reason == "completely different text B"
    thread.join(5)


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
    assert post(server, REPORT_PATH, b"{}", token="0123456789abcdeX" * 3)[0] == 401
    assert post(server, "/v1/other", b"{}")[0] == 404


def raw_request(server: HubServer, data: bytes) -> bytes:
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as sock:
        sock.sendall(data)
        chunks = []
        while chunk := sock.recv(65536):
            chunks.append(chunk)
    return b"".join(chunks)


def test_framing_errors(server: HubServer) -> None:
    head = f"POST {REPORT_PATH} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
    head += "Content-Type: application/json\r\n"
    assert raw_request(server, (head + "\r\n").encode()).startswith(b"HTTP/1.0 411")
    bad = head + "Content-Length: -5\r\n\r\n"
    assert raw_request(server, bad.encode()).startswith(b"HTTP/1.0 400")
    short = head + "Content-Length: 100\r\n\r\n{}"
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as sock:
        sock.sendall(short.encode())
        sock.shutdown(socket.SHUT_WR)
        assert sock.recv(100).startswith(b"HTTP/1.0 400")


def test_gzip_must_be_whole_and_alone(server: HubServer) -> None:
    gz = {"Content-Encoding": "gzip"}
    assert post(server, REPORT_PATH, b"not gzip", gz)[0] == 400
    assert post(server, REPORT_PATH, gzip.compress(b"{}")[:-6], gz)[0] == 400
    assert post(server, REPORT_PATH, gzip.compress(b"{}") + gzip.compress(b"{}"), gz)[0] == 400


def test_deep_bodies_are_refused(server: HubServer) -> None:
    status, body = post(server, REPORT_PATH, b"[" * 7000 + b"]" * 7000)
    assert status == 400 and "deep" in body["error"]


def wait_unauthenticated(server: HubServer, n: int) -> None:
    deadline = time.monotonic() + 5
    while server.unauthenticated() != n and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.unauthenticated() == n


def test_strangers_cannot_lock_agents_out(server: HubServer, clock: Clock) -> None:
    server.max_connections = 3
    port = server.server_address[1]
    # Idle strangers from several addresses fill every unauthenticated slot...
    idle = [
        socket.create_connection(("127.0.0.1", port), source_address=(f"127.0.0.{i + 2}", 0))
        for i in range(3)
    ]
    wait_unauthenticated(server, 3)
    # ...an agent still gets in: the oldest stranger is cut to make room.
    agent_for(server, BoardAdapter(), clock).report()
    assert idle[0].recv(10) == b""
    for sock in idle:
        sock.close()
    wait_unauthenticated(server, 0)


def test_one_address_gets_at_most_its_share(
    server: HubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.remote.hub as hub_module

    monkeypatch.setattr(hub_module, "MAX_PER_PEER", 2)
    port = server.server_address[1]
    idle = [socket.create_connection(("127.0.0.1", port)) for _ in range(2)]
    wait_unauthenticated(server, 2)
    with socket.create_connection(("127.0.0.1", port), timeout=5) as extra:
        assert extra.recv(10) == b""  # closed at accept
    for sock in idle:
        sock.close()
    wait_unauthenticated(server, 0)


def test_peers_count_by_address_or_ipv6_64() -> None:
    from kelpie.remote.hub import _peer_key

    assert _peer_key("192.0.2.7") == "192.0.2.7"
    assert _peer_key("2001:db8:1:2:aaaa::1") == _peer_key("2001:db8:1:2:bbbb::9")
    assert _peer_key("2001:db8:1:3::1") != _peer_key("2001:db8:1:2::1")
    assert _peer_key("::ffff:192.0.2.7") == "192.0.2.7"


def test_one_token_holds_few_requests(server: HubServer, hub: Hub) -> None:
    port = server.server_address[1]
    head = (
        f"POST {ACTIONS_PATH} HTTP/1.1\r\nHost: x\r\nAuthorization: Bearer {TOKEN}\r\n"
        "Content-Type: application/json\r\nContent-Length: 50\r\n\r\n{"
    )
    stalled = []
    for _ in range(2):  # bodies that never finish
        sock = socket.create_connection(("127.0.0.1", port), timeout=5)
        sock.sendall(head.encode())
        stalled.append(sock)
    deadline = time.monotonic() + 5
    while hub._agents["laptop"].in_flight["poll"] < 2 and time.monotonic() < deadline:
        time.sleep(0.01)
    poll = json.dumps({"version": 1, "wait": 0, "results": []}).encode()
    assert post(server, ACTIONS_PATH, poll)[0] == 409
    assert hub._agents["laptop"].polling == 0  # a stalled body replaced nothing
    assert post(server, ACTIONS_PATH, poll, token=OTHER)[0] == 200
    for sock in stalled:
        sock.close()
    deadline = time.monotonic() + 5
    while hub._agents["laptop"].in_flight["poll"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert post(server, ACTIONS_PATH, poll)[0] == 200


def test_a_failed_tls_handshake_leaves_nothing_behind(
    certificate: tuple[Path, Path], clock: Clock
) -> None:
    cert, key = certificate
    settings = hub_settings(tls_cert=cert, tls_key=key, allow_plain_http=False)
    srv = HubServer(Hub(settings, {"laptop": TOKEN, "vm-1": OTHER}, clock))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        before = threading.active_count()
        for _ in range(30):
            with socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=5) as s:
                s.sendall(b"GET / HTTP/1.1\r\n\r\n")  # not TLS
                with contextlib.suppress(OSError):
                    s.recv(10)
        deadline = time.monotonic() + 5
        while threading.active_count() > before + 2 and time.monotonic() < deadline:
            time.sleep(0.05)
        assert threading.active_count() <= before + 2  # no guard timers left running
        wait_unauthenticated(srv, 0)
    finally:
        srv.shutdown()
        srv.server_close()


def test_undelivered_actions_go_back_to_the_front(hub: Hub) -> None:
    thread, out = send_async(hub, stop_action(), timeout=5)
    wait_queued(hub)
    actions = hub.poll("laptop", [], 0)
    hub.undeliver("laptop", actions)  # the reply could not be written
    assert [a.id for a in hub.poll("laptop", [], 0)] == [a.id for a in actions]
    hub.poll("laptop", [Result(actions[0].id, Outcome.DONE)], 0)
    thread.join(5)
    assert out["result"].outcome is Outcome.DONE


def test_an_answered_action_is_not_handed_out_again(hub: Hub) -> None:
    thread, _ = send_async(hub, stop_action(), timeout=0.3)
    wait_queued(hub)
    (first,) = hub.poll("laptop", [], 0)
    thread.join(5)  # timed out after delivery
    thread, out = send_async(hub, stop_action(), timeout=2)  # re-queues the same id
    wait_queued(hub)
    assert hub.poll("laptop", [Result(first.id, Outcome.DONE)], 0) == []
    thread.join(5)
    assert out["result"].outcome is Outcome.DONE


def test_the_agent_drops_results_the_hub_refuses(clock: Clock) -> None:
    calls: list[bytes] = []

    def refuse(*args: Any, **kw: Any) -> net.Response:
        calls.append(args[3])
        return net.Response(400, {}, b'{"error": "result: id must be a string"}')

    settings = AgentSettings(name="laptop", server="https://hub.example.internal:9465")
    agent = Agent(settings, [BoardAdapter()], TOKEN, clock=clock, post=refuse)
    agent._unsent = [Result("a1", Outcome.DONE)]
    with pytest.raises(AgentError, match="400"):
        agent.poll(wait=0)
    assert agent._unsent == []


def test_a_slow_stranger_is_cut_at_the_deadline(
    server: HubServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    import kelpie.remote.hub as hub_module

    monkeypatch.setattr(hub_module, "PREAUTH_DEADLINE", 0.5)
    with socket.create_connection(("127.0.0.1", server.server_address[1]), timeout=5) as sock:
        sock.sendall(b"POST /v1/agent/report HTTP/1.1\r\n")
        started = time.monotonic()
        for _ in range(20):  # a byte every 0.1s: each read is quick, the whole is not
            try:
                sock.sendall(b"X")
            except OSError:
                break
            time.sleep(0.1)
        assert sock.recv(100) == b""
        assert time.monotonic() - started < 3
    deadline = time.monotonic() + 5
    while server.unauthenticated() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert server.unauthenticated() == 0


def test_an_agent_frees_its_connection_slot(server: HubServer, clock: Clock) -> None:
    agent_for(server, BoardAdapter(), clock).report()
    assert server.unauthenticated() == 0


def test_the_agent_refuses_an_oversized_reply(clock: Clock) -> None:
    def post_big(*args: Any, **kw: Any) -> Any:
        raise net.RequestError("too_large", "response over 1048576 bytes")

    settings = AgentSettings(name="laptop", server="https://hub.example.internal:9465")
    agent = Agent(settings, [BoardAdapter()], TOKEN, clock=clock, post=post_big)
    with pytest.raises(AgentError, match="over 1048576"):
        agent.poll(wait=0)


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


def test_a_new_poll_replaces_a_held_one(server: HubServer, hub: Hub) -> None:
    poll = json.dumps({"version": 1, "wait": 20, "results": []}).encode()
    replies: list[tuple[int, dict[str, Any]]] = []
    first = threading.Thread(
        target=lambda: replies.append(post(server, ACTIONS_PATH, poll)), daemon=True
    )
    first.start()
    deadline = time.monotonic() + 5
    while not hub._agents["laptop"].polling and time.monotonic() < deadline:
        time.sleep(0.01)
    quick = json.dumps({"version": 1, "wait": 0, "results": []}).encode()
    started = time.monotonic()
    assert post(server, ACTIONS_PATH, quick) == (200, {"actions": []})
    first.join(5)
    assert replies == [(200, {"actions": []})] and time.monotonic() - started < 5
    assert hub._agents["laptop"].polling == 0


def test_close_releases_held_polls_and_waits(hub: Hub) -> None:
    thread, out = send_async(hub, stop_action(), timeout=20)
    wait_queued(hub)
    hub.poll("laptop", [], 0)  # delivered, never answered
    poller = threading.Thread(target=hub.poll, args=("vm-1", [], 20), daemon=True)
    poller.start()
    started = time.monotonic()
    hub.close()
    thread.join(5)
    poller.join(5)
    assert time.monotonic() - started < 2
    assert "error" in out and not poller.is_alive()


def test_a_poll_reply_stays_under_its_cap(hub: Hub) -> None:
    conditions = [
        {"id": f"c{i}", "kind": "custom", "params": {f"k{j}": "x" * 1000 for j in range(16)}}
        for i in range(32)
    ]
    for n in range(4):  # about 530 KB each
        wait = WireAction(
            "", "fake", ActionKind.WAIT, "r", "x", f"t_{n}", {"conditions": conditions}
        )
        send_async(hub, wait, timeout=5)
    deadline = time.monotonic() + 5
    while len(hub._agents["laptop"].queue) < 4 and time.monotonic() < deadline:
        time.sleep(0.01)
    sizes = []
    for _ in range(4):
        batch = hub.poll("laptop", [], 0)
        sizes.append(len(batch))
    assert sizes == [1, 1, 1, 1]  # one over-sized action per reply, never two
    hub.close()


def test_withdrawn_actions_leave_the_queue(hub: Hub) -> None:
    for n in range(3):
        with pytest.raises(AgentUnavailable, match="not carried out"):
            hub.send("laptop", replace(stop_action(), params={"session_id": f"s{n}"}), 0.05)
    assert not hub._agents["laptop"].queue and not hub._agents["laptop"].pending


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
        agent = Agent(
            untrusted, [BoardAdapter()], TOKEN, clock=clock, context=tls_context(untrusted)
        )
        with pytest.raises(AgentError, match="connection failed"):
            agent.report()  # self-signed, not in the system store: refused
        by_ip = AgentSettings(
            name="laptop", server=f"https://127.0.0.1:{srv.server_address[1]}", ca_file=cert
        )
        agent = Agent(by_ip, [BoardAdapter()], TOKEN, clock=clock, context=tls_context(by_ip))
        with pytest.raises(AgentError, match="connection failed"):
            agent.report()  # trusted CA, but the name on the certificate is not this one
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


def test_an_ipv6_listen_address(clock: Clock) -> None:
    hub = Hub(hub_settings(host="::1"), {"laptop": TOKEN, "vm-1": OTHER}, clock)
    try:
        srv = HubServer(hub)
    except OSError:
        pytest.skip("no IPv6 loopback")
    srv.server_close()
    assert srv.address_family == socket.AF_INET6


def test_results_go_in_batches_the_hub_accepts(clock: Clock) -> None:
    bodies: list[int] = []

    def accept(*args: Any, **kw: Any) -> net.Response:
        bodies.append(len(gzip.decompress(args[3])))
        return net.Response(413 if len(bodies) == 1 else 200, {}, b'{"actions": []}')

    settings = AgentSettings(name="laptop", server="https://hub.example.internal:9465")
    agent = Agent(settings, [BoardAdapter()], TOKEN, clock=clock, post=accept)
    detail = 'é"' * 2000  # escapes to far more bytes than characters
    agent._unsent = [Result(f"a{i}", Outcome.FAILED, detail) for i in range(256)]
    with pytest.raises(AgentError, match="413"):
        agent.poll(wait=0)
    assert len(agent._unsent) == 256  # kept, not dropped
    while agent._unsent:
        agent.poll(wait=0)
    assert all(size <= 1024 * 1024 for size in bodies)
    assert max(bodies[1:]) <= 512 * 1024 + 64 * 1024  # halved after the 413


def test_a_version_mismatch_keeps_the_results(clock: Clock) -> None:
    def refuse(*args: Any, **kw: Any) -> net.Response:
        return net.Response(400, {}, b'{"error": "protocol version 2 is not 1"}')

    settings = AgentSettings(name="laptop", server="https://hub.example.internal:9465")
    agent = Agent(settings, [BoardAdapter()], TOKEN, clock=clock, post=refuse)
    agent._unsent = [Result("a1", Outcome.DONE)]
    with pytest.raises(AgentError):
        agent.poll(wait=0)
    assert agent._unsent == [Result("a1", Outcome.DONE)]
