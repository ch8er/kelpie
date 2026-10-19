# Quickstart: Kelpie on a farm of issue agents (GitHub Issues)

For agents that take a GitHub issue and return a pull request: the GitHub Copilot coding
agent, Codex cloud, the Claude Code GitHub Action, OpenHands. Kelpie reads the issues as
tasks (who holds each, since when, what blocks it, its pull request) and runs its rules in
`observe` mode with global dry-run on: it changes nothing. Step 6 lets one rule write.

These agents run on someone else's machines: Kelpie sees no sessions or traces of them, so
the session rules (loop breaker, exit contract's requeues) have nothing to read here. What
it sees comes from the issues and their pull requests. The status and owner rules are in the
[adapter README](../src/kelpie/adapters/issues/README.md).

Things marked **unverified** have not been tried against live agents.

## 1. Install

Kelpie runs anywhere that can reach GitHub's API. Python 3.12 and
[uv](https://docs.astral.sh/uv/):

```bash
git clone https://github.com/ch8er/kelpie.git
cd kelpie
uv sync
```

## 2. A token

A **fine-grained** personal access token, resource owner the organization, **only select
repositories** (the ones the agents work), with an expiry: **Issues: read**, **Pull
requests: read** and Metadata to observe; add **Issues: write** for step 6, nothing else
(Kelpie writes only labels, assignees and comments of issues). Not a classic token: its
`repo` scope can push code to every repository you can. Keep it in an environment variable,
never in `kelpie.toml`:

```bash
export KELPIE_ISSUES_TOKEN=github_pat_...
```

The token's account is who Kelpie's labels, assignments and comments come from, with that
account's standing (an agent that trusts maintainers' comments trusts Kelpie's). Prefer a
dedicated machine account with the lowest role the agents' triggers accept (triage), not a
maintainer's own token. A GitHub Actions `GITHUB_TOKEN` starts no workflow, so Kelpie cannot
restart an Action-based agent with one. **Unverified**: whether assigning Copilot through the
REST API with such a token starts it; do not widen the token's permissions on a guess.

## 3. Config

Create `kelpie.toml` in the repository directory (or point `KELPIE_CONFIG` at it). Name your
agents by how they are started on an issue:

```toml
[engine]
dry_run = true            # nothing leaves Kelpie
default_mode = "observe"  # no rule suggests or acts

[adapters.github_issues]
repos = ["your-org/your-repo"]
token_env = "KELPIE_ISSUES_TOKEN"
label = "agent"               # only issues with this label are tasks (set one on public repos)
ready_labels = ["agent ready"] # an issue nobody holds with one waits for an agent
blocked_labels = ["blocked"]

[adapters.github_issues.agents.copilot]
assignee = "Copilot"                # assigned to start it
author = "copilot-swe-agent[bot]"   # the account its pull requests come from

[adapters.github_issues.agents.openhands]
label = "fix-me"                    # labelled to start it

[adapters.github_issues.agents.codex]
mention = "@codex"                  # a comment starts it...
author = "chatgpt-codex-connector[bot]"  # ...and its pull requests show it (unverified login)
```

If you already have `[connectors.github]`, `repos`, `api_url` and `token_env` default to it.
Every key: [configuration](configuration.md#adaptersgithub_issues).

## 4. Run and check

```bash
uv run kelpie serve                # second shell
uv run kelpie doctor               # PASS|WARN|FAIL|SKIP per check; exit 1 on FAIL
uv run kelpie status               # rules, their mode, the tracker, boards and agents
```

What `kelpie doctor` checks for GitHub Issues (GETs only):

- **token**: the variable is set: a FAIL when `token_env` names one that is not set (as in
  step 3), a WARN when no `token_env` is configured and Kelpie would read anonymously (60
  requests an hour, public repositories only).
- **repositories**: each answers, and the account's role there: a WARN when it cannot label,
  assign or comment, or when it administers the repository (Kelpie's changes would carry
  that standing). What a fine-grained token itself may do cannot be read with GETs.
- **labels**: every label the config names exists in its repository (a misspelt label never
  matches); names with spaces or symbols are listed as not checked.
- **read**: one read of the issues as `kelpie serve` makes it: how many tasks, how many held,
  and what it could not read.

`kelpie status` shows the tracker, the boards and the agents. What a read of the issues finds
and misses (a repository served from its last read, pull requests not read) is doctor's
**read** check; a read that fails entirely is also logged by `kelpie serve` on every tick.

## 5. Watch (still observe)

After a day, the daily report and the alerts say what the rules saw on the issues:

- acceptance criteria a person wrote into an issue that nothing shows were met when its pull
  request lands (rule 1, with `acceptance_patterns`);
- issues carrying a blocked label for hours with no condition that would clear them (rule 4
  alerts on such blocks; GitHub's "blocked by" dependencies are dependencies, not blocks);
- ready-labelled issues waiting while agents are idle (rule 6);
- an issue in progress or in review that nobody holds (rule 3; rare, since an assignee or the
  author of its pull request holds it).

## 6. Let one rule write

Follow the [act checklist](act-checklist.md). On issues, a rule in `act` mode writes through
the issues API, only on tasks of the last read, at most 10 writes a tick and 300 an hour:

```toml
[engine]
dry_run = false

[engine.rules.exit_contract]
mode = "act"
acceptance_patterns = ['\b(\d+)\s*(?:of|/)\s*\1\b']   # "20 of 20", "20/20"
```

With this, each acceptance criterion a person (owner, member or collaborator) states in an
issue an agent works on is announced on the issue once: a comment with its reference
(`AC-1a2b3c`), asking for the evidence before it lands. Kelpie's comments quote the rule's
text in a code block with mentions and slash commands broken, so they start no agent and
ping nobody; they are as public as the repository.

Other actions on issues: `unblock` (blocked labels off, when rule 4 has a condition linked to
the block), `reassign` (rule 3), `requeue` (restart the agent holding an issue; no rule uses
it on issues yet). See the adapter README, "Actions".

## Known limits

- No sessions or traces: loop and budget stops do not apply to cloud agents.
- Per repository, the newest 500 open issues and 300 open pull requests are read.
- The agents' logins (`Copilot`, `copilot-swe-agent[bot]`, Codex's and OpenHands' accounts)
  are **unverified**: check them on one of your own pull requests and set `assignee` and
  `author` to what GitHub shows.
- `kelpie serve` with `[hub]` does not run in-process adapters: run this one under
  `kelpie agent`.
