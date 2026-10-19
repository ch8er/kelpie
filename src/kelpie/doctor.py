"""``kelpie doctor``: is a first live run on a Hermes or Claude Code farm set up correctly?

One line per check, ``PASS|WARN|FAIL|SKIP  <check>: <detail>``, and a ``  fix: ...`` line
under a FAIL or a WARN (a WARN does not fail the run). Nothing is written anywhere: the
kanban database and the span store are opened read-only. This module may know about
adapters and the app wiring; ``kelpie.core`` does not.
"""

from __future__ import annotations

import os
import re
import sqlite3
import time
import urllib.request
from collections import Counter
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

from kelpie.adapters.claude_code import doctor as claude_code
from kelpie.adapters.hermes.adapter import HermesAdapter, HermesError, format_age
from kelpie.adapters.hermes.git_state import read_git_state
from kelpie.adapters.openclaw import doctor as openclaw
from kelpie.app import (
    UNPRICED_WINDOW,
    ConfigError,
    LoadedRules,
    Settings,
    load_rules,
    load_settings,
    read_unpriced,
    utc_now,
)
from kelpie.connectors.github.blocks import MAX_REASON
from kelpie.connectors.github.client import GitHubError
from kelpie.connectors.github.connector import (
    ACTIONS,
    CHECK_RUNS,
    PERMISSIONS,
    PULLS,
    STATUSES,
    CiState,
    Forbidden,
    GitHubConnector,
    parse_pr_ref,
    web_host,
)
from kelpie.core.config import Mode
from kelpie.core.model import Status
from kelpie.resources.prometheus import PrometheusClient, QueryError

FRESH = timedelta(hours=1)  # newest spool line or span must be younger than this
MAX_WORKTREES = 20  # git is asked about at most this many workspaces
GIT_DEADLINE = 15.0  # seconds for all git calls together
DEFAULT_PROMETHEUS = "http://localhost:9090"
PROMETHEUS_ENV = "KELPIE_PROMETHEUS_URL"
# How agents name a pull request in a block reason. A bare "#12" only next to PR or merge
# wording: "issue #45", "attempt #3" or "build #1234" name no PR.
PR_REFS = (
    re.compile(r"\bPR\b[\s#:-]*\d+", re.IGNORECASE),
    re.compile(r"\bpull[\s_-]*request\s*#?\s*\d+", re.IGNORECASE),
    re.compile(r"\bpulls?/\d+", re.IGNORECASE),
    re.compile(r"\b[\w.-]+/[\w.-]+#\d+"),
    re.compile(
        r"\b(?:merge[sd]?|merging|automerge|review|CI)\b[^#\n]{0,40}(?<![\w&])#\d+",
        re.IGNORECASE,
    ),
)
SHOWN = 5  # task ids named in a detail
MAX_GITHUB_REPOS = 20  # GitHub is asked about at most this many repositories
CI_SOURCES = {STATUSES: "statuses", CHECK_RUNS: "check-runs", ACTIONS: "Actions runs"}


@dataclass(frozen=True, slots=True)
class Check:
    status: str  # PASS | WARN | FAIL | SKIP
    name: str
    detail: str
    fix: str = ""

    def render(self) -> str:
        line = f"{self.status}  {self.name}: {self.detail}"
        shown = self.status in ("FAIL", "WARN") and self.fix
        return f"{line}\n  fix: {self.fix}" if shown else line


def _pass(name: str, detail: str) -> Check:
    return Check("PASS", name, detail)


def _skip(name: str, detail: str) -> Check:
    return Check("SKIP", name, detail)


def _fail(name: str, detail: str, fix: str) -> Check:
    return Check("FAIL", name, detail, fix)


def _warn(name: str, detail: str, fix: str) -> Check:
    return Check("WARN", name, detail, fix)


def _quiet_or_fail(name: str, detail: str, fix: str, running: int | None) -> Check:
    """A signal is missing: fine on an idle farm, a failure while tasks are running."""
    if running == 0:
        return _skip(name, f"{detail}; no task is running, start one and run doctor again")
    return _fail(name, detail, fix)


