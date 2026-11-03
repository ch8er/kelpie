# Security

How Kelpie handles the secrets it holds and the endpoints it opens. How to report a
vulnerability: [SECURITY.md](../SECURITY.md). What Kelpie stores from the farm:
[privacy.md](privacy.md).

This page grows with the whole-codebase security audit before the first public release
([roadmap](roadmap.md), "project: whole-codebase security audit"), which adds the threat
model and what Kelpie trusts. Until then it covers the parts already settled.

## Secrets Kelpie holds

Tokens come from environment variables whose names the config holds (`token_env`,
`webhook_env`, `headers_env`, `admin_key_env` and the like) or, for the traces token, a
token file; the config holds names, not values. Where Kelpie says which secret it uses
(logs, `kelpie status`, `kelpie doctor`, error messages) it names the variable or the file.
The audit checks that no value reaches logs, `/metrics`, errors or reports.

Secrets never go to a proxy in clear: plain-http requests that carry one (GitHub and Linear
on loopback, `[[traces.forward]]` with `headers_env`, `kelpie agent` on a private network)
are sent directly, never through `http_proxy`; https requests may use `https_proxy` through
a CONNECT tunnel.

## Traces token

The OTLP ingest token (`[traces] token_env`, `KELPIE_TRACES_TOKEN`) is the one secret the
agents' side needs too: every exporter sends it. That makes it the one most likely to end
up copied into many files, or printed where an agent can see it. Keep it rotatable:

- **One copy.** Set `[traces] token_file` to an env file (mode 600) that holds
  `KELPIE_TRACES_TOKEN=<token>`, read by `kelpie serve`, and on the agents' side read the
  token from there (into the exporter's header variable) rather than writing it into each
  profile's config, where that is safe
  ([quickstart-hermes.md](quickstart-hermes.md#4-otlp-traces-optional)).
- **Rotation without dropped spans.** `kelpie traces rotate-token` writes a new token to
  the file and keeps the old one valid for `[traces] token_grace` (default 2h), so sessions
  that started with it keep reporting while new ones start with the new token. The old
  token is kept only as a SHA-256 hash with its expiry, never in clear. `kelpie serve` reads
  the file again within a second, without a restart. The command prints no token.
- **Revoking at once.** `kelpie traces rotate-token --grace 0s` writes a new token and
  accepts no previous one, including tokens still in an earlier grace period. Deleting or
  breaking the file revokes nothing: `kelpie serve` keeps the last tokens it read (and logs
  that it cannot read the file) until it restarts.
- **Who may write it.** The command runs only as the user who owns the file's folder
  (the user `kelpie serve` runs as; root only in a folder root owns), refuses a symlink,
  another user's file and a folder others may write to, and writes mode 600, replaced
  atomically under a lock, after checking the file was not swapped since it was read. So
  it cannot be led to write a file someone else chose. `kelpie serve` trusts only such a
  file: a regular file of its own user (or root), not writable by others, in a folder not
  writable by others; whoever could write it could choose the token.
  The folders above it count too: every folder of its real path must belong to that user
  or root and be closed to others' writes (a sticky folder such as `/tmp` excepted), so
  nobody else can swap a folder on the way for a link elsewhere; a symlink on the way is
  followed only when that user or root owns it (another user's link in `/tmp` is not).
  `kelpie_traces_token_file_ok` on `/metrics` is 0 when serve keeps old tokens because it
  cannot read or trust the file: a rotation or revocation has not taken effect. A setup
  where root owns the file and serve reads it through its group (640) cannot be rotated:
  the rotation writes mode 600. Give the file to the user serve runs as.
- **Other lines.** Lines of the file other than the token's are kept as they are (minus
  commented-out copies of the token lines): keep nothing else secret there.
- **Who still uses the old one.** `kelpie_traces_grace_requests_total` on `/metrics` counts
  requests that came with a previous token; `kelpie_traces_grace_tokens` how many are still
  accepted.
- **Stray copies.** `kelpie doctor` warns when the token is in a backup file (`*.bak*`,
  `*~`, `*.orig`, `*.old`, `*.save`, `*.tmp`) or a file others can read, under the token
  file's folder, the Hermes home or `~/.claude` (three levels deep, files up to 64 MiB,
  256 MiB in all), and lists the files that hold a copy, which a rotation must update.
  Older tokens are known only by their hash, so it also warns about backups with a
  `<token_env>=` line. It names files, never the token.
- **The agents' side.** Setting `OTEL_EXPORTER_OTLP_TRACES_HEADERS` once is convenient but
  goes to every OTLP exporter without headers of its own in that process and is inherited
  by the agents it starts; see [quickstart-hermes.md](quickstart-hermes.md#4-otlp-traces-optional)
  for when to prefer per-backend headers.

When the token leaked (printed into a tool output, a log, a chat), rotate it, restart the
exporters so new sessions use the new token, delete the backups doctor lists, and watch
`kelpie_traces_grace_requests_total` stop growing before the grace period ends. If the
leaked token must stop working now, rotate with `--grace 0s` (running sessions then get
401 until they restart).

The comparison of a presented token takes constant time, for the current token and for the
hashes of previous ones.
