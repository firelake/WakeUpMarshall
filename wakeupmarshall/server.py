"""Local web server & REST API for WakeUpMarshall.

Serves the minimal UI (``web/index.html``) and a small JSON API:

    GET  /                  -> UI
    GET  /api/status        -> adapter / devices / scheduler / last wake
    POST /api/wake          -> trigger a manual wake round (async)
    POST /api/scan          -> trigger a manual scan (async)
    POST /api/devices/bind  -> persist a discovered Marshall device
    DELETE /api/devices/:id -> remove a saved device
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
from urllib.parse import unquote, urlparse

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
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Request body must contain valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("Request body must be a JSON object")
        return payload

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
            started = self.server.scheduler.trigger_wake(manual=True)
            self._send_json(
                {
                    "started": started,
                    "operation": self.server.scheduler.status()["scheduler"]["operation"],
                    **({} if started else {"error": "Another Bluetooth operation is already running"}),
                },
                202 if started else 409,
            )
        elif parsed.path == "/api/scan":
            started = self.server.scheduler.trigger_scan()
            self._send_json(
                {
                    "started": started,
                    "operation": self.server.scheduler.status()["scheduler"]["operation"],
                    **({} if started else {"error": "Another Bluetooth operation is already running"}),
                },
                202 if started else 409,
            )
        elif parsed.path == "/api/devices/bind":
            try:
                data = self._read_json()
                address = str(data.get("address") or "").strip()
                if not address:
                    raise ValueError("Device address is required")
                device = self.server.scheduler.bind_device(address)
            except KeyError as exc:
                self._send_json({"error": str(exc.args[0])}, 404)
                return
            except ValueError as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json({"device": device})
        else:
            self._send_json({"error": "not found"}, 404)

    def do_PUT(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/settings":
            try:
                data = self._read_json()
                self.server.scheduler.patch_settings(data)
            except (TypeError, ValueError) as exc:
                self._send_json({"error": str(exc)}, 400)
                return
            self._send_json(self._settings_payload())
        else:
            self._send_json({"error": "not found"}, 404)

    def do_DELETE(self) -> None:
        parsed = urlparse(self.path)
        if parsed.path == "/api/history":
            self.server.scheduler.clear_history()
            self._send_json({"cleared": True})
        elif parsed.path.startswith("/api/devices/"):
            address = unquote(parsed.path.removeprefix("/api/devices/")).strip()
            if not address:
                self._send_json({"error": "Device address is required"}, 400)
            elif self.server.scheduler.unbind_device(address):
                self._send_json({"deleted": True})
            else:
                self._send_json({"error": "Saved device not found"}, 404)
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
