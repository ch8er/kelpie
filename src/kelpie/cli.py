"""``kelpie`` command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence
from datetime import UTC, datetime

from kelpie import __version__
from kelpie.app import (
    ConfigError,
    Settings,
    build_adapters,
    build_app,
    check_exposure,
    find_config,
    load_rules,
    load_settings,
    read_budgets,
    read_models,
    read_unpriced,
    render_status,
)

log = logging.getLogger("kelpie.cli")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kelpie", description=__doc__)
    parser.add_argument("--version", action="version", version=f"kelpie {__version__}")
    parser.add_argument(
        "--config", help="TOML config (default: $KELPIE_CONFIG, else ./kelpie.toml if present)"
    )
    parser.add_argument("--log-level", default=os.environ.get("KELPIE_LOG_LEVEL", "INFO"))
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the control loop and HTTP endpoints")
    serve.add_argument("--host", default=os.environ.get("KELPIE_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("KELPIE_PORT", "9464")))
    sub.add_parser("status", help="show the rules kelpie serve would load, with their mode")
    report = sub.add_parser("report", help='the daily "cost of chaos" report as Markdown')
    report.add_argument("--date", help="YYYY-MM-DD (default: yesterday in [report] timezone)")
    report.add_argument(
        "--send", action="store_true", help="send the summary to [[alerts.channels]] too"
    )
    fix = sub.add_parser("fix", help="note a fix so the daily report verifies its effect")
    fix.add_argument("note", nargs="?", help="what changed, e.g. 'lifted the merge hold'")
    fix.add_argument("--rule", help="the rule whose problem it should reduce")
    fix.add_argument(
        "--command", dest="command_prefix", help="a command prefix whose output it filters"
    )
    fix.add_argument("--role", help="the role it applies to")
    fix.add_argument(
        "--at", help="when (ISO time; without an offset in [report] timezone; default: now)"
    )
    fix.add_argument(
        "--list", action="store_true", help="list the fixes recorded in the last --days instead"
    )
    fix.add_argument("--days", type=int, help="how far back --list looks (default 7)")
    why = sub.add_parser("why", help="explain why a task is not moving (read-only)")
    why.add_argument("task", help="the task id, as the board shows it")
    why.add_argument("--json", action="store_true", help="machine-readable output")
    why.add_argument(
        "--entries", type=int, default=10, help="latest ledger entries to show (default 10)"
    )
    why.add_argument(
        "--refresh",
        action="store_true",
        help="also read network connectors (GitHub) as a tick would; default: offline",
    )
    doctor = sub.add_parser(
        "doctor",
        help="check a first live run: config, rule modes, boards and task lists, hooks, spans, "
        "git, stop markers",
    )
    doctor.add_argument(
        "--prometheus",
        default=os.environ.get("KELPIE_PROMETHEUS_URL"),
        help="Prometheus base URL (default: $KELPIE_PROMETHEUS_URL; else localhost:9090 if up)",
    )

    sub.add_parser(
        "agent",
        help="on a machine with agents: report its boards to a central kelpie serve ([agent]) "
        "and carry out the actions it routes here",
    )
    demo = sub.add_parser(
        "demo",
        help="see Kelpie work on a simulated farm, offline (about a minute; no config needed)",
    )
    demo.add_argument("--fast", action="store_true", help="no pauses between lines")
    demo.add_argument(
        "--keep",
        action="store_true",
        help="keep the farm directory: its board, config, Kelpie's log and the full report",
    )
    demo.add_argument("--web", action="store_true", help="also open the day's report as a web page")

    spool = sub.add_parser("spool", help="maintain the hooks spools on this host")
    spool_sub = spool.add_subparsers(dest="spool_command", required=True)
    scrub = spool_sub.add_parser(
        "scrub",
        help="re-redact the commands in existing spools (secrets kept by older redaction)",
    )
    scrub.add_argument(
        "paths",
        nargs="*",
        help="spool files (default: the configured Hermes and Claude Code spools and their .1)",
    )
    scrub.add_argument("--dry-run", action="store_true", help="count, change nothing")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if args.command == "demo":
        from kelpie.demo import run as run_demo

        return run_demo(
            fast=args.fast, keep=args.keep, web=args.web, log_level=args.log_level.upper()
        )[0]
    if args.command == "doctor":
        from kelpie.doctor import run as run_doctor

        text, code = run_doctor(find_config(args.config), args.prometheus)
        sys.stdout.write(text)
        return code
    try:
        settings = load_settings(find_config(args.config))
        if args.command == "fix":
            from kelpie.report.cli import list_fixes, note_fix

            recording = [args.note, args.rule, args.command_prefix, args.role, args.at]
            if args.list and any(v is not None for v in recording):
                fix.error(
                    "--list cannot be combined with a note, --rule, --command, --role or --at"
                )
            if not args.list and args.days is not None:
                fix.error("--days only applies to --list")
            if not args.list and args.note is None:
                fix.error("a note is required unless --list is given")
            if args.list:
                text, code = list_fixes(settings, 7 if args.days is None else args.days)
            else:
                text, code = note_fix(
                    settings, args.note, args.rule, args.command_prefix, args.role, args.at
                )
            sys.stdout.write(text)
            return code
        if args.command == "report":
            from kelpie.report.cli import run as run_report

            out, err, code = run_report(settings, args.date, args.send)
            sys.stdout.write(out)
            sys.stderr.write(err)
            return code
        if args.command == "why":
            from kelpie.why import run as run_why

            out, err, code = run_why(
                settings, args.task, args.json, max(0, args.entries), refresh=args.refresh
            )
            sys.stdout.write(out)
            sys.stderr.write(err)
            return code
        if args.command == "spool":
            return _scrub(settings, args.paths, args.dry_run)
        if args.command == "agent":
            return _agent(settings)
        if args.command == "status":
            rules = load_rules(settings.engine)
            adapters = build_adapters(settings)
            budgets = read_budgets(settings, adapters)
            models = read_models(settings)
            unpriced = read_unpriced(settings, models=models)
            sys.stdout.write(
                render_status(settings, rules, adapters, budgets, unpriced=unpriced, models=models)
            )
            return 0
        load_rules(settings.engine)  # rule errors first: they are the likelier mistake
        check_exposure(settings, args.host)
        app = build_app(settings)
    except ConfigError as exc:
        print(f"kelpie: config error: {exc}", file=sys.stderr)
        return 2
    if settings.db_path is not None and settings.source is not None:
        from kelpie.report.fixes import record_config_change

        # The app is built from this config: the daily report verifies its effect (never
        # raises).
        record_config_change(settings.db_path, settings.source, datetime.now(UTC))
    from kelpie.report.cli import report_schedule

    if (schedule := report_schedule(settings, app.alerts)) is not None:
        app.services.append(schedule)  # [report] at: started with the loop, stopped first

    from kelpie.server import serve as run

    run(app, args.host, args.port)
    return 0


def _agent(settings: Settings) -> int:
    """``kelpie agent``: runs until SIGTERM or SIGINT. ConfigError when it cannot start."""
    import signal
    import threading

    from kelpie.app import utc_now
    from kelpie.remote.agent import Agent, tls_context
    from kelpie.remote.relay import Relay

    config = settings.agent
    if config is None:
        raise ConfigError("kelpie agent needs an [agent] section (name, server, token_env)")
    relay = None
    try:
        token = config.token()
        config.check_server()
        context = tls_context(config)
        agent = Agent(config, build_adapters(settings), token, clock=utc_now, context=context)
        if config.relay is not None:
            relay = Relay(
                config.relay, config.server, token, config.max_body_bytes, context=context
            )
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    except OSError as exc:
        host, port = config.relay or ("", 0)
        raise ConfigError(
            f"[agent] relay cannot listen on {host}:{port}: {exc} (another OTLP receiver "
            "there? set [agent] relay to another port, or relay = false)"
        ) from exc
    if config.plain_http:
        log.warning("[agent] reports over plain http: the token crosses the network in clear")

    def on_signal(signum: int, frame: object) -> None:
        log.info("received %s, stopping", signal.Signals(signum).name)
        agent.stop()

    if threading.current_thread() is threading.main_thread():
        signal.signal(signal.SIGTERM, on_signal)
        signal.signal(signal.SIGINT, on_signal)
    log.info(
        "kelpie agent %s reporting %s to %s",
        config.name,
        ", ".join(f"{name} ({', '.join(sorted(kinds))})" for name, kinds in agent.offered.items()),
        config.server,
    )
    if relay is not None:
        threading.Thread(target=relay.serve_forever, name="kelpie-relay", daemon=True).start()
        host, port = config.relay or ("", 0)
        log.info("OTLP relay on http://%s:%d/v1/traces", host, port)
    try:
        agent.run()
    finally:
        if relay is not None:
            relay.shutdown()
            relay.server_close()
    log.info("kelpie agent stopped")
    return 0


def _scrub(settings: Settings, paths: Sequence[str], dry_run: bool) -> int:
    """``kelpie spool scrub``: 0 when every spool was scrubbed, 1 otherwise."""
    from pathlib import Path

    from kelpie.traces.scrub import default_spools, scrub_file

    targets = [Path(p) for p in paths] or default_spools(
        settings.hermes.spool if settings.hermes is not None else None,
        settings.claude_code.spool if settings.claude_code is not None else None,
        settings.openclaw.spool if settings.openclaw is not None else None,
    )
    if not targets:
        print("kelpie: no spool found; pass the spool paths", file=sys.stderr)
        return 1
    code = 0
    for path in targets:
        try:
            print(scrub_file(path, dry_run=dry_run).render(dry_run))
        except OSError as exc:
            print(f"kelpie: {path}: {exc}", file=sys.stderr)
            code = 1
    return code


if __name__ == "__main__":
    raise SystemExit(main())