def check_dry_run(settings: Settings, loaded: LoadedRules) -> Check:
    name = "observe only"
    acting = sorted(s.name for s in loaded.statuses if s.implemented and s.mode is not Mode.OBSERVE)
    problems = []
    if not settings.engine.dry_run:
        problems.append("dry_run is off")
    if acting:
        problems.append(f"rules not in observe: {', '.join(acting)}")
    if problems:
        return _fail(
            name,
            "; ".join(problems),
            'in [engine] set dry_run = true and default_mode = "observe", and remove '
            "mode = ... from [engine.rules.*]",
        )
    return _pass(name, "dry_run on, every rule in observe")


def check_board(adapter: HermesAdapter) -> tuple[Check, int | None]:
    """The check, and the number of running tasks (None when the board cannot be read)."""
    name = "kanban.db"
    fix = f"set db_path in [adapters.hermes] to the board's kanban.db (now {adapter.db_path})"
    try:
        total, running = adapter.board_counts()
    except (HermesError, sqlite3.Error) as exc:
        return _fail(name, f"not readable: {exc}", fix), None
    detail = f"readable, {total} tasks, {running} running ({adapter.db_path})"
    return _pass(name, detail), running


def check_hooks(adapter: HermesAdapter, now: datetime, running: int | None) -> Check:
    name = "hooks fire"
    spool = adapter.spool
    if spool is None:
        return _skip(name, "spool disabled")
    fix = (
        "install and enable the Kelpie plugin on the Hermes host (docs/quickstart-hermes.md), "
        "restart the dispatcher and run a task"
    )
    if not spool.exists():
        return _quiet_or_fail(name, f"spool missing ({spool})", fix, running)
    last = adapter.last_spool_at()
    if last is None:
        return _quiet_or_fail(name, f"spool has no readable lines ({spool})", fix, running)
    age = now - last
    if age > FRESH:
        detail = f"newest spool line is {format_age(age)} old ({spool})"
        return _quiet_or_fail(name, detail, fix, running)
    return _pass(name, f"newest spool line {format_age(age)} old ({spool})")


def _newest_span(path: Path) -> str | None:
    """``last_at`` of the newest stored span, None if none ever arrived; read-only."""
    if not path.is_file():
        return None
    conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)
    try:
        return conn.execute("SELECT MAX(last_at) FROM spans").fetchone()[0]
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc):
            return None
        raise
    finally:
        conn.close()


def check_spans(
    settings: Settings,
    now: datetime,
    running: int | None,
    exporter: str = "the OTLP backend of hermes-otel",
) -> Check:
    name = "OTLP spans arrive"
    if not settings.traces.enabled:
        return _skip(name, "[traces] disabled")
    path = settings.db_path
    if path is None:
        return _skip(name, "storage is in memory; nothing to read from outside")
    fix = (
        f"check the exporter ({exporter}): endpoint http://<kelpie>:9464/v1/traces, "
        f"Authorization bearer token = ${settings.traces.token_env}; run doctor with the same "
        "KELPIE_DB as kelpie serve"
    )
    try:
        newest = _newest_span(path)
    except sqlite3.Error as exc:
        return _fail(name, f"span store {path} not readable: {exc}", fix)
    if newest is None:
        return _skip(name, f"no span has arrived in {path}; OTLP is optional (quickstart step 4)")
    age = now - datetime.fromisoformat(newest)
    if age > FRESH:  # spans used to arrive and stopped
        return _quiet_or_fail(name, f"newest span is {format_age(age)} old ({path})", fix, running)
    return _pass(name, f"newest span {format_age(age)} old ({path})")


def check_prices(settings: Settings, now: datetime) -> Check:
    """Every model the span store saw lately has a price (a model without one costs $0)."""
    name = "models have prices"
    if not settings.traces.enabled:
        return _skip(name, "[traces] disabled")
    if settings.db_path is None:
        return _skip(name, "storage is in memory; nothing to read from outside")
    found = read_unpriced(settings, now)
    if isinstance(found, str):
        return _skip(name, found)
    window = format_age(min(UNPRICED_WINDOW, settings.traces.retention))
    if not found:
        return _pass(name, f"no model without a price in the last {window}")
    named = _named([f"{m} ({t:,} tokens)" for m, t in found])
    first = found[0][0]
    return _warn(
        name,
        f"spend on these models in the last {window} counts $0: {named}",
        f'add [prices."{first}"] input = <USD/1M>, output = <USD/1M> (cached_input optional)',
    )


