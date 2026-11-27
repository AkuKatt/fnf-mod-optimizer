"""
eventlog.py
-----------
The process log shown in the UI's terminal panel, and the persistent
error trail on disk.

* In memory: a ring buffer of the last MAX_LINES entries. Each entry has
  an increasing id, so the browser just asks "everything after id N".
* On disk: every warning/error is appended to ~/.fnf_mod_optimizer/
  events.log (rotated at 1 MB) so a problem can still be inspected after
  the page or the program is closed.

An entry is {id, t, level, key, args, text}. `text` is always a readable
English fallback; when `key` is set the UI shows its own translation of
that key instead (so status messages follow the chosen language while
raw tool output - ffmpeg, pip - stays exactly as printed).
"""

import os
import threading
import time
from collections import deque

from config import APP_DIR

MAX_LINES = 1500
_DISK_LEVELS = ("warn", "error")
_ROTATE_BYTES = 1024 * 1024
LEVELS = ("info", "ok", "warn", "error", "out")


class EventLog:
    def __init__(self, path: str = None):
        self._lock = threading.Lock()
        self._lines = deque(maxlen=MAX_LINES)
        self._next_id = 1
        self.path = path or os.path.join(APP_DIR, "events.log")
        self.counts = {"warn": 0, "error": 0}

    # -- writing ---------------------------------------------------------
    def add(self, level: str, text: str, key: str = None, args: dict = None) -> int:
        if level not in LEVELS:
            level = "info"
        text = str(text)[:2000]
        with self._lock:
            entry = {"id": self._next_id, "t": time.time(), "level": level,
                     "key": key, "args": args or {}, "text": text}
            self._next_id += 1
            self._lines.append(entry)
            if level in self.counts:
                self.counts[level] += 1
        if level in _DISK_LEVELS:
            self._to_disk(entry)
        return entry["id"]

    def info(self, text, key=None, **args):
        return self.add("info", text, key, args)

    def ok(self, text, key=None, **args):
        return self.add("ok", text, key, args)

    def warn(self, text, key=None, **args):
        return self.add("warn", text, key, args)

    def error(self, text, key=None, **args):
        return self.add("error", text, key, args)

    def out(self, text):
        """Raw output of an external program (one line)."""
        return self.add("out", text.rstrip("\r\n"))

    # -- reading ---------------------------------------------------------
    def since(self, last_id: int = 0, limit: int = 400) -> dict:
        with self._lock:
            fresh = [dict(e) for e in self._lines if e["id"] > last_id]
            newest = self._next_id - 1
            counts = dict(self.counts)
        truncated = len(fresh) > limit
        if truncated:
            fresh = fresh[-limit:]
        return {"lines": fresh, "last": newest, "counts": counts, "truncated": truncated}

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self.counts = {"warn": 0, "error": 0}

    # -- disk ------------------------------------------------------------
    def _to_disk(self, entry: dict) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            if os.path.exists(self.path) and os.path.getsize(self.path) > _ROTATE_BYTES:
                os.replace(self.path, self.path + ".1")
            stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(entry["t"]))
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(f"[{stamp}] {entry['level'].upper():5s} {entry['text']}\n")
        except OSError:
            pass            # logging must never break the program


log = EventLog()
