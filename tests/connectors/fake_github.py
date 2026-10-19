"""A local stand-in for the GitHub REST API: no real network in tests."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

TOKEN = "ghp_SyntheticTestToken0123456789"


@dataclass
class Reply:
    status: int = 200
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass
class Seen:
    path: str
    headers: Any  # case-insensitive, like HTTP
    method: str = "GET"
    body: Any = None  # a write's decoded JSON body


class FakeGitHub:
    """Routes ``path`` (with query) to a :class:`Reply`; records every request. A write's
    route is keyed ``"<METHOD> <path>"`` (``"POST /repos/..."``)."""

    def __init__(self) -> None:
        self.routes: dict[str, Reply] = {}
        self.requests: list[Seen] = []
        self.url = ""

    def reply(self, path: str, body: Any = None, status: int = 200, **headers: str) -> None:
        self.routes[path] = Reply(
            status, body, {k.replace("_", "-"): v for k, v in headers.items()}
        )

    def pull(self, repo: str, number: int, **fields: Any) -> None:
        body = {"number": number, "state": "open", "merged": False, "merged_at": None}
        body["head"] = {"sha": fields.pop("sha", "a" * 40)}
        body.update(fields)
        self.reply(f"/repos/{repo}/pulls/{number}", body)

    def ci(
        self,
        repo: str,
        sha: str,
        statuses: list[dict[str, Any]] | None = None,
        runs: list[dict[str, Any]] | None = None,
        total: int | None = None,
        status_state: str | None = None,
        status_total: int | None = None,
    ) -> None:
        statuses = statuses or []
        state = status_state or ("success" if statuses else "pending")
        self.reply(
            f"/repos/{repo}/commits/{sha}/status?per_page=100",
            {
                "state": state,
                "total_count": len(statuses) if status_total is None else status_total,
                "statuses": statuses,
            },
        )
        runs = runs or []
        body = {"total_count": len(runs) if total is None else total, "check_runs": runs}
        self.reply(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100", body)

    def forbid_ci(self, repo: str, sha: str, *, statuses: bool = True, checks: bool = True) -> None:
        """Answer 403 like a fine-grained token without Checks / Commit statuses."""
        denied = {"message": "Resource not accessible by personal access token"}
        if statuses:
            self.reply(f"/repos/{repo}/commits/{sha}/status?per_page=100", denied, 403)
        if checks:
            self.reply(f"/repos/{repo}/commits/{sha}/check-runs?per_page=100", denied, 403)

    def actions(
        self,
        repo: str,
        sha: str,
        runs: list[dict[str, Any]] | None = None,
        total: int | None = None,
        page: int = 1,
    ) -> None:
        """The workflow runs of a commit (``?head_sha=``), one page of them."""
        runs = runs or []
        body = {"total_count": len(runs) if total is None else total, "workflow_runs": runs}
        paging = "" if page == 1 else f"&page={page}"
        self.reply(f"/repos/{repo}/actions/runs?head_sha={sha}{paging}&per_page=100", body)

    def workflows(self, repo: str, *states: str | dict[str, Any]) -> None:
        """The repository's workflows: a state (``active``, ``disabled_manually``) or a full
        workflow object each."""
        listed = [
            {"id": i, **({"state": w} if isinstance(w, str) else w)}
            for i, w in enumerate(states, 1)
        ]
        body = {"total_count": len(listed), "workflows": listed}
        self.reply(f"/repos/{repo}/actions/workflows?per_page=100", body)

    def paths(self) -> list[str]:
        return [s.path for s in self.requests]


def _handler(fake: FakeGitHub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._answer("GET", self.path)

        def do_POST(self) -> None:
            self._write("POST")

        def do_PATCH(self) -> None:
            self._write("PATCH")

        def do_PUT(self) -> None:
            self._write("PUT")

        def do_DELETE(self) -> None:
            self._write("DELETE")

        def _write(self, method: str) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length)
            body = json.loads(raw) if raw else None
            self._answer(method, f"{method} {self.path}", body)

        def _answer(self, method: str, route: str, body: Any = None) -> None:
            fake.requests.append(Seen(self.path, self.headers, method, body))
            reply = fake.routes.get(route, Reply(404, {"message": "Not Found"}))
            body = reply.body
            raw = body if isinstance(body, bytes) else json.dumps(body).encode()
            payload = b"" if body is None else raw
            self.send_response(reply.status)
            for name, value in reply.headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args: object) -> None:
            pass

    return Handler


@contextmanager
def serve() -> Iterator[FakeGitHub]:
    fake = FakeGitHub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()


class Clock:
    def __init__(self, now: datetime = datetime(2026, 3, 2, 12, tzinfo=UTC)) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: float) -> None:
        self.now += timedelta(**kwargs)


class Recorder:
    """Observer for client metrics."""

    def __init__(self) -> None:
        self.outcomes: list[str] = []
        self.rate: tuple[int, int] | None = None
        self.blocks: dict[str, int] | None = None

    def github_request(self, outcome: str) -> None:
        self.outcomes.append(outcome)

    def github_rate_limit(self, remaining: int, limit: int) -> None:
        self.rate = (remaining, limit)

    def block_conditions(self, outcomes: dict[str, int]) -> None:
        self.blocks = {k: v for k, v in outcomes.items() if v}
