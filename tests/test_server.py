import threading
import urllib.request

from kelpie.cli import main
from kelpie.server import make_server


def test_metrics_endpoint() -> None:
    server = make_server("127.0.0.1", 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/metrics") as resp:
            body = resp.read().decode()
        assert resp.status == 200
        assert "kelpie_build_info" in body
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz") as resp:
            assert resp.read() == b"ok\n"
    finally:
        server.shutdown()
        server.server_close()


def test_cli_version(capsys) -> None:
    try:
        main(["--version"])
    except SystemExit as exc:
        assert exc.code == 0
    assert "kelpie" in capsys.readouterr().out