def _named(ids: list[str]) -> str:
    shown = ", ".join(ids[:SHOWN])
    return shown + (f" and {len(ids) - SHOWN} more" if len(ids) > SHOWN else "")


def check_pr_blocks(settings: Settings, adapter: HermesAdapter, now: datetime) -> Check:
    """Blocks whose reason names a PR wait on that PR, but only ``[[block_conditions]]`` and
    the GitHub connector turn them into conditions; without them every such block looks like
    a stuck task or a question for a person (on a live farm, most of the noise of a 24 h
    review). A reason counts when it names a PR the way agents write it (``PR #12``,
    ``owner/repo#12``, a ``/pull/12`` link, ``#12`` next to PR or merge wording); ``guard:``
    blocks (the platform's own, with error text) do not. Each is then resolved as ``kelpie
    serve`` does: a block pattern must match, its ``pr`` group must name a PR of a repository
    in ``[connectors.github] repos``. Links other connectors add (human tasks) are not
    considered."""
    name = "PR blocks have conditions"
    try:
        # The board only: no spool, no git (worktrees readable already asked git).
        tasks = replace(adapter, git_state=False, spool=None).snapshot(now).tasks
    except (HermesError, sqlite3.Error) as exc:
        return _skip(name, f"board not readable: {exc}")
    blocks = {
        t.id: t.block.reason
        for t in tasks
        if t.status is Status.BLOCKED
        and t.block is not None
        and not t.block.reason.lower().startswith("guard:")
        and _names_a_pr(t.block.reason)
    }
    named = sorted(blocks, key=_natural)
    if not named:
        return _pass(name, "no block reason on the board names a PR")
    found = f"{len(named)} block reason(s) name a PR ({_named(named)})"
    github = settings.github
    if github is None:
        return _warn(
            name,
            f"{found}, but [connectors.github] is not configured",
            "add [connectors.github] with the repos agents push to, and [[block_conditions]] "
            "patterns for these reasons, so rules 2 and 4 see the PR they wait on",
        )
    if not github.enabled:
        return _warn(
            name,
            f"{found}, but [connectors.github] is disabled",
            "set enabled = true in [connectors.github]",
        )
    patterns = [p for p in settings.block_conditions if "block" in p.applies_to]
    if not patterns:
        return _warn(
            name,
            f"{found}, but no [[block_conditions]] pattern reads block reasons",
            "add [[block_conditions]] patterns (with a named group pr) for these reasons",
        )
    host = web_host(github.api_url)
    repos = {r.lower() for r in github.repos}
    unmatched, unresolved = [], []
    for tid in named:
        reason = blocks[tid][:MAX_REASON]
        refs = [
            parse_pr_ref(m["pr"], p.repo, host)
            for p in patterns
            if (m := p.pattern.search(reason)) and m["pr"]
        ]
        if not refs:
            unmatched.append(tid)
            continue
        if not any(ref is not None and ref.repo in repos for ref in refs):
            unresolved.append(tid)
    problems = []
    if unmatched:
        problems.append(
            f"{len(unmatched)} match no [[block_conditions]] pattern ({_named(unmatched)})"
        )
    if unresolved:
        problems.append(
            f"{len(unresolved)} match but name no PR of [connectors.github] repos (a bare #N "
            f"with no repo = on the pattern, another repository or host) ({_named(unresolved)})"
        )
    if problems:
        return _warn(
            name,
            f"{found}; " + "; ".join(problems),
            "widen the [[block_conditions]] patterns to the wording these blocks use (only "
            "waiting wording: a question that names a PR is still a question), set repo = "
            "for bare #N, and list every repository agents push to",
        )
    return _pass(name, f"{found}; every one names a PR [[block_conditions]] can check")


