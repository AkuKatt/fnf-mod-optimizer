"""
engine.py
---------
Everything the UIs (web + command line) need, with no UI code in it:

    scan_path(path)      -> summary dict (categories with count + size)
    Job(path, ...)       -> runs backup + optimization in a background
                            thread; Job.snapshot() is safe to poll.
    deps_status()        -> which external tools / Python packages exist

Errors the user can act on are raised as UserError(code, message): the
web UI translates `code`, `message` is the English fallback.
"""

import os
import threading
import time
import traceback
import zipfile

import config
import tools
import deps
from config import SETTINGS
from backup import backup_folder, backup_zip_file
from eventlog import log
from parallel import HybridExecutor
from progress_store import ProgressStore, state_path_for_folder, preset_signature
from runner import OptimizationRunner, drive_futures
from progress_channel import tracked
from scanner import scan_folder, scan_zip, build_exclude_rules
from optimizers.font_optimizer import build_charset
from utils import safe_relpath
from zip_optimizer import optimize_zip

# (scan attribute, label shown to the user)
CATEGORIES = [
    ("images", "Images (PNG/JPG/WebP)"),
    ("atlas", "Atlases (PNG + XML)"),
    ("astc", "ASTC textures"),
    ("videos", "Video"),
    ("audio", "Audio"),
    ("fonts", "Fonts"),
    ("scripts", "Scripts"),
    ("shaders", "Shaders"),
    ("json", "JSON"),
]
CATEGORY_LABELS = dict(CATEGORIES)

# File types scanned for characters so font subsetting keeps every
# letter the mod actually displays (lyrics, names, menus ...).
_TEXT_EXTS = {".json", ".hxc", ".hx", ".lua", ".txt", ".xml", ".csv", ".md", ".frag", ".yml", ".yaml"}
_CHARSET_BUDGET = 30 * 1024 * 1024
_CHARSET_PER_FILE = 4 * 1024 * 1024


class UserError(ValueError):
    """An expected problem with a clear cause (bad path, backup failed ...)."""

    def __init__(self, code: str, message: str, **args):
        super().__init__(message)
        self.code = code
        self.args_ = args


# ----------------------------------------------------------------------
# Environment
# ----------------------------------------------------------------------
def deps_status(force: bool = False) -> list:
    return deps.manager.status(force=force)


def tool_status() -> list:
    """Compatibility view: [{id, name, ok, note}] for the external tools."""
    notes = {"ffmpeg": "Video and audio can't be optimized.",
             "oxipng": "PNGs can be ~10% larger without oxipng.",
             "astcenc": ".astc files can't be optimized."}
    return [{"id": d["id"], "name": d["name"], "ok": d["installed"], "note": notes.get(d["id"], "")}
            for d in deps_status(force=True) if d["kind"] == "binary"]


def gpu_info() -> dict:
    return {"available": bool(tools.GPU_AVAILABLE), "encoders": sorted(tools.GPU_ENCODERS.keys())}


# ----------------------------------------------------------------------
# Scan
# ----------------------------------------------------------------------
def _size_of_ref(ref, is_zip: bool) -> int:
    if is_zip:
        return ref.file_size
    try:
        return os.path.getsize(ref)
    except OSError:
        return 0


def _scan(path: str, exclude=None):
    path = os.path.abspath(os.path.expanduser(path))
    if os.path.isdir(path):
        return path, False, scan_folder(path, exclude)
    if os.path.isfile(path) and path.lower().endswith(".zip"):
        try:
            return path, True, scan_zip(path, exclude)
        except zipfile.BadZipFile as exc:
            raise UserError("bad_zip", f"The ZIP file is damaged: {exc}")
    raise UserError("not_mod", "Choose a mod folder or a .zip file")


def scan_path(path: str, exclude=None) -> dict:
    """Scan a folder or .zip and summarise it (UserError otherwise)."""
    path, is_zip, result = _scan(path, exclude)

    cats, present = [], set()
    for attr, label in CATEGORIES:
        items = getattr(result, attr)
        if not items:
            continue
        if attr == "atlas":
            size = sum(_size_of_ref(a, is_zip) + _size_of_ref(b, is_zip) for a, b in items)
        else:
            size = sum(_size_of_ref(i, is_zip) for i in items)
        cats.append({"id": attr, "label": label, "files": len(items), "size": size})
        present.add(attr)

    # Only dependencies THIS mod actually needs are reported as missing.
    # Optional ones (oxipng) are never listed here - they stay opt-in.
    missing = []
    for dep in deps_status(force=True):
        if dep["installed"] or dep["role"] == "optional":
            continue
        used = [c for c in dep["needed_for"] if c in present]
        if used:
            missing.append({"id": dep["id"], "name": dep["name"], "categories": used,
                            "blocking": dep["blocking"], "can_install": dep["can_install"]})

    return {
        "path": path,
        "kind": "zip" if is_zip else "folder",
        "name": os.path.basename(path.rstrip(os.sep)) or path,
        "total_files": result.total_files,
        "total_size": result.total_size,
        "categories": cats,
        "missing": missing,
    }


