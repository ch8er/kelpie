"""A local stand-in for Linear's GraphQL API (loopback only), answering the operations the
issues adapter sends (by ``operationName``) from an in-memory workspace.

Like Linear, a request without the workspace's API key is answered ``AUTHENTICATION_ERROR``,
and with ``rate_limited`` set every request is answered HTTP 400 with a ``RATELIMITED``
error and a reset time in epoch milliseconds."""

from __future__ import annotations

import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

KEY = "lin_api_SyntheticTestKey0123456789"
CLOSED = {"completed", "canceled", "duplicate"}


def iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass
class Issue:
    identifier: str
    title: str
    created: datetime
    state: str = "Todo"
    state_type: str = "unstarted"
    description: str = ""
    assignee: str | None = None
    delegate: str | None = None
    labels: list[str] = field(default_factory=list)
    creator: dict[str, Any] = field(
        default_factory=lambda: {"displayName": "dana", "app": False, "guest": False}
    )
    children: list[str] = field(default_factory=list)
    blockers: list[str] = field(default_factory=list)
    attachments: list[dict[str, Any]] = field(default_factory=list)
    updated: datetime | None = None
    started: datetime | None = None
    history: list[dict[str, Any]] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)
    bot_actor: dict[str, Any] | None = None  # created through an integration

    @property
    def team(self) -> str:
        return self.identifier.split("-", 1)[0]


class Broken(Exception):
    """An answer with a GraphQL error."""


