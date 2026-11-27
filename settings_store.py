"""
settings_store.py
-----------------
Everything the Settings screen can change, with validation.

  * COMPRESSION settings  - keys of config.SETTINGS (images, atlas, ASTC,
    text files, video, audio). The user's changes are stored as
    OVERRIDES in ~/.fnf_mod_optimizer/user_settings.json and laid over
    config.SETTINGS when a job starts, so config.py stays the set of
    defaults and "reset" simply deletes the override.
  * APP preferences       - language, theme, worker count ... stored in
    ~/.fnf_mod_optimizer/ui_settings.json.

Each entry says its type and allowed range, and the server refuses
anything outside it (the browser is not trusted to be right). The
human-readable names/descriptions live in the web UI's language file
(web/i18n.js) under  set.<key>.label / set.<key>.desc  and
pref.<key>.label / pref.<key>.desc.
"""

import json
import os
import threading

from config import SETTINGS, APP_DIR

USER_SETTINGS_FILE = os.path.join(APP_DIR, "user_settings.json")
PREFS_FILE = os.path.join(APP_DIR, "ui_settings.json")


def _b(key, group, requires=None):
    return {"key": key, "group": group, "type": "bool", "requires": requires}


def _n(key, group, lo, hi, step=1, unit=None, requires=None, kind="int"):
    return {"key": key, "group": group, "type": kind, "min": lo, "max": hi,
            "step": step, "unit": unit, "requires": requires}


def _t(key, group, maxlen=500, requires=None):
    return {"key": key, "group": group, "type": "text", "maxlen": maxlen, "requires": requires}


def _c(key, group, choices, requires=None):
    return {"key": key, "group": group, "type": "choice", "choices": choices, "requires": requires}


# Order here = order on screen.
SCHEMA = [
    # --- images -----------------------------------------------------------
    _b("png_quantize", "images"),
    _n("png_quantize_colors", "images", 2, 256, unit="colors"),
    _n("image_lossy_tolerance", "images", 0, 80),
    _b("png_quantize_adaptive", "images"),
    _b("png_exact_palette", "images"),
    _b("png_clear_dirty_alpha", "images"),
    _b("png_strip_metadata", "images"),
    _c("png_oxipng_level", "images", [-1, 1, 2, 3, 4, 5, 6], requires="oxipng"),
    _b("png_zopfli", "images", requires="oxipng"),
    _n("jpeg_quality", "images", 0, 100),
    _n("webp_quality", "images", 0, 100),
    # --- atlas ------------------------------------------------------------
    _b("atlas_trim_enabled", "atlas"),
    _n("atlas_trim_padding", "atlas", 0, 8, unit="px"),
    _b("atlas_trim_extrude", "atlas"),
    _b("atlas_trim_dedupe", "atlas"),
    _b("atlas_trim_pow2", "atlas"),
    # --- ASTC ---------------------------------------------------------------
    _c("astc_max_block", "astc", [0, 5, 6, 8, 10, 12], requires="astcenc"),
    _c("astc_quality", "astc", ["-fastest", "-fast", "-medium", "-thorough"], requires="astcenc"),
    _b("astc_quantize", "astc", requires="astcenc"),
    _b("astc_clear_dirty_alpha", "astc", requires="astcenc"),
    _b("astc_low_entropy", "astc", requires="astcenc"),
    # --- text-like files ----------------------------------------------------
    _b("script_strip_comments_enabled", "text"),
    _b("shader_minify_enabled", "text"),
    _b("json_minify_enabled", "text"),
    _c("json_round_decimals", "text", [0, 6, 5, 4, 3, 2]),
    # --- fonts ----------------------------------------------------------------
    _b("font_subset_enabled", "fonts", requires="fonttools"),
    _b("font_keep_upper", "fonts", requires="fonttools"),
    _b("font_keep_lower", "fonts", requires="fonttools"),
    _b("font_keep_digits", "fonts", requires="fonttools"),
    _b("font_keep_ascii_symbols", "fonts", requires="fonttools"),
    _b("font_keep_latin_accents", "fonts", requires="fonttools"),
    _b("font_keep_punctuation", "fonts", requires="fonttools"),
    _b("font_keep_symbols", "fonts", requires="fonttools"),
    _b("font_keep_cyrillic", "fonts", requires="fonttools"),
    _b("font_keep_greek", "fonts", requires="fonttools"),
    _b("font_keep_used", "fonts", requires="fonttools"),
    _t("font_extra_chars", "fonts", 400, requires="fonttools"),
    _b("font_drop_hinting", "fonts", requires="fonttools"),
    # --- video --------------------------------------------------------------
    _c("video_codec", "video", ["auto", "h264", "hevc"], requires="ffmpeg"),
    _n("video_crf", "video", 18, 40, requires="ffmpeg"),
    _c("video_preset", "video", ["ultrafast", "superfast", "veryfast", "faster", "fast",
                                  "medium", "slow", "slower", "veryslow"], requires="ffmpeg"),
    _n("video_max_width", "video", 160, 3840, step=2, unit="px", requires="ffmpeg"),
    _n("video_max_height", "video", 90, 2160, step=2, unit="px", requires="ffmpeg"),
    _n("video_max_fps", "video", 5, 60, unit="fps", requires="ffmpeg"),
    _c("video_audio_bitrate", "video", ["24k", "32k", "48k", "56k", "64k", "96k", "128k"], requires="ffmpeg"),
    # --- audio --------------------------------------------------------------
    _c("audio_mp3_bitrate", "audio", ["24k", "32k", "48k", "64k", "96k", "128k", "192k"], requires="ffmpeg"),
    _c("audio_ogg_quality", "audio", ["-1", "0", "1", "2", "3", "4", "5", "6"], requires="ffmpeg"),
    _c("audio_wav_sample_rate", "audio", [8000, 11025, 12000, 16000, 22050, 32000, 44100], requires="ffmpeg"),
    _b("audio_mono", "audio", requires="ffmpeg"),
    # --- advanced -------------------------------------------------------------
    _t("exclude_names", "advanced", 500),
    _c("exclude_name_scope", "advanced", ["file", "path"]),
    _t("exclude_patterns", "advanced", 1000),
]
GROUPS = ["images", "atlas", "astc", "text", "fonts", "video", "audio", "advanced"]
_BY_KEY = {e["key"]: e for e in SCHEMA}

