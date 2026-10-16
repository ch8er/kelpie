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
    build_adapters,
    build_app,
    check_exposure,
    find_config,
    load_rules,
    load_settings,
    read_budgets,
    render_status,
)


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
    fix.add_argument("--days", type=int, default=7, help="how far back --list looks (7)")
    doctor = sub.add_parser(
        "doctor", help="check a first live run: config, observe mode, board, hooks, spans, git"
    )
    doctor.add_argument(
        "--prometheus",
        default=os.environ.get("KELPIE_PROMETHEUS_URL"),
        help="Prometheus base URL (default: $KELPIE_PROMETHEUS_URL; else localhost:9090 if up)",
    )

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    if args.command == "doctor":
        from kelpie.doctor import run as run_doctor

        text, code = run_doctor(find_config(args.config), args.prometheus)
        sys.stdout.write(text)
        return code
    try:
        settings = load_settings(find_config(args.config))
        if args.command == "fix":
            from kelpie.report.cli import list_fixes, note_fix

            if args.list:
                text, code = list_fixes(settings, args.days)
            elif args.note is None:
                text, code = "kelpie: fix needs a note (or --list)\n", 2
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
        if args.command == "status":
            rules = load_rules(settings.engine)
            adapters = build_adapters(settings)
            budgets = read_budgets(settings, adapters)
            sys.stdout.write(render_status(settings, rules, adapters, budgets))
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

    from kelpie.server import serve as run

    run(app, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
