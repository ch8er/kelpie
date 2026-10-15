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

- **It holds credentials.** Typical deployments give Kelpie tokens for agent platforms,
  GitHub (PRs, CI status), LLM provider APIs (quota checks), Slack (alerts) and optionally
  Langfuse. Leaking any of them through logs, reports, traces, `/metrics` or error
  messages is in scope.
- **It acts on task boards.** Kelpie can requeue, unblock, comment on, reassign and
  WIP-commit tasks. Anything that lets an untrusted party trigger or forge those actions
  (for example spoofed platform events or OTLP spans, or crafted condition payloads) is
  in scope.
- **It ingests untrusted text.** Task titles, comments, tool outputs and trace attributes
  are produced by agents and may contain attacker-controlled content. Injection into
  Slack messages, the HTML/Markdown report, SQL storage, or (when enabled) prompts for the
  optional report LLM is in scope.
- **Network exposure.** The OTLP ingest endpoint, the HTTP API and `/metrics` should not be
  exposed to the internet without authentication. Missing or bypassable authentication on
  these endpoints is in scope.
- **Out of scope:** vulnerabilities in the agent platforms themselves, Prometheus, Grafana,
  node_exporter or Langfuse (report them upstream), and issues that require an attacker
  who already controls the Kelpie host.
