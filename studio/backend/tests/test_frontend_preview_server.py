"""The local preview server must emit self-terminating mock redirects."""

import http.client
import socket
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _wait_until_listening(port: int, deadline_seconds: float = 10) -> None:
    deadline = time.monotonic() + deadline_seconds
    while True:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                return
        except OSError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.05)


def test_mock_root_redirect_ends_without_waiting_for_a_body():
    port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "frontend/serve_preview.py",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--mock",
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
    )
    try:
        _wait_until_listening(port)
        connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
        connection.request("GET", "/")
        response = connection.getresponse()

        assert response.status == 302
        assert response.getheader("Location") == "/?demo=1"
        assert response.getheader("Content-Length") == "0"
        assert response.read() == b""
        connection.close()
    finally:
        process.terminate()
        process.wait(timeout=10)
