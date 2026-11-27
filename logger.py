"""
logger.py
---------
Append-only logging for the optimization process.
The log file is NEVER truncated or rewritten - every run simply adds
new entries at the end, as required.
"""

import datetime
import os
from config import LOG_FILE, APP_DIR
from utils import format_size


class Logger:
    def __init__(self, path: str = LOG_FILE):
        self.path = path
        try:
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        except OSError:
            pass

    def log(self, status: str, filepath: str, before: int = None, after: int = None) -> None:
        """Append a single log entry. Opens/closes the file each call so
        no file handle is held open longer than necessary (keeps RAM/FD
        usage minimal on low-end devices)."""
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"[{timestamp}]", status, filepath]
        if before is not None and after is not None:
            lines.append(f"{format_size(before)} -> {format_size(after)}")
        lines.append("-" * 32)

        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            # Logging must never crash the optimization process.
            pass

    def error(self, filepath: str, message: str) -> None:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        lines = [f"[{timestamp}]", "FAILED", filepath, f"Reason: {message}", "-" * 32]
        try:
            with open(self.path, "a", encoding="utf-8") as f:
                f.write("\n".join(lines) + "\n")
        except OSError:
            pass
