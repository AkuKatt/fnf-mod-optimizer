#!/usr/bin/env python3
"""
FNF Mod Optimizer

    python main.py                   open the web UI in your browser
    python main.py --no-browser      just print the address (Termux / remote)
    python main.py --cli PATH        no UI: optimize a folder or .zip and print progress
"""

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser


def _open_browser(url: str) -> None:
    """Termux has no default browser hook; try its own opener first."""
    for opener in ("termux-open-url", "xdg-open"):
        if shutil.which(opener):
            try:
                subprocess.Popen([opener, url], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                return
            except OSError:
                pass
    try:
        webbrowser.open(url)
    except Exception:
        pass


def run_web(port: int, open_it: bool) -> int:
    import server
    httpd, token, port = server.make_server("127.0.0.1", port)
    url = f"http://127.0.0.1:{port}/?t={token}"
    print("\n  FNF Mod Optimizer")
    print(f"  Open in your browser:  {url}")
    print("  Press Ctrl+C to quit.\n")
    if open_it:
        threading.Timer(0.6, _open_browser, args=(url,)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  Bye.")
    finally:
        httpd.server_close()
    return 0


def _size(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} TB"


def run_cli(path: str, backup: bool, gpu: bool) -> int:
    import engine
    try:
        from settings_store import SettingsStore
        from scanner import build_exclude_rules
        summary = engine.scan_path(path, build_exclude_rules(SettingsStore().effective()))
    except ValueError as exc:
        print(f"Error: {exc}")
        return 2
    print(f"{summary['name']}  -  {summary['total_files']} files, {_size(summary['total_size'])}")
    for c in summary["categories"]:
        print(f"  {c['label']}: {c['files']} files, {_size(c['size'])}")
    for m in summary["missing"]:
        print(f"  ! {m['name']} is not installed ({', '.join(m['categories'])} will be skipped)")

    job = engine.Job(path, backup=backup, gpu=gpu)
    job.start()
    last = ""
    try:
        while job.active:
            time.sleep(0.5)
            snap = job.snapshot()
            line = f"\r  {snap.get('phase')}: {snap.get('processed', 0)}/{snap.get('total', 0)} files"
            if line != last:
                print(line, end="", flush=True)
                last = line
    except KeyboardInterrupt:
        job.cancel()
        print("\n  Cancelling...")
    job.wait()
    snap = job.snapshot()
    print()
    if snap.get("error"):
        print(f"Error: {snap['error']}")
        return 1
    before, after = snap.get("original", 0), snap.get("optimized", 0)
    saved = (1 - after / before) * 100 if before else 0
    print(f"Done: {_size(before)} -> {_size(after)}  (saved {saved:.1f}%)")
    print(f"  optimized {snap['succeeded']}  already optimal {snap['skipped']}  failed {snap['failed']}  ({snap['elapsed']:.0f}s)")
    if snap.get("backup_path"):
        print(f"  Backup: {snap['backup_path']}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="FNF Mod Optimizer")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser")
    parser.add_argument("--cli", metavar="PATH", help="optimize PATH without the web UI")
    parser.add_argument("--no-backup", action="store_true", help="(--cli) skip the safety backup")
    parser.add_argument("--no-gpu", action="store_true", help="(--cli) never use the GPU for video")
    args = parser.parse_args()
    if args.cli:
        return run_cli(args.cli, backup=not args.no_backup, gpu=not args.no_gpu)
    return run_web(args.port, open_it=not args.no_browser)


if __name__ == "__main__":
    sys.exit(main())
