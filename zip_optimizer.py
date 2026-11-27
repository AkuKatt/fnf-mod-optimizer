"""
zip_optimizer.py
----------------
Optimizes every supported file inside a ZIP in place, preserving
structure and leaving unsupported entries untouched. Takes an
already-computed ScanResult (scanner.scan_zip) so atlas pairs get
optimized together instead of falling back to per-file handling.

Files are extracted to temp, optimized in parallel, then the archive
is rebuilt in one sequential pass (zipfile can't write from multiple
threads). If cancelled, unfinished entries are copied through
unchanged and the progress store lets a re-run resume.
"""

import os
import shutil
import tempfile
import zipfile

from config import COPY_CHUNK_SIZE
from parallel import HybridExecutor
from runner import OptimizationRunner, drive_futures
from progress_channel import tracked
from optimizers.atlas_optimizer import trim_and_repack_atlas
from progress_store import ProgressStore, state_path_for_zip, preset_signature


def _copy_entry_untouched(zin: zipfile.ZipFile, zout: zipfile.ZipFile, info: zipfile.ZipInfo) -> None:
    """Stream-copy one entry (file or directory) from the source zip to
    the destination zip without altering its contents."""
    new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    new_info.external_attr = info.external_attr
    new_info.compress_type = _pick_compress_type(info) if not info.is_dir() else info.compress_type

    if info.is_dir():
        zout.writestr(new_info, b"")
        return

    with zin.open(info, "r") as source, zout.open(new_info, "w") as dest:
        shutil.copyfileobj(source, dest, COPY_CHUNK_SIZE)


# Already-compressed payloads: deflating them again gains nothing, so an
# entry stored in the source zip stays stored. Everything else (astc,
# xml, scripts, shaders, fonts, json ...) is ALWAYS deflated, even when
# the source zip had stored it - that was pure wasted space.
_INCOMPRESSIBLE_EXTS = {
    ".png", ".jpg", ".jpeg", ".webp", ".gif", ".ogg", ".mp3", ".m4a",
    ".mp4", ".webm", ".mkv", ".ogv", ".mov", ".zip", ".7z", ".rar",
    ".gz", ".woff2",
}


def _pick_compress_type(info: zipfile.ZipInfo) -> int:
    ext = os.path.splitext(info.filename)[1].lower()
    if ext in _INCOMPRESSIBLE_EXTS and info.compress_type == zipfile.ZIP_STORED:
        return zipfile.ZIP_STORED
    if info.compress_type in (zipfile.ZIP_STORED, None):
        return zipfile.ZIP_DEFLATED
    return info.compress_type


def _write_file_from_disk(zout: zipfile.ZipFile, info: zipfile.ZipInfo, disk_path: str) -> None:
    new_info = zipfile.ZipInfo(info.filename, date_time=info.date_time)
    new_info.external_attr = info.external_attr
    # A fresh ZipInfo defaults to ZIP_STORED even inside a
    # ZIP_DEFLATED archive when using open()/writestr() with an
    # explicit ZipInfo - must set this or optimized files end up
    # uncompressed in the final zip.
    new_info.compress_type = _pick_compress_type(info)
    with open(disk_path, "rb") as source, zout.open(new_info, "w") as dest:
        shutil.copyfileobj(source, dest, COPY_CHUNK_SIZE)


