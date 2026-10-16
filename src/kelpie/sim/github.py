"""A stateful fake of the GitHub REST API (loopback only).

It holds repositories, pull requests, CI results per commit and GitHub Actions runs, answers
the endpoints Kelpie's GitHub connector reads, and carries out the one write Kelpie makes
(``POST .../actions/runs/{id}/cancel``): the run is then completed as ``cancelled`` at once
(real GitHub answers 202 and keeps the run in progress until the runner stops, so a later
scan may still see it active; the farm never produces that case). Every request is recorded
with its method, path and the bearer token it carried; one without the farm's token gets
401, as GitHub answers bad credentials.

Query strings are ignored: the run list returns every run of the repository (the farm only
creates recent ones), on one page.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY = 4096  # Kelpie's only write (cancel) has an empty body
TOKEN = "ghp_SimulatedFarmToken0123456789abcdef"
BILLING_TOKEN = "ghp_SimulatedBillingToken0123456789ab"  # may read billing usage, nothing else


def iso(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha_of(label: str) -> str:
    """A stable 40-hex commit id for a readable label."""
    return hashlib.sha1(label.encode()).hexdigest()


@dataclass
class Request:
    method: str
    path: str
    token: str | None  # the bearer token sent, if any


@dataclass
class Pull:
    number: int
    head_sha: str
    state: str = "open"  # open | closed
    merged_at: datetime | None = None
    draft: bool = False
    files: list[str] = field(default_factory=lambda: ["src/app.py"])

    def body(self) -> dict[str, Any]:
        return {
            "number": self.number,
            "state": self.state,
            "merged": self.merged_at is not None,
            "merged_at": iso(self.merged_at) if self.merged_at else None,
            "draft": self.draft,
            "head": {"sha": self.head_sha},
            "changed_files": len(self.files),
        }


@dataclass
class Run:
    id: int
    workflow: str  # path, e.g. ".github/workflows/ci.yml"
    branch: str
    sha: str
    created: datetime
    event: str = "push"
    status: str = "in_progress"  # queued | in_progress | completed
    conclusion: str | None = None
    attempt: int = 1
    pr: int | None = None
    message: str = "Change things"
    updated: datetime | None = None

    def body(self, repo: str) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.workflow.rsplit("/", 1)[-1],
            "path": self.workflow,
            "workflow_id": int(sha_of(self.workflow)[:8], 16),  # stable per workflow file
            "head_branch": self.branch,
            "head_sha": self.sha,
            "head_repository": {"full_name": repo},
            "repository": {"full_name": repo},
            "event": self.event,
            "status": self.status,
            "conclusion": self.conclusion,
            "run_attempt": self.attempt,
            "created_at": iso(self.created),
            "run_started_at": iso(self.created),
            "updated_at": iso(self.updated or self.created),
            "head_commit": {"message": self.message},
            "pull_requests": [{"number": self.pr, "base": {"ref": "main"}}] if self.pr else [],
        }


@dataclass
class Repo:
    name: str
    default_branch: str = "main"
    pulls: dict[int, Pull] = field(default_factory=dict)
    # sha -> (state, completed) per commit: "success" | "failure" | "pending"
    checks: dict[str, tuple[str, datetime | None]] = field(default_factory=dict)
    runs: dict[int, Run] = field(default_factory=dict)


class FakeGitHub:
    def __init__(self) -> None:
        self.repos: dict[str, Repo] = {}
        self.requests: list[Request] = []
        self.cancelled: list[tuple[str, int]] = []
        self.lock = threading.Lock()
        self.tokens = {TOKEN}  # bearer tokens GitHub accepts for repositories
        self.billing_tokens = {BILLING_TOKEN}  # ...and for billing usage reports
        # account -> billing usage items, as GET .../settings/billing/usage returns them
        self.usage: dict[str, list[dict[str, Any]]] = {}
        self.url = ""
        self._next_run = 1000

    # --- state -----------------------------------------------------------------------

    def repo(self, name: str) -> Repo:
        return self.repos.setdefault(name.lower(), Repo(name.lower()))

    def pull(self, repo: str, number: int, head: str | None = None, **fields: Any) -> Pull:
        with self.lock:
            pr = Pull(number, head or sha_of(f"{repo}#{number}"), **fields)
            self.repo(repo).pulls[number] = pr
            return pr

    def checks(self, repo: str, sha: str, state: str, completed: datetime | None = None) -> None:
        with self.lock:
            self.repo(repo).checks[sha] = (state, completed)

    def minutes(
        self, account: str, day: datetime, quantity: float, sku: str = "actions_linux"
    ) -> None:
        """Actions minutes an account used on ``day``, in its billing usage report."""
        with self.lock:
            self.usage.setdefault(account.lower(), []).append(
                {
                    "date": day.astimezone(UTC).strftime("%Y-%m-%dT00:00:00Z"),
                    "product": "Actions",
                    "sku": sku,
                    "quantity": quantity,
                    "unitType": "minutes",
                    "pricePerUnit": 0.008,
                    "grossAmount": round(quantity * 0.008, 2),
                    "discountAmount": round(quantity * 0.008, 2),
                    "netAmount": 0.0,
                }
            )

    def billing_answer(self, path: str) -> tuple[int, Any] | None:
        """The billing usage report, for ``/users|organizations/{name}/settings/billing/usage``
        (``year``/``month`` from the query); None for any other path."""
        base, _, query = path.partition("?")
        m = re.fullmatch(r"/(users|organizations)/([^/]+)/settings/billing/usage", base)
        if m is None:
            return None
        params = dict(p.split("=", 1) for p in query.split("&") if "=" in p)
        now = datetime.now(UTC)
        year, month = int(params.get("year", now.year)), int(params.get("month", now.month))
        prefix = f"{year:04d}-{month:02d}-"
        with self.lock:
            items = [i for i in self.usage.get(m[2].lower(), []) if i["date"].startswith(prefix)]
        return 200, {"usageItems": items}

    def run(self, repo: str, **fields: Any) -> Run:
        with self.lock:
            self._next_run += 1
            run = Run(self._next_run, **fields)
            self.repo(repo).runs[run.id] = run
            return run

    # --- answers ---------------------------------------------------------------------

    def answer(self, method: str, path: str) -> tuple[int, Any]:
        path = path.split("?", 1)[0]
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
        if m is None:
            return 404, {"message": "Not Found"}
        repo = self.repos.get(m[1].lower())
        if repo is None:
            return 404, {"message": "Not Found"}
        rest = m[2] or ""
        with self.lock:
            return self._route(method, repo, rest)

    def _route(self, method: str, repo: Repo, rest: str) -> tuple[int, Any]:
        if method == "POST":
            if m := re.fullmatch(r"/actions/runs/(\d+)/cancel", rest):
                run = repo.runs.get(int(m[1]))
                if run is None:
                    return 404, {"message": "Not Found"}
                if run.status == "completed":
                    return 409, {"message": "Cannot cancel a workflow run that is completed."}
                run.status, run.conclusion = "completed", "cancelled"
                run.updated = datetime.now(UTC)
                self.cancelled.append((repo.name, run.id))
                return 202, {}
            return 404, {"message": "Not Found"}
        if rest == "":
            return 200, {"full_name": repo.name, "default_branch": repo.default_branch}
        if m := re.fullmatch(r"/pulls/(\d+)", rest):
            pr = repo.pulls.get(int(m[1]))
            return (200, pr.body()) if pr else (404, {"message": "Not Found"})
        if m := re.fullmatch(r"/pulls/(\d+)/files", rest):
            pr = repo.pulls.get(int(m[1]))
            if pr is None:
                return 404, {"message": "Not Found"}
            return 200, [{"filename": f} for f in pr.files]
        if m := re.fullmatch(r"/commits/([0-9a-f]{7,40})/status", rest):
            state, completed = repo.checks.get(m[1], ("none", None))
            statuses = []
            if state != "none":
                statuses.append(
                    {
                        "context": "ci",
                        "state": state,
                        "updated_at": iso(completed or datetime.now(UTC)),
                    }
                )
            combined = state if state != "none" else "pending"
            return 200, {"state": combined, "total_count": len(statuses), "statuses": statuses}
        if re.fullmatch(r"/commits/([0-9a-f]{7,40})/check-runs", rest):
            return 200, {"total_count": 0, "check_runs": []}
        if rest == "/actions/runs":
            runs = sorted(repo.runs.values(), key=lambda r: r.created, reverse=True)
            return 200, {
                "total_count": len(runs),
                "workflow_runs": [r.body(repo.name) for r in runs],
            }
        if m := re.fullmatch(r"/actions/runs/(\d+)", rest):
            run = repo.runs.get(int(m[1]))
            return (200, run.body(repo.name)) if run else (404, {"message": "Not Found"})
        if m := re.fullmatch(r"/actions/runs/(\d+)/attempts/(\d+)/jobs", rest):
            run = repo.runs.get(int(m[1]))
            if run is None:
                return 404, {"message": "Not Found"}
            done = run.updated or run.created
            job = {
                "name": "build",
                "started_at": iso(run.created),
                "completed_at": iso(done) if run.status == "completed" else None,
                "conclusion": run.conclusion,
            }
            return 200, {"total_count": 1, "jobs": [job]}
        return 404, {"message": "Not Found"}

    # --- inspection ------------------------------------------------------------------

    def paths(self, method: str = "GET") -> list[str]:
        return [r.path for r in self.requests if r.method == method]


def _handler(fake: FakeGitHub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._serve("GET")

        def do_POST(self) -> None:
            length = self.headers.get("Content-Length") or "0"
            self.rfile.read(min(int(length), MAX_BODY) if length.isdigit() else 0)
            self._serve("POST")

        def _serve(self, method: str) -> None:
            auth = self.headers.get("Authorization") or ""
            token = auth.removeprefix("Bearer ") if auth.startswith("Bearer ") else None
            fake.requests.append(Request(method, self.path, token))
            billing = fake.billing_answer(self.path) if method == "GET" else None
            if billing is not None:  # least privilege: the billing token reads billing only
                ok = token in fake.billing_tokens
                status, body = billing if ok else (403, {"message": "Resource not accessible"})
            elif token not in fake.tokens:
                status, body = 401, {"message": "Bad credentials"}
            else:
                status, body = fake.answer(method, self.path)
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-RateLimit-Limit", "5000")
            self.send_header("X-RateLimit-Remaining", "4999")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    return Handler


@contextmanager
def serve() -> Iterator[FakeGitHub]:
    fake = FakeGitHub()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    server.daemon_threads = True
    fake.url = f"http://127.0.0.1:{server.server_address[1]}"
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
