"""The simulated GitHub's issues: lists (filters, paging, pull requests among them) and the
writes the issues adapter makes, with GitHub's status codes."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from kelpie.sim.github import MAX_BODY, TOKEN, FakeGitHub, serve

REPO = "example/repo"
T0 = datetime(2026, 10, 19, 9, tzinfo=UTC)


@pytest.fixture
def gh() -> Iterator[FakeGitHub]:
    with serve() as fake:
        fake.issue(REPO, 1, "one", T0, labels=["agent ready", "Bug"], assignees=["Copilot"])
        fake.issue(REPO, 2, "two", T0 + timedelta(hours=1), labels=["bug"])
        fake.issue(REPO, 3, "three", T0 + timedelta(hours=2), state="closed")
        fake.pull(REPO, 4, created=T0 + timedelta(hours=3), description="Fixes #1")
        yield fake


def call(gh: FakeGitHub, method: str, path: str, body: Any = None, raw: bytes | None = None):  # type: ignore[no-untyped-def]
    data = raw if raw is not None else (None if body is None else json.dumps(body).encode())
    request = urllib.request.Request(gh.url + path, data=data, method=method)
    request.add_header("Authorization", f"Bearer {TOKEN}")
    try:
        with urllib.request.urlopen(request, timeout=10) as answer:
            return answer.status, json.loads(answer.read() or b"null")
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read() or b"null")


def numbers(answer: Any) -> list[int]:
    return [item["number"] for item in answer]


def test_lists_filter_page_and_hold_pull_requests(gh: FakeGitHub) -> None:
    status, listed = call(gh, "GET", f"/repos/{REPO}/issues?state=open&direction=desc")
    assert status == 200 and numbers(listed) == [4, 2, 1]  # a pull request is listed too
    assert "pull_request" in listed[0] and "pull_request" not in listed[1]
    _, spaced = call(gh, "GET", f"/repos/{REPO}/issues?state=open&labels=agent+ready")
    assert numbers(spaced) == [1]  # query values are decoded as GitHub does
    _, both = call(gh, "GET", f"/repos/{REPO}/issues?state=all&labels=bug,agent%20ready")
    assert numbers(both) == [1]  # every label, any case
    _, page2 = call(gh, "GET", f"/repos/{REPO}/issues?state=all&direction=asc&per_page=2&page=2")
    assert numbers(page2) == [3, 4]
    _, pulls = call(gh, "GET", f"/repos/{REPO}/pulls?state=open&per_page=1&page=2")
    assert pulls == []


def test_label_writes(gh: FakeGitHub) -> None:
    base = f"/repos/{REPO}/issues/2"
    assert call(gh, "POST", f"{base}/labels", {"labels": ["blocked"]})[0] == 200
    assert call(gh, "DELETE", f"{base}/labels/BLOCKED")[0] == 200  # any case
    assert call(gh, "DELETE", f"{base}/labels/blocked")[0] == 404  # gone
    status, labels = call(gh, "PUT", f"{base}/labels", {"labels": ["x"]})
    assert status == 200 and labels == [{"name": "x"}]
    kinds = [e["event"] for e in gh.repo(REPO).issues[2].events]
    assert kinds == ["labeled", "labeled", "unlabeled", "unlabeled", "labeled"]
    assert call(gh, "POST", f"{base}/labels", {"labels": "x"})[0] == 422


def test_assignee_writes(gh: FakeGitHub) -> None:
    base = f"/repos/{REPO}/issues/1"
    status, issue = call(gh, "DELETE", f"{base}/assignees", {"assignees": ["copilot"]})
    assert status == 200 and issue["assignees"] == []  # any case
    gh.unassignable.add("copilot")
    status, issue = call(gh, "POST", f"{base}/assignees", {"assignees": ["Copilot"]})
    assert status == 201 and issue["assignees"] == []  # dropped silently, as GitHub does
    gh.unassignable.clear()
    _, issue = call(gh, "POST", f"{base}/assignees", {"assignees": ["Copilot"]})
    assert [a["login"] for a in issue["assignees"]] == ["Copilot"]


def test_comments_reopen_and_bad_writes(gh: FakeGitHub) -> None:
    base = f"/repos/{REPO}/issues/3"
    assert call(gh, "POST", f"{base}/comments", {"body": "hi"})[0] == 201
    assert gh.repo(REPO).issues[3].comments[0]["body"] == "hi"
    status, issue = call(gh, "PATCH", base, {"state": "open", "state_reason": "reopened"})
    assert status == 200 and (issue["state"], issue["state_reason"]) == ("open", "reopened")
    assert call(gh, "POST", f"{base}/comments", {"body": ""})[0] == 422
    assert call(gh, "POST", f"{base}/comments", raw=b"{not json")[0] == 400
    assert call(gh, "POST", f"{base}/comments", raw=b"x" * (MAX_BODY + 1))[0] == 413
    assert call(gh, "POST", f"/repos/{REPO}/issues/99/comments", {"body": "x"})[0] == 404


def test_a_pull_request_read_as_an_issue(gh: FakeGitHub) -> None:
    status, answer = call(gh, "GET", f"/repos/{REPO}/issues/4")
    assert status == 200 and "pull_request" in answer
    assert call(gh, "GET", f"/repos/{REPO}/issues/4/events")[0] == 200
