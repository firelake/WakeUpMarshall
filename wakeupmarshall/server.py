"""Local web server & REST API for WakeUpMarshall.

Serves the minimal UI (``web/index.html``) and a small JSON API:

    GET  /                  -> UI
    GET  /api/status        -> adapter / devices / scheduler / last wake
    POST /api/wake          -> trigger a manual wake round (async)
    POST /api/scan          -> trigger a manual scan (async)
    GET  /api/settings      -> current settings
    PUT  /api/settings      -> update settings (enabled, interval_minutes, ...)
    GET  /api/history       -> wake history (newest first)
    DELETE /api/history     -> clear history
"""

from __future__ import annotations

import json
import logging
import mimetypes
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Dict
from urllib.parse import urlparse

from . import __version__
from .config import Settings
from .scheduler import WakeScheduler

log = logging.getLogger("wakeupmarshall.http")

WEB_DIR = os.path.join(os.path.dirname(__file__), "web")


class ApiHandler(BaseHTTPRequestHandler):
    server: "WakeServer"  # type: ignore[assignment]

    # -- helpers --------------------------------------------------------
    def _send_json(self, payload: Dict, status: int = 200) -> None:
        body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_file(self, path: str) -> None:
        if not os.path.isfile(path):
            self._send_json({"error": "not found"}, 404)
            return
        ctype, _ = mimetypes.guess_type(path)
        with open(path, "rb") as fh:
            body = fh.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype or "application/octet-stream")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self) -> Dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except Exception:
            return {}

    def log_message(self, fmt: str, *args) -> None:  # quieter access log
        log.debug(fmt % args)

    # -- routing --------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            self._send_file(os.path.join(WEB_DIR, "index.html"))
        elif path == "/api/status":
            self._send_json(self.server.scheduler.status())
        elif path == "/api/settings":
            self._send_json(self._settings_payload())
        elif path == "/api/history":
            self._send_json({"history": self.server.scheduler.history()})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/wake":
            self.server.scheduler.trigger_wake(manual=True)
            self._send_json({"started": True})
        elif parsed.path == "/api/scan":
            self.server.scheduler.trigger_scan()
            self._send_json({"started": True})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/settings":
            data = self._read_json()
            s = self.server.scheduler.settings
            if "enabled" in data:
                s.enabled = bool(data["enabled"])
            if "interval_minutes" in data:
                s.interval_minutes = int(data["interval_minutes"])
            if "scan_timeout" in data:
                s.scan_timeout = int(data["scan_timeout"])
            if "connect_timeout" in data:
                s.connect_timeout = int(data["connect_timeout"])
            if "port" in data:
                s.port = int(data["port"])
            if "keywords" in data and isinstance(data["keywords"], list):
                s.keywords = [str(k) for k in data["keywords"]]
            s.clamp()
            self.server.scheduler.update_settings(s)
            self._send_json(self._settings_payload())
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/history":
            self.server.scheduler.clear_history()
            self._send_json({"cleared": True})
        else:
            self._send_json({"error": "not found"}, 404)

    def _settings_payload(self) -> Dict:
        return self._settings_dict(self.server.scheduler.settings)

    @staticmethod
    def _settings_dict(s: Settings) -> Dict:
        return {
            "enabled": s.enabled,
            "interval_minutes": s.interval_minutes,
            "scan_timeout": s.scan_timeout,
            "connect_timeout": s.connect_timeout,
            "keywords": s.keywords,
            "port": s.port,
            "backend": s.backend,
            "version": __version__,
        }


class WakeServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, addr: tuple, handler, scheduler: WakeScheduler) -> None:
        super().__init__(addr, handler)
        self.scheduler = scheduler
        self.settings = scheduler.settings


def create_server(scheduler: WakeScheduler, host: str = "127.0.0.1", port: int = 8756) -> WakeServer:
    server = WakeServer((host, port), ApiHandler, scheduler)
    return server