def _names_a_pr(reason: str) -> bool:
    return any(p.search(reason) for p in PR_REFS)


def _natural(text: str) -> list[object]:
    return [int(part) if part.isdigit() else part for part in re.split(r"(\d+)", text)]


def check_worktrees(adapter: HermesAdapter) -> Check:
    name = "worktrees readable"
    if not adapter.git_state:
        return _skip(name, "git_state = false")
    try:
        paths = adapter.live_workspaces()[:MAX_WORKTREES]
    except (HermesError, sqlite3.Error) as exc:
        return _skip(name, f"board not readable: {exc}")
    if not paths:
        return _skip(name, "no live task has a worktree workspace on this host")
    timeout = adapter.git_timeout.total_seconds()
    deadline = time.monotonic() + GIT_DEADLINE
    problems: Counter[str] = Counter()
    for path in paths:
        state = read_git_state(path, timeout, deadline=deadline)
        if not (path / ".git").exists():
            problems["no .git in the workspace"] += 1
        elif state.problem is not None:
            problems[str(state.problem)] += 1
    failed = sum(problems.values())
    summary = f"{len(paths) - failed} ok, {failed} failed"
    if not failed:
        return _pass(name, summary)
    return _fail(
        name,
        f"{summary} (most common: {problems.most_common(1)[0][0]})",
        "install git where Kelpie runs (docker: derive an image with git, run with init: true) "
        "and run Kelpie as the user that owns the workspaces; or set git_state = false",
    )


def check_resources_watched(
    settings: Settings, loaded: LoadedRules, environ: Mapping[str, str] = os.environ
) -> Check:
    """Rule 7 sees disks only through ``[resources.prometheus]`` (node_exporter): without it
    a disk fills unseen (incident 19: 94 % full, growing 9 GB a day, and nothing was told).
    It asks that Prometheus once which configured hosts report disks and directory sizes."""
    name = "disk watched"
    if not any(r.name == "resources" for r in loaded.rules):
        return _skip(name, "rule 7 (resources) is not loaded")
    prometheus = settings.resources.prometheus
    if prometheus is None or not prometheus.hosts:
        return _fail(
            name,
            "disk is not watched: rule 7 has no resource source",
            "install node_exporter on every agent host and Prometheus, then set "
            "[resources.prometheus] url and hosts (docs/install.md, Host resources)",
        )
    where = f"[resources.prometheus] {prometheus.display_url}"
    try:
        source = prometheus.build(environ=environ)
    except ValueError as exc:
        return _fail(name, str(exc), "set the variable, or drop token_env")
    config = source.config
    client = PrometheusClient(config.url, min(config.timeout, 5), config.token)
    label = config.host_label
    try:  # the queries rule 7 runs, custom ones included
        # On lines of their own: a "#" comment in a custom query ends at its line.
        disks = _labels(client, f"count by ({label}) (\n{config.query_text('disk_size')}\n)", label)
        paths = _labels(
            client, f"count by ({label}) (\n{config.query_text('path_bytes')}\n)", label
        )
    except QueryError as exc:
        if exc.cause == "http_auth":
            refused = "refused the token" if config.token else "requires a token"
            return _fail(
                name,
                f"{where} {refused} ({exc.detail}): rule 7 reads nothing",
                f"{'check' if config.token else 'set'} ${prometheus.token_env}",
            )
        return _warn(
            name,
            f"{where} did not answer ({exc.cause}: {exc.detail}): cannot tell whether "
            "node_exporter reports the disks",
            "run doctor where Kelpie runs, or check the url and the Prometheus server",
        )
    blind = sorted(h for h, value in prometheus.hosts.items() if value not in disks)
    if blind:
        return _fail(
            name,
            f"{where} has no disk readings for {', '.join(blind)}",
            "install node_exporter on those hosts and add them to Prometheus's scrape "
            f"targets, with {label} as in [resources.prometheus] hosts (docs/install.md)",
        )
    hosts = ", ".join(sorted(prometheus.hosts))
    unsized = sorted(h for h, value in prometheus.hosts.items() if value not in paths)
    if unsized:
        return _warn(
            name,
            f"{where} reads the disks of {hosts}; no directory sizes from "
            f"{', '.join(unsized)}, so a filling disk's alert cannot say what grows",
            "run scripts/kelpie-path-sizes.sh there (deploy/textfile, docs/install.md)",
        )
    if prometheus.sends_token_in_clear(environ):
        return _warn(
            name,
            f"{where} reads the disks and directory sizes of {hosts}, but the token goes "
            "over plain http",
            "use https, or keep Prometheus on loopback",
        )
    return _pass(name, f"{where} reads the disks and directory sizes of {hosts}")


