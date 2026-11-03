# CI configuration proposal fixtures

One fixture per waste kind rule 9 (`ci_economy`) classifies, run by
`tests/connectors/test_ci_config_fixtures.py`: a snapshot whose builds waste minutes in that
one way, a GitHub Actions workflow file, and what Kelpie would propose for it.

| Fixture | `waste` | Change the rule names | Rendered for GitHub Actions |
| --- | --- | --- | --- |
| `superseded-pushes` | `superseded` | `cancel_in_progress` | a top-level `concurrency` group with `cancel-in-progress: true` |
| `docs-only` | `docs_only` | `ignore_docs_paths` | `paths-ignore` with the docs globs on `push` and `pull_request` |
| `drafts` | `draft` | `skip_drafts` | a draft condition on the job, `ready_for_review` among the pull request types |
| `verified-commits` | `already_verified` | `skip_verified` | left out: GitHub Actions has no such setting |
| `wip-commits` | `wip` | `skip_wip` | left out: GitHub expressions have no regular expressions |
| `all-kinds` | `all` | all five, most minutes first | the three that can be placed, in that order |

Each `<id>.yaml` holds `id`, `waste`, `description`, the rule's `config`, `default_branch`,
`snapshot` (format: [../incidents/README.md](../incidents/README.md); `builds_from` lists
fixtures whose builds it takes), the `workflow` text,
and `expected`: `changes` (in the rule's order), `applied` and `left_out` (`[change, why]`).
`<id>.diff` beside it is the exact unified diff, empty when nothing is placed. The test checks
the diff byte for byte, twice, so a change to the rule or the renderer that moves a line
shows here.

"Expensive jobs after a cheap required check failed" (plan, rule 9) is not a waste kind the
rule classifies yet, so it has no fixture; add one with the category.

All data is synthetic.
