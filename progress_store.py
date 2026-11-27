"""
progress_store.py
------------------
A tiny persisted "already done" list so that cancelling and re-running
the optimizer resumes instead of re-processing everything from scratch.

A file is considered "already done" if its key (relative path, or ZIP
entry name) is recorded together with:
  - the exact size it had last time we finished with it, and
  - a signature of the preset settings that were active for that run.

BUG FIX: the original version only checked size. That meant if you
changed a preset value (e.g. pushed video_crf higher for smaller
files) and re-ran the optimizer over a folder/zip you'd already
processed, every file's *current* size still matched what was
recorded, so everything was silently skipped - your new, more
aggressive settings never actually ran. Recording a preset signature
alongside the size means a settings change is correctly detected as
"this file needs to be reprocessed", while an unchanged preset still
gets the fast skip-if-already-done behavior.
"""

import hashlib
import json
import os

import tools
from config import APP_DIR

# Bump when an optimizer's output changes, so files processed by an older
# version are processed again instead of being skipped as "already done".
ENGINE_VERSION = 4


def preset_signature(preset: dict) -> str:
    """Short, stable hash of a preset's settings. Two presets with the
    same values (in any key order) hash identically; any value change
    changes the hash. `use_gpu` is intentionally included since it can
    change the encoded output too."""
    # Installing oxipng/astcenc/ffmpeg later changes what is possible, so
    # the set of available tools is part of the signature too.
    available = [bool(tools.OXIPNG_PATH), bool(tools.ASTCENC_PATH), bool(tools.FFMPEG_PATH)]
    # keys starting with "_" are runtime knobs (worker count ...), not output-affecting
    settings = {k: v for k, v in preset.items() if not str(k).startswith("_")}
    payload = json.dumps([settings, available, ENGINE_VERSION], sort_keys=True, default=str)
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]


class ProgressStore:
    def __init__(self, state_path: str):
        self.state_path = state_path
        self.data = self._load()

    def _load(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def is_done(self, key: str, size: int, preset_sig: str = None) -> bool:
        entry = self.data.get(key)
        if entry is None or entry.get("size") != size:
            return False
        # Entries written before this fix have no "sig" field. Treat
        # those as stale (reprocess once) rather than crashing/skipping
        # blindly, so upgrading the tool doesn't get stuck on old state.
        if preset_sig is not None and entry.get("sig") != preset_sig:
            return False
        return True

    def mark_done(self, key: str, size: int, preset_sig: str = None) -> None:
        self.data[key] = {"size": size, "sig": preset_sig}

    def forget(self, key: str) -> None:
        self.data.pop(key, None)

    def save(self) -> None:
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(self.data, f)
        except OSError:
            pass

    def clear(self) -> None:
        self.data = {}
        try:
            if os.path.exists(self.state_path):
                os.remove(self.state_path)
        except OSError:
            pass


def _state_file(kind: str, path: str) -> str:
    """Progress lives in the app's own folder, NOT inside the mod: a
    cancelled run used to leave a stray .json file in the user's mod."""
    folder = os.path.join(APP_DIR, "progress")
    try:
        os.makedirs(folder, exist_ok=True)
    except OSError:
        pass
    digest = hashlib.sha1(os.path.abspath(path).encode("utf-8")).hexdigest()[:20]
    return os.path.join(folder, f"{kind}_{digest}.json")


def state_path_for_folder(root: str) -> str:
    return _state_file("folder", root)


def state_path_for_zip(zip_path: str) -> str:
    return _state_file("zip", zip_path)