# kelpie_processes.prom: the macOS process count rule 7 reads (scripts/kelpie-process-limit.sh).
_PROCESS_FILE = 'node_textfile_mtime_seconds{file=~"(.*/)?kelpie_processes[.]prom"}'


def check_process_counts(
    settings: Settings, loaded: LoadedRules, environ: Mapping[str, str] = os.environ
) -> Check:
    """On macOS rule 7 counts processes from a textfile the shipped script rewrites every
    minute, and ignores it once it is 5 minutes old: a script that stopped (a LaunchAgent
    ends at logout) leaves the process limit unwatched, and the reading just disappears."""
    name = "process counts fresh"
    prometheus = settings.resources.prometheus
    if not any(r.name == "resources" for r in loaded.rules) or prometheus is None:
        return _skip(name, "rule 7 reads no host resources")
    try:
        config = prometheus.build(environ=environ).config
    except ValueError as exc:
        return _skip(name, str(exc))
    client = PrometheusClient(config.url, min(config.timeout, 5), config.token)
    label = config.host_label
    try:
        darwin = 'node_uname_info{sysname="Darwin"}'
        macs = _labels(client, f"count by ({label}) ({darwin})", label)
        hosts = {value: host for host, value in prometheus.hosts.items() if value in macs}
        if not hosts:  # most farms: ask nothing more
            return _skip(name, "no macOS host (node_uname_info) among [resources.prometheus] hosts")
        counted = _labels(
            client, f"count by ({label}) (\n{config.query_text('processes')}\n)", label
        )
        ages = {
            s.labels.get(label, ""): s.value
            for s in client.query(f"max by ({label}) (time() - {_PROCESS_FILE})")
        }
    except QueryError as exc:
        return _skip(name, f"Prometheus did not answer ({exc.cause})")
    blind = sorted(hosts[v] for v in hosts if v not in counted)
    if not blind:
        return _pass(name, f"fresh process counts from {', '.join(sorted(hosts.values()))}")
    by_host = {host: value for value, host in hosts.items()}
    custom = "processes" in config.queries

    def why(host: str) -> str:
        age = ages.get(by_host[host])
        if age is None:
            return f"{host} (no kelpie_processes.prom in node_exporter's textfile collector)"
        written = f"last written {format_age(timedelta(seconds=min(max(age, 0), 10**9)))} ago"
        if age < 300 and custom:
            return f"{host} ({written}, but the custom processes query returns nothing for it)"
        return f"{host} ({written})"

    fix = (
        "check [resources.prometheus.queries] processes: it must return the host's count"
        if custom and all(ages.get(by_host[h], 10**9) < 300 for h in blind)
        else "load deploy/textfile/com.kelpie.process-limit.plist as the agent user (a "
        "LaunchDaemon with UserName on a headless farm), and keep the host's clock in sync "
        "with Prometheus's (docs/install.md, Host resources)"
    )
    return _warn(
        name,
        f"no fresh process count from {', '.join(why(h) for h in blind)}: rule 7 cannot "
        "see them near kern.maxprocperuid",
        fix,
    )


def _labels(client: PrometheusClient, promql: str, label: str) -> set[str]:
    return {s.labels.get(label, "") for s in client.query(promql)}


