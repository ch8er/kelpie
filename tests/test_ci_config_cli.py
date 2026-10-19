"""``kelpie ci-config``: rule 9's proposals printed as diffs of the workflow files, read
with the read token; nothing is written."""

from __future__ import annotations

import base64
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from kelpie import ci_config
from kelpie.app import load_settings
from kelpie.core.model import Build, BuildStatus, Snapshot
from kelpie.why import SnapshotRead
from tests.connectors.fake_github import TOKEN, serve
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


def test_proposals_print_as_diffs_and_nothing_is_written(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with serve() as gh:
        gh.reply("/repos/example/app", {"default_branch": "main"})
        content = base64.b64encode(TYPICAL.encode()).decode()
        gh.reply(
            f"/repos/example/app/contents/{PATH}?ref=main",
            {"encoding": "base64", "content": content, "sha": "blob-1"},
        )
        config = tmp_path / "kelpie.toml"
        config.write_text(
            f'[connectors.github]\nrepos = ["example/app"]\napi_url = "{gh.url}"\n'
            'token_env = "GH_READ"\n[connectors.github.ci]\n'
        )
        monkeypatch.setenv("GH_READ", TOKEN)
        settings = load_settings(config)
        builds = drafts(4, 20.0)  # 80 min of drafts: over propose_min_minutes
        monkeypatch.setattr(
            ci_config,
            "read_snapshot",
            lambda s, clock, refresh: SnapshotRead(Snapshot(now=NOW, builds=builds), [], []),
        )
        out, err, code = ci_config.run(settings, lambda: NOW)
        assert code == 0 and err == ""
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


def test_it_needs_the_ci_connector(tmp_path: Path) -> None:
    config = tmp_path / "kelpie.toml"
    config.write_text("")
    _, err, code = ci_config.run(load_settings(config))
    assert code == 2 and "needs [connectors.github.ci]" in err