class FakeLinear:
    def __init__(self) -> None:
        self.issues: dict[str, Issue] = {}
        self.users: dict[str, dict[str, Any]] = {}  # displayName -> {id, app}
        self.labels: dict[str, dict[str, Any]] = {}  # id -> {name, team}
        self.states: dict[str, list[dict[str, Any]]] = {}  # team -> states
        self.requests: list[dict[str, Any]] = []
        self.headers: dict[str, str] = {}  # sent with every answer (rate-limit headers)
        self.status: int | None = None  # answer every request with this HTTP status
        self.rate_limited = False
        self.limited_ops: set[str] = set()  # operations answered RATELIMITED
        self.broken_teams: set[str] = set()  # their issue lists fail
        self.broken_ops: set[str] = set()  # operations answered with a GraphQL error
        self.ignore_slots = False  # issueUpdate succeeds but leaves assignee and delegate
        self.url = ""
        self.lock = threading.Lock()

    # --- state -------------------------------------------------------------------------

    def user(self, name: str, app: bool = False) -> None:
        self.users[name] = {"id": f"user-{name.lower()}", "app": app}

    def label(self, name: str, team: str | None = None) -> None:
        self.labels[f"label-{team or 'ws'}-{name.lower()}"] = {"name": name, "team": team}

    def team(self, key: str) -> None:
        self.states[key] = [
            {"id": f"{key}-backlog", "type": "backlog", "position": 0, "name": "Backlog"},
            {"id": f"{key}-todo", "type": "unstarted", "position": 1, "name": "Todo"},
            {"id": f"{key}-doing", "type": "started", "position": 2, "name": "In Progress"},
            {"id": f"{key}-done", "type": "completed", "position": 3, "name": "Done"},
        ]

    def issue(self, identifier: str, title: str, created: datetime, **fields: Any) -> Issue:
        issue = Issue(identifier, title, created, **fields)
        self.issues[identifier] = issue
        return issue

    def ops(self) -> list[str]:
        return [r["operationName"] for r in self.requests]

    def find(self, ident: str) -> Issue:
        """By identifier or by UUID, as Linear finds an issue."""
        return self.issues[ident.removeprefix("uuid-")]

    # --- answers -----------------------------------------------------------------------

    def answer(self, request: dict[str, Any]) -> Any:
        op, v = request.get("operationName"), request.get("variables") or {}
        if op in self.broken_ops:
            raise Broken
        return getattr(self, f"_{op}")(v)

    def _node(self, i: Issue) -> dict[str, Any]:
        def ref(name: str | None, app: bool | None = None) -> dict[str, Any] | None:
            if name is None:
                return None
            user = self.users.get(name, {})
            if app is None:
                app = user.get("app", False)
            return {"id": user.get("id", f"anon-{name}"), "displayName": name, "app": app}

        def brief(identifier: str) -> dict[str, Any]:
            other = self.issues.get(identifier)
            kind = other.state_type if other else "unstarted"
            return {"identifier": identifier, "state": {"type": kind}}

        return {
            "id": f"uuid-{i.identifier}",
            "identifier": i.identifier,
            "title": i.title,
            "description": i.description,
            "url": f"https://linear.example/issue/{i.identifier}",
            "createdAt": iso(i.created),
            "updatedAt": iso(i.updated or i.created),
            "startedAt": iso(i.started) if i.started else None,
            "completedAt": None,
            "canceledAt": None,
            "state": {"name": i.state, "type": i.state_type},
            "team": {"key": i.team},
            "creator": i.creator,
            "botActor": i.bot_actor,
            "assignee": ref(i.assignee),
            "delegate": ref(i.delegate, app=True),  # only ever an app user
            "labels": {"nodes": [{"name": n} for n in i.labels]},
            "children": {"nodes": [brief(c) for c in i.children]},
            "inverseRelations": {
                "nodes": [{"type": "blocks", "issue": brief(b)} for b in i.blockers]
            },
            "attachments": {"nodes": i.attachments},
        }

    def _KelpieIssues(self, v: dict[str, Any]) -> dict[str, Any]:
        f = v["filter"]
        team = f["team"]["key"]["eq"]
        if team in self.broken_teams:
            raise Broken
        kinds = f["state"]["type"]
        found = [i for i in self.issues.values() if i.team == team]
        if "nin" in kinds:
            found = [i for i in found if i.state_type not in kinds["nin"]]
        if "in" in kinds:
            found = [i for i in found if i.state_type in kinds["in"]]
        if "labels" in f:
            want = f["labels"]["some"]["name"]["eqIgnoreCase"].lower()
            found = [i for i in found if want in {n.lower() for n in i.labels}]
        if "updatedAt" in f:
            since = f["updatedAt"]["gt"]
            found = [i for i in found if iso(i.updated or i.created) > since]
        if v["order"] == "updatedAt":  # newest first, as Linear lists by updatedAt
            found.sort(key=lambda i: i.updated or i.created, reverse=True)
        else:
            found.sort(key=lambda i: i.created)
        start = int(v.get("after") or 0)
        page = found[start : start + v["first"]]
        more = start + v["first"] < len(found)
        return {
            "issues": {
                "nodes": [self._node(i) for i in page],
                "pageInfo": {"hasNextPage": more, "endCursor": str(start + v["first"])},
            }
        }

    def _KelpieIssue(self, v: dict[str, Any]) -> dict[str, Any]:
        issue = self.issues.get(v["id"].removeprefix("uuid-"))
        return {"issue": self._node(issue) if issue else None}

    def _KelpieHistory(self, v: dict[str, Any]) -> dict[str, Any]:
        issue = self.find(v["id"])
        more = len(issue.history) > 50  # history(first: 50)
        nodes = issue.history[:50]
        return {"issue": {"history": {"nodes": nodes, "pageInfo": {"hasNextPage": more}}}}

    def _KelpieLabels(self, v: dict[str, Any]) -> dict[str, Any]:
        by_name, by_team = v["filter"]["and"]
        names = {f["name"]["eqIgnoreCase"].lower() for f in by_name["or"]}
        (team,) = [f["team"]["key"]["eq"] for f in by_team["or"] if "key" in f["team"]]
        nodes = [
            {"id": ident, "name": lb["name"], "team": {"key": lb["team"]} if lb["team"] else None}
            for ident, lb in self.labels.items()
            if lb["name"].lower() in names and lb["team"] in (None, team)
        ]
        return {"issueLabels": {"nodes": nodes}}

    def _KelpieUsers(self, v: dict[str, Any]) -> dict[str, Any]:
        names = {f["displayName"]["eqIgnoreCase"].lower() for f in v["filter"]["or"]}
        nodes = [
            {"id": u["id"], "displayName": name, "app": u["app"]}
            for name, u in self.users.items()
            if name.lower() in names
        ]
        return {"users": {"nodes": nodes}}

    def _KelpieTeams(self, v: dict[str, Any]) -> dict[str, Any]:
        return {"teams": {"nodes": [{"key": k} for k in v["keys"] if k in self.states]}}

    def _KelpieStates(self, v: dict[str, Any]) -> dict[str, Any]:
        states = self.states.get(v["team"])
        nodes = [{"states": {"nodes": states}}] if states else []
        return {"teams": {"nodes": nodes}}

    def _KelpieUpdate(self, v: dict[str, Any]) -> dict[str, Any]:
        issue = self.find(v["id"])
        change = v["input"]
        by_id = {u["id"]: name for name, u in self.users.items()}
        if self.ignore_slots:
            change = {k: x for k, x in change.items() if k not in ("assigneeId", "delegateId")}
        if "assigneeId" in change:
            issue.assignee = by_id.get(change["assigneeId"]) if change["assigneeId"] else None
        if "delegateId" in change:
            issue.delegate = by_id.get(change["delegateId"]) if change["delegateId"] else None
        for ident in change.get("addedLabelIds", []):
            name = self.labels[ident]["name"]
            if name not in issue.labels:
                issue.labels.append(name)
        for ident in change.get("removedLabelIds", []):
            name = self.labels[ident]["name"]
            issue.labels = [n for n in issue.labels if n != name]
        if "stateId" in change:
            state = next(s for s in self.states[issue.team] if s["id"] == change["stateId"])
            issue.state, issue.state_type = state["name"], state["type"]
        issue.updated = datetime.now(UTC)
        node = self._node(issue)
        return {
            "issueUpdate": {
                "success": True,
                "issue": {k: node[k] for k in ("assignee", "delegate")},
            }
        }

    def _KelpieComment(self, v: dict[str, Any]) -> dict[str, Any]:
        self.find(v["input"]["issueId"]).comments.append(v["input"]["body"])
        return {"commentCreate": {"success": True}}