def check_prometheus(url: str | None, environ: Mapping[str, str]) -> Check:
    name = "Prometheus reachable"
    explicit = url or environ.get(PROMETHEUS_ENV)
    base = (explicit or DEFAULT_PROMETHEUS).rstrip("/")
    fix = "start Prometheus (docker compose up prometheus) or pass the right --prometheus URL"
    if urlsplit(base).scheme not in ("http", "https"):
        return _fail(name, f"{base}: only http and https URLs are supported", fix)
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a proxy
    try:
        with opener.open(f"{base}/-/ready", timeout=3) as response:
            problem = "" if response.status == 200 else f"HTTP {response.status}"
    except (OSError, ValueError) as exc:
        problem = str(getattr(exc, "reason", exc))
    if not problem:
        return _pass(name, f"{base}/-/ready")
    if explicit is None:
        return _skip(name, f"nothing at {base}; pass --prometheus URL or set ${PROMETHEUS_ENV}")
    return _fail(name, f"{base}/-/ready: {problem}", fix)


def check_github(settings: Settings, environ: Mapping[str, str]) -> list[Check]:
    """Per repository: which source CI is read from (check-runs and statuses, or the
    commit's Actions runs where those are refused) and the permission a 403 names. Probes
    with the head of the newest pull request or workflow run. Empty without
    ``[connectors.github]``."""
    github = settings.github
    if github is None:
        return []
    if not github.enabled:
        return [_skip("github ci", "[connectors.github] is disabled")]
    try:
        token = github.token(environ)
    except ValueError as exc:
        return [
            _fail("github token", str(exc), f"export ${github.token_env} before running doctor")
        ]
    connector = github.build(token, utc_now)
    repos = sorted(connector.repos)
    checks: list[Check] = []
    rejected = False
    for repo in repos[:MAX_GITHUB_REPOS]:
        if rejected:
            checks.append(_skip(f"github ci {repo}", "not asked: the token was rejected above"))
            continue
        connector.client.begin_tick(github.max_requests_per_tick, github.tick_budget)
        found, rejected = _github_repo(connector, repo, github.token_env)
        checks += found
    if len(repos) > MAX_GITHUB_REPOS:
        checks.append(_skip("github ci", f"{len(repos) - MAX_GITHUB_REPOS} more repos not asked"))
    return checks


def _github_repo(connector: GitHubConnector, repo: str, token_env: str) -> tuple[list[Check], bool]:
    """The checks for one repository, and whether GitHub rejected the token."""
    name, pulls = f"github ci {repo}", f"github pull requests {repo}"
    found: list[Check] = []
    try:
        sample = connector.head_commit(repo)
        if sample.pulls_refused:  # check() reads the PR first: no PR, no CI condition
            found.append(_github_problem(pulls, repo, "forbidden", PULLS, token_env))
        if sample.runs_refused:
            found.append(_github_problem(name, repo, "forbidden", PERMISSIONS[ACTIONS], token_env))
        elif sample.sha is None:
            found.append(_github_problem(name, repo, "no_sample", "", token_env))
        else:
            found.append(
                _ci_check(name, repo, connector.probe_ci(repo, sample.sha, sample.pr), connector)
            )
    except GitHubError as exc:
        needs = exc.permission if isinstance(exc, Forbidden) else ""
        problem = _github_problem(name, repo, exc.kind, needs, token_env)
        if (
            isinstance(exc, Forbidden)
            and exc.permission == PERMISSIONS[ACTIONS]
            and connector.workflows_for(repo)
        ):
            problem = replace(problem, detail=f"{problem.detail} (ci_workflows needs it)")
        found.append(problem)
        return found, exc.kind == "unauthorized"
    if connector.workflows_for(repo) and not sample.runs_refused:
        entries = f"github ci_workflows {repo}"
        try:
            found.append(_workflow_entries(connector, repo, entries))
        except GitHubError as exc:  # its own line: the CI line above is not a duplicate
            needs = exc.permission if isinstance(exc, Forbidden) else ""
            found.append(_github_problem(entries, repo, exc.kind, needs, token_env))
            return found, exc.kind == "unauthorized"
    return found, False


