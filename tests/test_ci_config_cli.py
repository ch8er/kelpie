"""``kelpie ci-config``: rule 9's proposals printed as diffs of the workflow files, read
with the read token; nothing is written."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie import ci_config
from kelpie.app import Settings, load_settings
from kelpie.core.model import Build, BuildStatus, Snapshot
from kelpie.why import SnapshotRead
from tests.connectors.fake_github import TOKEN, FakeGitHub, serve
from tests.connectors.test_workflow_config import TYPICAL

NOW = datetime(2026, 3, 4, 12, tzinfo=UTC)
PATH = ".github/workflows/test.yml"


def drafts(n: int, minutes: float) -> tuple[Build, ...]:
    return tuple(
        Build(
            f"G-{i}",
            source="github_ci",
            repo="example/app",
            workflow=PATH,
            sha=f"{i}" * 7,
            event="pull_request",
            draft=True,
            status=BuildStatus.SUCCESS,
            started_at=NOW - timedelta(hours=2 + i),
            finished_at=NOW - timedelta(hours=1 + i),
            minutes=minutes,
        )
        for i in range(n)
    )


def settings_for(tmp_path: Path, url: str, extra: str = "") -> Settings:
    config = tmp_path / "kelpie.toml"
    config.write_text(
        f'[connectors.github]\nrepos = ["example/app"]\napi_url = "{url}"\n'
        f'token_env = "GH_READ"\n[connectors.github.ci]\n{extra}'
    )
    return load_settings(config)


def workflow(gh: FakeGitHub, text: str = TYPICAL) -> None:
    gh.reply("/repos/example/app", {"default_branch": "main"})
    gh.reply(
        f"/repos/example/app/contents/{PATH}?ref=main",
        {"encoding": "base64", "content": base64.b64encode(text.encode()).decode(), "sha": "b"},
    )


def test_proposals_print_as_diffs_and_nothing_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve() as gh:
        workflow(gh)
        monkeypatch.setenv("GH_READ", TOKEN)
        monkeypatch.setenv("GH_PROPOSE", "ghp_SyntheticProposeToken0123456789")
        settings = settings_for(
            tmp_path,
            gh.url,
            'allow_propose = true\npropose_repos = ["example/app"]\n'
            'propose_token_env = "GH_PROPOSE"\n',
        )
        builds = drafts(4, 20.0)  # 80 min of drafts: over propose_min_minutes
        read_with: list[Settings] = []

        def read(s: Settings, clock: object, refresh: bool) -> SnapshotRead:
            read_with.append(s)
            return SnapshotRead(Snapshot(now=NOW, builds=builds), [], [])

        monkeypatch.setattr(ci_config, "read_snapshot", read)
        out, err, code = ci_config.run(settings, lambda: NOW)
        assert code == 0 and err.startswith("warning: [engine.rules.ci_economy] propose_config")
        (ci,) = [s.github.ci for s in read_with if s.github]
        assert ci is not None and not ci.allow_propose and not ci.allow_cancel  # no write token
        assert f"# {PATH} in example/app:" in out
        assert f"--- a/{PATH}\n+++ b/{PATH}\n" in out
        assert "+    types: [opened, synchronize, reopened, ready_for_review]" in out
        assert all(s.method == "GET" for s in gh.requests)  # read-only
        monkeypatch.setattr(
            ci_config,
            "read_snapshot",
            lambda s, clock, refresh: SnapshotRead(Snapshot(now=NOW, builds=drafts(1, 5)), [], []),
        )
        out, _, code = ci_config.run(settings, lambda: NOW)
        assert code == 0 and out.startswith("No pipeline wasted enough minutes")


def test_what_the_repository_wrote_cannot_drive_the_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve() as gh:
        workflow(gh, TYPICAL.replace("[main]\n", "[main]  # \u202emain\u202c\n"))
        monkeypatch.setenv("GH_READ", TOKEN)
        settings = settings_for(tmp_path, gh.url)
        snap = SnapshotRead(Snapshot(now=NOW, builds=drafts(4, 20.0)), [], [])
        monkeypatch.setattr(ci_config, "read_snapshot", lambda s, clock, refresh: snap)
        out, _, code = ci_config.run(settings, lambda: NOW)
        assert code == 0 and "\u202e" not in out and "\u202c" not in out
        assert "# \N{REPLACEMENT CHARACTER}main\N{REPLACEMENT CHARACTER}" in out


def test_a_missing_read_token_cannot_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GH_READ", raising=False)
    _, err, code = ci_config.run(settings_for(tmp_path, "http://127.0.0.1:9"))
    assert code == 2 and "GH_READ" in err


def test_it_needs_the_ci_connector(tmp_path: Path) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text("")
    _, err, code = ci_config.run(load_settings(config))
    assert code == 2 and "needs [connectors.github.ci]" in err