def optimize_zip(zip_path: str, scan_result, preset: dict, runner: OptimizationRunner = None) -> OptimizationRunner:
    """
    Optimize every supported file (and every atlas pair) inside
    `zip_path` using the given preset, based on an already-computed
    `scan_result` (from scanner.scan_zip). Returns the
    OptimizationRunner used, so the caller can print the final summary.
    """
    progress_store = ProgressStore(state_path_for_zip(zip_path))
    if runner is None:
        runner = OptimizationRunner(scan_result.total_files, preset)
    runner.progress_store = progress_store

    zip_dir = os.path.dirname(os.path.abspath(zip_path)) or "."
    new_zip_path = zip_path + ".fnfopt.tmp.zip"
    work_dir = tempfile.mkdtemp(dir=zip_dir, prefix=".fnfopt_work_")

    try:
        with zipfile.ZipFile(zip_path, "r") as zin:
            infos = zin.infolist()
            preset_sig = preset_signature(preset)

            temp_paths = {}       # entry filename -> extracted temp file path
            pending_single = []   # (info, category, tpath)
            pending_atlas = []    # (image_info, xml_info, image_tpath, xml_tpath)
            idx = 0

            def _extract(info):
                nonlocal idx
                idx += 1
                ext = os.path.splitext(info.filename)[1]
                tpath = os.path.join(work_dir, f"item_{idx}{ext}")
                with zin.open(info, "r") as source, open(tpath, "wb") as dest:
                    shutil.copyfileobj(source, dest, COPY_CHUNK_SIZE)
                return tpath

            for category, items in (
                ("images", scan_result.images),
                ("videos", scan_result.videos),
                ("audio", scan_result.audio),
                ("astc", scan_result.astc),
                ("fonts", scan_result.fonts),
                ("shaders", scan_result.shaders),
                ("scripts", scan_result.scripts),
                ("json", scan_result.json),
            ):
                for info in items:
                    if progress_store.is_done(info.filename, info.file_size, preset_sig):
                        runner.skip_already_done(info.file_size, info.filename, category)
                        continue
                    try:
                        tpath = _extract(info)
                    except Exception:
                        runner.record_result(
                            info.filename, info.filename, "FAILED",
                            info.file_size, info.file_size, category,
                        )
                        continue
                    temp_paths[info.filename] = tpath
                    pending_single.append((info, category, tpath))

            for image_info, xml_info in scan_result.atlas:
                combined_key = image_info.filename
                combined_size = image_info.file_size + xml_info.file_size
                if progress_store.is_done(combined_key, combined_size, preset_sig):
                    runner.skip_already_done(combined_size, combined_key, "atlas")
                    continue
                try:
                    image_tpath = _extract(image_info)
                    xml_tpath = _extract(xml_info)
                except Exception:
                    runner.record_result(
                        combined_key, combined_key, "FAILED",
                        combined_size, combined_size, "atlas",
                    )
                    continue
                temp_paths[image_info.filename] = image_tpath
                temp_paths[xml_info.filename] = xml_tpath
                pending_atlas.append((image_info, xml_info, image_tpath, xml_tpath))

            # Optimize extracted files/pairs in parallel.
            executor = HybridExecutor.from_preset(preset)
            futures = {}

            for info, category, tpath in pending_single:
                fn = runner.optimizer_for(category)
                futures[executor.submit(category, tracked, fn, tpath, tpath, preset)] = ("single", info, tpath, category)

            for image_info, xml_info, image_tpath, xml_tpath in pending_atlas:
                futures[executor.submit(
                    "atlas", tracked, trim_and_repack_atlas, image_tpath, image_tpath, xml_tpath, preset
                )] = (
                    "atlas", (image_info, xml_info), (image_tpath, xml_tpath)
                )

            def on_result(entry, status, before, after):
                kind = entry[0]
                if kind == "single":
                    _kind, info, tpath, category = entry
                    if status is None:
                        size = os.path.getsize(tpath) if os.path.exists(tpath) else info.file_size
                        status, before, after = "FAILED", size, size
                    runner.record_result(info.filename, info.filename, status, before, after, category)
                else:
                    _kind, (image_info, xml_info), (image_tpath, xml_tpath) = entry
                    if status is None:
                        size = 0
                        for p, fallback in (
                            (image_tpath, image_info.file_size),
                            (xml_tpath, xml_info.file_size),
                        ):
                            size += os.path.getsize(p) if os.path.exists(p) else fallback
                        status, before, after = "FAILED", size, size
                    runner.record_result(image_info.filename, image_info.filename, status, before, after, "atlas")

            def _display_of(entry):
                if entry[0] == "single":
                    return entry[1].filename
                return entry[1][0].filename

            def _key_of(entry):
                return entry[2] if entry[0] == "single" else entry[2][0]

            def _retry(entry):
                if entry[0] == "single":
                    return runner.optimizer_for(entry[3])(entry[2], preset)
                return trim_and_repack_atlas(entry[2][0], entry[2][1], preset)

            drive_futures(executor, futures, runner, on_result, display_of=_display_of, retry=_retry, key_of=_key_of)

            # Sequential pass: rebuild the archive. Every entry either
            # gets its optimized bytes (if a temp file exists for it)
            # or is streamed through unchanged (unsupported files, any
            # unpaired/unrecognized .xml, and anything that never got
            # to run because of a cancel).
            with zipfile.ZipFile(new_zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zout:
                for info in infos:
                    if info.is_dir():
                        _copy_entry_untouched(zin, zout, info)
                        continue
                    tpath = temp_paths.get(info.filename)
                    if tpath and os.path.exists(tpath):
                        _write_file_from_disk(zout, info, tpath)
                    else:
                        _copy_entry_untouched(zin, zout, info)

        # Extremely rare, but never trade the user's zip for a bigger one.
        if os.path.getsize(new_zip_path) < os.path.getsize(zip_path):
            os.replace(new_zip_path, zip_path)

    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
        try:
            if os.path.exists(new_zip_path):
                os.remove(new_zip_path)
        except OSError:
            pass

    runner.finalize()
    return runner