def _workflow_entries(connector: GitHubConnector, repo: str, name: str) -> Check:
    """Every ``ci_workflows`` entry of ``repo`` must name a workflow, by name or by path; a
    name that several workflow files share is ambiguous (the path form is exact)."""
    entries = connector.workflows_for(repo)
    found, complete = connector.workflow_list(repo, fresh=True)
    paths = {w.path for w in found}
    unmatched = [e for e in entries if not any(e in (w.name, w.path) for w in found)]
    disabled = [
        e
        for e in entries
        if e not in unmatched
        and not any(e in (w.name, w.path) and w.state == "active" for w in found)
    ]
    shared = {
        e: sorted(w.path for w in found if w.name == e)
        for e in entries
        if e not in paths and sum(w.name == e for w in found) > 1
    }
    if unmatched:
        shown = ", ".join(repr(e) for e in unmatched)
        fix = (
            "write each entry as a workflow's name (its `name:`) or its file path, exactly; "
            "a listed workflow with no run keeps every condition pending"
        )
        if not complete:  # only the first 100 workflows were read
            return Check("WARN", name, f"{shown} not among the first 100 workflows", fix)
        return _fail(name, f"matches no workflow: {shown}", fix)
    if disabled:  # a disabled workflow never runs: the condition would wait for ever
        shown = ", ".join(repr(e) for e in disabled)
        fix = "enable the workflow, or remove the entry from ci_workflows"
        if not complete:  # a name may match more workflows beyond the first 100
            return Check("WARN", name, f"only disabled workflows match: {shown} (first 100)", fix)
        return _fail(name, f"only disabled workflows match: {shown}", fix)
    if shared:
        text = "; ".join(f"{e!r} is {', '.join(p)}" for e, p in shared.items())
        more = "; only the first 100 workflows were read, there may be more" if not complete else ""
        return Check(
            "WARN",
            name,
            f"a name is shared by several workflows ({text}){more}",
            "list the path instead",
        )
    return _pass(name, f"{len(entries)} entries, each matching a workflow")


def _ci_check(name: str, repo: str, ci: CiState, connector: GitHubConnector) -> Check:
    """PASS when CI is read as intended; FAIL when the Actions runs are refused too (a red
    status decides alone, nothing else can be told); WARN when statuses are refused."""
    detail = _ci_source(ci, connector.workflows_for(repo))
    if ACTIONS in ci.refused:
        return _fail(
            name,
            f"{detail}; the Actions runs are refused (403), needs {PERMISSIONS[ACTIONS]}",
            f"give the token {PERMISSIONS[ACTIONS]} on {repo}; without it only a red status "
            "or check run can be told, never green",
        )
    if STATUSES in ci.refused:
        return Check(
            "WARN",
            name,
            detail,
            f"give the token {PERMISSIONS[STATUSES]} on {repo} if any CI reports commit statuses "
            "(Jenkins, Buildkite, CircleCI): Actions runs cannot stand in for them",
        )
    return _pass(name, detail)


def _ci_source(ci: CiState, workflows: tuple[str, ...]) -> str:
    """What CI is read from, why, and what the probe commit shows."""
    sources = " and ".join(CI_SOURCES[s] for s in ci.sources)
    detail = sources
    refused = [s for s in ci.refused if s != ACTIONS]
    if refused:
        why = "; ".join(f"{CI_SOURCES[s]} refused (403), needs {PERMISSIONS[s]}" for s in refused)
        counted = (
            f"workflows required: {', '.join(workflows)}" if workflows else "all workflows count"
        )
        detail = f"{sources} ({why}; {counted})"
        if CHECK_RUNS in refused:
            detail += "; check runs of GitHub Apps other than Actions are not seen"
    detail += f"; probe commit (newest pull request or run): {ci.state}"
    if ci.missing:
        detail += f" (no result yet from {', '.join(map(repr, ci.missing))} in ci_workflows)"
    return detail