PREFS_SCHEMA = [
    {"key": "language", "type": "choice", "choices": ["en", "id"], "default": "en"},
    {"key": "theme", "type": "choice", "choices": ["system", "light", "dark"], "default": "system"},
    {"key": "reduce_motion", "type": "bool", "default": False},
    {"key": "backup", "type": "bool", "default": True},
    {"key": "gpu", "type": "bool", "default": True},
    {"key": "workers", "type": "choice", "choices": ["auto", 1, 2, 3, 4, 6, 8], "default": "auto"},
    {"key": "parallel_mode", "type": "choice", "choices": ["process", "thread"], "default": "process"},
    {"key": "progress_style", "type": "choice", "choices": ["both", "bar", "percent", "icon"], "default": "both"},
    {"key": "progress_rows", "type": "choice", "choices": [1, 2, 3, 4, 6, 8], "default": 4},
    {"key": "progress_stage", "type": "bool", "default": True},
    {"key": "welcomed", "type": "bool", "default": False},     # first-run screen was seen
]
_PREF_BY_KEY = {e["key"]: e for e in PREFS_SCHEMA}


# ----------------------------------------------------------------------
# Validation
# ----------------------------------------------------------------------
def _coerce(entry: dict, value):
    """Return the value converted to the entry's type, or raise
    ValueError with a short reason."""
    kind = entry["type"]
    if kind == "bool":
        if isinstance(value, bool):
            return value
        raise ValueError("expected true/false")
    if kind in ("int", "float"):
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError("expected a number")
        number = int(value) if kind == "int" and float(value) == int(value) else value
        if kind == "int" and not isinstance(number, int):
            raise ValueError("expected a whole number")
        if kind == "float":
            number = float(value)
        if number < entry["min"] or number > entry["max"]:
            raise ValueError(f"must be between {entry['min']} and {entry['max']}")
        return number
    if kind == "text":
        if not isinstance(value, str):
            raise ValueError("expected text")
        if len(value) > entry["maxlen"]:
            raise ValueError(f"at most {entry['maxlen']} characters")
        return value
    if kind == "choice":
        for choice in entry["choices"]:
            if value == choice and type(value) is type(choice):
                return choice
        # JSON numbers vs numeric-looking strings: accept a safe match
        for choice in entry["choices"]:
            if str(value) == str(choice):
                return choice
        raise ValueError("not an allowed option")
    raise ValueError("unknown setting type")


