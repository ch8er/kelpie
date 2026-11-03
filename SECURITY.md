# Security Policy

## Supported versions

Kelpie is **pre-alpha**. Only the `main` branch is supported; there are no maintained
release branches yet, and fixes land on `main` only.

| Version | Supported |
|---------|-----------|
| `main`  | Yes       |
| anything else | No  |

## Reporting a vulnerability

**Please do not open a public issue, discussion or pull request for security problems.**

Report privately through GitHub Security Advisories (private vulnerability reporting):

1. Go to <https://github.com/ch8er/kelpie/security/advisories/new>
   (repository **Security** tab, then **Report a vulnerability**).
2. Describe the issue, affected component and commit, reproduction steps, and the impact
   you expect.

We aim to acknowledge reports within 7 days and to agree on a disclosure timeline with you.
Credit is given in the advisory unless you prefer otherwise.

## Scope notes

Kelpie is an automation service with write access to the systems it supervises, so
please keep these properties in mind when assessing impact:

- **It holds credentials.** Typical deployments give Kelpie tokens for agent platforms
  and issue trackers (GitHub Issues, Linear), GitHub (PRs, CI status), LLM provider APIs
  (quota checks, cost reports with an admin key), Telegram and Slack (alerts), the OTLP
  ingest and `kelpie agent` tokens, and optionally Langfuse. Leaking any of them through
  logs, reports, traces, `/metrics` or error messages is in scope. How Kelpie keeps them:
  [docs/security.md](docs/security.md).
- **It can cancel CI runs.** With `[connectors.github.ci] allow_cancel` (off by default)
  Kelpie cancels superseded GitHub Actions runs in the `cancel_repos` allow-list. It needs
  `Actions: write`, which should be granted to a separate token (`cancel_token_env`) scoped
  to those repositories only, so the token that reads PRs and CI stays read-only. Anything
  that makes Kelpie cancel a run outside that policy, or one that was not superseded, is in
  scope.
- **It writes to issues.** With `[adapters.github_issues]` or `[adapters.linear]` and a
  rule in `act` mode, Kelpie changes labels, assignees and comments of the issues it reads,
  reopens a closed issue to requeue it, and on Linear also sets an issue's workflow state and
  delegate, with the standing of the token's account. Those and cancelling runs are its only
  writes to GitHub.
- **It acts on task boards and sessions.** Kelpie can requeue, unblock, wake, park,
  close, comment on, reassign and WIP-commit tasks, stop agent sessions (through the platform's
  CLI, a stop marker its hook enforces or aborting the run in flight (OpenClaw), ending the stopped run's worker on Hermes), and
  cancel superseded CI runs (above). Anything that lets an untrusted party trigger or forge
  those actions (for example spoofed platform events, hook spool lines or OTLP spans,
  crafted condition payloads, or a forged `kelpie agent` report) is in scope.
- **It takes input from chat.** With `buttons = true` on a Telegram channel, an allowed
  user's button press snoozes a task. Anything that lets someone else snooze, or a press
  do more than snooze, is in scope.
- **It ingests untrusted text.** Task titles, comments, tool outputs and trace attributes
  are produced by agents and may contain attacker-controlled content. Injection into
  Slack messages, the HTML/Markdown report, SQL storage, or (when enabled) prompts for the
  optional report LLM is in scope.
- **Network exposure.** The OTLP ingest endpoint, the hub that `kelpie agent` reports to,
  `/metrics` and `/healthz` should not be exposed to the internet without authentication.
  Missing or bypassable authentication on these endpoints is in scope.
- **Out of scope:** vulnerabilities in the agent platforms themselves, Prometheus, Grafana,
  node_exporter or Langfuse (report them upstream), and issues that require an attacker
  who already controls the Kelpie host.