def _github_problem(name: str, repo: str, kind: str, needs: str, token_env: str) -> Check:
    """A GitHub call that gave no answer, as a check. Names the permission for a 403."""
    if kind == "forbidden":
        return _fail(
            name,
            f"HTTP 403, the token lacks {needs}",
            f"give the token {needs} on {repo} (fine-grained token: repository permissions, "
            "all read-only; see docs/configuration.md, [connectors.github])",
        )
    if kind == "unauthorized":
        return _fail(
            name,
            "HTTP 401, the token is missing, invalid or expired",
            f"set a valid token in ${token_env}",
        )
    if kind == "not_found":
        fix = f"check the name and select {repo} in the token's repository access"
        return _fail(name, "HTTP 404, no such repository or no access to it", fix)
    if kind == "policy":
        return _fail(
            name,
            "HTTP 403, blocked by an organization policy (SAML SSO or an IP allow list)",
            "authorize the token for the organization's SSO, or allow this host's IP address",
        )
    if kind == "no_sample":
        return _skip(name, "no pull request and no workflow run to probe with; run doctor again")
    if kind in {"rate_limited", "backoff", "budget"}:
        return _skip(name, f"GitHub was not asked ({kind}); run doctor again later")
    if kind in {"network", "redirect", "server"}:
        return _fail(
            name, f"GitHub did not answer ({kind})", "check the network, proxy and api_url"
        )
    return _fail(name, f"unexpected answer ({kind})", "check api_url and the repository name")


def run_checks(
    config: Path | None,
    prometheus: str | None = None,
    now: Callable[[], datetime] = utc_now,
    environ: Mapping[str, str] = os.environ,
) -> list[Check]:
    try:
        settings = load_settings(config)
        loaded = load_rules(settings.engine)
    except ConfigError as exc:
        fix = "fix the config; kelpie status shows what it parsed"
        return [_fail("config loads", str(exc), fix)]
    at = now().astimezone(UTC)
    checks = [
        _pass("config loads", f"{settings.source or 'defaults'}, {len(loaded.rules)} rules"),
        check_dry_run(settings, loaded),
    ]
    hermes = settings.hermes
    adapter = hermes.build() if hermes is not None and hermes.enabled else None
    why = "no [adapters.hermes] section" if hermes is None else "adapter disabled"
    # Tasks in progress per enabled platform (None: unknown); spans are only expected then.
    counts: list[int | None] = []
    if adapter is None:
        checks += [_skip("kanban.db", why), _skip("hooks fire", why)]
    else:
        board, running = check_board(adapter)
        counts.append(running)
        checks += [board, check_hooks(adapter, at, running)]
    cc = settings.claude_code
    cc_checks: list[Check] = []
    if cc is not None:  # only when configured, so Hermes-only output stays as it was
        if cc.enabled:
            found, cc_running = claude_code.run(cc.build(), at)
            counts.append(cc_running)
        else:
            found = claude_code.skipped("adapter disabled")
        cc_checks = [Check(*c) for c in found]
    oc = settings.openclaw
    oc_checks: list[Check] = []
    if oc is not None:  # only when configured; its spans come from the plugin, not OTLP
        found = openclaw.run(oc.build(), at) if oc.enabled else openclaw.skipped("adapter disabled")
        oc_checks = [Check(*c) for c in found]
    total = None if not counts or None in counts else sum(c for c in counts if c is not None)
    exporters = [
        name
        for name, on in (
            ("the OTLP backend of hermes-otel", adapter is not None),
            ("Claude Code's OTEL_EXPORTER_OTLP_TRACES_* variables", cc is not None and cc.enabled),
        )
        if on
    ]
    checks.append(check_spans(settings, at, total, " or ".join(exporters) or "your exporter"))
    checks.append(check_prices(settings, at))
    checks.append(_skip("worktrees readable", why) if adapter is None else check_worktrees(adapter))
    pr_blocks = "PR blocks have conditions"
    checks.append(
        _skip(pr_blocks, why) if adapter is None else check_pr_blocks(settings, adapter, at)
    )
    checks += cc_checks
    checks += oc_checks
    checks += check_github(settings, environ)
    checks.append(check_resources_watched(settings, loaded, environ))
    checks.append(check_process_counts(settings, loaded, environ))
    checks.append(check_prometheus(prometheus, environ))
    return checks


def run(config: Path | None, prometheus: str | None = None) -> tuple[str, int]:
    """Output text and exit code (1 if any check failed)."""
    checks = run_checks(config, prometheus)
    text = "\n".join(c.render() for c in checks) + "\n"
    return text, int(any(c.status == "FAIL" for c in checks))
