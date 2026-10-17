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
from collections.abc import Callable, Mapping
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
MAX_LOAD_PATHS = 32  # plugins.load.paths entries looked at
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


WRONG_USER = "run Kelpie as the Gateway's user"


def _shown(value: object) -> str:
    """A path for the terminal: names come from config an agent can edit, so control
    characters are replaced."""
    return "".join(c if c.isprintable() else "?" for c in str(value))


def _errname(exc: BaseException) -> str:
    if isinstance(exc, OSError) and exc.errno:
        return errno.errorcode.get(exc.errno, type(exc).__name__)
    return type(exc).__name__


def _probe(test: Callable[[], bool]) -> bool | OSError:
    """A pathlib test (``exists``, ``is_file``, ...): its answer, or the OSError that kept
    it from answering (EACCES when Kelpie is not the Gateway's user, ENAMETOOLONG, ...)."""
    try:
        return test()
    except OSError as exc:
        return exc


def check_workboard(adapter: OpenClawAdapter, now: datetime) -> tuple[CheckTuple, int | None]:
    """The check, and how many cards run (None when unknown)."""
    db = adapter.workboard_db
    if db is None:
        return _skip(WORKBOARD, "workboard = false: sessions only, no board tasks"), None
    found = _probe(db.exists)
    if isinstance(found, OSError):
        return _fail(WORKBOARD, f"{_shown(db)} not accessible: {_errname(found)}", WRONG_USER), None
    if not found:
        return _skip(
            WORKBOARD,
            f"{_shown(db)} not found: sessions only (openclaw plugins enable workboard for tasks)",
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

    found = _probe(spool.exists)
    if isinstance(found, OSError):
        return _fail(HOOKS, f"{_shown(spool)} not accessible: {_errname(found)}", WRONG_USER)
    if not found:
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
    if _probe(path.is_symlink) is True:
        return "a symlink, not followed (agents can write next to it)"
    if isinstance(exc, OSError):
        hint = f" ({WRONG_USER})" if exc.errno == errno.EACCES else ""
        return _errname(exc) + hint
    return str(exc)


def _plugin_dirs(load: Any) -> list[Path]:
    """The absolute directories of ``plugins.load.paths`` (``~/`` expanded), each once, at
    most :data:`MAX_LOAD_PATHS`. Relative and ``~user`` paths are left out: they would
    resolve against Kelpie's directory or another user's home, not as OpenClaw reads them."""
    paths = load.get("paths") if isinstance(load, Mapping) else None
    dirs: list[Path] = []
    for raw in paths if isinstance(paths, list) else []:
        if not isinstance(raw, str) or "\0" in raw or not raw.strip():
            continue
        if raw == "~" or raw.startswith("~/"):
            raw = str(Path.home()) + raw[1:]
        path = Path(raw)
        if path.is_absolute() and path not in dirs:
            dirs.append(path)
        if len(dirs) == MAX_LOAD_PATHS:
            break
    return dirs


def _holds_plugin(path: Path) -> bool:
    return all(_probe((path / f).is_file) is True for f in PLUGIN_FILES)


def _same_files(path: Path, ours: Mapping[str, bytes]) -> bool:
    try:
        return all(_read(path / f) == content for f, content in ours.items())
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
        return _skip(CONFIGURED, f"{_shown(path)} not found; is state_dir the Gateway's?")
    except (OSError, ValueError) as exc:
        return _skip(CONFIGURED, f"{_shown(path)} not readable: {_why(path, exc)}")
    try:
        config = json.loads(raw.decode("utf-8", "replace"))
    except (ValueError, RecursionError):
        return _skip(
            CONFIGURED, f"{_shown(path)} is not plain JSON (JSON5?); check {QUICKSTART} by hand"
        )
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
    found = [d for d in _plugin_dirs(plugins.get("load")) if _holds_plugin(d)]
    ours = {f: (PLUGIN_DIR / f).read_bytes() for f in PLUGIN_FILES}
    if not found:
        problems.append("no absolute plugins.load.paths entry holds the Kelpie plugin")
    elif not any(_same_files(d, ours) for d in found):
        problems.append(
            f"the installed plugin ({_shown(found[0])}) is not this Kelpie's copy: copy it "
            "again and restart the Gateway"
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
        if not isinstance(theirs, str) or _expanded(theirs) != ours:
            problems.append(f"the plugin's {key} is not Kelpie's ({_shown(ours)})")
    if problems:
        return _fail(CONFIGURED, "; ".join(problems) + f" ({_shown(path)})", fix)
    return _ok(
        CONFIGURED, f"enabled, this Kelpie's copy loaded from {_shown(found[0])} ({_shown(path)})"
    )


def _expanded(value: str) -> Path | None:
    try:
        return Path(value).expanduser()
    except (RuntimeError, ValueError):  # ~user of no such user
        return None


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
    link, found = _probe(path.is_symlink), _probe(path.exists)
    for answer in (link, found):
        if isinstance(answer, OSError):
            return _fail(STOP, f"{_shown(path)} not accessible: {_errname(answer)}", fix)
    if link:
        return _fail(STOP, f"{path} is a symlink; markers are not written through one", fix)
    if found:
        if not path.is_dir():
            return _fail(STOP, f"{path} is not a directory", fix)
        if not os.access(path, os.W_OK | os.X_OK):
            return _fail(STOP, f"{path} is not writable by this user", fix)
        return _ok(STOP, f"{path} writable")
    parent = next((p for p in path.parents if _probe(p.exists) is True), None)
    if parent is None or not parent.is_dir() or not os.access(parent, os.W_OK | os.X_OK):
        return _fail(STOP, f"{path} does not exist and cannot be created here", fix)
    return _ok(STOP, f"{path} will be created on the first stop")


def _guarded(name: str, check: Callable[[], CheckTuple]) -> CheckTuple:
    """A check that cannot take ``kelpie doctor`` down: an unexpected error is a FAIL that
    shows its type only (never a message, which could hold file content)."""
    try:
        return check()
    except Exception as exc:  # the report must go on
        return _fail(name, f"the check failed: {_errname(exc)}", f"{WRONG_USER}; {QUICKSTART}")


def run(adapter: OpenClawAdapter, now: datetime) -> list[CheckTuple]:
    running: list[int | None] = [None]

    def board() -> CheckTuple:
        check, running[0] = check_workboard(adapter, now)
        return check

    return [
        _guarded(WORKBOARD, board),
        _guarded(HOOKS, lambda: check_hooks(adapter, now, running[0])),
        _guarded(CONFIGURED, lambda: check_configured(adapter)),
        _guarded(GATEWAY, lambda: check_gateway(adapter)),
        _guarded(STOP, lambda: check_stop_dir(adapter)),
    ]


def skipped(why: str) -> list[CheckTuple]:
    return [_skip(name, why) for name in (WORKBOARD, HOOKS, CONFIGURED, GATEWAY, STOP)]
