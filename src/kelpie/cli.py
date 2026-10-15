"""``kelpie`` command-line entry point."""

from __future__ import annotations

import argparse
import logging
import os
from collections.abc import Sequence

from kelpie import __version__


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kelpie", description=__doc__)
    parser.add_argument("--version", action="version", version=f"kelpie {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the control loop and HTTP endpoints")
    serve.add_argument("--host", default=os.environ.get("KELPIE_HOST", "0.0.0.0"))
    serve.add_argument("--port", type=int, default=int(os.environ.get("KELPIE_PORT", "9464")))
    serve.add_argument("--log-level", default=os.environ.get("KELPIE_LOG_LEVEL", "INFO"))

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(), format="%(asctime)s %(levelname)s %(message)s"
    )

    if args.command == "serve":
        from kelpie.server import serve as run

        run(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
