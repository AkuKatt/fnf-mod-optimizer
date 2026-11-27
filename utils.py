"""
utils.py
--------
Small, dependency-free helper functions shared across the project.
"""

import os
import sys


def setup_console() -> None:
    """Make console output safe on every platform/terminal.

    Windows' classic cmd.exe can be stuck on a legacy codepage (cp437 /
    cp1252) that raises UnicodeEncodeError on some characters, which
    would crash the whole program mid-run. Forcing UTF-8 with
    errors='replace' means worst case a glyph prints as '?' - it never
    crashes. Safe to call on any OS; silently does nothing if the
    stream doesn't support reconfigure (e.g. output redirected to a
    file/pipe in some environments).
    """
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def format_size(num_bytes: float) -> str:
    """Convert a byte count into a human readable string."""
    if num_bytes is None:
        num_bytes = 0
    num_bytes = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB"):
        if num_bytes < 1024.0:
            return f"{num_bytes:.2f} {unit}"
        num_bytes /= 1024.0
    return f"{num_bytes:.2f} TB"


def format_time(seconds: float) -> str:
    """Convert seconds into a human readable duration string."""
    if seconds is None or seconds < 0 or seconds != seconds:  # NaN guard
        seconds = 0
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h {minutes}m {seconds}s"


def safe_relpath(path: str, root: str) -> str:
    """Return a relative path for display purposes, falling back to the
    original path if it cannot be computed (e.g. different drives)."""
    try:
        return os.path.relpath(path, root)
    except ValueError:
        return path


def clear_screen() -> None:
    """Clear the terminal. Silently ignored if it fails (e.g. redirected
    output), since this is a cosmetic feature only."""
    try:
        os.system("cls" if os.name == "nt" else "clear")
    except Exception:
        pass


def render_bar(fraction: float, width: int = 34) -> str:
    """Render a plain-ASCII progress bar (no unicode) so it always
    displays correctly, including on default Windows cmd.exe codepages."""
    if fraction is None or fraction != fraction:  # NaN guard
        fraction = 0.0
    fraction = max(0.0, min(1.0, fraction))
    filled = int(round(width * fraction))
    return "#" * filled + "-" * (width - filled)


def truncate_middle(text: str, max_len: int = 56) -> str:
    """Shorten a long path for display by cutting out the middle,
    keeping the start (context) and end (filename) visible."""
    if text is None:
        return ""
    if len(text) <= max_len:
        return text
    if max_len <= 5:
        return text[:max_len]
    keep = max_len - 3
    head = keep // 2
    tail = keep - head
    return f"{text[:head]}...{text[-tail:]}"
