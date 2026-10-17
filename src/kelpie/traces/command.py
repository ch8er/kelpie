"""Shell commands in traces: the redacted prefix Kelpie keeps, how calls are grouped, and
which output filter a command already runs through.

:func:`redact_command` and its constants form one block (between the ``shared redaction``
markers) kept byte-for-byte identical in :mod:`kelpie.adapters.hermes.plugin`,
:mod:`kelpie.adapters.claude_code.hook` and :mod:`kelpie.adapters.openclaw.plugin.kelpie_hook`,
which run on agent hosts, must stay standard-library only and import nothing from Kelpie;
``tests/traces/test_redaction.py`` compares the four. A command reported by OTLP and by a
hook is therefore redacted the same way.

:func:`command_prefix` groups calls: the executable (without its directory) and, for tools
known to take subcommands, the subcommand (``npm test``, ``git status``, ``xcodebuild``,
``pytest``); a script runner keeps common script names (``npm run build``). Any other word
is dropped, since it may be a host, a database or a secret (``ssh prod-db01`` is ``ssh``).
Wrappers that only run the real command (``sudo -u x``, ``time``, ``env``, ``uv run``,
``poetry run``, ``bundle exec``) are skipped with their flags, and so is ``rtk``, the output
filter Kelpie recommends: ``rtk git status`` groups with ``git status``, so the effect of
introducing it can be compared before/after. :func:`detect_filter` names the filter a
command already uses (``rtk``, ``xcbeautify``, ``tail``, ``head``, ``grep``) from the first
line of the raw command where ingest has it, else from the redacted prefix.
"""

from __future__ import annotations

import re

_COMMAND_KEYS = ("command", "cmd")

# --- shared redaction: begin ---
# Byte-for-byte identical in kelpie.traces.command, the Hermes plugin, the Claude Code hook
# and the OpenClaw hook (tests/traces/test_redaction.py compares them): the hooks run on agent
# hosts without Kelpie installed, and a command reported both by a hook and over OTLP must
# redact the same.
COMMAND_LIMIT = 200
_MAX_TOKENS = 4
_MAX_LINE = 4096  # characters of the first line that are looked at
_MASK = "***"
_QUOTES = ("'", '"', "`")
_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
_EXE = re.compile(r"^[A-Za-z0-9_][\w.+-]{0,63}$")
_WORD = re.compile(r"^(?:[a-z][a-z0-9_-]{0,23}|[0-9]{1,10})$")
_FLAG = re.compile(r"^(?:-[A-Za-z0-9]|--[A-Za-z][A-Za-z0-9-]{0,40})$")
_SEPARATORS = frozenset({"|", "||", "|&", "&&", ";", "&"})
_PYTHON_MODULES = frozenset({"pytest", "unittest", "pip", "mypy", "ruff", "black", "build"})
# Wrappers that only run the real command, with the flags of theirs that take a value.
# Wrappers that only run the real command: their flags that take a value, and those that
# do not. Any other flag of a wrapper may take one: the rest of the command is masked.
_WRAPPERS = {
    "sudo": (
        frozenset(
            {
                *("-u", "-g", "-h", "-p", "-C", "-U", "-r", "-t", "-D", "-T", "-R"),
                *("--user", "--group", "--host", "--prompt", "--close-from", "--other-user"),
                *("--role", "--type", "--chdir", "--command-timeout", "--chroot"),
            }
        ),
        frozenset(
            {
                *("-A", "-b", "-E", "-H", "-i", "-K", "-k", "-n", "-P", "-S", "-s", "-B"),
                *("--askpass", "--background", "--preserve-env", "--set-home", "--login"),
                *("--remove-timestamp", "--reset-timestamp", "--non-interactive"),
                *("--preserve-groups", "--stdin", "--shell", "--bell"),
            }
        ),
    ),
    "time": (
        frozenset({"-o", "-f", "--output", "--format"}),
        frozenset({"-p", "-v", "-a", "-q", "--portability", "--verbose", "--append", "--quiet"}),
    ),
    "env": (
        frozenset({"-u", "-C", "-S", "--unset", "--chdir", "--split-string"}),
        frozenset({"-i", "-0", "-v", "--ignore-environment", "--null", "--debug"}),
    ),
    "nice": (frozenset({"-n", "--adjustment"}), frozenset()),
    "nohup": (frozenset(), frozenset()),
    "command": (frozenset(), frozenset({"-p", "-v", "-V"})),
    "exec": (frozenset({"-a"}), frozenset({"-c", "-l"})),
    "rtk": (frozenset(), frozenset()),
}
_MAX_WRAPPED = 8  # wrapper tokens kept on top of _MAX_TOKENS
_BLANKS = re.compile(r"[ \t]+")  # what bash splits words on


