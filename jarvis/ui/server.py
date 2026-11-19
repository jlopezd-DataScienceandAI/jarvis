"""Local web server behind the HUD: the page, a live event stream, and two actions.

It listens on 127.0.0.1 only, but that is not enough on its own: any web page
open in the user's browser can send requests to localhost, and what this server
accepts ends up as commands for an assistant that controls the PC. So every
request must carry the right Host (blocks DNS rebinding), and the stream and the
actions require a random token that only the HUD window is given.
"""

from __future__ import annotations

import hmac
import json
import logging
import queue
import secrets
import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .hub import Hub

log = logging.getLogger(__name__)

_PAGE = Path(__file__).parent / "static" / "index.html"
_MAX_BODY = 8 * 1024
_PING_S = 15


class UiServer:
    def __init__(self, hub: Hub) -> None:
        self.hub = hub
        self.token = secrets.token_urlsafe(24)
        # Set by the assistant once it is ready; until then actions are refused.
        self.on_activate: Callable[[], None] | None = None
        self.on_say: Callable[[str], None] | None = None
        self._httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler_class())
        self._httpd.daemon_threads = True
        self.port: int = self._httpd.server_address[1]

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def url(self) -> str:
        return f"{self.origin}/?token={self.token}"

    def start(self) -> None:
        threading.Thread(target=self._httpd.serve_forever, name="ui-server", daemon=True).start()

    def stop(self) -> None:
        self.hub.close()
        self._httpd.shutdown()
        self._httpd.server_close()

    def _token_ok(self, candidate: str) -> bool:
        return hmac.compare_digest(candidate.encode(), self.token.encode())

    def _handler_class(self) -> type[BaseHTTPRequestHandler]:
        server = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, format: str, *args) -> None:  # noqa: A002 - signature fixed by the base class
                log.debug("ui-server: " + format, *args)

            # ---------------------------------------------------------- helpers

            def _reply(self, status: int, body: bytes = b"", content_type: str = "text/plain; charset=utf-8") -> None:
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.end_headers()
                self.wfile.write(body)

            def _host_ok(self) -> bool:
                return self.headers.get("Host", "") in (f"127.0.0.1:{server.port}", f"localhost:{server.port}")

            # ------------------------------------------------------------- GET

            def do_GET(self) -> None:  # noqa: N802 - name fixed by the base class
                if not self._host_ok():
                    self._reply(403, b"forbidden")
                    return
                url = urlsplit(self.path)
                if url.path == "/":
                    self._reply(200, _PAGE.read_bytes(), "text/html; charset=utf-8")
                elif url.path == "/events":
                    token = (parse_qs(url.query).get("token") or [""])[0]
                    if server._token_ok(token):
                        self._stream()
                    else:
                        self._reply(403, b"forbidden")
                else:
                    self._reply(404, b"not found")

            def _stream(self) -> None:
                """Server-sent events: the full state first, then every change."""
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8")
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                updates = server.hub.subscribe()
                try:
                    self._send(server.hub.snapshot())
                    while True:
                        try:
                            item = updates.get(timeout=_PING_S)
                        except queue.Empty:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            continue
                        if item is None:  # the hub closed: the assistant is shutting down
                            return
                        self._send(item)
                except (ConnectionError, OSError):
                    pass  # the window was closed
                finally:
                    server.hub.unsubscribe(updates)
                    self.close_connection = True

            def _send(self, payload: dict) -> None:
                self.wfile.write(b"data: " + json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n\n")
                self.wfile.flush()

            # ------------------------------------------------------------ POST

            def do_POST(self) -> None:  # noqa: N802 - name fixed by the base class
                origin = self.headers.get("Origin")
                if (
                    not self._host_ok()
                    or (origin is not None and origin != server.origin)
                    or not server._token_ok(self.headers.get("X-Jarvis-Token", ""))
                ):
                    self._reply(403, b"forbidden")
                    return
                try:
                    length = int(self.headers.get("Content-Length", "0"))
                except ValueError:
                    length = -1
                if not 0 <= length <= _MAX_BODY:
                    self._reply(413, b"too large")
                    return
                raw = self.rfile.read(length) if length else b""
                path = urlsplit(self.path).path

                if path == "/api/activate":
                    if server.on_activate is None:
                        self._reply(503, b"starting")
                        return
                    server.on_activate()
                    self._reply(204)
                elif path == "/api/say":
                    try:
                        text = json.loads(raw.decode("utf-8")).get("text", "")
                    except (ValueError, AttributeError):
                        text = None
                    if not isinstance(text, str) or not text.strip():
                        self._reply(400, b"missing text")
                    elif server.on_say is None:
                        self._reply(503, b"starting")
                    else:
                        server.on_say(text.strip()[:2000])
                        self._reply(204)
                else:
                    self._reply(404, b"not found")

        return Handler