# ----------------------------------------------------------------------
# Font charset
# ----------------------------------------------------------------------
def _latin_and_punctuation() -> set:
    return set(range(0x20, 0x7F)) | set(range(0xA0, 0x100)) | set(range(0x2010, 0x2028))


def collect_charset(path: str, is_zip: bool) -> set:
    """All code points above ASCII that the mod's text files use. Keeps
    subsetted fonts from turning non-English text into empty boxes while
    still dropping thousands of unused glyphs."""
    chars = set()
    budget = _CHARSET_BUDGET

    def eat(data: bytes):
        chars.update(ord(c) for c in data.decode("utf-8", "ignore") if ord(c) > 0x7F)

    try:
        if is_zip:
            with zipfile.ZipFile(path) as zf:
                for info in zf.infolist():
                    if info.is_dir() or os.path.splitext(info.filename)[1].lower() not in _TEXT_EXTS:
                        continue
                    if info.file_size > _CHARSET_PER_FILE or budget <= 0:
                        continue
                    budget -= info.file_size
                    eat(zf.read(info))
        else:
            for dirpath, _dirs, names in os.walk(path):
                for name in names:
                    if os.path.splitext(name)[1].lower() not in _TEXT_EXTS:
                        continue
                    full = os.path.join(dirpath, name)
                    try:
                        size = os.path.getsize(full)
                        if size > _CHARSET_PER_FILE or budget <= 0:
                            continue
                        budget -= size
                        with open(full, "rb") as f:
                            eat(f.read())
                    except OSError:
                        continue
    except Exception:
        pass
    return chars


def _drop_unavailable(result) -> list:
    """Categories whose program/package is missing are skipped (and
    logged) instead of being sent to fail one file at a time."""
    rules = [("videos", tools.FFMPEG_AVAILABLE, "FFmpeg"), ("audio", tools.FFMPEG_AVAILABLE, "FFmpeg"),
             ("astc", bool(tools.ASTCENC_PATH), "astcenc"),
             ("fonts", _module_ok("fontTools"), "fontTools"),
             ("images", _module_ok("PIL"), "Pillow"), ("atlas", _module_ok("PIL"), "Pillow")]
    skipped = []
    for attr, available, name in rules:
        items = getattr(result, attr)
        if items and not available:
            skipped.append((attr, len(items), name))
            setattr(result, attr, [])
    return skipped


def _module_ok(name: str) -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec(name) is not None
    except (ImportError, ValueError):
        return False


