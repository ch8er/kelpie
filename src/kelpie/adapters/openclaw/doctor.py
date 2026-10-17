"""``kelpie doctor`` checks for ``[adapters.openclaw]``.

Each check is ``(status, name, detail, fix)`` with status ``PASS``, ``FAIL`` or ``SKIP``;
:mod:`kelpie.doctor` renders them. Nothing is created or written: the stop directory is only
tested for being writable, and the Gateway is only asked for its health
(``openclaw gateway call health``).
"""

from __future__ import annotations

import errno
import json
import os
from collections.abc import Mapping
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from kelpie.adapters.openclaw import workboard
from kelpie.adapters.openclaw.adapter import (
    FUTURE_SKEW,
    STOP_VERSION,
    OpenClawAdapter,
    OpenClawError,
    _open_regular,
    format_age,
)
from kelpie.adapters.openclaw.plugin import kelpie_hook
from kelpie.adapters.openclaw.stop import OpenClawCliError, gateway_call

CheckTuple = tuple[str, str, str, str]

FRESH = timedelta(hours=1)  # the newest spool line must be younger than this
MAX_FILE = 1024 * 1024  # bytes read of the OpenClaw config and of the installed plugin files
HEALTH_TIMEOUT = 20.0
PLUGIN_ID = "kelpie"
PLUGIN_DIR = Path(kelpie_hook.__file__).parent  # this Kelpie's copy of the plugin
PLUGIN_FILES = ("index.js", "kelpie_hook.py")
QUICKSTART = "docs/quickstart-openclaw.md"

WORKBOARD, HOOKS, CONFIGURED, GATEWAY, STOP = (
    "openclaw workboard",
    "openclaw plugin fires",
    "openclaw plugin configured",
    "openclaw gateway",
    "openclaw stop markers",
)


def _ok(name: str, detail: str) -> CheckTuple:
    return ("PASS", name, detail, "")


def _skip(name: str, detail: str) -> CheckTuple:
    return ("SKIP", name, detail, "")


def _fail(name: str, detail: str, fix: str) -> CheckTuple:
    return ("FAIL", name, detail, fix)


def check_workboard(adapter: OpenClawAdapter, now: datetime) -> tuple[CheckTuple, int | None]:
    """The check, and how many cards run (None when unknown)."""
    db = adapter.workboard_db
    if db is None:
        return _skip(WORKBOARD, "workboard = false: sessions only, no board tasks"), None
    if not db.exists():
        return _skip(
            WORKBOARD,
            f"{db} not found: sessions only (openclaw plugins enable workboard for tasks)",
        ), None
    try:
        board = adapter.board(now)
    except OpenClawError as exc:
        fix = (
            "check the file and the OpenClaw version (Kelpie knows the Workboard schema of "
            "OpenClaw 2026.10.x); set workboard = false to read sessions only"
        )
        return _fail(WORKBOARD, str(exc), fix), None
    counts = workboard.statuses(board.cards)
    running = counts.get("running", 0)
    return _ok(WORKBOARD, f"{len(board.cards)} cards, {running} running ({db})"), running


def check_hooks(adapter: OpenClawAdapter, now: datetime, running: int | None) -> CheckTuple:
    spool = adapter.spool
    if spool is None:
        return _skip(HOOKS, "spool disabled")
    fix = f"install and enable the Kelpie plugin ({QUICKSTART}, step 3) and restart the Gateway"

    def quiet(detail: str) -> CheckTuple:
        if running == 0:
            return _skip(HOOKS, f"{detail}; no card is running, let an agent work and run again")
        return _fail(HOOKS, detail, fix)

    if not spool.exists():
        return quiet(f"spool missing ({spool})")
    newest = adapter.last_spool_event(until=now + FUTURE_SKEW)
    if newest is None:
        return quiet(f"spool has no readable lines ({spool})")
    age = now - newest.at
    if age > FRESH:
        return quiet(f"newest spool line is {format_age(age)} old ({spool})")
    if adapter.stop_dir is not None and newest.plugin < STOP_VERSION:
        return _fail(
            HOOKS,
            f"the Gateway runs a Kelpie plugin without stop support, or a new copy it has not "
            f"loaded yet ({spool})",
            f"copy the current plugin and restart the Gateway ({QUICKSTART}, step 3)",
        )
    access = "with" if newest.gate else "no run seen yet, or without"
    return _ok(
        HOOKS, f"newest spool line {format_age(age)} old, {access} conversation access ({spool})"
    )


def _read(path: Path) -> bytes:
    """A regular file (not through a symlink, agents can write there), bounded. Raises
    OSError or ValueError; the message never holds the content."""
    fd = _open_regular(path)
    try:
        raw = os.read(fd, MAX_FILE + 1)
    finally:
        os.close(fd)
    if len(raw) > MAX_FILE:
        raise ValueError("too large to read")
    return raw


def _why(path: Path, exc: Exception) -> str:
    if path.is_symlink():
        return "a symlink, not followed (agents can write next to it)"
    if isinstance(exc, OSError):
        return errno.errorcode.get(exc.errno or 0, type(exc).__name__)
    return str(exc)


def _plugin_dirs(load: Any) -> list[Path]:
    paths = load.get("paths") if isinstance(load, Mapping) else None
    return [
        Path(p).expanduser()
        for p in (paths if isinstance(paths, list) else [])
        if isinstance(p, str) and p.strip()
    ]


def _same_file(a: Path, b: Path) -> bool:
    try:
        return _read(a) == _read(b)
    except (OSError, ValueError):
        return False


