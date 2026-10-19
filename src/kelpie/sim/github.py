"""A stateful fake of the GitHub REST API (loopback only).

It holds repositories, pull requests (with their labels and when each went on), issues
(labels, assignees, comments and their events), CI results per commit and GitHub Actions
runs, answers the endpoints Kelpie's GitHub connector and its issues adapter read, and
carries out the writes they make: the CI cancel (``POST .../actions/runs/{id}/cancel``) and
the issue writes (labels, assignees, comments, reopen). A cancelled run is then completed
as ``cancelled`` at once (real GitHub answers 202 and keeps the run in
progress until the runner stops, so a later scan may still see it active; the farm never
produces that case). Every request is recorded with its method, path and the bearer token
it carried; one without the farm's token gets 401, as GitHub answers bad credentials.

Query strings are ignored but for ``state``, ``page`` and ``per_page`` of the pull request
and issue lists (plus ``labels`` and ``direction`` of the issue list), and ``head_sha`` of
the run list: the run list returns every run of the repository (the farm only creates recent
ones), or those of one commit. Issues assigned a login in ``unassignable`` are answered 201
without it, as GitHub drops a login it cannot assign. A repository with ``fine_grained`` set
answers 403 on check runs and commit statuses, as a fine-grained token does that has no
Checks permission and no Commit statuses access.
"""

from __future__ import annotations

import hashlib
import json
import re
import threading
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

MAX_BODY = 256 * 1024  # a write's JSON body (an issue comment is the largest)
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
    closed_at: datetime | None = None  # when closed without merging (merged: merged_at)
    draft: bool = False
    description: str = ""  # its body ("Fixes #12")
    author: str = "farm-agent"
    files: list[str] = field(default_factory=lambda: ["src/app.py"])
    title: str = ""
    branch: str = ""  # head branch, in the same repository
    base: str = "main"  # the branch it targets; the simulated repositories' default is "main"
    mergeable_state: str = "unknown"  # clean | blocked | dirty | ... as GitHub reports it
    created: datetime | None = None
    updated: datetime | None = None
    # label -> when it was put on (issue events); the PR carries these labels now
    labels: dict[str, datetime] = field(default_factory=dict)

    def body(self, repo: str = "", default_branch: str = "main") -> dict[str, Any]:
        stamp = self.updated or self.created or datetime.now(UTC)
        return {
            "number": self.number,
            "title": self.title,
            "body": self.description,
            "user": {
                "login": self.author,
                "type": "Bot" if self.author.endswith("[bot]") else "User",
            },
            "state": self.state,
            "merged": self.merged_at is not None,
            "merged_at": iso(self.merged_at) if self.merged_at else None,
            "closed_at": iso(closed) if (closed := self.closed()) else None,
            "draft": self.draft,
            "head": {
                "sha": self.head_sha,
                "ref": self.branch or f"pr-{self.number}",
                "repo": {"full_name": repo},
            },
            "base": {"ref": self.base, "repo": {"default_branch": default_branch}},
            "labels": [{"name": name} for name in self.labels],
            "mergeable_state": self.mergeable_state,
            "changed_files": len(self.files),
            "created_at": iso(self.created or stamp),
            "updated_at": iso(stamp),
        }

    def closed(self) -> datetime | None:
        if self.state != "closed":
            return None
        return self.closed_at or self.merged_at or self.updated or self.created

    def events(self) -> list[dict[str, Any]]:
        """The issue events that put the current labels on, oldest first."""
        return [
            {"event": "labeled", "label": {"name": name}, "created_at": iso(at)}
            for name, at in sorted(self.labels.items(), key=lambda item: item[1])
        ]


