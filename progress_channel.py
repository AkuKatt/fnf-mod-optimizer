"""
progress_channel.py
-------------------
Live, per-file progress from the optimizers to the UI.

Each file is processed inside a worker (a separate process or a thread).
Optimizers call `report(fraction, stage)` at meaningful points; the main
process collects those reports and the UI shows them next to the file.

  * `tracked(fn, key, *args)` is what gets submitted to the executor: it
    remembers which file this worker is busy with, then runs `fn(*args)`.
  * `scope(lo, hi)` maps a nested job (for example the image optimizer
    that an atlas calls) into a slice of the outer file's progress.
  * Worker processes send reports through a queue; threads in the main
    process write straight into the shared table. Reporting never raises
    and never blocks the real work.
"""

import queue
import threading
import time
from contextlib import contextmanager

_q = None                 # multiprocessing queue (workers + parent when processes are used)
_is_worker = False
_local = threading.local()
_state = {}               # key -> (fraction, stage, timestamp)   (main process only)
_lock = threading.Lock()
_MIN_GAP = 0.12           # seconds between reports for the same stage


def init_worker(q) -> None:
    """ProcessPoolExecutor initializer: called once in every worker process."""
    global _q, _is_worker
    _q = q
    _is_worker = True


def set_queue(q) -> None:
    global _q
    _q = q


def _stack():
    s = getattr(_local, "stack", None)
    if s is None:
        s = _local.stack = []
    return s


@contextmanager
def scope(lo: float, hi: float, stage: str = None):
    """Inside the block, report(0..1) fills only lo..hi of the file's bar
    (and `stage`, if given, replaces the inner step's label)."""
    st = _stack()
    st.append((float(lo), float(hi), stage))
    try:
        yield
    finally:
        st.pop()


def report(fraction, stage: str = "") -> None:
    try:
        key = getattr(_local, "key", None)
        if key is None:
            return
        f = max(0.0, min(1.0, float(fraction)))
        forced = None
        for lo, hi, label in reversed(_stack()):
            f = lo + (hi - lo) * f
            if label:
                forced = label          # the outermost label wins
        if forced:
            stage = forced
        now = time.time()
        last = getattr(_local, "last", (None, 0.0, 0.0))
        if stage == last[0] and abs(f - last[1]) < 0.004 and now - last[2] < _MIN_GAP:
            return
        _local.last = (stage, f, now)
        msg = (key, f, stage, now)
        if _is_worker:
            if _q is not None:
                _q.put_nowait(msg)
        else:
            with _lock:
                _state[key] = msg[1:]
    except Exception:
        pass


def tracked(fn, key, *args):
    """Run fn(*args) as the job for file `key` (picklable: submit this)."""
    _local.key = key
    _local.stack = []
    _local.last = (None, 0.0, 0.0)
    report(0.0, "start")
    try:
        return fn(*args)
    finally:
        _local.key = None


# ---- main-process side ---------------------------------------------------

def drain() -> None:
    """Move everything workers have reported into the shared table."""
    if _q is None or _is_worker:
        return
    for _ in range(5000):
        try:
            key, f, stage, ts = _q.get_nowait()
        except queue.Empty:
            break
        except Exception:
            break
        with _lock:
            _state[key] = (f, stage, ts)


def get(key):
    with _lock:
        return _state.get(key)


def forget(key) -> None:
    with _lock:
        _state.pop(key, None)


def reset() -> None:
    with _lock:
        _state.clear()
