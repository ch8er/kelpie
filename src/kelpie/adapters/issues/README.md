# Issues adapter (GitHub Issues; Linear next)

For farms whose task queue is an issue tracker rather than a platform's own board: one adapter
covers every agent that takes an issue and returns a pull request. Adapter name
`github-issues`, section [`[adapters.github_issues]`](../../../../docs/configuration.md#adaptersgithub_issues).

| Agent | Started by | Seen on the issue as |
| --- | --- | --- |
| GitHub Copilot coding agent | assigning the issue to Copilot | assignee `Copilot`; its draft pull request (`copilot/…`, author `copilot-swe-agent[bot]`) says `Fixes #n` |
| Claude Code GitHub Action | `assignee_trigger`, `label_trigger`, or `@claude` | the assignee or label you configured; otherwise its pull request |
| OpenHands resolver | the `fix-me` label, or `@openhands-agent` | the label; its draft pull request |
| Codex cloud | `@codex` in a comment | its pull request (`author`) |

Agents run on someone else's machines, so the adapter has tasks and owners but no sessions or
spans: the session rules (1, 10, 11) have nothing to read here.

## Mapping

Tasks are issues (`owner/repo#12`); pull requests in the issue lists are left out. With
`label`, only issues carrying it.

| Issue | Status | Since |
| --- | --- | --- |
| closed as completed | `done` | closed |
| closed as not planned / duplicate | `cancelled` | closed |
| open, a `blocked_labels` label | `blocked` (block reason `label '<name>'`) | the label was put on |
| open, a linked ready pull request | `review` | the pull request was opened (not when a draft was marked ready: GitHub's list does not say) |
| open, a linked draft pull request, or someone holds it | `in_progress` | the agent or person was assigned / labelled, else the pull request was opened |
| open, a `ready_labels` label, no open blocker off the boards | `ready` | created, reopened, or last unassigned |
| open | `backlog` | same |

People start these agents, not a dispatcher, so an issue nobody holds is `backlog`: only a
ready label says it waits for an agent (and only then do rule 6's idle-agent checks count
it). Tasks and agents default to the role `issue-agent`, apart from other adapters' roles.

- **Linked pull request**: an open pull request of the same repository whose description
  closes the issue (`Fixes #12`, `closes acme/app#12`, `fixes https://github.com/acme/app/issues/12`:
  GitHub's closing keywords). That is
  GitHub's own link: it closes the issue on merge. Mentions never link (incident 17); forks
  link nothing. Its head branch becomes the task's `branch`, so Kelpie's pull request lists
  ([connectors.github.pulls]) link the pull request to the task too.
- **Owner**, first match: an agent assigned (`assignee`), an agent's `label`, the agent whose
  account (`author`) opened the linked pull request, a person assigned, the person (or bot)
  who opened the linked pull request (a contributor's draft is not an orphan; a `[bot]`
  account such as Dependabot is an agent owner, not a person).
  An agent's account is that agent on every board; a board's `agents` only limits which
  agents Kelpie may hand its tasks to. An agent seen only by `author` is timed from its pull
  request, never from unrelated label or assignee events.
- **Dependencies**: GitHub's issue dependencies. An issue blocked by open issues depends on
  them. Closed blockers are met and left out. An open blocker that is not a task here, or
  blockers GitHub counts but Kelpie could not list, keep a ready-labelled issue in `backlog`
  (no rule would hold it back otherwise).
- **Subtasks**: GitHub's sub-issues, kept only when every open sub-issue is a task here
  (another repository's, or one without the board `label`, would read as finished and make
  the parent look done).
- `body` is the issue's description (8000 characters); `body_author` its author when an owner,
  member or collaborator wrote it (not a bot, an agent's account or a stranger: rule 1 reads
  the body as a person's acceptance criteria); `reopened_at` from reopen events;
  `platform_status` says what was seen (`open, draft pull request #7`).
- **Times without events** (not read yet, the budget spent, a read failed): the earlier of the
  issue's last change and when Kelpie first saw it as it is now, so a comment does not restart
  a stuck task's clock.

## Reading GitHub

Through the GitHub connector's client: a cache with conditional requests (`ETag`; an
unchanged answer costs nothing against the rate limit), back-off on rate limits and
secondary limits, a per-tick request and time budget, no redirects. Per repository and tick:

- every repository's lists first: open issues newest first (pages of 50, at most 10), closed
  issues updated within `lookback` (1 page), open pull requests (pages of 10, at most 30,
  newest first: their descriptions can be 64K characters);
- then per issue, only when needed and only within `max_requests_per_tick` and
  `tick_budget` (the lists are outside both, bounded by their page caps and each request's
  `timeout`, so a board is never cut for budget): events (open issues
  with labels or assignees; up to 3 pages, oldest first), blockers (when GitHub counts any),
  sub-issues (when it has some). These are read again only when the issue changed, or after
  10 minutes.

When a repository's lists cannot be read as GitHub has them now (an error, a back-off, the
budget spent: the client's old cached copy does not count), its last good read is used for
up to 10 minutes, so its tasks do not vanish and come back with every hiccup (that would
resolve and reopen their alerts); after that the repository is left out, and with no
repository readable the snapshot fails. `kelpie status` says which and why. When the pull
requests cannot be read, issues are read without links and `kelpie status` says so. A detail
that cannot be read falls back (see times above).

**Hostile input.** Anyone who can open an issue or a pull request writes what the adapter
reads. Values are checked and bounded: numbers, `owner/repo` of linked issues, logins,
labels (printable, 50 characters, 100 per issue), branches, titles (300 characters, control
characters replaced); the `Fixes #n` search runs in linear time and links at most 20 issues
per pull request. On a public repository set `label` to one only maintainers apply.

The lists draw on the same rate limit as `[connectors.github]` when they share a token (5000
requests an hour; unchanged pages cost nothing with a token, every request counts without
one). Busy repositories whose pages change every minute use the most; a separate token
(`token_env`) keeps them apart.

The token needs read access to issues and pull requests (a fine-grained token: Issues and Pull
requests, read; Metadata).

## Actions

None yet: `requeue`, `unblock`, `comment` and `reassign` are refused, so a rule in `act` mode
alerts a human instead. They come next, through the issues API.

## Limits and unverified

- **Unverified against live agents**: the assignee login a Copilot assignment shows
  (`Copilot`), its pull requests' author (`copilot-swe-agent[bot]`), and the accounts Codex
  and OpenHands open pull requests from. Logins compare without case and without `[bot]`.
- An issue with more than 300 events is timed from its first 300; `reopened_at` comes from
  events, read only for open issues with labels or assignees.
- A page of a list is cached on its own: an issue closing while a list is read can shift it
  and hide another for a tick.
- `[hub]` farms: like the other adapters it does not run inside `kelpie serve` with `[hub]`;
  it runs under `kelpie agent` on one host.
- It is the first adapter that imports a connector (`kelpie.connectors.github.client`, to
  share its HTTP client); import-linter allows adapters to depend on connectors, never the
  reverse.