@dataclass
class Issue:
    """An issue: its labels and assignees (each with when it went on), comments and events,
    oldest first."""

    number: int
    title: str
    created: datetime
    body: str = ""
    author: str = "maintainer"
    association: str = "MEMBER"  # GitHub's author_association
    state: str = "open"
    state_reason: str | None = None
    closed_at: datetime | None = None
    updated: datetime | None = None
    labels: dict[str, datetime] = field(default_factory=dict)
    assignees: dict[str, datetime] = field(default_factory=dict)
    comments: list[dict[str, Any]] = field(default_factory=list)
    events: list[dict[str, Any]] = field(default_factory=list)

    def answer(self, repo: str) -> dict[str, Any]:
        return {
            "number": self.number,
            "title": self.title,
            "body": self.body,
            "user": {"login": self.author, "type": "User"},
            "author_association": self.association,
            "state": self.state,
            "state_reason": self.state_reason,
            "created_at": iso(self.created),
            "updated_at": iso(self.updated or self.created),
            "closed_at": iso(self.closed_at) if self.closed_at else None,
            "labels": [{"name": name} for name in self.labels],
            "assignees": [{"login": login} for login in self.assignees],
            "issue_dependencies_summary": {"blocked_by": 0, "total_blocked_by": 0},
            "sub_issues_summary": {"total": 0},
            "html_url": f"https://github.example/{repo}/issues/{self.number}",
            "repository_url": f"https://api.github.example/repos/{repo}",
        }

    def changed(self, kind: str, at: datetime, **detail: Any) -> None:
        self.updated = at
        self.events.append({"event": kind, "created_at": iso(at), **detail})


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
    issues: dict[int, Issue] = field(default_factory=dict)
    # sha -> (state, completed) per commit: "success" | "failure" | "pending"
    checks: dict[str, tuple[str, datetime | None]] = field(default_factory=dict)
    runs: dict[int, Run] = field(default_factory=dict)
    fine_grained: bool = False  # 403 on check runs and statuses; Actions runs stay readable


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
        self.unassignable: set[str] = set()  # logins GitHub silently does not assign

    # --- state -----------------------------------------------------------------------

    def repo(self, name: str) -> Repo:
        return self.repos.setdefault(name.lower(), Repo(name.lower()))

    def pull(self, repo: str, number: int, head: str | None = None, **fields: Any) -> Pull:
        with self.lock:
            pr = Pull(number, head or sha_of(f"{repo}#{number}"), **fields)
            self.repo(repo).pulls[number] = pr
            return pr

    def issue(self, repo: str, number: int, title: str, created: datetime, **fields: Any) -> Issue:
        """An issue; ``labels`` and ``assignees`` may be given as names (put on at
        ``created``) or as name -> when."""
        with self.lock:
            for key in ("labels", "assignees"):
                value = fields.get(key)
                if isinstance(value, (list, tuple)):
                    fields[key] = {name: created for name in value}
            issue = Issue(number, title, created, **fields)
            for name, at in issue.labels.items():
                issue.events.append(
                    {"event": "labeled", "label": {"name": name}, "created_at": iso(at)}
                )
            for login, at in issue.assignees.items():
                issue.events.append(
                    {"event": "assigned", "assignee": {"login": login}, "created_at": iso(at)}
                )
            issue.events.sort(key=lambda e: e["created_at"])
            self.repo(repo).issues[number] = issue
            return issue

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

    def answer(self, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        path, _, query = path.partition("?")
        params = dict(urllib.parse.parse_qsl(query))
        m = re.fullmatch(r"/repos/([^/]+/[^/]+)(/.*)?", path)
        if m is None:
            return 404, {"message": "Not Found"}
        repo = self.repos.get(m[1].lower())
        if repo is None:
            return 404, {"message": "Not Found"}
        rest = m[2] or ""
        with self.lock:
            if "/issues" in rest:
                return self._issues(method, repo, rest, params, body)
            return self._route(method, repo, rest, params)

    def _route(
        self, method: str, repo: Repo, rest: str, params: dict[str, str] | None = None
    ) -> tuple[int, Any]:
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
        if rest == "/pulls":
            state = (params or {}).get("state", "open")
            pulls = [p for p in repo.pulls.values() if state in ("all", p.state)]
            ordered = [
                p.body(repo.name, repo.default_branch)
                for p in sorted(pulls, key=lambda p: -p.number)
            ]
            return 200, _page(ordered, params or {})
        if m := re.fullmatch(r"/pulls/(\d+)", rest):
            pr = repo.pulls.get(int(m[1]))
            return (
                (200, pr.body(repo.name, repo.default_branch))
                if pr
                else (404, {"message": "Not Found"})
            )
        if m := re.fullmatch(r"/pulls/(\d+)/files", rest):
            pr = repo.pulls.get(int(m[1]))
            if pr is None:
                return 404, {"message": "Not Found"}
            return 200, [{"filename": f} for f in pr.files]
        if repo.fine_grained and re.fullmatch(r"/commits/[0-9a-f]{7,40}/(status|check-runs)", rest):
            return 403, {"message": "Resource not accessible by personal access token"}
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
            if "head_sha" in (params or {}):
                runs = [r for r in runs if r.sha == (params or {})["head_sha"]]
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

    def _issues(
        self, method: str, repo: Repo, rest: str, params: dict[str, str], body: Any
    ) -> tuple[int, Any]:
        now = datetime.now(UTC)
        if method == "GET" and rest == "/issues":
            state = params.get("state", "open")
            wanted = {n.lower() for n in params.get("labels", "").split(",") if n}
            found = [
                i
                for i in repo.issues.values()
                if state in ("all", i.state) and wanted <= {name.lower() for name in i.labels}
            ]
            listed = [(i.created, i.answer(repo.name)) for i in found]
            # GitHub lists pull requests among issues (with "pull_request").
            listed += [
                (p.created or now, {**p.body(repo.name), "pull_request": {"url": ""}})
                for p in repo.pulls.values()
                if state in ("all", p.state) and wanted <= {n.lower() for n in p.labels}
            ]
            listed.sort(key=lambda pair: pair[0], reverse=params.get("direction", "desc") == "desc")
            return 200, _page([answer for _, answer in listed], params)
        m = re.fullmatch(r"/issues/(\d+)(/.*)?", rest)
        if m is None:
            return 404, {"message": "Not Found"}
        number, tail = int(m[1]), m[2] or ""
        issue = repo.issues.get(number)
        if issue is None:
            pr = repo.pulls.get(number)
            if method == "GET" and pr is not None and tail == "/events":
                return 200, pr.events()
            if method == "GET" and pr is not None and tail == "":
                return 200, {**pr.body(repo.name), "pull_request": {"url": "x"}}
            return 404, {"message": "Not Found"}
        needs_body = method in ("POST", "PUT", "PATCH") or (
            method == "DELETE" and tail == "/assignees"
        )
        if needs_body and not isinstance(body, dict):
            return 400, {"message": "Problems parsing JSON"}
        body = body if isinstance(body, dict) else {}
        if method == "GET" and tail == "":
            return 200, issue.answer(repo.name)
        if method == "GET" and tail == "/events":
            return 200, _page(issue.events, params)
        if method == "GET" and tail == "/comments":
            return 200, list(issue.comments)
        if method == "POST" and tail == "/comments":
            if not isinstance(body.get("body"), str) or not body["body"]:
                return 422, {"message": "Validation Failed"}
            comment = {"body": body["body"], "created_at": iso(now)}
            issue.comments.append(comment)
            issue.updated = now
            return 201, comment
        if method in ("POST", "PUT") and tail == "/labels" and not _strings(body, "labels"):
            return 422, {"message": "Validation Failed"}
        if (
            method in ("POST", "DELETE")
            and tail == "/assignees"
            and not _strings(body, "assignees")
        ):
            return 422, {"message": "Validation Failed"}
        if method == "POST" and tail == "/labels":
            for name in body.get("labels", []):
                if name not in issue.labels:
                    issue.labels[name] = now
                    issue.changed("labeled", now, label={"name": name})
            return 200, [{"name": n} for n in issue.labels]
        if method == "PUT" and tail == "/labels":
            keep = list(body.get("labels", []))
            for name in [n for n in issue.labels if n not in keep]:
                del issue.labels[name]
                issue.changed("unlabeled", now, label={"name": name})
            for name in keep:
                if name not in issue.labels:
                    issue.labels[name] = now
                    issue.changed("labeled", now, label={"name": name})
            return 200, [{"name": n} for n in issue.labels]
        if method == "DELETE" and (lm := re.fullmatch(r"/labels/([^/]+)", tail)):
            name = next((n for n in issue.labels if n.lower() == lm[1].lower()), None)
            if name is None:
                return 404, {"message": "Label does not exist"}
            del issue.labels[name]
            issue.changed("unlabeled", now, label={"name": name})
            return 200, [{"name": n} for n in issue.labels]
        if method == "POST" and tail == "/assignees":
            have = {login.lower() for login in issue.assignees}
            for login in body.get("assignees", []):
                if login.lower() not in have and login.lower() not in self.unassignable:
                    issue.assignees[login] = now
                    issue.changed("assigned", now, assignee={"login": login})
            return 201, issue.answer(repo.name)
        if method == "DELETE" and tail == "/assignees":
            drop = {login.lower() for login in body.get("assignees", [])}
            for login in [a for a in issue.assignees if a.lower() in drop]:
                del issue.assignees[login]
                issue.changed("unassigned", now, assignee={"login": login})
            return 200, issue.answer(repo.name)
        if method == "PATCH" and tail == "":
            if body.get("state") == "open" and issue.state == "closed":
                issue.state, issue.state_reason, issue.closed_at = "open", "reopened", None
                issue.changed("reopened", now)
            return 200, issue.answer(repo.name)
        return 404, {"message": "Not Found"}

    # --- inspection ------------------------------------------------------------------

    def paths(self, method: str = "GET") -> list[str]:
        return [r.path for r in self.requests if r.method == method]


def _handler(fake: FakeGitHub) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._serve("GET")

        def do_POST(self) -> None:
            self._write("POST")

        def do_PATCH(self) -> None:
            self._write("PATCH")

        def do_PUT(self) -> None:
            self._write("PUT")

        def do_DELETE(self) -> None:
            self._write("DELETE")

        def _write(self, method: str) -> None:
            length = self.headers.get("Content-Length") or "0"
            if length.isdigit() and int(length) > MAX_BODY:
                fake.requests.append(Request(method, self.path, None))
                left = int(length)
                while left > 0:  # drain it: closing on unread data would reset the connection
                    left -= len(self.rfile.read(min(left, 64 * 1024)) or b"x" * left)
                self._send(413, {"message": "Payload too large"})
                return
            raw = self.rfile.read(int(length) if length.isdigit() else 0)
            try:
                body = json.loads(raw) if raw else None
            except (ValueError, RecursionError):
                body = None
            self._serve(method, body)

        def _serve(self, method: str, body: Any = None) -> None:
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
                status, body = fake.answer(method, self.path, body)
            self._send(status, body)

        def _send(self, status: int, body: Any) -> None:
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


def _strings(body: dict[str, Any], key: str) -> bool:
    value = body.get(key)
    return isinstance(value, list) and all(isinstance(v, str) for v in value)


def _page(items: list[Any], params: dict[str, str]) -> list[Any]:
    """One page of ``items`` (``page``/``per_page``; all of them when not asked)."""
    if "per_page" not in params:
        return items
    size = int(params["per_page"]) if params["per_page"].isdigit() else 30  # GitHub's default
    page = int(params.get("page", "1")) if params.get("page", "1").isdigit() else 1
    size, page = max(1, size), max(1, page)
    return items[(page - 1) * size : page * size]


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
