"""
config.py - the ONE place you edit.

Everything the optimizer does is controlled by SETTINGS below. There are
no presets: change a value, save, run again. Set a feature to False to
turn it off. External programs (ffmpeg, oxipng, astcenc) are auto-detected
in tools.py.
"""

import os

# Tool paths are NOT re-exported here on purpose: they change when a tool
# is installed from the UI, so always read them as `tools.FFMPEG_PATH` etc.
from tools import IS_WINDOWS, IS_TERMUX, APP_DIR, APP_BIN  # noqa: F401

SETTINGS = {
    # ---------------- PNG ----------------
    "png_quantize": True,              # reduce colors (lossy, the big win)
    "png_quantize_colors": 64,         # palette size to try first
    "png_quantize_adaptive": True,     # also try the SMALLEST palette that still looks right
    "png_quantize_max_palette": 256,   # adaptive search ceiling (256 = PNG limit)
    "png_quantize_max_mae": 3.0,       # adaptive quality gate: mean error (0-255)
    "png_quantize_max_bad_fraction": 0.03,  # ...and max share of visibly wrong pixels
    "png_quantize_keep_indexed": True, # save as palette PNG (1 byte/pixel) - much smaller
    "png_exact_palette": True,         # lossless palette when <= 256 colors
    "png_clear_dirty_alpha": True,     # drop invisible RGB under alpha=0
    "png_strip_metadata": True,        # drop ICC/gamma/text chunks
    "png_compress_level": 9,
    "png_optimize": True,
    "png_oxipng_level": 4,             # 0-6, -1 = off (needs oxipng installed)
    "png_oxipng_alpha": True,          # oxipng's own alpha optimization
    "png_zopfli": False,               # smallest PNG, MUCH slower

    # ---------------- JPG / WEBP ----------------
    "jpeg_quality": 0,
    "jpeg_optimize": True,
    "jpeg_progressive": True,
    "webp_quality": 0,
    "webp_method": 4,                  # 0 fast .. 6 slow/small
    "webp_allow_lossy_alpha": True,
    "image_lossless_tolerance": 3,     # how different a lossless re-save may look
    "image_lossy_tolerance": 31,       # how different a lossy re-save may look (higher = smaller files)
    "image_lossy_max_mae": None,       # optional extra full-resolution error cap (None = off)

    # ---------------- Atlas (PNG + Sparrow XML) ----------------
    # Trims empty borders and repacks. Every frame is re-rendered and
    # compared with the original first; anything unprovable is left alone.
    "atlas_trim_enabled": True,
    "atlas_trim_alpha_threshold": 0,
    "atlas_trim_padding": 2,           # empty px kept around each sprite (stops neighbours bleeding in when scaled)
    "atlas_trim_extrude": False,       # copy edge pixels into the padding (can fatten edges: off by default)
    "atlas_trim_pow2": False,
    "atlas_trim_dedupe": True,         # identical sprites share one rectangle

    # ---------------- ASTC ----------------
    # ASTC is fixed-rate: only a bigger block shrinks the raw file. Files
    # already at 12x12 (the max) can't get smaller, only more ZIP-friendly.
    "astc_max_block": 12,              # largest block edge to try: 0 = keep, 5, 6, 8, 10 or 12 (the format's maximum)
    "astc_quality": "-fast",
    "astc_tolerance": 35,
    "astc_max_mae": 30,
    "astc_same_block_max_mae": 4.0,
    "astc_alpha_weight_radius": 2,
    "astc_quantize": True,
    "astc_quantize_colors": 64,
    "astc_quantize_max_mae": 3.0,
    "astc_clear_dirty_alpha": True,
    "astc_low_entropy": True,

    # ---------------- Text-like files (all lossless, token-verified) ----------------
    "script_strip_comments_enabled": True,   # .lua .hx .hxc
    "shader_minify_enabled": True,           # .frag .vert .glsl ...
    "json_minify_enabled": True,
    "json_round_decimals": 0,                # LOSSY, off by default: round long decimals (0 = off, else digits kept)

    # ---------------- Fonts (.ttf / .otf) ----------------
    "font_subset_enabled": True,
    "font_keep_upper": True,                 # A-Z
    "font_keep_lower": True,                 # a-z
    "font_keep_digits": True,                # 0-9
    "font_keep_ascii_symbols": True,         # ! " # $ % & ' ( ) * + , - . / : ; < = > ? @ [ \\ ] ^ _ ` { | } ~
    "font_keep_latin_accents": True,         # e-acute, n-tilde, c-cedilla, u-umlaut ... (Latin-1, Extended-A/B, Vietnamese)
    "font_keep_punctuation": True,           # curly quotes, dashes, ellipsis, bullet, euro, trademark ...
    "font_keep_symbols": False,              # arrows, math, shapes, music notes, hearts, stars
    "font_keep_cyrillic": False,
    "font_keep_greek": False,
    "font_keep_used": True,                  # every non-ASCII character found in the mod's text files
    "font_extra_chars": "",                  # always keep these characters too
    "font_drop_hinting": False,              # smaller, but small text can look blurrier
    "font_subset_unicodes": None,            # None = automatic; or a set of code points (overrides the options above)

    # ---------------- Advanced ----------------
    "exclude_patterns": "",                  # files/folders never touched, e.g.  data/*.json, scripts/, readme.txt
    "exclude_names": "",                     # skip every file whose NAME contains one of these words, e.g.  fog, backup
    "exclude_name_scope": "file",            # "file" = match the file name only, "path" = the whole path (folders too)

    # ---------------- Video ----------------
    "video_codec": "auto",             # auto = hevc when this machine can encode it, else h264
    "video_crf": 30,
    "video_preset": "medium",
    "video_max_width": 854,
    "video_max_height": 480,
    "video_max_fps": 15,
    "video_audio_bitrate": "56k",
    "video_nvenc_preset": "p4",
    "video_nvenc_tune": "hq",
    "video_amf_quality": "quality",
    "video_qsv_preset": "medium",

    # ---------------- Audio ----------------
    "audio_mp3_bitrate": "32k",
    "audio_ogg_quality": "-1",
    "audio_wav_sample_rate": 12000,
    "audio_mono": False,
}