def check_configured(adapter: OpenClawAdapter) -> CheckTuple:
    """Whether OpenClaw's config enables, loads and permits this Kelpie's plugin, and points
    it at Kelpie's spool and stop directory. Only the ``plugins`` section is looked at;
    nothing of the file (which holds credentials) reaches the output."""
    path = adapter.state_dir / "openclaw.json"
    try:
        raw = _read(path)
    except FileNotFoundError:
        return _skip(CONFIGURED, f"{path} not found; is state_dir the Gateway's?")
    except (OSError, ValueError) as exc:
        return _skip(CONFIGURED, f"{path} not readable: {_why(path, exc)}")
    try:
        config = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return _skip(CONFIGURED, f"{path} is not plain JSON (JSON5?); check {QUICKSTART} by hand")
    plugins = config.get("plugins") if isinstance(config, Mapping) else None
    plugins = plugins if isinstance(plugins, Mapping) else {}
    entries = plugins.get("entries")
    entry = entries.get(PLUGIN_ID) if isinstance(entries, Mapping) else None
    entry = entry if isinstance(entry, Mapping) else {}
    fix = f"follow {QUICKSTART}, step 3"
    problems: list[str] = []
    if plugins.get("enabled") is False:
        problems.append("plugins.enabled is false")
    allow, deny = plugins.get("allow"), plugins.get("deny")
    if isinstance(deny, list) and PLUGIN_ID in deny:
        problems.append("kelpie is in plugins.deny")
    if isinstance(allow, list) and PLUGIN_ID not in allow:
        problems.append("plugins.allow does not list kelpie")
    if entry.get("enabled") is not True:
        problems.append("the plugin is not enabled (openclaw plugins enable kelpie)")
    found = [d for d in _plugin_dirs(plugins.get("load")) if (d / "index.js").is_file()]
    found = [d for d in found if (d / "kelpie_hook.py").is_file()]
    if not found:
        problems.append("no plugins.load.paths entry holds the Kelpie plugin")
    elif not any(all(_same_file(d / f, PLUGIN_DIR / f) for f in PLUGIN_FILES) for d in found):
        problems.append(
            f"the installed plugin ({found[0]}) is not this Kelpie's copy: copy it again and "
            "restart the Gateway"
        )
    hooks = entry.get("hooks")
    if not (isinstance(hooks, Mapping) and hooks.get("allowConversationAccess") is True):
        problems.append(
            "no conversation access (needed for token usage, to refuse scheduled runs of a "
            "stopped session and to let the operator lift a stop)"
        )
    settings = entry.get("config")
    settings = settings if isinstance(settings, Mapping) else {}
    for key, ours, default in (
        ("spool", adapter.spool, kelpie_hook.DEFAULT_SPOOL),
        ("stopDir", adapter.stop_dir, "~/.openclaw/kelpie/stop"),
    ):
        if ours is None:
            continue
        theirs = settings.get(key, default)
        if not isinstance(theirs, str) or Path(theirs).expanduser() != ours:
            problems.append(f"the plugin's {key} is not Kelpie's ({ours})")
    if problems:
        return _fail(CONFIGURED, "; ".join(problems) + f" ({path})", fix)
    return _ok(CONFIGURED, f"enabled, this Kelpie's copy loaded from {found[0]} ({path})")


def check_gateway(adapter: OpenClawAdapter) -> CheckTuple:
    if adapter.cli is None:
        return _skip(GATEWAY, "cli = false: stops refuse tool calls but do not abort runs")
    fix = (
        "start the Gateway, run Kelpie as the Gateway's user (the CLI uses its OpenClaw "
        "config to authenticate) and set cli to the openclaw executable"
    )
    try:
        answer = gateway_call(
            adapter.cli, "health", timeout=min(adapter.cli_timeout, HEALTH_TIMEOUT)
        )
    except OpenClawCliError as exc:
        return _fail(GATEWAY, f"openclaw gateway call health: {exc.public_reason}", fix)
    plugins = answer.get("plugins")
    loaded = plugins.get("loaded") if isinstance(plugins, Mapping) else None
    if not isinstance(loaded, list) or PLUGIN_ID not in loaded:
        return _fail(
            GATEWAY,
            "the Gateway answers but has not loaded the kelpie plugin",
            f"enable the plugin and restart the Gateway ({QUICKSTART}, step 3); "
            "openclaw plugins inspect kelpie --runtime --json shows why",
        )
    return _ok(GATEWAY, "answers, kelpie plugin loaded")


def check_stop_dir(adapter: OpenClawAdapter) -> CheckTuple:
    path = adapter.stop_dir
    if path is None:
        return _skip(STOP, "stop_dir = false: stop actions become alerts")
    fix = (
        f"run Kelpie as the Gateway's user, or set stop_dir (and the plugin's stopDir) to a "
        f"directory both can use (now {path})"
    )
    if path.is_symlink():
        return _fail(STOP, f"{path} is a symlink; markers are not written through one", fix)
    if path.exists():
        if not path.is_dir():
            return _fail(STOP, f"{path} is not a directory", fix)
        if not os.access(path, os.W_OK | os.X_OK):
            return _fail(STOP, f"{path} is not writable by this user", fix)
        return _ok(STOP, f"{path} writable")
    parent = next((p for p in path.parents if p.exists()), None)
    if parent is None or not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        return _fail(STOP, f"{path} does not exist and cannot be created here", fix)
    return _ok(STOP, f"{path} will be created on the first stop")


def run(adapter: OpenClawAdapter, now: datetime) -> list[CheckTuple]:
    board, running = check_workboard(adapter, now)
    return [
        board,
        check_hooks(adapter, now, running),
        check_configured(adapter),
        check_gateway(adapter),
        check_stop_dir(adapter),
    ]


def skipped(why: str) -> list[CheckTuple]:
    return [_skip(name, why) for name in (WORKBOARD, HOOKS, CONFIGURED, GATEWAY, STOP)]
