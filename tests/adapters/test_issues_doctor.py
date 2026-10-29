"""``kelpie doctor`` checks for ``[adapters.github_issues]`` against a local fake GitHub."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from kelpie.adapters.issues import doctor
from kelpie.adapters.issues.config import load_github_issues
from kelpie.doctor import run_checks
from tests.adapters.test_issues_github import NOW, REPO, Repo, issue
from tests.connectors.fake_github import TOKEN, FakeGitHub, serve

ENV = {"GH_ISSUES": TOKEN}


@pytest.fixture
def gh() -> Iterator[FakeGitHub]:
    with serve() as fake:
        yield fake


def settings(gh: FakeGitHub, **section: object):  # type: ignore[no-untyped-def]
    base = {"repos": [REPO], "api_url": gh.url, "token_env": "GH_ISSUES"}
    return load_github_issues({**base, **section}, None)


def by_name(checks: list[tuple[str, str, str, str]]) -> dict[str, tuple[str, str, str]]:
    return {name: (status, detail, fix) for status, name, detail, fix in checks}


def test_everything_in_order(gh: FakeGitHub) -> None:
    gh.reply(f"/repos/{REPO}", {"permissions": {"pull": True, "triage": True}})
    gh.reply(f"/repos/{REPO}/labels/fix-me", {"name": "fix-me"})
    gh.reply(f"/repos/{REPO}/labels/blocked", {"name": "blocked"})
    Repo(gh).lists([issue(1, labels=[{"name": "fix-me"}])])
    gh.reply(f"/repos/{REPO}/issues/1/events?page=1&per_page=100", [])
    agents = {"openhands": {"label": "fix-me"}}
    checks = by_name(doctor.run(settings(gh, agents=agents), NOW, ENV))
    assert {name: c[0] for name, c in checks.items()} == {
        doctor.TOKEN: "PASS",
        doctor.REPOS: "PASS",
        doctor.LABELS: "PASS",
        doctor.READ: "PASS",
    }
    assert checks[doctor.READ][1] == "1 tasks, 1 held by an agent or a person"
    assert all(s.method == "GET" for s in gh.requests)  # doctor writes nothing


def test_token(gh: FakeGitHub) -> None:
    missing = by_name(doctor.run(settings(gh), NOW, {}))
    assert missing[doctor.TOKEN][0] == "FAIL" and "GH_ISSUES" in missing[doctor.TOKEN][1]
    assert {c[0] for n, c in missing.items() if n != doctor.TOKEN} == {"SKIP"}
    loose = load_github_issues({"repos": [REPO]}, None)
    assert doctor.check_token(loose, {})[0] == "WARN"


def test_repos_and_permissions(gh: FakeGitHub) -> None:
    reader = settings(gh).build(ENV).tracker
    gh.reply(f"/repos/{REPO}", {"permissions": {"pull": True, "push": False}})
    check = doctor.check_repos(reader)  # type: ignore[arg-type]
    assert check[0] == "WARN" and "allows no labels, assignments or comments" in check[2]
    gh.reply(f"/repos/{REPO}", {"permissions": {"admin": True, "push": True}})
    check = doctor.check_repos(reader)  # type: ignore[arg-type]
    assert check[0] == "WARN" and "administers or maintains" in check[2] and "triage" in check[3]
    gh.reply(f"/repos/{REPO}", {"message": "Not Found"}, 404)
    check = doctor.check_repos(reader)  # type: ignore[arg-type]
    assert check[0] == "FAIL" and "not_found" in check[2]
    gh.reply(f"/repos/{REPO}", {"full_name": REPO})  # an App token: no permissions field
    assert doctor.check_repos(reader)[0] == "PASS"  # type: ignore[arg-type]


def test_labels(gh: FakeGitHub) -> None:
    s = settings(gh, label="agent", ready_labels=["agent ready"], agents={"o": {"label": "fix-me"}})
    reader = s.build(ENV).tracker
    gh.reply(f"/repos/{REPO}/labels/agent", {"name": "agent"})
    gh.reply(f"/repos/{REPO}/labels/blocked", {"name": "blocked"})
    check = doctor.check_labels(reader, s)  # type: ignore[arg-type]
    assert check[0] == "WARN" and check[2] == (
        f"not defined: {REPO}: fix-me; not checked (spaces or symbols): 'agent ready'"
    )
    gh.reply(f"/repos/{REPO}/labels/fix-me", {"name": "fix-me"})
    check = doctor.check_labels(reader, s)  # type: ignore[arg-type]
    assert check[0] == "PASS" and "not checked (spaces or symbols): 'agent ready'" in check[2]
    gh.reply(f"/repos/{REPO}/labels/agent", {"message": "boom"}, 500)
    s2 = settings(gh, label="agent")
    check = doctor.check_labels(s2.build(ENV).tracker, s2)  # type: ignore[arg-type]
    assert check[0] == "WARN" and "could not be checked" in check[2] and "server" in check[2]


def test_read(gh: FakeGitHub) -> None:
    s = settings(gh)
    adapter = s.build(ENV)
    failed = doctor.check_read(adapter, NOW)
    assert failed[0] == "FAIL" and "could not be read" in failed[2]
    Repo(gh).lists([issue(1)])
    check = doctor.check_read(s.build(ENV), NOW)  # doctor builds a new adapter each run
    assert check[0] == "WARN" and "no agents configured" in check[2]


def test_doctor_lists_them_only_when_configured(
    tmp_path: Path, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KELPIE_DB", str(tmp_path / "kelpie.db"))
    plain = tmp_path / "plain.toml"
    plain.write_text("[engine]\ndry_run = true\n")
    names = [c.name for c in run_checks(plain, prometheus="http://127.0.0.1:9", environ=ENV)]
    assert not any(n.startswith("github issues") for n in names)
    config = tmp_path / "issues.toml"
    config.write_text(
        f'[engine]\ndry_run = true\n[adapters.github_issues]\nrepos = ["{REPO}"]\n'
        f'api_url = "{gh.url}"\ntoken_env = "GH_ISSUES"\nenabled = false\n'
    )
    checks = {c.name: c for c in run_checks(config, prometheus="http://127.0.0.1:9", environ=ENV)}
    assert {n for n in checks if n.startswith("github issues")} == {
        doctor.TOKEN,
        doctor.REPOS,
        doctor.LABELS,
        doctor.READ,
    }
    assert checks[doctor.READ].status == "SKIP"


def test_a_check_that_breaks_is_a_fail_naming_only_its_type(
    gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    def boom(*args: object) -> None:
        raise RuntimeError("secret text")

    monkeypatch.setattr(doctor, "check_repos", boom)
    checks = by_name(doctor.run(settings(gh), NOW, ENV))
    status, detail, _ = checks[doctor.REPOS]
    assert status == "FAIL" and "RuntimeError" in detail and "secret" not in detail
    s = settings(gh)
    monkeypatch.setattr(type(s), "build", boom)
    checks = by_name(doctor.run(s, NOW, ENV))
    assert checks[doctor.READ][0] == "SKIP" and "RuntimeError" in checks[doctor.READ][1]


def test_doctor_reads_the_token_from_its_environ(
    tmp_path: Path, gh: FakeGitHub, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The adapters are built from the environment doctor was given, not os.environ."""
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("KELPIE_DB", str(tmp_path / "kelpie.db"))
    monkeypatch.delenv("GH_ISSUES", raising=False)
    config = tmp_path / "issues.toml"
    config.write_text(
        f'[engine]\ndry_run = true\n[adapters.github_issues]\nrepos = ["{REPO}"]\n'
        f'api_url = "{gh.url}"\ntoken_env = "GH_ISSUES"\n'
    )
    checks = {c.name: c for c in run_checks(config, prometheus="http://127.0.0.1:9", environ=ENV)}
    assert checks["config loads"].status == "PASS"
    assert checks[doctor.TOKEN].status == "PASS"
    unset = {c.name: c for c in run_checks(config, prometheus="http://127.0.0.1:9", environ={})}
    assert unset["config loads"].status == "FAIL" and "$GH_ISSUES" in unset["config loads"].detail