def _quoted_span(tokens: list[str], i: int) -> int:
    """Index after the argument that starts at ``tokens[i]``: past the token that closes
    the first quote it opens, else the next token. An unclosed quote runs to the end."""
    tok = tokens[i]
    if "\\" in tok and any(q in tok for q in _QUOTES):
        return len(tokens)  # an escaped quote: where the argument ends is not clear
    quote = next((c for c in tok if c in _QUOTES), None)
    if quote is None or tok.count(quote) % 2 == 0:
        return i + 1
    for j in range(i + 1, len(tokens)):
        if tokens[j].count(quote) % 2 == 1:
            return j + 1
    return len(tokens)


def _wrapper_flag(
    tok: str, valued: frozenset[str], plain: frozenset[str]
) -> tuple[str, bool, bool]:
    """A wrapper's flag as kept, whether the next argument is its value, and whether it is
    unknown (so its value, or what follows, cannot be told apart from the command)."""
    if tok == "--":
        return tok, False, False
    if tok[:2] == "--" or tok[1:2] == "-":
        known = tok in valued or tok in plain
        return (tok if _FLAG.match(tok) else _MASK), tok not in plain, not known
    for k in range(1, len(tok)):  # a cluster: -iu root, -Eu root, -uroot
        flag = "-" + tok[k]
        if flag in plain:
            continue
        last = k == len(tok) - 1
        if flag in valued:
            return (tok, True, False) if last else (tok[: k + 1] + _MASK, False, False)
        return tok[: k + 1] + ("" if last else _MASK), True, True
    return tok, False, False


def redact_command(command: str) -> str | None:
    """A shell command reduced to a prefix that is safe to keep, e.g. ``gh pr checks 5``.

    An allowlist, not a list of secrets: of the first line (its first 4096 characters),
    without leading ``VAR=value`` assignments, at most four arguments are kept, and only
    these: the executable without its directory; plain lowercase words and numbers right
    after it (subcommands: ``gh pr checks 5``, ``npm run build``); flag names (``-q``,
    ``--watch``; a cluster or glued value keeps its first letter, ``-p***``); a pipe or
    list separator and the next executable. Everything else is ``***``: flag values,
    ``--flag=value`` values, paths, URLs, hosts, ``user:pass``, quoted text, variables. Once
    a flag or a masked argument appears, later plain words are masked too: they may be its
    value. A secret passed as a lowercase word right after the executable
    (``redis-cli auth hunter2``) is still kept.
    """
    lines = command.strip().splitlines()
    if not lines:
        return None
    tokens = [t for t in _BLANKS.split(lines[0][:_MAX_LINE]) if t]
    if len(lines[0]) > _MAX_LINE and tokens:
        tokens[-1] = _MASK  # the cut may have split it
    i = 0
    while i < len(tokens) and _ASSIGN.match(tokens[i]):
        i = _quoted_span(tokens, i)
    out: list[str] = []
    exe = True  # the next argument is an executable
    words = False  # plain words are still kept (right after the executable)
    wrapper = None  # flags of the wrapper whose options come next (sudo -u x make)
    extra = 0  # wrapper tokens, not counted against _MAX_TOKENS
    while i < len(tokens) and len(out) < _MAX_TOKENS + extra:
        tok = tokens[i]
        end = _quoted_span(tokens, i)
        name, eq, _ = tok.partition("=")
        if wrapper is not None:
            before = len(out)
            if _ASSIGN.match(tok):
                out.append(f"{name}={_MASK}")  # env FOO=bar
            elif eq and tok[:1] == "-" and _FLAG.match(name):
                out.append(f"{name}={_MASK}")
            elif tok[:1] == "-" and tok[1:].isdigit() and len(tok) < 5:
                out.append(tok)  # nice -5
            elif tok[:1] == "-" and len(tok) > 1 and end == i + 1:
                kept, value, unknown = _wrapper_flag(tok, *wrapper)
                out.append(kept)
                if value and end < len(tokens):
                    out.append(_MASK)  # its value: sudo -u root
                    end = _quoted_span(tokens, end)
                if unknown:  # whatever comes next may be its value
                    wrapper, words = None, False
            else:
                wrapper, exe = None, True  # the wrapped command: read it as an executable
                continue
            extra = min(extra + len(out) - before, _MAX_WRAPPED)
            i = end
            continue
        if tok.startswith("#"):
            break
        if end > i + 1 or any(q in tok for q in _QUOTES):
            out.append(_MASK)
            exe = words = False
        elif tok in _SEPARATORS:
            out.append(tok)
            exe, words = True, False
        elif exe:
            base = tok.rstrip("/").rsplit("/", 1)[-1]
            ok = bool(_EXE.match(base)) and not _ASSIGN.match(base)
            out.append(base if ok else _MASK)
            exe, words = False, ok
            if ok and base in _WRAPPERS:
                wrapper = _WRAPPERS[base]
                extra = min(extra + 1, _MAX_WRAPPED)
        elif _FLAG.match(tok):
            out.append(tok)
            words = False
        elif eq and _FLAG.match(name):
            out.append(f"{name}={_MASK}")
            words = False
        elif tok[:1] == "-" and tok[1:2].isalpha() and tok[1:2] != "-":
            out.append(tok[:2] + _MASK)  # -la, -sSu, -pSECRET: a cluster or a glued value
            words = False
        elif words and _WORD.match(tok):
            out.append(tok)
        elif out and out[-1] == "-m" and tok in _PYTHON_MODULES:
            out.append(tok)  # python -m pytest
        else:
            out.append(_MASK)
            words = False
        i = end
    return " ".join(out)[:COMMAND_LIMIT] or None


