"""GitHub connector: read-only PR state and CI status, and block reasons turned into
conditions. See ``client`` for the security model."""

from kelpie.connectors.github.blocks import (
    BlockConditionPattern,
    ConditionSync,
    load_block_conditions,
)
from kelpie.connectors.github.client import GitHubClient, GitHubError
from kelpie.connectors.github.connector import GitHubConnector, PrRef, parse_pr_ref
from kelpie.connectors.github.settings import GitHubSettings, load_github

__all__ = [
    "BlockConditionPattern",
    "ConditionSync",
    "GitHubClient",
    "GitHubConnector",
    "GitHubError",
    "GitHubSettings",
    "PrRef",
    "load_block_conditions",
    "load_github",
    "parse_pr_ref",
]
