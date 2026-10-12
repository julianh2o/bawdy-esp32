#!/usr/bin/env python3
"""Launcher for the WLED Sequence Editor (Native Java 17 Desktop GUI with optional web mode)."""

from __future__ import annotations
import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

# Ensure src/ is in sys.path
root_dir = Path(__file__).resolve().parent
src_dir = root_dir / "src"
if str(src_dir) not in sys.path:
    sys.path.insert(0, str(src_dir))

from rich.console import Console


def parse_args() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sequencer.py",
        description="WLED Sequence Editor: Native Java Desktop GUI & Web Mode",
    )
    parser.add_argument(
        "--web",
        action="store_true",
        help="Launch legacy browser-based web application instead of native Java GUI",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SEQUENCER_PORT") or 8081),
        help="Web port for web mode (default: 8081) [SEQUENCER_PORT]",
    )
    parser.add_argument(
        "--host",
        default=os.environ.get("SEQUENCER_HOST") or "127.0.0.1",
        help="Host address for web mode (default: 127.0.0.1) [SEQUENCER_HOST]",
    )
    parser.add_argument(
        "--engine",
        default=os.environ.get("ENGINE_URL") or "http://127.0.0.1:8765",
        help="Target Lighting Engine REST URL, proxied to the browser at /engine (default: http://127.0.0.1:8765) [ENGINE_URL]",
    )
    parser.add_argument(
        "--no-browser",
        action="store_true",
        help="Do not automatically open web browser in web mode (server-only; also set for containers)",
    )
    parser.add_argument(
        "--sequences",
        default=os.environ.get("SEQUENCES_DIR") or None,
        help="Directory for saved sequences (default: ./sequences) [SEQUENCES_DIR]",
    )
    return parser


def run_java_gui(engine_url: str, console: Console) -> None:
    jar_path = root_dir / "sequencer-java" / "target" / "wled-sequencer.jar"
    build_script = root_dir / "sequencer-java" / "build.sh"

    if not jar_path.is_file():
        console.print("[yellow]⚡ wled-sequencer.jar not found, building with Java 17...[/yellow]")
        res = subprocess.run([str(build_script)])
        if res.returncode != 0:
            console.print("[bold red]❌ Failed to build Java Sequencer jar.[/bold red]")
            sys.exit(1)

    console.print()
    console.print("[bold yellow]══════════════════════════════════════════════════════════════[/bold yellow]")
    console.print("[bold cyan]       WLED Sequence Editor [Native Java 17 Swing]             [/bold cyan]")
    console.print("[bold yellow]══════════════════════════════════════════════════════════════[/bold yellow]")
    console.print(f"  • Sequences Dir: [dim]{root_dir / 'sequences'}[/dim]")
    console.print(f"  • Engine Target: [cyan]{engine_url}[/cyan]")
    console.print()

    cmd = [
        "java",
        "-Dawt.useSystemAAFontSettings=on",
        "-Dswing.aatext=true",
        "-jar",
        str(jar_path),
        "--sequences",
        str(root_dir / "sequences"),
        "--universe",
        str(root_dir / "data" / "venue" / "universe.json"),
        "--patch",
        str(root_dir / "data" / "venue" / "patch.json"),
        "--engine",
        engine_url,
    ]
    try:
        subprocess.run(cmd)
    except KeyboardInterrupt:
        console.print("\n[yellow]Java Sequencer exited.[/yellow]")


def run_web_app(args: argparse.Namespace, console: Console) -> None:
    import webbrowser
    from wled_sequencer.server import SequencerAppServer

    app_server = SequencerAppServer(
        host=args.host, port=args.port, sequences_dir=args.sequences, engine_url=args.engine
    )
    app_server.start()

    url = f"http://{args.host}:{app_server.port}"

    console.print()
    console.print("[bold yellow]══════════════════════════════════════════════════════════════[/bold yellow]")
    console.print("[bold cyan]       WLED Pattern Sequencer Studio [Web Browser Edition]     [/bold cyan]")
    console.print("[bold yellow]══════════════════════════════════════════════════════════════[/bold yellow]")
    console.print(f"  • Web Studio:    [bold green]{url}[/bold green]")
    console.print(f"  • Sequences Dir: [dim]{app_server.sequences_dir}[/dim]")
    console.print(f"  • Engine Target: [cyan]{args.engine}[/cyan]")
    console.print()
    console.print("Press [bold red]Ctrl+C[/bold red] to stop.")
    console.print()

    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    # Treat SIGTERM (docker stop) like Ctrl+C so PID 1 shuts down promptly.
    def _raise_interrupt(*_: object) -> None:
        raise KeyboardInterrupt

    signal.signal(signal.SIGTERM, _raise_interrupt)
    try:
        while True:
            time.sleep(1.0)
    except KeyboardInterrupt:
        console.print("\n[yellow]Shutting down Web Sequencer...[/yellow]")
    finally:
        app_server.stop()
        console.print("[green]Web Sequencer stopped cleanly.[/green]")


def main() -> None:
    parser = parse_args()
    args = parser.parse_args()
    console = Console()

    if args.web:
        run_web_app(args, console)
    else:
        run_java_gui(args.engine, console)


if __name__ == "__main__":
    main()
