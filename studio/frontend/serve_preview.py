#!/usr/bin/env python3
"""Serve the static QuicData frontend with Vue Router history fallback and API proxy."""

import argparse
import urllib.error
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent


class SpaHandler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(ROOT), **kwargs)

    def _is_backend_proxy(self):
        path = urlparse(self.path).path
        return path == "/health" or path.startswith(("/api/", "/socket.io/"))

    def _proxy_request(self):
        backend_target = getattr(self.server, "backend_target", "http://127.0.0.1:8000")
        backend_url = f"{backend_target.rstrip('/')}{self.path}"
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length) if length > 0 else None

        req = urllib.request.Request(
            backend_url,
            data=body,
            method=self.command,
        )
        for key, value in self.headers.items():
            if key.lower() not in ("host", "connection", "content-length"):
                req.add_header(key, value)
        if body is not None:
            req.add_header("Content-Length", str(len(body)))

        try:
            with urllib.request.urlopen(req) as resp:  # nosec B310 - request is built from the local preview proxy
                status = resp.status
                headers = resp.getheaders()
                content = resp.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            headers = exc.headers.items()
            content = exc.read()
        except Exception as exc:
            self.send_error(502, f"Backend Gateway Error: {exc}")
            return

        self.send_response(status)
        for key, value in headers:
            if key.lower() not in (
                "transfer-encoding",
                "connection",
                "content-encoding",
                "content-length",
                "server",
                "date",
            ):
                self.send_header(key, value)
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def _apply_spa_fallback(self):
        path = urlparse(self.path).path
        requested = (ROOT / path.lstrip("/")).resolve()
        try:
            requested.relative_to(ROOT)
            is_asset = requested.is_file()
        except ValueError:
            is_asset = False
        if path != "/" and not is_asset:
            self.path = "/index.html"

    def do_GET(self):
        if getattr(self.server, "mock_mode", False):
            parsed = urlparse(self.path)
            if parsed.path in ("/", "/index.html") and not parsed.query:
                self.send_response(302)
                self.send_header("Location", "/?demo=1")
                self.send_header("Cache-Control", "no-store")
                # HTTP/1.1 keep-alive needs an explicit body length, otherwise
                # clients block waiting for a body that never arrives.
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
        if self._is_backend_proxy():
            return self._proxy_request()
        self._apply_spa_fallback()
        super().do_GET()

    def do_HEAD(self):
        if self._is_backend_proxy():
            return self._proxy_request()
        self._apply_spa_fallback()
        super().do_HEAD()

    def do_POST(self):
        if self._is_backend_proxy():
            return self._proxy_request()
        self.send_error(405, "Method Not Allowed")

    def do_PUT(self):
        if self._is_backend_proxy():
            return self._proxy_request()
        self.send_error(405, "Method Not Allowed")

    def do_PATCH(self):
        if self._is_backend_proxy():
            return self._proxy_request()
        self.send_error(405, "Method Not Allowed")

    def do_DELETE(self):
        if self._is_backend_proxy():
            return self._proxy_request()
        self.send_error(405, "Method Not Allowed")

    def do_OPTIONS(self):
        if self._is_backend_proxy():
            return self._proxy_request()
        self.send_error(405, "Method Not Allowed")

    def end_headers(self):
        if not self._is_backend_proxy():
            self.send_header("Cache-Control", "no-store")
        super().end_headers()


def main():
    parser = argparse.ArgumentParser(description="QuicData local SPA preview server")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8080, type=int)
    parser.add_argument("--backend", default="http://127.0.0.1:8000", help="Backend API target")
    parser.add_argument(
        "--mock",
        "--demo",
        dest="mock",
        action="store_true",
        help="Start frontend directly in mock mode",
    )
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), SpaHandler)
    server.backend_target = args.backend
    server.mock_mode = args.mock
    if args.mock:
        print(f"QuicData mock preview: http://{args.host}:{args.port}/?demo=1")
    else:
        print(
            f"QuicData preview: http://{args.host}:{args.port}/ (proxying /api/v1 to {args.backend})"
        )
    server.serve_forever()


if __name__ == "__main__":
    main()
