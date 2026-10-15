"""GitHub connector: PR state and CI status, block reasons turned into conditions, and
GitHub Actions runs for rule 9 with the opt-in ``cancel_build`` action. See ``client`` for
the security model and ``builds`` for the guards on the one write."""

from kelpie.connectors.github.blocks import (
    BlockConditionPattern,
    ConditionSync,
    load_block_conditions,
)
from kelpie.connectors.github.builds import CiSettings, GitHubCI, load_ci
from kelpie.connectors.github.client import GitHubClient, GitHubError
from kelpie.connectors.github.connector import GitHubConnector, PrRef, parse_pr_ref
from kelpie.connectors.github.settings import GitHubSettings, load_github

__all__ = [
    "BlockConditionPattern",
    "CiSettings",
    "ConditionSync",
    "GitHubCI",
    "GitHubClient",
    "GitHubConnector",
    "GitHubError",
    "GitHubSettings",
    "PrRef",
    "load_block_conditions",
    "load_ci",
    "load_github",
    "parse_pr_ref",
]