APP_NAME = "FNF Mod Optimizer"
APP_VERSION = "1.0.0"

# ---------------- Behaviour ----------------
DEFAULT_BACKUP = True            # zip the mod next to itself before changing anything

_cpu = os.cpu_count() or 4
MAX_WORKERS = max(4, min(_cpu, 8)) if IS_WINDOWS else max(2, min(_cpu, 4))
FFMPEG_THREADS_PER_WORKER = max(2, _cpu // MAX_WORKERS)

def threads_per_worker(preset: dict, minimum: int = 1) -> int:
    """CPU threads one external tool call may use: the cores divided
    evenly between the parallel workers (never oversubscribe)."""
    workers = int((preset or {}).get("_workers") or MAX_WORKERS)
    return max(minimum, _cpu // max(1, workers))

GPU_VIDEO_CONCURRENCY = 2        # hardware encoders handle 1-2 streams at once
PARALLEL_MODE = "process"        # "process" (faster) or "thread" (if processes misbehave)

# ---------------- Internals (rarely touched) ----------------
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp"}
VIDEO_EXTS = {".mp4"}
AUDIO_EXTS = {".wav", ".ogg", ".mp3"}
ASTC_EXTS = {".astc"}
FONT_EXTS = {".ttf", ".otf"}
SHADER_EXTS = {".frag", ".fsh", ".vert", ".vsh", ".glsl"}
SCRIPT_EXTS = {".lua", ".hx", ".hxc"}
JSON_EXTS = {".json"}
XML_EXT = ".xml"
ASTC_BLOCK_LADDER = [(4, 4), (5, 4), (5, 5), (6, 5), (6, 6), (8, 5), (8, 6),
                     (8, 8), (10, 5), (10, 6), (10, 8), (10, 10), (12, 10), (12, 12)]

LOG_FILE = os.path.join(APP_DIR, "optimizer.log")
SETTINGS_FILE = os.path.join(APP_DIR, "ui_settings.json")
TEMP_SUFFIX = ".fnfopt.tmp"
COPY_CHUNK_SIZE = 1024 * 1024
FFMPEG_TIMEOUT = 60 * 20
ASTCENC_TIMEOUT = 60 * 5
