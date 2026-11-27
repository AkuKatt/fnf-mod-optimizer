"""
tools.py
--------
Finds the external programs and hardware the optimizer can use.

Everything is auto-detected. Programs installed from the web UI go to
APP_BIN (~/.fnf_mod_optimizer/bin), which is searched too, so no PATH
editing is ever needed. Call refresh() after installing something and
the new paths are picked up immediately - always read the values as
`tools.FFMPEG_PATH` (attribute access at call time), never copy them
with `from tools import FFMPEG_PATH`, or you keep a stale value.
"""

import os
import shutil
import subprocess


IS_WINDOWS = os.name == "nt"

APP_DIR = os.path.join(os.path.expanduser("~"), ".fnf_mod_optimizer")
APP_BIN = os.path.join(APP_DIR, "bin")


def _is_termux() -> bool:
    prefix = os.environ.get("PREFIX", "")
    return "com.termux" in prefix


IS_TERMUX = _is_termux()

_ASTCENC_CANDIDATES = [
    "astcenc", "astcenc-neon", "astcenc-avx2", "astcenc-sse4.1",
    "astcenc-sse2", "astcenc-none",
]

_HERE = os.path.dirname(os.path.abspath(__file__))
_TERMUX_BIN = os.path.join(os.environ.get("PREFIX", "/data/data/com.termux/files/usr"), "bin")


def _ensure_app_bin_on_path() -> None:
    """Put APP_BIN on this process's PATH (children inherit it, and a
    bare "ffmpeg" in any subprocess call resolves too)."""
    path = os.environ.get("PATH", "")
    if APP_BIN not in path.split(os.pathsep):
        os.environ["PATH"] = APP_BIN + os.pathsep + path


def _exe(name: str) -> str:
    return name + ".exe" if IS_WINDOWS else name


def _first_existing(paths):
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def _find_astcenc():
    for name in _ASTCENC_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
    names = [_exe(n) for n in _ASTCENC_CANDIDATES]
    bases = [APP_BIN, _TERMUX_BIN] if not IS_WINDOWS else [
        APP_BIN, os.path.join(_HERE, "astcenc-bin"), _HERE]
    return _first_existing(os.path.join(b, n) for b in bases for n in names)


def _find_oxipng():
    found = shutil.which("oxipng")
    if found:
        return found
    return _first_existing([
        os.path.join(APP_BIN, _exe("oxipng")),
        os.path.join(_HERE, "oxipng.exe") if IS_WINDOWS else None,
        os.path.join(_TERMUX_BIN, "oxipng") if not IS_WINDOWS else None,
        os.path.expanduser("~/.cargo/bin/oxipng"),
    ])


def _find_ffmpeg_tool(name: str):
    found = shutil.which(name)
    if found:
        return found
    return _first_existing([
        os.path.join(APP_BIN, _exe(name)),
        os.path.join(_HERE, "ffmpeg-bin", name + ".exe") if IS_WINDOWS else None,
    ])


_GPU_ENCODER_FAMILIES = [
    ("nvenc", "h264_nvenc", "hevc_nvenc"),
    ("amf", "h264_amf", "hevc_amf"),
    ("qsv", "h264_qsv", "hevc_qsv"),
    ("videotoolbox", "h264_videotoolbox", "hevc_videotoolbox"),
]


def _list_ffmpeg_encoders() -> str:
    if not FFMPEG_PATH:
        return ""
    try:
        result = subprocess.run(
            [FFMPEG_PATH, "-hide_banner", "-encoders"],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, text=True, timeout=15,
        )
        return result.stdout
    except Exception:
        return ""


def _detect_gpu_encoders(encoders_text: str) -> dict:
    found = {}
    for family, h264_name, hevc_name in _GPU_ENCODER_FAMILIES:
        has_h264 = h264_name in encoders_text
        has_hevc = hevc_name in encoders_text
        if has_h264 or has_hevc:
            found[family] = {"h264": h264_name if has_h264 else None,
                             "hevc": hevc_name if has_hevc else None}
    return found


# ---- module state (rewritten by refresh()) ---------------------------
ASTCENC_PATH = None
OXIPNG_PATH = None
FFMPEG_PATH = None
FFPROBE_PATH = None
FFMPEG_AVAILABLE = False
GPU_ENCODERS = {}
GPU_AVAILABLE = False
CPU_HEVC_AVAILABLE = False
NVENC_AVAILABLE = False
DEFAULT_VIDEO_CODEC = "h264"


def refresh() -> None:
    """Re-detect every external tool (cheap: a few PATH lookups plus one
    `ffmpeg -encoders` call when ffmpeg exists)."""
    global ASTCENC_PATH, OXIPNG_PATH, FFMPEG_PATH, FFPROBE_PATH, FFMPEG_AVAILABLE
    global GPU_ENCODERS, GPU_AVAILABLE, CPU_HEVC_AVAILABLE, NVENC_AVAILABLE, DEFAULT_VIDEO_CODEC

    _ensure_app_bin_on_path()
    ASTCENC_PATH = _find_astcenc()
    OXIPNG_PATH = _find_oxipng()
    FFMPEG_PATH = _find_ffmpeg_tool("ffmpeg")
    FFPROBE_PATH = _find_ffmpeg_tool("ffprobe")
    FFMPEG_AVAILABLE = FFMPEG_PATH is not None and FFPROBE_PATH is not None

    encoders = _list_ffmpeg_encoders() if FFMPEG_AVAILABLE else ""
    GPU_ENCODERS = _detect_gpu_encoders(encoders)
    GPU_AVAILABLE = bool(GPU_ENCODERS)
    CPU_HEVC_AVAILABLE = "libx265" in encoders
    NVENC_AVAILABLE = "nvenc" in GPU_ENCODERS
    if IS_TERMUX:
        DEFAULT_VIDEO_CODEC = "h264"
    else:
        DEFAULT_VIDEO_CODEC = "hevc" if (NVENC_AVAILABLE or CPU_HEVC_AVAILABLE) else "h264"


refresh()
