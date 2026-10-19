"""Reading and changing Linear issues as :class:`~kelpie.adapters.issues.mapping.Item`
through Linear's GraphQL API.

Boards are teams (``ENG``); task ids are Linear's identifiers (``ENG-123``). Agents that work
Linear issues (Codex, Cursor, Copilot and others integrated as Linear agents) are app users
the issue is **delegated** to; people are **assigned**. Both are the item's assignees, so an
:class:`~kelpie.adapters.issues.mapping.Agent` names its app user as ``assignee``.

**Reading**, at most every ``refresh`` (GraphQL has no conditional requests, and Linear
meters an API key by requests and by query complexity per hour; between refreshes the last
read is served), first every team's lists, then history:

- open issues (any state but completed, canceled or duplicate), pages of ``PAGE``, at most
  ``MAX_PAGES``, and the most recently updated page of issues closed within ``lookback``:
  state, assignee, delegate, labels, creator, sub-issues, the issues that block them
  (``blocks`` relations) and the GitHub pull requests Linear's GitHub integration attached;
- per issue, only when needed (an assignee, a delegate or labels; a description edited
  since it was created) and within ``max_requests``: its history (who was assigned or
  delegated, labels put on, state changes, who edited the description), kept until the
  issue changes.

Every query stays well under Linear's 10,000-point limit for one query. Reads stop while
less than ``RESERVE_REQUESTS`` / ``RESERVE_COMPLEXITY`` (plus what a team's last read took,
before that team's read) is left of the hour (writes still go), as Linear last said, until
that limit's reset or for ``LIMITS_TTL``; the refresh stretches so reads take at most
``READ_SHARE`` of the hour's complexity, to ``MAX_REFRESH`` at most. A team that cannot be
read is served from its last good read for ``BOARD_GRACE``, then left out.

A workflow state the board names in ``ready_states`` means the issue waits for an agent
(``ready``); ``started`` with nobody on it is ``in_progress`` with no owner.

**Writes** go through :class:`~kelpie.adapters.issues.writes.WriteGate` (paced, capped,
reserved per action): ``issueUpdate`` (``addedLabelIds`` / ``removedLabelIds``, which change
only those labels; ``assigneeId`` / ``delegateId``; ``stateId`` to reopen) and
``commentCreate``, to the UUID of the issue the action checked. Every id they may need
(labels, users, the state to reopen to) is looked up when the action reserves its writes,
before the first, and kept for ``LOOKUP_TTL``.

The HTTP client keeps to one origin (https, or http on loopback), follows no redirect, reads
at most ``MAX_BODY`` bytes, and backs off when Linear says it is rate limited (HTTP 429 or a
``RATELIMITED`` error) until the reset it gives, at most ``MAX_BACKOFF``. The API key goes
only in the ``Authorization`` header and never into a message.
"""

from __future__ import annotations

import json
import logging
import re
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from kelpie import __version__
from kelpie.adapters.issues.mapping import (
    Agent,
    Board,
    Event,
    IssuesError,
    Item,
    LinkedPull,
    login_key,
)
from kelpie.adapters.issues.writes import WriteGate
from kelpie.connectors.github.client import check_api_url, valid_token
from kelpie.core.ports import Deferred

log = logging.getLogger(__name__)

DEFAULT_API_URL = "https://api.linear.app/graphql"
# Linear rejects a query whose complexity (estimated from the ``first`` of every list) is
# above 10,000 points; these keep a page of issues near 5,000 (``tests`` estimate it).
PAGE = 25
MAX_PAGES = 20  # pages of open issues per team (500 issues)
MAX_COMPLEXITY = 10_000
DETAIL_TTL = timedelta(minutes=10)
DEFAULT_REFRESH = timedelta(minutes=2)
DEFAULT_MAX_REQUESTS = 20  # history reads per refresh
BOARD_GRACE = timedelta(minutes=10)  # a team that cannot be read keeps its last read this long
LOOKUP_TTL = timedelta(hours=1)  # label, user and state ids are looked up again after this
# Reads stop while less than this (plus what a team's last read took, before that team's
# read) is left of the hour's requests or complexity, so writes (and the lookups they need)
# can still be made; reads take at most READ_SHARE of the hourly
# complexity, the refresh stretching to keep within it.
RESERVE_REQUESTS = 100
RESERVE_COMPLEXITY = 50_000
READ_SHARE = 0.5
LIMITS_TTL = timedelta(minutes=5)  # what Linear said is left holds this long (or to its reset)
# The stretched refresh never exceeds this: less than the ledger's 30-minute cooldown, so
# no rule acts twice on one stale read.
MAX_REFRESH = timedelta(minutes=25)
UNKNOWN = "@unknown"  # someone holds the slot, under a name Kelpie does not take as a name
MAX_BODY = 4 * 1024 * 1024
MAX_BACKOFF = timedelta(hours=1)
ERROR_BACKOFF = timedelta(seconds=60)
MAX_COMMENT = 60_000
MAX_TITLE = 300
CLOSED_TYPES = {"completed": "completed", "canceled": "not_planned", "duplicate": "duplicate"}
_ID = re.compile(r"(?P<team>[A-Z][A-Z0-9_]{0,15})-(?P<number>[1-9][0-9]{0,8})")
_PULL = re.compile(
    r"https://(?P<host>[A-Za-z0-9.-]{1,255})/(?P<repo>[\w.-]{1,100}/[\w.-]{1,100})"
    r"/pull/(?P<number>[1-9][0-9]{0,8})"
)
_UUID = re.compile(r"[A-Za-z0-9-]{1,64}")
_HEADER_INT = re.compile(r"-?[0-9]{1,15}")
MAX_NAME = 100
MAX_STATE = 64
GITHUB_HOST = "github.com"

