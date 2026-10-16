# Deploying Kelpie on one machine or several

Kelpie runs in one of two shapes:

- **Single host** (the default): one `kelpie serve` on the machine where the agents run.
  It reads the boards and runs local actions in-process.
- **Multi-host:** a `kelpie agent` on every machine with agents, and one central
  `kelpie serve` with a `[hub]` section. Each agent reads its own machine's boards, hook
  spools, task lists and worktrees, and carries out the actions the server routes to it.
  The server keeps the rules, the ledger, alerts, the daily report and the connectors. How
  it works and why: [multi-host.md](multi-host.md).

Pick the case that matches your farm:

| Case | Shape | Where `kelpie serve` runs |
| --- | --- | --- |
| [Laptop only](#laptop-only) | single host | the laptop |
| [Laptop + cloud VM](#laptop--cloud-vm) | multi-host | the VM (it stays up when the laptop sleeps) |
| [Two VMs](#two-vms) | multi-host | either VM, or a third small machine |

Install Kelpie on every machine as in [install.md](install.md). Every machine must run the
**same Kelpie release**: agents and server refuse each other's reports otherwise, and say so
in the log.

## Laptop only

Nothing changes from [install.md](install.md) and the quickstarts
([Hermes](quickstart-hermes.md), [Claude Code](quickstart-claude-code.md)): `kelpie serve`
with `[adapters.*]` sections, on the laptop. Exporters send spans to
`http://127.0.0.1:9464/v1/traces`. Watch out: the watchdog sleeps with the laptop. When
that matters, move `kelpie serve` to a VM as in the next case.

## Laptop + cloud VM

Agents run on the laptop and on the VM. The server runs on the VM, so it keeps watching (and
alerting about the laptop going silent) when the laptop sleeps. The laptop connects out to
the VM. It needs no open port and may sit behind NAT.

```
laptop                          cloud VM
kelpie agent "laptop"  ──────▶  kelpie serve  (hub :9465, /metrics :9464)
                                kelpie agent "vm" ─▶ 127.0.0.1:9465
```

**1. Tokens.** Make one per agent, on any machine, and keep them like passwords:

```bash
openssl rand -hex 32   # for the laptop
openssl rand -hex 32   # for the VM's own agent
```

**2. TLS for the hub.** The laptop reaches the VM over the internet, so the hub serves HTTPS.

- If the VM has a DNS name, use a certificate for it (e.g. from your ACME client).
- Otherwise make a self-signed one and give the agents the certificate itself as `ca_file`:

```bash
openssl req -x509 -newkey rsa:2048 -nodes -days 825 \
  -subj "/CN=kelpie.example.internal" -addext "subjectAltName=DNS:kelpie.example.internal" \
  -keyout /etc/kelpie/tls.key -out /etc/kelpie/tls.crt
```

The name in the certificate must be the name the agents use in `server`; it is checked.

If both machines are on one private network you control (e.g. Tailscale), you may skip TLS
there. Set `allow_plain_http = true` on both sides and use the private address, as in
[Two VMs](#two-vms).

**3. The server, on the VM** (`/etc/kelpie/kelpie.toml`). It has no `[adapters.*]`
section: with a hub, every board comes through an agent. The server refuses to start with
both, so its own agents get an agent process like any other machine.

```toml
[storage]
path = "/var/lib/kelpie/kelpie.db"

[hub]
listen = "0.0.0.0:9465"
tls_cert = "/etc/kelpie/tls.crt"
tls_key = "/etc/kelpie/tls.key"

[[hub.agents]]
name = "laptop"
token_env = "KELPIE_AGENT_LAPTOP_TOKEN"

[[hub.agents]]
name = "vm"
token_env = "KELPIE_AGENT_VM_TOKEN"

# [engine], [alerts], [connectors.*], [[budgets]], [report] as in single-host mode.
```

```bash
export KELPIE_AGENT_LAPTOP_TOKEN=...   # the laptop's token
export KELPIE_AGENT_VM_TOKEN=...       # the VM agent's token
kelpie --config /etc/kelpie/kelpie.toml serve --host 127.0.0.1
```

`--host 127.0.0.1` keeps `/metrics` local to the VM (Prometheus on the VM scrapes it). Open
port 9465 in the VM's firewall to the laptop (or to the internet: everything on it needs a
token).

**4. The VM's own agent** (`/etc/kelpie/agent.toml`):

```toml
[agent]
name = "vm"
server = "https://kelpie.example.internal:9465"
ca_file = "/etc/kelpie/tls.crt"        # only for a self-signed certificate
token_env = "KELPIE_AGENT_TOKEN"

[adapters.hermes]                      # and/or [adapters.claude_code]
db_path = "/home/agents/.hermes/kanban.db"

[storage]
path = "/var/lib/kelpie-agent/agent.db"   # where the Hermes park key lives
```

```bash
KELPIE_AGENT_TOKEN=<the VM agent's token> kelpie --config /etc/kelpie/agent.toml agent
```

(`server = "https://127.0.0.1:9465"` would fail the certificate's name check; use the
certificate's name and, if needed, an `/etc/hosts` entry pointing it at 127.0.0.1.)

**5. The laptop's agent:** the same file with `name = "laptop"`, the laptop's adapter
paths and the laptop's token. Run it as the user that runs the agents, so it can read their
boards and run the `hermes` CLI.

**6. Traces.** Each agent runs an OTLP relay on `127.0.0.1:4318`. Point exporters on that
machine at `http://127.0.0.1:4318/v1/traces` without a token (Hermes' Langfuse plugin,
Claude Code's `OTEL_EXPORTER_OTLP_ENDPOINT=http://127.0.0.1:4318`). The relay forwards
the spans to the hub under the agent's token. With a hub, the main port's `/v1/traces` is
off: spans whose ids no agent token vouches for could be filed under any machine. If something
else already listens on 4318 (an OpenTelemetry collector), set `[agent] relay` to another
loopback port, or `relay = false` when the server has traces off.

**7. Resources.** node_exporter runs on both machines and Prometheus scrapes both, as in
single-host mode (Prometheus reaching the laptop needs a private network such as Tailscale;
without one, run node_exporter only where Prometheus can reach it). Name each agent like its
node_exporter instance in `[resources.prometheus] hosts`, so disk and memory alerts and
tasks name the same machine.

**8. Check.**

- `kelpie_agent_up{agent="laptop"}` and `{agent="vm"}` are 1 on the server's `/metrics`.
- Task ids in alerts read `laptop:t_1a`, `vm:t_2b`.
- Close the laptop: within about 2 minutes one alert says "agent laptop silent", and the
  laptop's tasks raise nothing else until it is back.

## Two VMs

Agents run on two VMs in one private network (one VPC, or Tailscale). The server runs on
either, or on a third small machine. That way it survives the agent VMs filling their disks
(incident 3), which is the point of a separate watchdog.

Inside a network you control, plain HTTP is allowed if both sides ask for it. The hub
listens on a private address (RFC 1918, `100.64.0.0/10` as Tailscale uses, IPv6 ULA; never
`0.0.0.0`), and the agent's server name resolves only to such addresses (checked at
startup):

```toml
# server (on vm-a, 10.0.1.10)
[hub]
listen = "10.0.1.10:9465"
allow_plain_http = true

[[hub.agents]]
name = "vm-a"
token_env = "KELPIE_AGENT_VM_A_TOKEN"

[[hub.agents]]
name = "vm-b"
token_env = "KELPIE_AGENT_VM_B_TOKEN"
```

```toml
# agent on vm-b
[agent]
name = "vm-b"
server = "http://10.0.1.10:9465"
allow_plain_http = true
token_env = "KELPIE_AGENT_TOKEN"

[adapters.hermes]
db_path = "/home/agents/.hermes/kanban.db"
```

vm-a runs its own `kelpie agent` (`name = "vm-a"`, same `server`). Tokens cross the
network in clear text over plain HTTP, and both sides log a warning about it. Use TLS (as in
the previous case) whenever anything else shares that network. Steps 6–8 of the previous
case apply as they are.

## Moving from single host to multi-host

Task, session and owner ids gain the agent's name as a prefix (`t_1a` becomes
`laptop:t_1a`), so the server sees a new farm:

- Open alert states resolve and new ones open once. Run the first minute with
  `[engine] dry_run = true` to skip that burst.
- Config that names tasks or owners needs the prefix: budget `match`, orphan
  `ignore_tasks`, ping-pong `escalate_to`, provider owners (`"laptop:WEB-*"`, or
  `"*:WEB-*"` for every machine).
- Copy the Hermes park key (`hermes-wait.key`, next to the old `kelpie.db`) into the agent's
  `[storage]` directory on the Hermes machine, or tasks Kelpie parked before the move are not
  recognised as its own.
- That day's daily report splits the spend at the switch.

## Limits of the first release

- `kelpie why` and `kelpie doctor` read local adapters. Run them on the agent's machine
  with the agent's config.
- Rule 12 (conflict warning) compares tasks on the same machine only.
- The plain-HTTP address check runs at startup. Use TLS when you do not control DNS.
- A restart of the server or an agent may repeat an action that was in flight. Stops, parks
  and wakes are idempotent; a comment may be posted twice.
