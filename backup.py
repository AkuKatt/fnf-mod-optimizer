"""
backup.py
---------
Creates a safety backup BEFORE any optimization touches the user's
files (folder or ZIP mode) - every optimizer here modifies files in
place, so this is the only way back if anything ever goes wrong. If
creating the backup fails, the caller must treat that as a hard stop.

The backup is written as a sibling of the original (never inside it)
using ZIP_STORED (uncompressed, since speed matters more than size for
a short-lived insurance copy).
"""

import os
import shutil
import time
import zipfile

COPY_CHUNK_SIZE = 1024 * 1024


def _unique_backup_path(parent: str, base_name: str) -> str:
    """A sibling '<base_name>_BACKUP_<timestamp>.zip' path, with a
    numeric suffix added if that exact name already exists."""
    stamp = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(parent, f"{base_name}_BACKUP_{stamp}.zip")
    suffix = 1
    while os.path.exists(path):
        path = os.path.join(parent, f"{base_name}_BACKUP_{stamp}_{suffix}.zip")
        suffix += 1
    return path


def _write_then_publish(tmp_path: str, final_path: str, write_fn) -> None:
    """Run write_fn(tmp_path), then atomically publish as final_path.
    On ANY failure (during write or publish), delete the partial file
    so it never looks like a valid backup."""
    try:
        write_fn(tmp_path)
        os.replace(tmp_path, final_path)
    except Exception:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass
        raise


def backup_folder(root: str, on_progress=None) -> str:
    """Zip `root`'s entire contents to a sibling backup archive.
    Returns the backup path, or raises on failure - the caller must
    not proceed without a successful backup. `on_progress(i, total)`
    is called per file if given."""
    root = os.path.abspath(root)
    if not os.path.isdir(root):
        raise FileNotFoundError(f"Not a directory: {root}")
    parent = os.path.dirname(root) or "."
    backup_path = _unique_backup_path(parent, os.path.basename(root.rstrip(os.sep)) or "mod")

    all_files = [
        os.path.join(dirpath, name)
        for dirpath, _dirnames, filenames in os.walk(root)
        for name in filenames
    ]

    def _write(tmp_path):
        with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_STORED) as zf:
            for i, full_path in enumerate(all_files, start=1):
                zf.write(full_path, os.path.relpath(full_path, root))
                if on_progress is not None:
                    on_progress(i, len(all_files))

    _write_then_publish(backup_path + ".partial", backup_path, _write)
    return backup_path


def backup_zip_file(zip_path: str) -> str:
    """Copy `zip_path` to a sibling backup file. Returns the backup
    path, or raises on failure."""
    zip_path = os.path.abspath(zip_path)
    parent = os.path.dirname(zip_path) or "."
    base_name = os.path.splitext(os.path.basename(zip_path))[0]
    backup_path = _unique_backup_path(parent, base_name)

    def _write(tmp_path):
        with open(zip_path, "rb") as src, open(tmp_path, "wb") as dst:
            shutil.copyfileobj(src, dst, COPY_CHUNK_SIZE)

    _write_then_publish(backup_path + ".partial", backup_path, _write)
    return backup_path
