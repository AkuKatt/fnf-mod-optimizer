"""
parallel.py
-----------
One executor for every optimizer category.

CPU-heavy Python work (image quantization, atlas repacking, ASTC
pre-processing, font subsetting, script/shader minifying) runs in
separate WORKER PROCESSES so it isn't serialized by Python's GIL.
Video/audio stay on threads: they only wait on ffmpeg, and the GPU
encoder slot limit (a semaphore) has to be shared by all of them.

If worker processes can't be started (restricted phone/container
environments) or the pool breaks mid-run, submissions transparently
fall back to threads - a file is never lost because of that.
Set config.PARALLEL_MODE = "thread" to force the old all-threads
behavior.
"""

from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor
from concurrent.futures import BrokenExecutor
import multiprocessing

import progress_channel
from config import MAX_WORKERS, PARALLEL_MODE

_THREAD_CATEGORIES = {"videos", "audio"}


class HybridExecutor:
    def __init__(self, max_workers: int = MAX_WORKERS, mode: str = PARALLEL_MODE):
        self._threads = ThreadPoolExecutor(max_workers=max_workers)
        self._procs = None
        self._q = None
        if mode == "process":
            try:
                ctx = multiprocessing.get_context("spawn")
                self._q = ctx.Queue()            # live per-file progress, worker -> main
                self._procs = ProcessPoolExecutor(
                    max_workers=max_workers, mp_context=ctx,
                    initializer=progress_channel.init_worker, initargs=(self._q,))
                progress_channel.set_queue(self._q)
            except Exception:
                self._procs = None
                self._q = None
        if self._procs is None:
            progress_channel.set_queue(None)

    @classmethod
    def from_preset(cls, preset: dict):
        """Worker count / mode chosen in Settings travel inside the
        preset as "_workers" / "_parallel_mode" (underscore keys are
        runtime-only and never part of the progress signature)."""
        workers = int((preset or {}).get("_workers") or MAX_WORKERS)
        mode = (preset or {}).get("_parallel_mode") or PARALLEL_MODE
        return cls(max_workers=max(1, workers), mode=mode)

    def submit(self, category: str, fn, *args):
        if self._procs is not None and category not in _THREAD_CATEGORIES:
            try:
                return self._procs.submit(fn, *args)
            except (BrokenExecutor, RuntimeError, OSError):
                self._procs = None
        return self._threads.submit(fn, *args)

    def shutdown(self, wait: bool = True):
        if self._procs is not None:
            try:
                self._procs.shutdown(wait=wait, cancel_futures=True)
            except Exception:
                pass
        self._threads.shutdown(wait=wait, cancel_futures=True)
        if self._q is not None:
            try:
                progress_channel.drain()
                self._q.close()
                self._q.cancel_join_thread()
            except Exception:
                pass
