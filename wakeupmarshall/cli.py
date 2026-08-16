"""Command line interface for WakeUpMarshall.

Usage::

    wakeupmarshall serve                  # start scheduler + web UI (default)
    wakeupmarshall once                   # one immediate wake round
    wakeupmarshall scan                   # scan and list Bluetooth devices
    wakeupmarshall status                 # show adapter/scheduler/last wake
    wakeupmarshall history [--clear]      # show / clear wake history
    wakeupmarshall toggle [on|off]        # enable/disable the schedule
    wakeupmarshall settings [--interval N] [--enabled yes|no] [--port N]
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import webbrowser
from typing import List, Optional

from . import __version__
from .bluetooth import get_backend
from .config import ConfigStore, Settings
from .scheduler import WakeScheduler

log = logging.getLogger("wakeupmarshall")


def _add_common(p: argparse.ArgumentParser) -> None:
    """Global options, available before or after the subcommand."""
    p.add_argument("--data-dir", help="override the data directory (default ~/.wakeupmarshall)")
    p.add_argument("--backend", choices=["auto", "bluetoothctl", "bleak", "fake"], default="auto",
                   help="Bluetooth backend to use (default: auto)")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wakeupmarshall",
        description="Auto-discover and wake Marshall Bluetooth speakers on a schedule, with a web UI.",
    )
    parser.add_argument("--version", action="version", version=f"wakeupmarshall {__version__}")
    _add_common(parser)
    sub = parser.add_subparsers(dest="command")

    p_serve = sub.add_parser("serve", help="start the scheduler and web UI (default command)")
    _add_common(p_serve)
    p_serve.add_argument("--port", type=int, default=None, help="web UI port (default 8756)")
    p_serve.add_argument("--host", default="127.0.0.1", help="bind address (default 127.0.0.1)")
    p_serve.add_argument("--interval", type=int, default=None, help="wake interval in minutes (default 10)")
    p_serve.add_argument("--no-wake", action="store_true", help="start with scheduled wake disabled")
    p_serve.add_argument("--no-open", action="store_true", help="do not auto-open the browser")
    p_serve.add_argument("--fake", action="store_true", help="use the fake backend (demo/testing)")

    p_once = sub.add_parser("once", help="run one wake round immediately and exit")
    _add_common(p_once)
    p_scan = sub.add_parser("scan", help="scan for Bluetooth devices and exit")
    _add_common(p_scan)
    p_status = sub.add_parser("status", help="show adapter, devices, scheduler and last wake")
    _add_common(p_status)

    p_history = sub.add_parser("history", help="show wake history")
    _add_common(p_history)
    p_history.add_argument("--clear", action="store_true", help="clear history")

    p_toggle = sub.add_parser("toggle", help="enable/disable the schedule")
    _add_common(p_toggle)
    p_toggle.add_argument("state", nargs="?", choices=["on", "off"], help="on = enable, off = disable")

    p_settings = sub.add_parser("settings", help="view or update settings")
    _add_common(p_settings)
    p_settings.add_argument("--interval", type=int, help="wake interval in minutes")
    p_settings.add_argument("--enabled", choices=["yes", "no"], help="enable/disable schedule")
    p_settings.add_argument("--port", type=int, help="web UI port")
    p_settings.add_argument("--scan-timeout", type=int, help="scan timeout in seconds")
    p_settings.add_argument("--connect-timeout", type=int, help="connect timeout in seconds")
    return parser


def _make_config(args: argparse.Namespace) -> ConfigStore:
    return ConfigStore(data_dir=args.data_dir)


def _make_scheduler(args: argparse.Namespace, config: ConfigStore) -> WakeScheduler:
    settings = config.load_settings()
    persist = False
    if getattr(args, "port", None):
        settings.port = args.port
        persist = True
    if getattr(args, "interval", None):
        settings.interval_minutes = args.interval
        persist = True
    if getattr(args, "no_wake", False):
        settings.enabled = False  # one-shot: do not persist
    if getattr(args, "fake", False):
        args.backend = "fake"
    settings.clamp()
    if persist:
        config.save_settings(settings)
    backend = get_backend(args.backend)
    return WakeScheduler(config, backend, settings)


def cmd_serve(args: argparse.Namespace) -> int:
    config = _make_config(args)
    scheduler = _make_scheduler(args, config)
    scheduler.start()

    from .server import create_server

    port = scheduler.settings.port
    try:
        server = create_server(scheduler, host=args.host, port=port)
    except OSError as exc:
        log.error("Cannot bind %s:%s - %s", args.host, port, exc)
        return 1

    url = f"http://{args.host}:{port}"
    print(f"WakeUpMarshall {__version__} - web UI: {url}")
    print(f"  data dir : {config.data_dir}")
    print(f"  backend  : {scheduler.backend.name}")
    print(f"  schedule : {'every %d min' % scheduler.settings.interval_minutes if scheduler.settings.enabled else 'DISABLED'}")
    print("  Ctrl+C to stop")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:
            pass
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopping...")
    finally:
        scheduler.stop()
        server.server_close()
    return 0


def cmd_once(args: argparse.Namespace) -> int:
    config = _make_config(args)
    scheduler = _make_scheduler(args, config)
    scheduler.run_wake(manual=True)
    print(json.dumps(scheduler.status(), ensure_ascii=False, indent=2))
    last = scheduler.last_wake or {}
    return 0 if last.get("result") in ("success", "already_awake") else 1


def cmd_scan(args: argparse.Namespace) -> int:
    config = _make_config(args)
    scheduler = _make_scheduler(args, config)
    scheduler.run_scan()
    print(json.dumps(scheduler.status(), ensure_ascii=False, indent=2))
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    config = _make_config(args)
    scheduler = _make_scheduler(args, config)
    scheduler.adapter = scheduler.backend.adapter_status()
    print(json.dumps(scheduler.status(), ensure_ascii=False, indent=2))
    return 0


def cmd_history(args: argparse.Namespace) -> int:
    config = _make_config(args)
    if args.clear:
        config.clear_history()
        print("History cleared.")
        return 0
    entries = config.load_history()
    if not entries:
        print("No history yet.")
        return 0
    for e in entries:
        print(f"{e.get('ts')}  [{e.get('mode')}/{e.get('action')}] {e.get('result'):>13}  {e.get('detail')}")
    return 0


def cmd_toggle(args: argparse.Namespace) -> int:
    config = _make_config(args)
    settings = config.load_settings()
    if args.state == "on":
        settings.enabled = True
    elif args.state == "off":
        settings.enabled = False
    else:
        settings.enabled = not settings.enabled
    config.save_settings(settings)
    print(f"Scheduled wake: {'enabled' if settings.enabled else 'disabled'} "
          f"(interval {settings.interval_minutes} min)")
    return 0


def cmd_settings(args: argparse.Namespace) -> int:
    config = _make_config(args)
    settings = config.load_settings()
    changed = False
    if args.interval is not None:
        settings.interval_minutes = args.interval
        changed = True
    if args.enabled is not None:
        settings.enabled = args.enabled == "yes"
        changed = True
    if args.port is not None:
        settings.port = args.port
        changed = True
    if args.scan_timeout is not None:
        settings.scan_timeout = args.scan_timeout
        changed = True
    if args.connect_timeout is not None:
        settings.connect_timeout = args.connect_timeout
        changed = True
    if changed:
        config.save_settings(settings)
    print(json.dumps({
        "enabled": settings.enabled,
        "interval_minutes": settings.interval_minutes,
        "scan_timeout": settings.scan_timeout,
        "connect_timeout": settings.connect_timeout,
        "port": settings.port,
        "keywords": settings.keywords,
        "backend": settings.backend,
        "data_dir": str(config.data_dir),
    }, ensure_ascii=False, indent=2))
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    command = args.command or "serve"
    handler = {
        "serve": cmd_serve,
        "once": cmd_once,
        "scan": cmd_scan,
        "status": cmd_status,
        "history": cmd_history,
        "toggle": cmd_toggle,
        "settings": cmd_settings,
    }[command]
    try:
        return handler(args)
    except KeyboardInterrupt:
        return 0
    except Exception as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