def _handler(fake: FakeLinear) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            request = json.loads(raw)
            request["auth"] = self.headers.get("Authorization")
            with fake.lock:
                fake.requests.append(request)
                headers = {"X-RateLimit-Requests-Remaining": "1400", **fake.headers}
                if fake.status is not None:
                    status, body = fake.status, {"errors": [{"message": "x"}]}
                elif fake.rate_limited or request.get("operationName") in fake.limited_ops:
                    status, body = 400, {"errors": [{"extensions": {"code": "RATELIMITED"}}]}
                    reset = int(datetime.now(UTC).timestamp() * 1000) + 120_000
                    headers = {"X-RateLimit-Requests-Reset": str(reset), **fake.headers}
                elif request["auth"] != KEY:
                    status = 400
                    body = {"errors": [{"extensions": {"code": "AUTHENTICATION_ERROR"}}]}
                else:
                    try:
                        status, body = 200, {"data": fake.answer(request)}
                    except Broken:
                        status = 200
                        body = {"errors": [{"extensions": {"code": "INTERNAL_ERROR"}}]}
            data = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, *args: object) -> None:
            pass

    return Handler


@contextmanager
def serve() -> Iterator[FakeLinear]:
    fake = FakeLinear()
    server = ThreadingHTTPServer(("127.0.0.1", 0), _handler(fake))
    server.daemon_threads = True
    fake.url = f"http://127.0.0.1:{server.server_address[1]}/graphql"
    thread = threading.Thread(target=server.serve_forever, args=(0.05,), daemon=True)
    thread.start()
    try:
        yield fake
    finally:
        server.shutdown()
        server.server_close()
