"""python -m lightd — run the pattern daemon."""

from __future__ import annotations
import argparse
import logging
import os
import signal
import sys
import threading
from pathlib import Path

from lightd import ApiServer, build, logs
from lightd.config import ConfigError

log = logging.getLogger("lightd")

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
                        help="don't send anything to the boards: frames are discarded and the output switch doesn't release them "
                             "(board info and segments are still read over HTTP)")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)
    logs.setup(verbose=args.verbose)

    transport = None
    if args.dry_run:
        from wled import FakeTransport

        transport = FakeTransport()
    app = build(args.controllers, args.patterns, args.user_patterns, args.state, args.scenes, fps=args.fps,
                discovery=not args.no_discovery, transport=transport, dry_run=args.dry_run)
    try:
        controllers = app.registry.load()
    except ConfigError as e:
        log.error("Invalid %s: %s", args.controllers, e)
        return 2
    if app.browser is not None:
        app.browser.start()
    app.registry.start()
    for err in app.runs.restore():
        log.warning("Could not restore run: %s", err)
    app.loop.start()
    server = ApiServer(app, args.host, args.port)
    server.start()

    log.info("lightd on http://%s:%d · %d controller(s) from %s · %d run(s) restored · %g fps",
             args.host, server.port, len(controllers), args.controllers, len(app.runs.runs()), args.fps)
    for cid, c in controllers.items():
        log.info("  %s -> %s:%s", cid, c["ip"], c["port"])

    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())
    try:
        while not stop.wait(1.0):
            pass
    except KeyboardInterrupt:
        pass
    log.info("Stopping: blacking out boards…")
    server.stop()
    app.loop.stop(blackout=True)
    app.registry.stop()
    if app.browser is not None:
        app.browser.stop()
    return 0


if __name__ == "__main__":
    sys.exit(main())