FIELDS = """
fragment KelpieIssue on Issue {
  id identifier title description url createdAt updatedAt startedAt completedAt canceledAt
  state { name type }
  team { key }
  creator { displayName app guest }
  botActor { id }
  assignee { id displayName app }
  delegate { id displayName app }
  labels(first: 20) { nodes { name } }
  children(first: 25) { nodes { identifier state { type } } }
  inverseRelations(first: 25) { nodes { type issue { identifier state { type } } } }
  attachments(first: 10) { nodes { url sourceType metadata createdAt } }
}
"""
ISSUES = (
    FIELDS
    + """
query KelpieIssues(
  $filter: IssueFilter!, $after: String, $first: Int!, $order: PaginationOrderBy!
) {
  issues(first: $first, after: $after, filter: $filter, orderBy: $order) {
    nodes { ...KelpieIssue }
    pageInfo { hasNextPage endCursor }
  }
}
"""
)
ISSUE = (
    FIELDS
    + """
query KelpieIssue($id: String!) { issue(id: $id) { ...KelpieIssue } }
"""
)
HISTORY = """
query KelpieHistory($id: String!) {
  issue(id: $id) {
    history(first: 50) {
      pageInfo { hasNextPage }
      nodes {
        createdAt
        actor { app guest } botActor { id } updatedDescription
        fromAssignee { id displayName app } toAssignee { id displayName app }
        fromDelegate { id displayName app } toDelegate { id displayName app }
        addedLabels { name } removedLabels { name }
        fromState { type } toState { type }
      }
    }
  }
}
"""
LABELS = """
query KelpieLabels($filter: IssueLabelFilter!) {
  issueLabels(first: 50, filter: $filter) {
    nodes { id name team { key } }
  }
}
"""
USERS = """
query KelpieUsers($filter: UserFilter!) {
  users(first: 50, filter: $filter) {
    nodes { id displayName app }
  }
}
"""
STATES = """
query KelpieStates($team: String!) {
  teams(first: 1, filter: { key: { eq: $team } }) {
    nodes { states(first: 50) { nodes { id name type position } } }
  }
}
"""
TEAMS = """
query KelpieTeams($keys: [String!]!) {
  teams(first: 50, filter: { key: { in: $keys } }) { nodes { key } }
}
"""
UPDATE = """
mutation KelpieUpdate($id: String!, $input: IssueUpdateInput!) {
  issueUpdate(id: $id, input: $input) {
    success
    issue { assignee { id displayName app } delegate { id displayName app } }
  }
}
"""
COMMENT = """
mutation KelpieComment($input: CommentCreateInput!) {
  commentCreate(input: $input) { success }
}
"""


class LinearError(Exception):
    """A request that gave no usable answer; ``kind`` is short and the message never holds
    the API key or a response body."""

    def __init__(self, kind: str, message: str) -> None:
        super().__init__(message)
        self.kind = kind


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def _time(value: Any) -> datetime | None:
    if not isinstance(value, str):
        return None
    try:
        at = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return at if at.tzinfo is not None else at.replace(tzinfo=UTC)


def _iso(at: datetime) -> str:
    return at.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000Z")