# --- shared redaction: end ---


RTK = "rtk"
_RUNNERS = {"uv": "run", "poetry": "run", "pipenv": "run", "bundle": "exec", "pnpm": "exec"}
# Executables whose first argument is a subcommand of the tool, not user data. Any other
# executable is kept alone: its next word may be a host, a database or a secret.
_SUBCOMMANDS = frozenset(
    {
        "git", "gh", "npm", "pnpm", "yarn", "bun", "deno", "cargo", "go", "docker", "podman",
        "kubectl", "helm", "dotnet", "xcodebuild", "swift", "mvn", "gradle",
        "gradlew", "terraform", "rake", "bundle", "pip", "pip3", "poetry", "uv", "flutter",
        "dart", "composer", "mix", "rustup", "brew", "nx", "turbo",
    }
)  # fmt: skip
_SCRIPT_RUNNERS = frozenset({"run", "run-script"})
# Script names kept after ``npm run``: common ones only, the rest are project-defined.
_SCRIPTS = frozenset(
    {"build", "test", "lint", "dev", "start", "typecheck", "check", "format", "e2e", "ci"}
)
# make targets and just recipes are project-defined too: only common ones are kept.
_TARGET_RUNNERS = frozenset({"make", "just"})
_TARGETS = frozenset(
    {"build", "test", "lint", "check", "install", "clean", "all", "format", "ci", "dev"}
)
_SUBCOMMAND = re.compile(r"^[a-z][a-z0-9:_-]*$")
_EXECUTABLE = re.compile(r"^[A-Za-z0-9_][\w.+-]*$")
_PIPE_FILTERS = ("xcbeautify", "xcpretty", "tail", "head", "grep")
_PIPE = re.compile(r"\|\s*(?:[\w./-]*/)?([\w-]+)")
_QUOTED = re.compile(r"'[^']*'|\"[^\"]*\"")


def _base(token: str) -> str:
    return re.split(r"[/\\]", token)[-1]


def command_prefix(command: str | None) -> str | None:
    """The grouping key of a (redacted or raw) command; None when there is no recognizable
    executable. Only whole words of ``command`` are kept, never a flag or its value."""
    if not command:
        return None
    tokens = command.split()
    while tokens:
        word = _base(tokens[0])
        if _ASSIGN.match(tokens[0]):
            tokens.pop(0)
        elif word in _WRAPPERS:
            valued = _WRAPPERS[word][0]
            tokens.pop(0)
            while tokens and (tokens[0].startswith("-") or _ASSIGN.match(tokens[0])):
                flag = tokens.pop(0)
                if tokens and (flag in valued or tokens[0] == _MASK):  # -iu ***: its value
                    tokens.pop(0)
        elif _RUNNERS.get(word) is not None and len(tokens) > 1 and tokens[1] == _RUNNERS[word]:
            del tokens[:2]
        else:
            break
    if not tokens:
        return None
    exe = _base(tokens[0])
    if not _EXECUTABLE.match(exe):
        return None
    rest = tokens[1:]
    if exe.startswith("python") and rest[:1] == ["-m"]:
        module = rest[1] if len(rest) > 1 and rest[1] in _PYTHON_MODULES else None
        return f"{exe} -m {module}" if module else f"{exe} -m"
    if exe in _TARGET_RUNNERS:
        return f"{exe} {rest[0]}" if rest and rest[0] in _TARGETS else exe
    if exe not in _SUBCOMMANDS or not rest or not _SUBCOMMAND.match(rest[0]):
        return exe
    if rest[0] in _SCRIPT_RUNNERS:
        script = rest[1] if len(rest) > 1 and rest[1] in _SCRIPTS else None
        return f"{exe} {rest[0]} {script}" if script else f"{exe} {rest[0]}"
    return f"{exe} {rest[0]}"


def detect_filter(command: str | None) -> str | None:
    """The output filter ``command`` already runs through, if any: ``rtk`` in front, or a
    pipe into a known filter on the first line, outside quotes."""
    if not command or not command.strip():
        return None
    line = _QUOTED.sub("", command.strip().splitlines()[0])
    first = line.split(None, 1)
    if first and _base(first[0]) == RTK:
        return RTK
    for match in _PIPE.finditer(line):
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
