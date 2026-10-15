"""``kelpie`` command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from collections.abc import Sequence

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

    from kelpie.server import serve as run

    run(app, args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