@dataclass(slots=True)
class LinearClient:
    """POST GraphQL to Linear. Not thread-safe: the control loop owns it."""

    api_url: str = DEFAULT_API_URL
    token: str | None = field(default=None, repr=False)
    timeout: timedelta = timedelta(seconds=10)
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    backoff_until: datetime | None = None
    requests_left: int | None = None  # this refresh's budget; None: no limit
    # From Linear's last answer: what is left of the hour's requests and complexity, the
    # hour's complexity limit; and the complexity spent since the client was made.
    hour_requests: int | None = None
    hour_complexity: int | None = None
    complexity_limit: int | None = None
    complexity_spent: int = 0
    requests_made: int = 0  # since the client was made
    # Until when the hour_* figures hold: their reset, at most LIMITS_TTL after they came.
    limits_until: datetime | None = None
    _opener: Any = field(default=None, init=False, repr=False)

    def __post_init__(self) -> None:
        self.api_url = check_api_url(self.api_url)
        if self.token is not None and not valid_token(self.token):
            raise ValueError("Linear API key contains characters not allowed in a header")
        handlers: list[Any] = [_NoRedirect()]
        if self.api_url.startswith("http://"):
            # Plain http (loopback only): never through a proxy, which would see the key.
            handlers.append(urllib.request.ProxyHandler({}))
        self._opener = urllib.request.build_opener(*handlers)

    def __repr__(self) -> str:
        auth = "key" if self.token else "anonymous"
        return f"LinearClient(api_url={self.api_url!r}, auth={auth})"

    def backing_off(self) -> bool:
        return self.backoff_until is not None and self.clock() < self.backoff_until

    def may_read(self, cost: int = 0, requests: int = 0) -> bool:
        """Whether a read costing ``cost`` complexity points and ``requests`` requests leaves
        the reserve kept for writes, as far as Linear's last answer says; figures past their
        reset (or ``LIMITS_TTL``) say nothing, so the next read asks Linear again."""
        if self.limits_until is None or self.clock() >= self.limits_until:
            return True
        low_requests = (
            self.hour_requests is not None and self.hour_requests < RESERVE_REQUESTS + requests
        )
        low_complexity = (
            self.hour_complexity is not None and self.hour_complexity < RESERVE_COMPLEXITY + cost
        )
        return not (low_requests or low_complexity)

    def query(
        self, op: str, document: str, variables: Mapping[str, Any], read: bool = False
    ) -> Any:
        """The ``data`` of ``op``. ``read``: a read that may wait, refused while what is left
        of the hour's limits is kept for writes. Raises :class:`LinearError`."""
        now = self.clock()
        if self.backoff_until is not None and now < self.backoff_until:
            raise LinearError("backoff", f"{op}: backing off")
        if read and not self.may_read():
            raise LinearError("reserve", f"{op}: the rest of the hour's limit is kept for writes")
        if self.requests_left is not None:
            if self.requests_left <= 0:
                raise LinearError("budget", f"{op}: request budget spent")
            self.requests_left -= 1
        self.requests_made += 1
        payload = json.dumps(
            {"query": document, "variables": dict(variables), "operationName": op}
        ).encode()
        request = urllib.request.Request(self.api_url, data=payload, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("User-Agent", f"kelpie/{__version__}")
        if self.token:
            # Personal API keys go as they are; OAuth access tokens as Bearer.
            auth = self.token if self.token.startswith("lin_api_") else f"Bearer {self.token}"
            request.add_header("Authorization", auth)
        try:
            with self._opener.open(request, timeout=self.timeout.total_seconds()) as answer:
                status, headers = answer.status, answer.headers
                raw = answer.read(MAX_BODY + 1)
        except urllib.error.HTTPError as exc:
            status, headers = exc.code, exc.headers
            raw = exc.read(64 * 1024)
        except (OSError, ValueError) as exc:
            self._back_off(now + ERROR_BACKOFF, now)
            raise LinearError("network", f"{op}: {type(exc).__name__}") from None
        self._note_limits(headers, now)
        if len(raw) > MAX_BODY:
            raise LinearError("bad_response", f"{op}: answer too large")
        try:
            body = json.loads(raw) if raw else {}
        except (ValueError, RecursionError):
            body = None
        errors = body.get("errors") if isinstance(body, Mapping) else None
        codes = [_code(e) for e in errors] if isinstance(errors, list) else []
        if status == 429 or "RATELIMITED" in codes:
            self._back_off(self._exhausted_reset(headers, now) or now + ERROR_BACKOFF, now)
            raise LinearError("rate_limited", f"{op}: rate limited")
        if status in (401, 403) or "AUTHENTICATION_ERROR" in codes:
            if status == 401:
                self._back_off(now + ERROR_BACKOFF, now)
            raise LinearError("unauthorized", f"{op}: HTTP {status} (API key missing or wrong)")
        if status >= 500:
            self._back_off(now + ERROR_BACKOFF, now)
            raise LinearError("server", f"{op}: HTTP {status}")
        if status >= 300 and status < 400:
            raise LinearError("redirect", f"{op}: HTTP {status} redirect (not followed)")
        if not isinstance(body, Mapping):
            raise LinearError("bad_response", f"{op}: not JSON")
        if codes or status >= 400:
            kinds = ", ".join(sorted({c for c in codes if c})) or f"HTTP {status}"
            raise LinearError("graphql", f"{op}: {kinds}")
        return body.get("data")

    def _note_limits(self, headers: Any, now: datetime) -> None:
        """What Linear's headers say is left; a limit used up backs off until its own reset."""
        self.hour_requests = _header_int(headers, "X-RateLimit-Requests-Remaining")
        self.hour_complexity = _header_int(headers, "X-RateLimit-Complexity-Remaining")
        resets = [
            r for kind in ("Requests", "Complexity") if (r := self._reset(headers, now, kind))
        ]
        self.limits_until = min([now + LIMITS_TTL, *resets])
        limit = _header_int(headers, "X-RateLimit-Complexity-Limit")
        if limit is not None and limit > 0:
            self.complexity_limit = max(limit, MAX_COMPLEXITY)
        cost = _header_int(headers, "X-Complexity")
        if cost is not None and cost > 0:
            self.complexity_spent += min(cost, MAX_COMPLEXITY)  # no query costs more
        for kind in ("Requests", "Endpoint-Requests", "Complexity"):
            left = _header_int(headers, f"X-RateLimit-{kind}-Remaining")
            if left is not None and left <= 0:
                reset = self._reset(headers, now, kind)
                self._back_off(reset or now + ERROR_BACKOFF, now)

    def _exhausted_reset(self, headers: Any, now: datetime) -> datetime | None:
        """The reset of the limit that ran out; else the earliest reset Linear gives."""
        kinds = ("Requests", "Endpoint-Requests", "Complexity")
        for kind in kinds:
            left = _header_int(headers, f"X-RateLimit-{kind}-Remaining")
            if left is not None and left <= 0 and (reset := self._reset(headers, now, kind)):
                return reset
        resets = [r for kind in kinds if (r := self._reset(headers, now, kind))]
        return min(resets, default=None)

    @staticmethod
    def _reset(headers: Any, now: datetime, kind: str | None = None) -> datetime | None:
        """When the limit ``kind`` resets (any limit's reset when ``kind`` is None)."""
        kinds = (kind,) if kind else ("Requests", "Endpoint-Requests", "Complexity")
        for name in kinds:
            value = _header_int(headers, f"X-RateLimit-{name}-Reset")
            if value is not None and value >= 0:
                try:
                    return datetime.fromtimestamp(value / 1000, UTC)  # epoch ms
                except (OverflowError, ValueError, OSError):
                    return None
        return None

    def _back_off(self, until: datetime, now: datetime) -> None:
        until = min(until, now + MAX_BACKOFF)
        if self.backoff_until is None or until > self.backoff_until:
            self.backoff_until = until


def _code(error: Any) -> str:
    ext = error.get("extensions") if isinstance(error, Mapping) else None
    code = ext.get("code") if isinstance(ext, Mapping) else None
    return code if isinstance(code, str) and code.isupper() and len(code) <= 64 else ""


def _header_int(headers: Any, name: str) -> int | None:
    value = headers.get(name) if headers is not None else None
    if isinstance(value, str) and _HEADER_INT.fullmatch(value.strip()):
        return int(value.strip())
    return None


def _text(value: Any, limit: int = MAX_NAME) -> str | None:
    """A label, state or display name as Linear gave it, if it is one: printable (no control,
    bidi or zero-width characters) and at most ``limit`` characters."""
    if isinstance(value, str) and 0 < len(value) <= limit and value.isprintable():
        return value
    return None


def _name(user: Any) -> str | None:
    """A user's display name. Names ending in ``[bot]`` are refused: Kelpie compares names as
    GitHub logins, without that suffix, so such a name could pass for an agent's."""
    name = _text(user.get("displayName") if isinstance(user, Mapping) else None)
    if name is None or name != name.strip() or name.casefold().endswith("[bot]"):
        return None
    return name


def _nodes(connection: Any) -> list[Any]:
    nodes = connection.get("nodes") if isinstance(connection, Mapping) else None
    return [n for n in nodes if isinstance(n, Mapping)] if isinstance(nodes, list) else []


def _state_type(issue: Any) -> str:
    state = issue.get("state") if isinstance(issue, Mapping) else None
    kind = state.get("type") if isinstance(state, Mapping) else None
    return kind if isinstance(kind, str) else ""


def _identifier(issue: Any) -> str | None:
    value = issue.get("identifier") if isinstance(issue, Mapping) else None
    return value if isinstance(value, str) and _ID.fullmatch(value) else None


@dataclass(slots=True)
class _Detail:
    updated_at: datetime
    read_at: datetime
    # The history as Linear gave it: its users are named (``_holder``) each time the item is
    # built, so a history read while the agents' users were unknown is not stuck that way.
    nodes: list[Mapping[str, Any]]
    # Every description edit in the history was a member's (not a guest's, an app's or an
    # unknown actor's), and the history was read whole.
    description_ok: bool = False


@dataclass(slots=True)
class _Fetched:
    """What a fetch saw of an issue, for the writes that follow: its UUID (writes go to the
    issue that was checked) and the user ids holding each slot (kept up to date by the
    writes; ``""``: held by a user Linear gave no id for)."""

    uuid: str
    assignee: str | None
    delegate: str | None
    open: bool = True


# Lookup errors that mean "not now": before an action's first write, the action is deferred.
_WAIT = frozenset({"backoff", "rate_limited", "network", "server", "budget"})


@dataclass(slots=True)
class LinearIssues:
    """Reads the teams' issues and carries out writes; one per adapter."""

    client: LinearClient
    boards: Sequence[Board]
    lookback: timedelta
    refresh: timedelta = DEFAULT_REFRESH
    max_requests: int = DEFAULT_MAX_REQUESTS  # history reads per refresh
    # Repositories whose pull requests count when attached by Linear's GitHub integration,
    # as "host/owner/repo" in lower case; empty: any repository on github.com.
    pull_repos: frozenset[str] = frozenset()
    agents: Mapping[str, Agent] = field(default_factory=dict)  # their ids are looked up early
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    gate: WriteGate = field(default_factory=WriteGate)
    problem: str | None = field(default=None, init=False)
    _last: tuple[datetime, list[Item]] | None = field(default=None, init=False, repr=False)
    _next: datetime | None = field(default=None, init=False, repr=False)  # the next read
    _failed: IssuesError | None = field(default=None, init=False, repr=False)
    # Per team: complexity points and requests its last whole read took.
    _team_cost: dict[str, tuple[int, int]] = field(default_factory=dict, init=False, repr=False)
    _good: dict[str, tuple[datetime, list[Any]]] = field(
        default_factory=dict, init=False, repr=False
    )
    _details: dict[str, _Detail] = field(default_factory=dict, init=False, repr=False)
    # Looked-up ids; None: looked up, and Linear has none (or more than one) by that name.
    _labels: dict[tuple[str, str], str | None] = field(default_factory=dict, init=False, repr=False)
    _users: dict[str, tuple[str, bool] | None] = field(default_factory=dict, init=False, repr=False)
    _states: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _looked_up: datetime | None = field(default=None, init=False, repr=False)
    _fetched: dict[str, _Fetched] = field(default_factory=dict, init=False, repr=False)
    _last_fetch: str | None = field(default=None, init=False, repr=False)  # an item id
    _stale: bool = field(default=False, init=False, repr=False)  # written since the last read
    _stretched: bool = field(default=False, init=False, repr=False)  # refresh > ``refresh``

    # --- reading -------------------------------------------------------------------------

    def read(self, now: datetime) -> list[Item]:
        """Every team's items: every team's lists first, then per-issue history with the
        budget left. Between refreshes (``refresh``, stretched to keep reads within
        ``READ_SHARE`` of the hour's complexity) the last read is served, unless something
        was written. A team whose lists cannot be read is served from its last good lists
        for ``BOARD_GRACE``, then left out; raises :class:`IssuesError` when no team can be
        read at all."""
        self.gate.new_tick()
        # A write makes the next read come early, unless reads are already stretched to
        # keep within the hour's complexity.
        early = self._stale and not self._stretched
        if not early and self._next is not None and now < self._next:
            if self._last is not None:
                return self._last[1]
            if self._failed is not None:
                raise self._failed
        self.problem = None
        before = self.client.complexity_spent
        self._agents_known()
        lists: list[tuple[Board, list[Any]]] = []
        failures: list[IssuesError] = []
        # Lists are bounded by their page caps, not by max_requests (cutting them would drop
        # teams); that budget is for history.
        self.client.requests_left = None
        for board in self.boards:
            try:
                raw = self._lists(board, now)
            except IssuesError as exc:
                failures.append(exc)
                kept = self._good.get(board.key)
                if kept is None or now - kept[0] > BOARD_GRACE:
                    self._report(f"{board.key}: not read ({exc.public_reason})")
                    continue
                self._report(f"{board.key}: last read {_iso(kept[0])} ({exc.public_reason})")
                raw = kept[1]
            else:
                self._good[board.key] = (now, raw)
            lists.append((board, raw))
        self._stale = False
        if not lists and failures:
            self._last, self._failed = None, failures[0]
            self._next = now + self.refresh
            raise failures[0]
        self.client.requests_left = self.max_requests
        try:
            items = [i for board, raw in lists for i in self._items(board, raw)]
        finally:
            self.client.requests_left = None
        interval = self._interval(self.client.complexity_spent - before)
        self._next, self._stretched = now + interval, interval > self.refresh
        self._last, self._failed = (now, items), None
        live = {i.id for i in items}
        self._details = {k: v for k, v in self._details.items() if k in live}
        self._fetched = {k: v for k, v in self._fetched.items() if k in live}
        return items

    def _agents_known(self) -> None:
        """The agents' users, to tell an agent from someone with its name. When the hourly
        lookup fails, the users looked up before are kept; with none, nobody is taken for
        those agents (a read reports it)."""
        before = dict(self._users)
        self._expire_lookups()
        try:
            self._look_up_users([a.assignee for a in self.agents.values() if a.assignee])
        except IssuesError as exc:
            self._users = {**before, **self._users}
            self._report(f"agents' users not looked up ({exc.public_reason})")

    def _interval(self, cost: int) -> timedelta:
        """``refresh``, or longer when reading this often would take more than
        ``READ_SHARE`` of the hour's complexity limit (several large teams)."""
        limit = self.client.complexity_limit
        if limit is None or cost <= 0:
            return self.refresh
        stretched = timedelta(hours=min(cost / (READ_SHARE * limit), 1.0))
        return max(self.refresh, min(stretched, MAX_REFRESH))

    def _report(self, problem: str) -> None:
        self.problem = _join(self.problem, problem)

    def _query(
        self, op: str, document: str, variables: Mapping[str, Any], read: bool = False
    ) -> Any:
        try:
            return self.client.query(op, document, variables, read=read)
        except LinearError as exc:
            reason = (
                "the rest of the hour's Linear limit is kept for writes"
                if exc.kind == "reserve"
                else f"Linear could not be read ({exc.kind})"
            )
            raise IssuesError(str(exc), reason, exc.kind) from exc

    def _lists(self, board: Board, now: datetime) -> list[Any]:
        """A team's lists, whole or not at all: checked once, before the first page (a page
        refused halfway would throw away the pages before it), that the hour has room for
        the reserve kept for writes plus what this team's last whole read cost (nothing
        before its first; the cost at most ``READ_SHARE`` of the hour's limit)."""
        cost, requests = self._team_cost.get(board.key, (0, 0))
        limit = self.client.complexity_limit
        if limit is not None:  # never more than reads may take: a large team is not starved
            cost = min(cost, int(READ_SHARE * limit))
        if not self.client.may_read(cost, requests):
            raise IssuesError(
                "KelpieIssues: reserve",
                "the rest of the hour's Linear limit is kept for writes",
                "reserve",
            )
        spent, made = self.client.complexity_spent, self.client.requests_made
        lists = self._list_pages(board, now)
        self._team_cost[board.key] = (
            self.client.complexity_spent - spent,
            self.client.requests_made - made,
        )
        return lists

    def _list_pages(self, board: Board, now: datetime) -> list[Any]:
        team: dict[str, Any] = {"team": {"key": {"eq": board.key}}}
        if board.label is not None:
            team["labels"] = {"some": {"name": {"eqIgnoreCase": board.label}}}
        open_ = {**team, "state": {"type": {"nin": list(CLOSED_TYPES)}}}
        closed = {
            **team,
            "state": {"type": {"in": list(CLOSED_TYPES)}},
            "updatedAt": {"gt": _iso(now - self.lookback)},
        }
        # Closed issues: the most recently updated page (an old issue closed today counts).
        return self._pages(open_, MAX_PAGES, "createdAt") + self._pages(closed, 1, "updatedAt")

    def _items(self, board: Board, raw: list[Any]) -> list[Item]:
        items: list[Item] = []
        seen: set[str] = set()
        for issue in raw:
            item = self._item(issue, board)
            if item is not None and item.id not in seen:
                seen.add(item.id)
                items.append(item)
        return items

    def _pages(self, filter_: Mapping[str, Any], pages: int, order: str) -> list[Any]:
        out: list[Any] = []
        after: str | None = None
        for _ in range(pages):
            variables = {"filter": filter_, "after": after, "first": PAGE, "order": order}
            data = self._query("KelpieIssues", ISSUES, variables)
            connection = data.get("issues") if isinstance(data, Mapping) else None
            if not isinstance(connection, Mapping):
                raise IssuesError("KelpieIssues: no issues", "Linear answered something else")
            out += _nodes(connection)
            info = connection.get("pageInfo")
            more = isinstance(info, Mapping) and info.get("hasNextPage") is True
            after = info.get("endCursor") if isinstance(info, Mapping) else None
            if not more or not isinstance(after, str):
                return out
        if order == "createdAt":
            log.warning("linear: more than %d pages of issues; the rest are left out", pages)
        return out

    def _item(self, issue: Mapping[str, Any], board: Board, details: bool = True) -> Item | None:
        key = _identifier(issue)
        title = issue.get("title")
        created, updated = _time(issue.get("createdAt")), _time(issue.get("updatedAt"))
        if key is None or not isinstance(title, str) or created is None or updated is None:
            return None
        kind = _state_type(issue)
        state = issue.get("state")
        state_name = _text(state.get("name") if isinstance(state, Mapping) else None, MAX_STATE)
        open_ = kind not in CLOSED_TYPES
        labels = tuple(n for x in _nodes(issue.get("labels")) if (n := _text(x.get("name"))))
        assignee, delegate = self._slots(issue)
        assignees = tuple(n for n in (assignee, delegate) if n)
        creator = issue.get("creator")
        creator = creator if isinstance(creator, Mapping) else {}
        description = issue.get("description")
        description = description if isinstance(description, str) else ""
        member = (
            _name(creator) is not None
            and creator.get("guest") is False
            and creator.get("app") is False
            and issue.get("botActor") is None  # not made by an integration on their behalf
        )
        blocked_by = tuple(
            b
            for r in _nodes(issue.get("inverseRelations"))
            if r.get("type") == "blocks"
            and _state_type(r.get("issue")) not in CLOSED_TYPES
            and (b := _identifier(r.get("issue")))
        )
        children = _nodes(issue.get("children"))
        sub_items = tuple(c for x in children if (c := _identifier(x)))
        open_subs = tuple(
            c for x in children if _state_type(x) not in CLOSED_TYPES and (c := _identifier(x))
        )
        # A description anyone but the creating member may have rewritten is no member's word:
        # it is trusted only while the history shows every edit was a member's.
        edited = member and bool(description.strip()) and updated != created
        detail = None
        if details and ((open_ and (labels or assignees)) or edited):
            detail = self._history(key, updated)
        elif not details:
            detail = self._details.get(key)
        events = _events(detail.nodes, self._holder) if detail is not None and details else None
        trusted = member and (
            not edited
            or (detail is not None and detail.updated_at == updated and detail.description_ok)
        )
        ready_states = {s.casefold() for s in board.ready_states}
        url = issue.get("url")
        return Item(
            id=key,
            board=board.key,
            title="".join(c if c.isprintable() else "?" for c in title[:MAX_TITLE]),
            open=open_,
            created_at=created,
            updated_at=updated,
            closed_reason=CLOSED_TYPES.get(kind),
            closed_at=_time(issue.get("completedAt")) or _time(issue.get("canceledAt")),
            body=description,
            author=_name(creator),
            author_is_bot=creator.get("app") is True,
            author_trusted=trusted,
            labels=labels,
            assignees=assignees,
            events=events,
            blocked_by=blocked_by,
            sub_items=sub_items,
            open_sub_items=open_subs,
            pulls=_pulls(issue, self.pull_repos),
            url=url if isinstance(url, str) else None,
            queued=state_name is not None and state_name.casefold() in ready_states,
            started=kind == "started",
            started_at=_time(issue.get("startedAt")),
            state=state_name,
        )

    def _history(self, key: str, updated: datetime) -> _Detail | None:
        """The issue's history: kept while the issue is unchanged (at most ``DETAIL_TTL``);
        the last one kept when it cannot be read now (whose word on the description holds
        only for the version of the issue it was read for)."""
        now = self.clock()
        kept = self._details.get(key)
        if kept is not None and kept.updated_at == updated and now - kept.read_at < DETAIL_TTL:
            return kept
        if self.client.backing_off():
            return kept
        try:
            data = self.client.query("KelpieHistory", HISTORY, {"id": key}, read=True)
        except LinearError as exc:
            if exc.kind not in ("budget", "reserve", "backoff", "rate_limited"):
                log.warning("linear: cannot read the history of %s: %s", key, exc)
            return kept
        issue = data.get("issue") if isinstance(data, Mapping) else None
        history = issue.get("history") if isinstance(issue, Mapping) else None
        nodes = _nodes(history)
        info = history.get("pageInfo") if isinstance(history, Mapping) else None
        whole = isinstance(info, Mapping) and info.get("hasNextPage") is False
        detail = _Detail(updated, now, nodes, whole and _members_edited(nodes))
        self._details[key] = detail
        return detail

    # --- writing -------------------------------------------------------------------------

    def fetch(self, item_id: str) -> Item:
        """The issue as Linear has it now; its UUID and slots are kept for the writes."""
        team = self._target(item_id)
        self._fetched.pop(item_id, None)
        data = self._lookup("KelpieIssue", ISSUE, {"id": item_id})
        issue = data.get("issue") if isinstance(data, Mapping) else None
        if not isinstance(issue, Mapping):
            raise IssuesError(f"KelpieIssue {item_id}: not an issue", f"{item_id} is not an issue")
        item = self._item(issue, self._board(team), details=False)
        owner, uuid = issue.get("team"), issue.get("id")
        if (
            item is None
            or item.id != item_id
            or not isinstance(owner, Mapping)
            or owner.get("key") != team
            or not isinstance(uuid, str)
            or not _UUID.fullmatch(uuid)
        ):
            raise IssuesError(f"KelpieIssue {item_id}: not an issue", f"{item_id} is not an issue")
        self._fetched[item_id] = _Fetched(uuid, *_slot_ids(issue), open=item.open)
        self._last_fetch = item_id
        return item

    def reserve(self, writes: int, agents: Sequence[Agent] = ()) -> None:
        """Hold the action's writes, then look up every id they may need (the team's blocked
        and agent labels, the agents' users, the state to reopen to), so no lookup can fail
        between two writes. A lookup Linear cannot answer now defers the action."""
        backoff = self.client.backoff_until
        waiting = "backoff" if backoff is not None and self.clock() < backoff else None
        self.gate.reserve(writes, waiting)
        try:
            self._prepare(agents)
        except IssuesError as exc:
            if exc.kind in _WAIT:
                raise Deferred(f"issues: Linear cannot be asked now ({exc.kind})") from exc
            raise

    def _prepare(self, agents: Sequence[Agent]) -> None:
        """Look every id up; an agent the action starts or takes off whose label or user
        Linear lacks refuses the action here, before its first write. When the hourly
        lookup of the agents' users fails, the users looked up before are kept."""
        before = dict(self._users)
        self._expire_lookups()
        item_id = self._last_fetch
        if item_id is None or item_id not in self._fetched:
            return
        team = self._target(item_id)
        board = self._board(team)
        labels = [*board.blocked_labels, *(a.label for a in self.agents.values() if a.label)]
        try:
            self._look_up_labels(team, labels)
            self._look_up_users([a.assignee for a in self.agents.values() if a.assignee])
        except IssuesError:
            self._users = {**before, **self._users}
            raise
        if not self._fetched[item_id].open:  # only a closed issue is reopened
            self._reopen_state(team)
        for agent in agents:
            if agent.label and self._labels.get((team, agent.label.casefold())) is None:
                raise IssuesError(
                    f"{agent.name}: label {agent.label!r} not in {team}",
                    f"Linear team {team} has no label {agent.label!r} ({agent.name}'s)",
                )
            if agent.assignee and self._users.get(agent.assignee.casefold()) is None:
                raise IssuesError(
                    f"{agent.name}: user {agent.assignee!r} not found",
                    f"Linear has no single user {agent.assignee!r} ({agent.name}'s)",
                )

    def _expire_lookups(self) -> None:
        now = self.clock()
        if self._looked_up is None or now - self._looked_up > LOOKUP_TTL:
            self._labels.clear()
            self._users.clear()
            self._states.clear()
            self._looked_up = now

    def _slots(self, issue: Mapping[str, Any]) -> tuple[str | None, str | None]:
        """Who holds the assignee and the delegate slot: their name; ``UNKNOWN`` when the slot
        is held under a name Kelpie does not take (unprintable, ending in ``[bot]``, an
        agent's name on a user that is not that agent's, a delegate that is no app user);
        None when the slot is empty."""
        return self._holder(issue.get("assignee")), self._holder(issue.get("delegate"), True)

    def _holder(self, user: Any, delegate: bool = False) -> str | None:
        if not isinstance(user, Mapping):
            return None
        name = _name(user)
        if name is None or (delegate and user.get("app") is not True):
            return UNKNOWN
        if login_key(name) in self._agent_names():
            known = self._users.get(name.casefold())
            if known is None or user.get("id") != known[0]:
                return UNKNOWN  # the agent's name, not the agent's user
        return name

    def _agent_names(self) -> set[str]:
        return {login_key(a.assignee) for a in self.agents.values() if a.assignee}

    def _agent_ids(self) -> set[str]:
        """The user ids looked up for the agents' assignees."""
        ids = (self._users.get(a.assignee.casefold()) for a in self.agents.values() if a.assignee)
        return {user[0] for user in ids if user is not None}

    def add_labels(self, item_id: str, labels: Sequence[str]) -> None:
        uuid = self._uuid(item_id)
        ids = self._label_ids(self._target(item_id), labels)
        self._update(uuid, item_id, {"addedLabelIds": ids})

    def remove_labels(self, item_id: str, labels: Sequence[str]) -> None:
        """Linear removes exactly these labels (``removedLabelIds``): no read-then-replace.
        A label not found for the team (another team's, a parent team's) refuses the change
        before anything is written: nothing would come off."""
        uuid = self._uuid(item_id)
        ids = self._label_ids(self._target(item_id), labels)
        if ids:
            self._update(uuid, item_id, {"removedLabelIds": ids})

    def assign(self, item_id: str, names: Sequence[str]) -> None:
        """Delegate to an agent (an app user) or assign a person; the issue Linear returns is
        checked. Linear has one assignee and one delegate: one held by anyone but an agent
        (a person, an app that is none of the agents) is refused, not replaced."""
        uuid = self._uuid(item_id)
        held = self._fetched[item_id]
        for name in names:
            user, app = self._user(name)
            on = held.delegate if app else held.assignee
            # One slot each, compared by user id: never replace a person, or an app that is
            # none of the agents.
            if on is not None and on != user and on not in self._agent_ids():
                where = "delegated" if app else "assigned"
                raise IssuesError(
                    f"KelpieUpdate {item_id}: {where} elsewhere",
                    f"{item_id} is {where} to someone else; Kelpie does not replace them "
                    f"with {name}",
                )
            answer = self._update(uuid, item_id, {"delegateId" if app else "assigneeId": user})
            on = (
                answer.get("delegate" if app else "assignee")
                if isinstance(answer, Mapping)
                else None
            )
            if (_name(on) or "").casefold() != name.casefold():
                raise IssuesError(
                    f"KelpieUpdate {item_id}: {name} not on it",
                    f"Linear did not {'delegate to' if app else 'assign'} {name}",
                )

    def unassign(self, item_id: str, names: Sequence[str]) -> None:
        """Clear the slot each named user holds now (the delegate or the assignee, by user
        id), as the action's fetch and its writes since show it."""
        uuid = self._uuid(item_id)
        held = self._fetched[item_id]
        for name in names:
            user, _ = self._user(name)
            if held.delegate == user:
                self._update(uuid, item_id, {"delegateId": None})
            elif held.assignee == user:
                self._update(uuid, item_id, {"assigneeId": None})

    def comment(self, item_id: str, body: str) -> None:
        uuid = self._uuid(item_id)
        self._write(
            "KelpieComment", COMMENT, {"input": {"issueId": uuid, "body": body[:MAX_COMMENT]}}
        )

    def reopen(self, item_id: str) -> None:
        uuid = self._uuid(item_id)
        self._update(uuid, item_id, {"stateId": self._reopen_state(self._target(item_id))})

    def _target(self, item_id: str) -> str:
        match = _ID.fullmatch(item_id)
        if match is None or match["team"] not in {b.key for b in self.boards}:
            raise IssuesError(
                f"{item_id!r} is not an issue of a configured team",
                "not an issue of a configured team",
            )
        return match["team"]

    def _board(self, team: str) -> Board:
        return next(b for b in self.boards if b.key == team)

    def _uuid(self, item_id: str) -> str:
        """The UUID of the issue an action checked (writes go to it, not to an identifier that
        could name another issue by then); fetched first if no action has."""
        self._target(item_id)
        if item_id not in self._fetched:
            self.fetch(item_id)
        return self._fetched[item_id].uuid

    def _update(self, uuid: str, item_id: str, change: Mapping[str, Any]) -> Any:
        data = self._write("KelpieUpdate", UPDATE, {"id": uuid, "input": dict(change)})
        payload = data.get("issueUpdate") if isinstance(data, Mapping) else None
        if not isinstance(payload, Mapping) or payload.get("success") is not True:
            raise IssuesError(f"KelpieUpdate {item_id}: not done", "Linear refused the change")
        issue = payload.get("issue")
        if isinstance(issue, Mapping) and item_id in self._fetched:
            held = self._fetched[item_id]
            held.assignee, held.delegate = _slot_ids(issue)
        return issue

    def _lookup(self, op: str, document: str, variables: Mapping[str, Any]) -> Any:
        """A read an action makes: outside the reads' request budget and reserve."""
        saved, self.client.requests_left = self.client.requests_left, None
        try:
            return self._query(op, document, variables)
        finally:
            self.client.requests_left = saved

    def _write(self, op: str, document: str, variables: Mapping[str, Any]) -> Any:
        first = self.gate.begin()
        saved, self.client.requests_left = self.client.requests_left, None  # outside reads' budget
        try:
            return self.client.query(op, document, variables)
        except LinearError as exc:
            if first and exc.kind in ("backoff", "rate_limited"):
                self.gate.defer_rest()
                raise Deferred(f"issues: Linear asks Kelpie to wait ({exc.kind})") from exc
            raise IssuesError(
                str(exc), f"Linear refused the change ({exc.kind})", exc.kind
            ) from exc
        finally:
            self.client.requests_left = saved
            self.gate.done()
            self._stale = True

    def _look_up_labels(self, team: str, names: Sequence[str]) -> None:
        missing = sorted({n.casefold() for n in names} - {k for t, k in self._labels if t == team})
        for start in range(0, len(missing), 20):
            chunk = missing[start : start + 20]
            filter_ = {
                "and": [
                    {"or": [{"name": {"eqIgnoreCase": n}} for n in chunk]},
                    {"or": [{"team": {"key": {"eq": team}}}, {"team": {"null": True}}]},
                ]
            }
            data = self._lookup("KelpieLabels", LABELS, {"filter": filter_})
            found: dict[str, str] = {}
            for label in _nodes(data.get("issueLabels") if isinstance(data, Mapping) else None):
                name, ident = _text(label.get("name")), label.get("id")
                owner = label.get("team")
                key = owner.get("key") if isinstance(owner, Mapping) else None
                if name is None or not isinstance(ident, str) or key not in (None, team):
                    continue
                # A team's own label wins over a workspace label of the same name.
                if key == team or name.casefold() not in found:
                    found[name.casefold()] = ident
            for name in chunk:
                self._labels[(team, name)] = found.get(name)

    def _label_ids(self, team: str, names: Sequence[str]) -> list[str]:
        self._look_up_labels(team, names)
        ids = [self._labels.get((team, n.casefold())) for n in names]
        if None in ids:
            unknown = [n for n, i in zip(names, ids, strict=True) if i is None]
            raise IssuesError(f"labels {unknown} not found", f"Linear has no label {unknown[0]!r}")
        return [i for i in ids if i is not None]

    def _look_up_users(self, names: Sequence[str]) -> None:
        missing = sorted({n.casefold() for n in names} - set(self._users))
        for start in range(0, len(missing), 20):
            chunk = missing[start : start + 20]
            filter_ = {"or": [{"displayName": {"eqIgnoreCase": n}} for n in chunk]}
            data = self._lookup("KelpieUsers", USERS, {"filter": filter_})
            found: dict[str, list[tuple[str, bool]]] = {}
            for user in _nodes(data.get("users") if isinstance(data, Mapping) else None):
                name, ident = _name(user), user.get("id")
                if name is not None and isinstance(ident, str):
                    found.setdefault(name.casefold(), []).append((ident, user.get("app") is True))
            for name in chunk:
                matches = found.get(name, [])
                self._users[name] = matches[0] if len(matches) == 1 else None

    def _user(self, name: str) -> tuple[str, bool]:
        self._look_up_users([name])
        user = self._users.get(name.casefold())
        if user is None:
            raise IssuesError(
                f"user {name!r}: not one found", f"Linear has no single user {name!r}"
            )
        return user

    def _reopen_state(self, team: str) -> str:
        if team not in self._states:
            data = self._lookup("KelpieStates", STATES, {"team": team})
            teams = _nodes((data or {}).get("teams") if isinstance(data, Mapping) else None)
            states = _nodes(teams[0].get("states")) if teams else []
            for kind in ("unstarted", "backlog"):
                fitting = [
                    s for s in states if s.get("type") == kind and isinstance(s.get("id"), str)
                ]
                if fitting:
                    fitting.sort(
                        key=lambda s: (
                            s.get("position") if isinstance(s.get("position"), (int, float)) else 0
                        )
                    )
                    self._states[team] = fitting[0]["id"]
                    break
            else:
                raise IssuesError(
                    f"team {team}: no open state", f"Linear team {team} has no open state"
                )
        return self._states[team]

    # --- checks (kelpie doctor) ----------------------------------------------------------

    def missing_teams(self) -> list[str]:
        """The configured teams Linear does not know (a misspelled key reads no issues)."""
        keys = [b.key for b in self.boards]
        data = self._lookup("KelpieTeams", TEAMS, {"keys": keys})
        found = {
            t.get("key") for t in _nodes(data.get("teams") if isinstance(data, Mapping) else None)
        }
        return [k for k in keys if k not in found]

    def missing_states(self) -> list[str]:
        """``ready_states`` a team does not have (as ``TEAM/state``): a misspelled one leaves
        nothing ready."""
        missing: list[str] = []
        for board in self.boards:
            if not board.ready_states:
                continue
            data = self._lookup("KelpieStates", STATES, {"team": board.key})
            teams = _nodes(data.get("teams") if isinstance(data, Mapping) else None)
            states = _nodes(teams[0].get("states")) if teams else []
            names = {n.casefold() for s in states if (n := _text(s.get("name"), MAX_STATE))}
            missing += [f"{board.key}/{s}" for s in board.ready_states if s.casefold() not in names]
        return missing

    def missing_names(self) -> tuple[list[str], list[str]]:
        """Labels the config names that a team lacks (as ``TEAM/label``), and agents' users
        that are not exactly one Linear user."""
        labels: list[str] = []
        for board in self.boards:
            names = [
                *([board.label] if board.label else []),
                *board.blocked_labels,
                *board.ready_labels,
                *(a.label for a in self.agents.values() if a.label),
            ]
            self._look_up_labels(board.key, names)
            labels += [
                f"{board.key}/{n}"
                for n in names
                if self._labels.get((board.key, n.casefold())) is None
            ]
        users = [a.assignee for a in self.agents.values() if a.assignee]
        self._look_up_users(users)
        return labels, [u for u in users if self._users.get(u.casefold()) is None]


def _events(
    nodes: list[Mapping[str, Any]], holder: Callable[[Any, bool], str | None]
) -> tuple[Event, ...]:
    """A history as events. Who was assigned or delegated to is named as the issue's slots
    name them (``holder``): an agent only on the user looked up for it."""
    events: list[Event] = []
    for entry in nodes:
        at = _time(entry.get("createdAt"))
        if at is None:
            continue
        for who, kind, delegate in (
            ("toAssignee", "assigned", False),
            ("fromAssignee", "unassigned", False),
            ("toDelegate", "assigned", True),
            ("fromDelegate", "unassigned", True),
        ):
            if (name := holder(entry.get(who), delegate)) is not None:
                events.append(Event(kind, at, name))
        for key, kind in (("addedLabels", "labeled"), ("removedLabels", "unlabeled")):
            labels = entry.get(key)
            for label in labels if isinstance(labels, list) else []:
                name = _text(label.get("name")) if isinstance(label, Mapping) else None
                if name is not None:
                    events.append(Event(kind, at, name))
        before = _state_type({"state": entry.get("fromState")})
        after = _state_type({"state": entry.get("toState")})
        if after in CLOSED_TYPES:
            events.append(Event("closed", at))
        elif before in CLOSED_TYPES and after:
            events.append(Event("reopened", at))
    return tuple(sorted(events, key=lambda e: e.at))


def _pulls(issue: Mapping[str, Any], repos: frozenset[str]) -> tuple[LinkedPull, ...]:
    """Open GitHub pull requests attached by Linear's GitHub integration: ``sourceType`` is
    ``github`` (set by Linear, not by whoever attaches a link), on github.com or a repository
    of ``repos``. Anyone can attach a link that looks like a pull request; it does not count.
    The branch is not taken from the attachment's metadata, which apps can write
    (**unverified**: the metadata keys ``status`` and ``draft``)."""
    pulls: list[LinkedPull] = []
    for attachment in _nodes(issue.get("attachments")):
        url = attachment.get("url")
        match = _PULL.fullmatch(url) if isinstance(url, str) else None
        if match is None or attachment.get("sourceType") != "github":
            continue
        host, repo = match["host"].lower(), match["repo"].lower()
        allowed = f"{host}/{repo}" in repos if repos else host == GITHUB_HOST
        if not allowed:
            continue
        meta = attachment.get("metadata")
        meta = meta if isinstance(meta, Mapping) else {}
        status = meta.get("status")
        if status in ("merged", "closed"):
            continue
        pulls.append(
            LinkedPull(
                number=int(match["number"]),
                branch=None,
                draft=meta.get("draft") is True or status == "draft",
                created_at=_time(attachment.get("createdAt")),
            )
        )
    return tuple(sorted(pulls, key=lambda p: p.number))


def _slot_ids(issue: Mapping[str, Any]) -> tuple[str | None, str | None]:
    """The ids of the users in the assignee and delegate slots; ``""`` for a slot held by a
    user Linear gave no id for, None for an empty one."""

    def held(user: Any) -> str | None:
        if not isinstance(user, Mapping):
            return None
        ident = user.get("id")
        return ident if isinstance(ident, str) and _UUID.fullmatch(ident) else ""

    return held(issue.get("assignee")), held(issue.get("delegate"))


def _members_edited(nodes: list[Mapping[str, Any]]) -> bool:
    """Whether every description edit in a history was a member's: an edit by a guest, an
    app (the agents among them), an actor Linear does not name or one made through an
    integration (``botActor``: a sync, an automation) is not."""
    for entry in nodes:
        if entry.get("updatedDescription") is not True:
            continue
        actor = entry.get("actor")
        if entry.get("botActor") is not None:  # through an integration (a sync), not a person
            return False
        if not isinstance(actor, Mapping) or actor.get("app") is not False:
            return False
        if actor.get("guest") is not False:
            return False
    return True


def _join(problem: str | None, more: str) -> str:
    return more if problem is None else f"{problem}; {more}"
