"""Shell commands in traces: the redacted prefix Kelpie keeps, how calls are grouped, and
which output filter a command already runs through.

:func:`redact_command` is kept byte-for-byte identical to the one in
:mod:`kelpie.adapters.hermes.plugin`, which must stay standard-library only and import
nothing from Kelpie; a test compares the source. A command reported by OTLP and by a hook
is therefore redacted the same way.

:func:`command_prefix` groups calls: the executable (without its directory) and, when the
next word looks like a subcommand, that word (``npm test``, ``git status``, ``xcodebuild``,
``pytest``); a script runner keeps the script too (``npm run build``). Wrappers that only
run the real command (``sudo``, ``time``, ``env``, ``uv run``, ``poetry run``,
``bundle exec``) are skipped, and so is ``rtk``, the output filter Kelpie recommends:
``rtk git status`` groups with ``git status``, so the effect of introducing it can be
compared before/after. :func:`detect_filter` names the filter a
command already uses (``rtk``, ``xcbeautify``, ``tail``, ``head``, ``grep``), from the raw
command where ingest has it, else from the redacted prefix.
"""

from __future__ import annotations

import re

COMMAND_LIMIT = 200
_COMMAND_KEYS = ("command", "cmd")

_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_KEY_VALUE = re.compile(r"^(-{0,2}[A-Za-z_][\w.-]*)=(.*)$")
_SENSITIVE_KEY = re.compile(r"token|secret|passw|key|auth|credential|cookie", re.IGNORECASE)
_SECRET_FLAGS = frozenset(
    {
        "-p",
        "--token",
        "--password",
        "--passwd",
        "--secret",
        "--api-key",
        "--apikey",
        "--access-token",
        "--auth-token",
    }
)
_URL_USERINFO = re.compile(r"([A-Za-z][A-Za-z0-9+.-]*://)[^/\s@]+@")
_QUERY_SECRET = re.compile(
    r"([?&][\w-]*(?:token|key|secret|passw|auth)[\w-]*=)[^&\s]+", re.IGNORECASE
)
_LONG_RUN = re.compile(r"[A-Za-z0-9_\-+/=]{24,}")
_MASK = "***"
_MAX_TOKENS = 4


def redact_command(command: str) -> str | None:
    """A shell command reduced to what is safe to keep: its first line, without leading
    ``VAR=value`` assignments, at most four tokens, secrets masked.

    Masked: values after ``Bearer``, ``Authorization:``, ``--token``, ``--password`` and
    ``-p``; values of ``key=value`` pairs whose key looks like a credential; URL userinfo;
    any run of 24 or more token characters; and the content of quoted arguments. Best
    effort: the aim is that tokens never reach the spool, while ``gh pr checks 5`` or
    ``pytest -q`` stay recognizable.
    """
    lines = command.strip().splitlines()
    tokens = lines[0].split() if lines else []
    while tokens and _ASSIGN.match(tokens[0]):
        tokens.pop(0)
    out: list[str] = []
    mask_next = 0
    for tok in tokens:
        if mask_next:
            out.append(_MASK)
            mask_next -= 1
            continue
        bare = tok.strip("'\"")
        low = bare.lower()
        if low.startswith("authorization:"):
            glued = len(bare) > len("authorization:")
            out.append("Authorization:" + (_MASK if glued else ""))
            mask_next = 1 if glued else 2  # (scheme and) credential
        elif low == "bearer" or low in _SECRET_FLAGS:
            out.append(bare)
            mask_next = 1
        elif (kv := _KEY_VALUE.match(bare)) and _SENSITIVE_KEY.search(kv.group(1)):
            out.append(f"{kv.group(1)}={_MASK}")
        elif bare.startswith("-p") and len(bare) > 2 and not bare.startswith("--"):
            out.append("-p" + _MASK)  # -psecret
        elif tok[:1] in {"'", '"'}:
            out.append(tok[0] + "…")  # quoted text is free-form: drop it
        else:
            out.append(tok)
    text = " ".join(out[:_MAX_TOKENS])
    text = _URL_USERINFO.sub(rf"\1{_MASK}@", text)
    text = _QUERY_SECRET.sub(rf"\1{_MASK}", text)
    text = _LONG_RUN.sub(_MASK, text)
    return text[:COMMAND_LIMIT] or None


RTK = "rtk"
_WRAPPERS = frozenset({"sudo", "time", "env", "nice", "nohup", "command", "exec", RTK})
_RUNNERS = {"uv": "run", "poetry": "run", "pipenv": "run", "bundle": "exec", "pnpm": "exec"}
_SCRIPT_RUNNERS = frozenset({"run", "run-script", "exec"})
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9:_-]*$")
_PIPE_FILTERS = ("xcbeautify", "xcpretty", "tail", "head", "grep")
_PIPE = re.compile(r"\|\s*(?:[\w./-]*/)?([\w-]+)")


def command_prefix(command: str | None) -> str | None:
    """The grouping key of a (redacted or raw) command; None for an empty one."""
    if not command:
        return None
    tokens = command.split()
    while tokens:
        word = tokens[0].rsplit("/", 1)[-1]
        if word in _WRAPPERS or _ASSIGN.match(tokens[0]):
            tokens.pop(0)
        elif _RUNNERS.get(word) is not None and len(tokens) > 1 and tokens[1] == _RUNNERS[word]:
            del tokens[:2]
        else:
            break
    if not tokens:
        return None
    exe = tokens[0].rsplit("/", 1)[-1] or tokens[0]
    rest = tokens[1:]
    if exe.startswith("python") and rest[:1] == ["-m"] and len(rest) > 1:
        return f"{exe} -m {rest[1]}"
    if rest and _SUBCOMMAND.match(rest[0]):
        if rest[0] in _SCRIPT_RUNNERS and len(rest) > 1 and _SUBCOMMAND.match(rest[1]):
            return f"{exe} {rest[0]} {rest[1]}"  # npm run build
        return f"{exe} {rest[0]}"
    return exe


def detect_filter(command: str | None) -> str | None:
    """The output filter ``command`` already runs through, if any."""
    if not command:
        return None
    first = command.split(None, 1)[0].rsplit("/", 1)[-1] if command.strip() else ""
    if first == RTK:
        return RTK
    for match in _PIPE.finditer(command):
        if match.group(1) in _PIPE_FILTERS:
            return match.group(1)
    return None


def command_of(args: object) -> str | None:
    """The shell command in a tool's arguments (``command`` or ``cmd``), if any."""
    if not isinstance(args, dict):
        return None
    for key in _COMMAND_KEYS:
        value = args.get(key)
        if isinstance(value, str) and value:
            return value
    return None