# ----------------------------------------------------------------------
# Store
# ----------------------------------------------------------------------
def _read_json(path: str) -> dict:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json(path: str, data: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    os.replace(tmp, path)


class SettingsStore:
    def __init__(self, settings_path: str = USER_SETTINGS_FILE, prefs_path: str = PREFS_FILE):
        self._lock = threading.Lock()
        self.settings_path = settings_path
        self.prefs_path = prefs_path
        self.overrides = {}
        self.prefs = {}
        self.problems = []          # human-readable notes about ignored saved values
        self._load()

    # -- loading ---------------------------------------------------------
    def _load(self) -> None:
        saved = _read_json(self.settings_path).get("values", {})
        if isinstance(saved, dict):
            for key, value in saved.items():
                entry = _BY_KEY.get(key)
                if entry is None:
                    continue
                try:
                    coerced = _coerce(entry, value)
                except ValueError as exc:
                    self.problems.append(f"{key}: {exc}")
                    continue
                if coerced != SETTINGS.get(key):
                    self.overrides[key] = coerced
        raw = _read_json(self.prefs_path)
        self.prefs = {"last_path": str(raw.get("last_path", ""))[:1000]}
        for entry in PREFS_SCHEMA:
            value = raw.get(entry["key"], entry["default"])
            try:
                self.prefs[entry["key"]] = _coerce(entry, value)
            except ValueError:
                self.prefs[entry["key"]] = entry["default"]

    # -- compression settings ---------------------------------------------
    def effective(self) -> dict:
        """config.SETTINGS with the user's overrides on top."""
        with self._lock:
            merged = dict(SETTINGS)
            merged.update(self.overrides)
            return merged

    def values(self) -> dict:
        eff = self.effective()
        return {e["key"]: eff.get(e["key"]) for e in SCHEMA}

    def defaults(self) -> dict:
        return {e["key"]: SETTINGS.get(e["key"]) for e in SCHEMA}

    def update_settings(self, changes: dict) -> dict:
        """Apply {key: value}. All-or-nothing: if any value is invalid,
        nothing is saved and ValueError lists every problem."""
        if not isinstance(changes, dict) or not changes:
            raise ValueError("no changes given")
        coerced, errors = {}, []
        for key, value in changes.items():
            entry = _BY_KEY.get(key)
            if entry is None:
                errors.append(f"{key}: unknown setting")
                continue
            try:
                coerced[key] = _coerce(entry, value)
            except ValueError as exc:
                errors.append(f"{key}: {exc}")
        if errors:
            raise ValueError("; ".join(errors))
        with self._lock:
            new = dict(self.overrides)
            for key, value in coerced.items():
                if value == SETTINGS.get(key):
                    new.pop(key, None)
                else:
                    new[key] = value
            _write_json(self.settings_path, {"version": 1, "values": new})
            self.overrides = new
        return self.values()

    def reset_settings(self, keys=None, group=None) -> dict:
        """Remove overrides (all, one group, or the listed keys)."""
        with self._lock:
            if keys is None and group is None:
                new = {}
            else:
                wanted = set(keys or [])
                if group:
                    wanted |= {e["key"] for e in SCHEMA if e["group"] == group}
                new = {k: v for k, v in self.overrides.items() if k not in wanted}
            _write_json(self.settings_path, {"version": 1, "values": new})
            self.overrides = new
        return self.values()

    def changed_count(self) -> int:
        return len(self.overrides)

    # -- app preferences ----------------------------------------------------
    def update_prefs(self, changes: dict) -> dict:
        if not isinstance(changes, dict) or not changes:
            raise ValueError("no changes given")
        coerced, errors = {}, []
        for key, value in changes.items():
            entry = _PREF_BY_KEY.get(key)
            if entry is None:
                errors.append(f"{key}: unknown preference")
                continue
            try:
                coerced[key] = _coerce(entry, value)
            except ValueError as exc:
                errors.append(f"{key}: {exc}")
        if errors:
            raise ValueError("; ".join(errors))
        with self._lock:
            self.prefs.update(coerced)
            self._save_prefs()
        return dict(self.prefs)

    def set_last_path(self, path: str) -> None:
        with self._lock:
            self.prefs["last_path"] = str(path)[:1000]
            self._save_prefs()

    def _save_prefs(self) -> None:
        try:
            _write_json(self.prefs_path, self.prefs)
        except OSError:
            pass

    # -- UI payload -----------------------------------------------------------
    def describe(self) -> dict:
        defaults = self.defaults()
        return {
            "groups": GROUPS,
            "schema": [dict(e, default=defaults.get(e["key"])) for e in SCHEMA],
            "values": self.values(),
            "prefs_schema": PREFS_SCHEMA,
            "problems": list(self.problems),
        }
