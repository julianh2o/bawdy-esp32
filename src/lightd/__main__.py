"""python -m lightd — run the pattern daemon."""

from __future__ import annotations
import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from lightd import ApiServer, build
from lightd.config import ConfigError

REPO_ROOT = Path(__file__).resolve().parents[2]


def main(argv: list[str] | None = None) -> int:
    env = os.environ.get
    parser = argparse.ArgumentParser(prog="lightd", description="Run Lua patterns on WLED controller channels.")
    parser.add_argument("--controllers", default=env("CONTROLLERS_FILE", "config/controllers.json"))
    parser.add_argument("--patterns", default=env("BUILTIN_PATTERNS_DIR", str(REPO_ROOT / "patterns")),
                        help="built-in pattern directory (read-only)")
    parser.add_argument("--user-patterns", default=env("USER_PATTERNS_DIR", "config/patterns"),
                        help="where patterns saved through the API go")
    parser.add_argument("--state", default=env("LIGHTD_STATE", "config/lightd-state.json"),
                        help="active runs and channel brightness are saved here and restored on start")
    parser.add_argument("--scenes", default=env("SCENES_FILE", "config/scenes.json"))
    parser.add_argument("--host", default=env("LIGHTD_HOST", "0.0.0.0"))
    parser.add_argument("--port", type=int, default=int(env("LIGHTD_PORT", "8765")))
    parser.add_argument("--fps", type=float, default=float(env("LIGHTD_FPS", "30")))
    parser.add_argument("--no-discovery", action="store_true", default=env("DISCOVERY", "true").lower() == "false")
    parser.add_argument("--dry-run", action="store_true", default=env("LIGHTD_DRY_RUN", "false").lower() == "true",
                        help="don't send UDP (frames are discarded)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO, format="%(levelname)s %(message)s")

    transport = None
    if args.dry_run:
        from wled import FakeTransport

        transport = FakeTransport()
    app = build(args.controllers, args.patterns, args.user_patterns, args.state, args.scenes, fps=args.fps,
                discovery=not args.no_discovery, transport=transport)
    try:
        controllers = app.registry.load()
    except ConfigError as e:
        print(f"Invalid {args.controllers}: {e}", file=sys.stderr)
        return 2
    if app.browser is not None:
        app.browser.start()
    app.registry.start()
    for err in app.runs.restore():
        print(f"Could not restore run: {err}", file=sys.stderr)
    app.loop.start()
    server = ApiServer(app, args.host, args.port)
    server.start()

    print(f"lightd on http://{args.host}:{server.port}  ·  {len(controllers)} controller(s) from {args.controllers}"
          f"  ·  {len(app.runs.runs())} run(s) restored  ·  {args.fps:g} fps", flush=True)
    for cid, c in controllers.items():
        print(f"  • {cid} -> {c['ip']}:{c['port']}", flush=True)

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.wait(1.0):
            pass
    except KeyboardInterrupt:
        pass
    print("Stopping: blacking out boards…", flush=True)
    server.stop()
    app.loop.stop(blackout=True)
    app.registry.stop()
    if app.browser is not None:
        app.browser.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
