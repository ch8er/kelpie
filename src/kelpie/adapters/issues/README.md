# Issues adapter (GitHub Issues; Linear next)

For farms whose task queue is an issue tracker rather than a platform's own board: one adapter
covers every agent that takes an issue and returns a pull request. Adapter name
`github-issues`, section [`[adapters.github_issues]`](../../../../docs/configuration.md#adaptersgithub_issues).
First live run, step by step: [docs/quickstart-github-issues.md](../../../../docs/quickstart-github-issues.md).

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

Through the issues API, only on tasks of the last read (an issue of a configured repository
that carried the board's `label`): each action re-reads the issue first and refuses one that
is no task now. Writes go one at a time, at least a second apart (GitHub's guidance against
secondary rate limits), at most 10 a tick and 300 an hour; beyond that, and while GitHub asks
for a back-off, the action is deferred (not a failed attempt) and the engine asks again. A
write makes the next read of that repository ask GitHub rather than the cache.

| Action | On the issue |
| --- | --- |
| `comment` | a comment |
| `unblock` | the board's blocked labels come off, each by itself (`DELETE /labels/{name}`; a name that cannot be a path: the other labels are put back) |
| `requeue` | the task starts over: a closed issue is reopened (not one closed as not planned or duplicate), and the agent holding it restarted: unassigned and assigned again, its label removed and put back, or its mention posted again. No agent holds an open issue: refused |
| `reassign` | the new agent is assigned, labelled or mentioned first, then the one holding it is unassigned (or its label removed): a failure halfway leaves the issue held. Only an agent of this adapter the board allows, only on an open issue. A mention cannot be taken back |
| `wake`, `wip_commit`, `stop`, `wait`, `close` | refused: these agents run on someone else's machines, and Kelpie parks no issue task |

**What Kelpie writes.** Rule reasons can quote text anyone wrote (issue titles, block
reasons, labels), so a comment is Kelpie's own words (fixed text and names from the config)
followed by the reason in a code block, where mentions, `Fixes #n`, links and HTML do
nothing for GitHub. Bots that match the raw comment text (the Claude Code Action's `@claude`,
`/command` lines) would still see them, so every `@` in the reason is followed by an
invisible word joiner and every line is indented: a note starts no agent and pings nobody.
A mention that starts an agent carries fixed words only (`@codex please pick this issue up
again …`). Comments are signed `— Kelpie`. The
note after a change is best effort: when it cannot be posted the change stands, and the
engine does not repeat it.

What cannot be done is refused before anything changes (`UnsupportedAction`), so a human is
alerted instead. A write GitHub refuses fails the action with a reason a human can read
(`GitHub refused the change (http_error)`), and the engine retries it.

Every action holds all its writes before the first: one the caps cannot take whole is
deferred, never cut halfway. When a write fails after others were made, the error says what
state the issue was left in (`copilot was taken off … but not started again completely`).
GitHub answers an assignment with 201 also when it drops the login; the adapter checks the
issue it returns. A requeue restarts only an agent the issue still shows (or one seen through
its pull request): a person who unassigned it is not overruled.

**Which rules act here today.** The adapter has no sessions and parks no task, so the session
rules (1's requeues, checkpoints, wait_condition's comments) never target issue tasks. What
can: `stale_blocks` unblock (a condition linked to the block), the `once` comments
(acceptance criteria, conflict and return heads-ups) and, rarely, `orphan_status` reassign.
Notes quote rule reasons, which can name other tasks, files and sessions: on a public
repository they are public.

The token needs Issues: write (and Pull requests: read) on the repositories. Use a token of
an account the agents' own triggers accept: a GitHub Actions `GITHUB_TOKEN` starts no
workflow, so Kelpie could not restart an Action-based agent with it.

## Linear

`[adapters.linear]` (adapter name `linear`) reads a Linear workspace's teams through its
GraphQL API (`linear.py`) and maps them with the same rules. What differs:

- **Agents** integrated in Linear (Codex, Cursor, Copilot and others) are app users an issue is
  delegated to; people are assigned. Both count as the issue's assignees, so an agent's
  `assignee` is its app user's display name, and Kelpie delegates to an app user and assigns a
  person when it starts one. Linear has one assignee and one delegate: Kelpie replaces only an
  agent in either (handing the issue from one agent to another), never a person or an app
  that is none of the agents (the action fails instead). An agent's `author` is refused
  (Linear shows no pull request author); whether a `mention` in a comment made through the
  API starts an agent is **unverified**.
- **Workflow states**: an issue nobody holds is `ready` in a state the board names in
  `ready_states` (`["Todo"]` on a team whose Todo column is the agents' queue), else
  `backlog`, as on GitHub: people start these agents, so a person's Todo is not work for
  idle agents. A started state with nobody on it is `in_progress` with no owner: rule 3
  (`orphan_status`) may hand it to one of the board's agents when its `reassign` is on, so
  give a team where people work too a `label` or a board `agents` list. Completed /
  canceled / duplicate are `done` / `cancelled`.
- **Dependencies** are `blocks` relations, **subtasks** sub-issues, **pull requests** the GitHub
  pull requests Linear's GitHub integration attaches: attachments whose `sourceType` is
  `github`, on github.com or, with `pull_repos`, only in those repositories. A link anyone
  pastes does not count. The branch is not read (apps can write an attachment's metadata), so
  Linear tasks link no CI builds by branch. `status` and `draft` come from that metadata too
  (keys **unverified**): an app could hide a pull request or show it as a draft. Without
  `pull_repos` any github.com repository the integration sees counts, and pull requests of two
  repositories with the same number look alike: set `pull_repos` to the agents'
  repositories.
- **Who wrote the description**: the creator, if a member (not a guest or an app) and the
  issue was not made through an integration (`botActor`), and only while the history shows no
  description edit by a guest, an app (the agents among them), an actor Linear does not name
  or an integration (a GitHub or Slack sync). A description changed since it was created is trusted only once
  that history is read (within `max_requests_per_tick`), and not when it is longer than 50
  entries (the history query is kept small: Linear counts each entry's label lists). Untrusted, it gives the task no acceptance criteria.
- **Matching agents**: by display name, as on GitHub, and then by user id: an agent's name on
  any user but the one Kelpie looked up for it is not the agent. A slot held under a name
  Kelpie does not take (unprintable, with spaces around it, ending in `[bot]`, or an agent's
  name on another user) is held by someone unknown (`@unknown`), never free; so is an agent's
  own issue while its user cannot be looked up (the read reports it). Slots are compared by
  user id when Kelpie writes: it never replaces anyone but an agent. The config
  refuses agent names that could never match. Unassigning clears the slot that holds the
  name; writes go to the UUID of the issue the action checked.
- **Reading**: at most every `refresh` (2 minutes): GraphQL has no conditional requests, and
  Linear meters an API key by requests and query complexity per hour. Every team's lists
  first (pages of 25 issues, each query estimated well under Linear's 10,000-point limit;
  the closed issues are the most recently updated page), then history within
  `max_requests_per_tick`. A team that cannot be read is served from its last good read for
  10 minutes, then left out, and the problem is reported. Reads stop while less than 100
  requests or 50,000 complexity points are left of the hour (writes still go), until that
  limit's reset or for 5 minutes, then ask Linear again; the refresh stretches so reads take
  at most half the hour's complexity (Linear's `X-Complexity` and
  `X-RateLimit-Complexity-Limit` headers; several large teams read less often), to 25 minutes
  at most (under the 30-minute cooldown, so no rule acts twice on one stale read). A `RATELIMITED` answer, HTTP 429, or a limit used up backs off until that limit's
  reset. A plain-http `api_url` (loopback, tests) never goes through a proxy.
- **Writes**: `issueUpdate` with `addedLabelIds` / `removedLabelIds` changes only those labels
  (no read-then-replace), `assigneeId` / `delegateId`, `stateId` to reopen (the team's first
  Todo state, else its first Backlog state); `commentCreate`. Paced, capped and reserved per
  action like GitHub's. Every id the writes may need (the team's blocked and agent labels,
  the agents' users, the state to reopen to) is looked up when the action reserves its
  writes, so no lookup fails between two writes: one Linear cannot answer now defers the
  whole action. Ids are kept for an hour.
- **`kelpie doctor`** checks the API key, that every team exists (a misspelled key reads no
  issues and Linear says nothing), that the labels, `ready_states` and agents' users the
  config names exist, and one read. An action starting or taking off an agent whose label or
  user Linear lacks is refused before its first write. Labels are looked up in the issue's
  team and the workspace: a blocked label from another team (a parent team's, on a
  sub-team's issue) cannot be removed, and `unblock` fails without writing.

## Limits and unverified

- **Unverified against live agents**: the assignee login a Copilot assignment shows
  (`Copilot`), its pull requests' author (`copilot-swe-agent[bot]`), and the accounts Codex
  and OpenHands open pull requests from. Logins compare without case and without `[bot]`.
  Whether assigning Copilot through `POST /assignees` starts it (GitHub documents an
  assignment API for it; community reports say it needs a user token), whether unassigning
  stops it, and whether a GitHub Action listening to `labeled` restarts on a label put back
  within a second.
- Anyone who can label or assign can steer what Kelpie sees: putting an agent's label on an
  issue makes that agent its holder for Kelpie, whose requeue then puts the label back under
  its own account; toggling labels or assignees restarts a task's clock. The per-task action
  limits cap the effect; who applied a label is not checked.
- An issue with more than 300 events is timed from its first 300; `reopened_at` comes from
  events, read only for open issues with labels or assignees.
- A page of a list is cached on its own: an issue closing while a list is read can shift it
  and hide another for a tick.
- `[hub]` farms: like the other adapters it does not run inside `kelpie serve` with `[hub]`;
  it runs under `kelpie agent` on one host.
- It is the first adapter that imports a connector (`kelpie.connectors.github.client`, to
  share its HTTP client); import-linter allows adapters to depend on connectors, never the
  reverse.