# ----------------------------------------------------------------------
# Job
# ----------------------------------------------------------------------
class Job:
    """One optimization run. Create, call start(), poll snapshot()."""

    def __init__(self, path: str, backup: bool = True, gpu: bool = True,
                 settings: dict = None, workers=None, parallel_mode: str = None):
        self.path = os.path.abspath(os.path.expanduser(path))
        self.is_zip = os.path.isfile(self.path)
        self.backup = backup
        self.gpu = gpu
        if settings is None:
            from settings_store import SettingsStore
            settings = SettingsStore().effective()
        self.settings = dict(settings)
        self.workers = workers if isinstance(workers, int) and workers > 0 else None
        self.parallel_mode = parallel_mode if parallel_mode in ("process", "thread") else None
        self.phase = "starting"     # starting|backup|optimizing|done|cancelled|error
        self.error = None
        self.error_code = None
        self.backup_path = None
        self.backup_progress = (0, 0)
        self.runner = None
        self.started = time.time()
        self.finished = None
        self._thread = None

    # -- control --------------------------------------------------------
    def start(self) -> None:
        self._thread = threading.Thread(target=self._run_safely, name="optimizer-job", daemon=True)
        self._thread.start()

    def wait(self) -> None:
        if self._thread:
            self._thread.join()

    def cancel(self) -> None:
        if self.runner is not None:
            self.runner.request_cancel()
            log.warn("Cancelling… waiting for the files in progress", key="log.job.cancelling")

    @property
    def active(self) -> bool:
        return self.phase in ("starting", "backup", "optimizing")

    # -- work -----------------------------------------------------------
    def _preset(self, scan_result) -> dict:
        preset = dict(self.settings)
        if preset.get("video_codec") in (None, "auto"):
            preset["video_codec"] = tools.DEFAULT_VIDEO_CODEC
        preset["use_gpu"] = bool(self.gpu and tools.GPU_AVAILABLE)
        preset["_workers"] = self.workers or config.MAX_WORKERS
        preset["_parallel_mode"] = self.parallel_mode or config.PARALLEL_MODE
        if scan_result.fonts and not preset.get("font_subset_unicodes"):
            used = collect_charset(self.path, self.is_zip) if preset.get("font_keep_used", True) else set()
            preset["font_subset_unicodes"] = sorted(build_charset(preset, used))
        return preset

    def _run_safely(self) -> None:
        try:
            self._run()
        except UserError as exc:
            self.error, self.error_code, self.phase = str(exc), exc.code, "error"
            log.error(str(exc), key="log.job.error", reason=str(exc))
        except Exception as exc:  # never let the thread die silently
            self.error = f"{type(exc).__name__}: {exc}"
            self.error_code = "internal"
            self.phase = "error"
            log.error("Unexpected error: " + traceback.format_exc().strip(), key=None)
        finally:
            self.finished = time.time()

    def _run(self) -> None:
        kind = "ZIP" if self.is_zip else "folder"
        log.info(f"Reading {self.path}…", key="log.job.scan", name=os.path.basename(self.path))
        excl = build_exclude_rules(self.settings)
        result = scan_zip(self.path, excl) if self.is_zip else scan_folder(self.path, excl)
        for attr, count, name in _drop_unavailable(result):
            log.warn(f"{count} {attr} file(s) skipped: {name} is not installed", key="log.job.skipdep",
                     count=count, what=attr, name=name)
        preset = self._preset(result)
        if result.total_files == 0:
            raise UserError("nothing_to_do", "There is nothing to optimize in this mod")

        if self.backup:
            self.phase = "backup"
            log.info("Creating a backup first…", key="log.job.backup")
            try:
                if self.is_zip:
                    self.backup_path = backup_zip_file(self.path)
                else:
                    self.backup_path = backup_folder(
                        self.path, on_progress=lambda i, n: setattr(self, "backup_progress", (i, n)))
            except Exception as exc:
                raise UserError("backup_failed",
                                f"Backup failed, no file was changed. ({exc})", reason=str(exc))
            log.ok(f"Backup saved: {self.backup_path}", key="log.job.backupok", path=self.backup_path)

        self.phase = "optimizing"
        self.runner = OptimizationRunner(result.total_files, preset)
        log.info(f"Optimizing {result.total_files} files ({kind}, {preset['_workers']} workers)…",
                 key="log.job.start", files=result.total_files, workers=preset["_workers"])
        if self.is_zip:
            optimize_zip(self.path, result, preset, self.runner)
        else:
            _run_folder(self.path, result, preset, self.runner)
        self.phase = "cancelled" if self.runner.cancelled else "done"
        snap = self.runner.snapshot()
        saved = snap["original"] - snap["optimized"]
        log.ok(f"Finished: {snap['succeeded']} optimized, {snap['skipped']} already optimal, "
               f"{snap['failed']} failed - saved {saved} bytes",
               key="log.job.done", ok=snap["succeeded"], skipped=snap["skipped"],
               failed=snap["failed"], saved=saved)

    # -- state ----------------------------------------------------------
    def snapshot(self, with_results: bool = False) -> dict:
        snap = {
            "phase": self.phase,
            "running": self.active,
            "error": self.error,
            "error_code": self.error_code,
            "path": self.path,
            "kind": "zip" if self.is_zip else "folder",
            "name": os.path.basename(self.path.rstrip(os.sep)) or self.path,
            "backup_path": self.backup_path,
            "backup_progress": list(self.backup_progress),
            "elapsed": (self.finished or time.time()) - self.started,
        }
        if self.runner is not None:
            snap.update(self.runner.snapshot())
            snap["category_labels"] = CATEGORY_LABELS
            if with_results:
                snap["results"] = list(self.runner.results)
        return snap


def _run_folder(root: str, result, preset: dict, runner: OptimizationRunner) -> None:
    progress_store = ProgressStore(state_path_for_folder(root))
    runner.progress_store = progress_store
    sig = preset_signature(preset)

    pending = []
    for category, items in (
        ("images", result.images), ("atlas", result.atlas), ("astc", result.astc),
        ("videos", result.videos), ("audio", result.audio), ("fonts", result.fonts),
        ("scripts", result.scripts), ("shaders", result.shaders), ("json", result.json),
    ):
        for item in items:
            first = item[0] if category == "atlas" else item
            name = safe_relpath(first, root)
            try:
                size = (os.path.getsize(item[0]) + os.path.getsize(item[1])) if category == "atlas" \
                    else os.path.getsize(item)
            except OSError:
                runner.record_result(name, name, "FAILED", 0, 0, category)
                continue
            if progress_store.is_done(name, size, sig):
                runner.skip_already_done(size, name, category)
            else:
                pending.append((item, name, category))

    executor = HybridExecutor.from_preset(preset)
    futures = {}
    for item, name, category in pending:
        fn = runner.optimizer_for(category)
        futures[executor.submit(category, tracked, fn, item[0] if category == "atlas" else item, item, preset)] = (item, name, category)

    def _size_now(item):
        paths = item[:2] if isinstance(item, tuple) else [item]
        return sum(os.path.getsize(p) for p in paths if os.path.exists(p))

    def on_result(info, status, before, after):
        item, name, category = info
        if status is None:
            size = _size_now(item)
            status, before, after = "FAILED", size, size
        runner.record_result(name, name, status, before, after, category)

    def retry(info):
        item, _name, category = info
        return runner.optimizer_for(category)(item, preset)

    drive_futures(executor, futures, runner, on_result, display_of=lambda i: i[1], retry=retry,
                  key_of=lambda i: i[0][0] if i[2] == "atlas" else i[0])
    runner.finalize()
