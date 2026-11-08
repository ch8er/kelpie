"""The docs stay runnable: every TOML example in them loads with the config loader (which
refuses unknown sections and keys), and every ``kelpie`` command they show names a real
subcommand and real options. A renamed key or flag then fails here, not on a reader."""

from __future__ import annotations

import argparse
import re
import shlex
from pathlib import Path

import pytest

from kelpie import cli
from kelpie.app import load_rules, load_settings

ROOT = Path(__file__).parents[1]

# plan.md and roadmap.md describe planned work (`kelpie run-once`); their TOML still loads.
PLANNING = {ROOT / "docs" / "plan.md", ROOT / "docs" / "roadmap.md"}


def doc_files() -> list[Path]:
    files = {ROOT / "README.md", ROOT / "CONTRIBUTING.md", *ROOT.glob("docs/*.md")}
    files |= set(ROOT.glob("src/kelpie/**/README.md")) | set(ROOT.glob("deploy/**/*.md"))
    return sorted(files)


def blocks(text: str, langs: str) -> list[tuple[int, str]]:
    """(first line number, body) of each fenced block in one of ``langs``."""
    found = []
    for m in re.finditer(rf"^```(?:{langs})\n(.*?)^```", text, re.S | re.M):
        found.append((text[: m.start()].count("\n") + 2, m.group(1)))
    return found


def toml_examples() -> list[object]:
    return [
        pytest.param(f"{f.relative_to(ROOT)}:{line}", body, id=f"{f.relative_to(ROOT)}:{line}")
        for f in doc_files()
        for line, body in blocks(f.read_text(encoding="utf-8"), "toml")
    ]


@pytest.mark.parametrize(("where", "body"), toml_examples())
def test_toml_examples_load(
    where: str, body: str, tmp_path: Path, tmp_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Examples name their token variables; the loader may insist they are set.
    for var in set(re.findall(r'token_env\s*=\s*"([A-Z_][A-Z0-9_]*)"', body)):
        monkeypatch.setenv(var, "x" * 40)
    config = tmp_path / "kelpie.toml"
    config.write_text(body, encoding="utf-8")
    settings = load_settings(config)
    load_rules(settings.engine)


# --- commands -------------------------------------------------------------------------


class _Parsed(Exception):
    def __init__(self, parser: argparse.ArgumentParser) -> None:
        self.parser = parser


@pytest.fixture(scope="module")
def parser() -> argparse.ArgumentParser:
    def grab(self: argparse.ArgumentParser, *args: object, **kwargs: object) -> None:
        raise _Parsed(self)

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(argparse.ArgumentParser, "parse_args", grab)
        try:
            cli.main(["status"])
        except _Parsed as done:
            return done.parser
    raise AssertionError("kelpie.cli.main did not parse its arguments")


def _subcommands(p: argparse.ArgumentParser) -> dict[str, argparse.ArgumentParser]:
    for action in p._actions:
        if isinstance(action, argparse._SubParsersAction):
            return dict(action.choices)
    return {}


def _options(p: argparse.ArgumentParser) -> set[str]:
    return {o for a in p._actions for o in a.option_strings}


# `kelpie` in command position: at the start, after a shell separator, `uv run`, `sudo -u
# X` or `docker compose exec <service>`; not `chown kelpie` or a path.
_START = r"(?:^|&&|;|\||\$\(|\()\s*(?:sudo -u \S+ )?(?:uv run |docker compose exec \S+ )?"
_COMMAND = re.compile(_START + r"kelpie(\s.*)$")
_INLINE = re.compile(r"`(?:uv run )?kelpie(\s[^`\n]+)`")


def commands() -> list[tuple[str, str]]:
    found = []
    for f in doc_files():
        if f in PLANNING:
            continue
        text = f.read_text(encoding="utf-8")
        where = str(f.relative_to(ROOT))
        for first, body in blocks(text, "bash|sh|shell|console"):
            for i, line in enumerate(body.replace("\\\n", " ").splitlines()):
                if m := _COMMAND.search(line.strip()):
                    found.append((f"{where}:{first + i}", m.group(1)))
        for m in _INLINE.finditer(text):
            found.append((f"{where}:{text[: m.start()].count(chr(10)) + 1}", m.group(1)))
    return found


def check(args: str, top: argparse.ArgumentParser) -> str | None:
    """Why ``kelpie <args>`` would not parse, or None. Placeholders (``<x>``, ``[x]``,
    ``...``) and everything after a shell separator or comment are ignored."""
    args = re.split(r"\s(?:#|\||&&|;|>|2>|&$)", " " + args)[0]
    args = re.sub(r"\[[^\]]*\]|<[^>]*>|\.\.\.", " ", args)
    try:
        words = shlex.split(args)
    except ValueError:
        words = args.split()
    p, path, known = top, ["kelpie"], _options(top)
    for word in words:
        if word.startswith("-"):
            name = word.split("=", 1)[0]
            if name not in known:
                return f"unknown option {name} for {' '.join(path)}"
            continue
        subs = _subcommands(p)
        if not subs:
            continue  # a positional argument
        if word not in subs:
            if path == ["kelpie"] and words[words.index(word) - 1 : words.index(word)] == [
                "--config"
            ]:
                continue  # --config's value
            return f"unknown subcommand {word!r} of {' '.join(path)}"
        p = subs[word]
        path.append(word)
        known = known | _options(p)
    return None


def test_commands_in_docs_parse(parser: argparse.ArgumentParser) -> None:
    found = commands()
    assert len(found) > 100  # the extraction still finds the docs' commands
    bad = [f"{where}: kelpie{args}: {why}" for where, args in found if (why := check(args, parser))]
    assert not bad, "\n".join(bad)


def test_the_checker_catches_drift(parser: argparse.ArgumentParser) -> None:
    assert check(" doctor --prometheus URL", parser) is None
    assert check(" --config /etc/kelpie.toml report --send", parser) is None
    assert check(" traces rotate-token --grace 0s", parser) is None
    assert check(" [--config PATH] <command>", parser) is None
    assert check(" doctr", parser) is not None
    assert check(" report --sned", parser) is not None
    assert check(" traces rotate", parser) is not None
