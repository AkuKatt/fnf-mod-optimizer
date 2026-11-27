"""
runner.py
---------
UI-independent bookkeeping for one optimization run (folder or ZIP):
counters, per-file results, per-category totals, cancel flag, and the
polling loop that collects finished futures. The web UI (server.py) and
the command-line mode both just read `snapshot()`.
"""

import threading
import time
from collections import deque
from concurrent.futures import wait, FIRST_COMPLETED, BrokenExecutor

from config import MAX_WORKERS
import progress_channel
from eventlog import log as eventlog
from logger import Logger
from progress_store import preset_signature
from optimizers.image_optimizer import optimize_image
from optimizers.video_optimizer import optimize_video
from optimizers.audio_optimizer import optimize_audio
from optimizers.astc_optimizer import optimize_astc
from optimizers.atlas_optimizer import trim_and_repack_atlas
from optimizers.font_optimizer import optimize_font
from optimizers.shader_optimizer import optimize_shader
from optimizers.script_optimizer import optimize_script
from optimizers.json_optimizer import optimize_json


def _optimize_atlas_ref(ref, preset):
    """`ref` is an (image_path, xml_path) tuple."""
    return trim_and_repack_atlas(ref[0], ref[1], preset)


OPTIMIZERS = {
    "images": optimize_image,
    "atlas": _optimize_atlas_ref,
    "astc": optimize_astc,
    "videos": optimize_video,
    "audio": optimize_audio,
    "fonts": optimize_font,
    "shaders": optimize_shader,
    "scripts": optimize_script,
    "json": optimize_json,
}

_POLL_INTERVAL = 0.4


def drive_futures(executor, futures: dict, runner, on_result, display_of, retry=None, key_of=None) -> None:
    """Collect finished futures until all are done or the run is
    cancelled. `on_result(info, status, before, after)` is called once per
    future. A crashed worker *process* (BrokenExecutor) is retried once
    in-process through `retry(info)` instead of failing every remaining
    file."""
    pending = set(futures)
    handled = set()
    active_limit = int((getattr(runner, "preset", None) or {}).get("_workers") or MAX_WORKERS)

    def _result_of(future, info):
        try:
            return future.result()
        except BrokenExecutor:
            if retry is not None:
                try:
                    return retry(info)
                except Exception:
                    pass
        except Exception:
            pass
        return None, None, None

    try:
        while pending:
            done, pending = wait(pending, timeout=_POLL_INTERVAL, return_when=FIRST_COMPLETED)
            progress_channel.drain()
            runner.set_active([(display_of(futures[f]), key_of(futures[f]) if key_of else None)
                               for f in pending if f.running()][:active_limit])
            for future in done:
                info = futures[future]
                if key_of:
                    progress_channel.forget(key_of(info))
                status, before, after = _result_of(future, info)
                on_result(info, status, before, after)
                handled.add(future)
            if runner.cancelled:
                break
    finally:
        for f in futures:
            if f not in handled:
                f.cancel()
        executor.shutdown(wait=True)
        for future, info in futures.items():
            if future in handled or future.cancelled():
                continue
            status, before, after = _result_of(future, info)
            on_result(info, status, before, after)
        runner.set_active([])


class OptimizationRunner:
    def __init__(self, total_files: int, preset: dict, progress_store=None):
        self.total_files = total_files
        self.preset = preset
        self.preset_sig = preset_signature(preset)
        self.progress_store = progress_store
        self.logger = Logger()
        self.start_time = time.time()
        self.cancelled = False
        self._lock = threading.Lock()
        self.processed = self.succeeded = self.skipped = self.failed = 0
        self.original_total = self.optimized_total = 0
        self.active = []
        self.recent = deque(maxlen=10)
        self.results = []          # every file: dict(name, category, status, before, after)
        self.by_category = {}      # category -> dict(files, before, after)

    @staticmethod
    def optimizer_for(category: str):
        return OPTIMIZERS.get(category)

    def request_cancel(self) -> None:
        self.cancelled = True

    def set_active(self, items) -> None:
        """items: file names, or (name, progress_key) pairs."""
        with self._lock:
            self.active = [it if isinstance(it, tuple) else (it, None) for it in items]

    def _add(self, name, category, status, before, after) -> None:
        self.processed += 1
        self.original_total += before
        self.optimized_total += after
        entry = {"name": name, "category": category or "other", "status": status,
                 "before": before, "after": after}
        self.results.append(entry)
        self.recent.appendleft(entry)
        cat = self.by_category.setdefault(entry["category"], {"files": 0, "before": 0, "after": 0})
        cat["files"] += 1
        cat["before"] += before
        cat["after"] += after

    def skip_already_done(self, size: int, display_name: str, category: str = None) -> None:
        with self._lock:
            self.succeeded += 1
            self._add(display_name, category, "DONE", size, size)

    def record_result(self, key, display_name: str, status: str, before: int, after: int,
                      category: str = None) -> None:
        with self._lock:
            if status == "FAILED":
                self.failed += 1
            elif status == "SKIPPED":
                self.skipped += 1
            else:
                self.succeeded += 1
            self._add(display_name, category, status, before or 0, after or 0)
        if status != "FAILED" and self.progress_store is not None and key is not None:
            self.progress_store.mark_done(key, after, self.preset_sig)
        self.logger.log(status, display_name, before, after)
        args = {"name": display_name, "before": before or 0, "after": after or 0}
        if status == "FAILED":
            eventlog.error(f"Failed: {display_name}", key="log.file.failed", **args)
        elif status == "SKIPPED":
            eventlog.add("out", f"= {display_name} (already optimal)", "log.file.skip", args)
        else:
            eventlog.add("ok", f"{display_name}  {before} -> {after} bytes", "log.file.ok", args)

    def finalize(self) -> None:
        if self.progress_store is not None:
            if self.cancelled:
                self.progress_store.save()
            else:
                self.progress_store.clear()

    def _active_view(self) -> list:
        view = []
        for name, key in self.active:
            entry = progress_channel.get(key) if key is not None else None
            view.append({"name": name,
                         "pct": None if entry is None else round(entry[0], 4),
                         "stage": "" if entry is None else entry[1]})
        return view

    def snapshot(self) -> dict:
        progress_channel.drain()
        with self._lock:
            elapsed = time.time() - self.start_time
            avg = elapsed / self.processed if self.processed else 0.0
            return {
                "total": self.total_files,
                "processed": self.processed,
                "succeeded": self.succeeded,
                "skipped": self.skipped,
                "failed": self.failed,
                "original": self.original_total,
                "optimized": self.optimized_total,
                "elapsed": elapsed,
                "eta": avg * max(self.total_files - self.processed, 0),
                "active_files": self._active_view(),
                "recent": list(self.recent),
                "by_category": {k: dict(v) for k, v in self.by_category.items()},
                "cancelled": self.cancelled,
            }
