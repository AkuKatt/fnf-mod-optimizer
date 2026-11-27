"""
optimizers/video_optimizer.py
-----------------------------
Preset-driven MP4 compression via FFmpeg.

Rules enforced:
- Never upscale (only downscale when the source exceeds the target box).
- Aspect ratio is always preserved - no cropping, no stretching.
- FPS is only lowered, never raised.
- The original file is only replaced if the new file is valid & smaller.

Hardware support:
This module does not assume any particular GPU vendor. config.py probes
FFmpeg once at startup for every hardware encoder family it knows about
(NVIDIA NVENC, AMD AMF, Intel QSV, Apple VideoToolbox) and reports what
it found in GPU_ENCODERS. At encode time this file just walks whatever
was found, in order, and falls back automatically:

    preferred GPU family (if use_gpu + one was found)
        -> any other detected GPU family
        -> CPU (libx264 / libx265)

A GPU "being detected" only means FFmpeg was compiled with that
encoder - it doesn't guarantee the actual driver/hardware will accept
a given job (wrong vendor for this machine, driver too old, VRAM busy,
codec not supported by this particular GPU generation, etc). Every
attempt that fails for any reason simply moves on to the next
candidate, ending at CPU encoding, which always works if FFmpeg has
libx264 (every normal FFmpeg build does) - so this never hard-fails
just because a preferred GPU path didn't pan out.

On Termux/Android, GPU_ENCODERS is normally empty (typical Termux
FFmpeg builds have no GPU encoders compiled in), so every job goes
straight to CPU - identical to the tool's original Termux behavior.

Codec support:
video_codec in the preset picks "h264" (libx264 / *_nvenc / *_amf /
*_qsv / *_videotoolbox - universally playable) or "hevc" (libx265 /
hevc_nvenc / hevc_amf / hevc_qsv / hevc_videotoolbox - smaller files,
but playback support depends on the mod's video player build and the
target device, so it's opt-in per preset rather than a default).
"""

import os
import subprocess

import ffrun
import threading
import tools
from config import FFMPEG_TIMEOUT, TEMP_SUFFIX, GPU_VIDEO_CONCURRENCY, threads_per_worker

# Consumer GPUs (of any vendor) have only one or two physical hardware
# encoder chips, so this bounds how many videos are actually being
# encoded on the GPU at once - independent of how many worker threads
# exist - rather than letting every worker pile onto the same encoder
# chip simultaneously.
_gpu_slot = threading.Semaphore(GPU_VIDEO_CONCURRENCY)

# Per-family NVENC/AMF/QSV/VideoToolbox CLI shape. Each entry describes
# how to build the GPU-specific args given a resolved encoder name
# (e.g. "h264_nvenc") - the scale/fps filters and audio args are shared
# and built once in _build_common_args / added by the caller.
_NVENC_PRESET_MAP = None  # nvenc uses its own p1..p7 dial, handled directly


def _build_gpu_encode_args(family: str, encoder_name: str, preset: dict, crf: int):
    """Return the ffmpeg args list for -c:v onward (video-side only) for
    one specific hardware encoder. Kept separate per family because
    NVENC/AMF/QSV each expose quality control differently."""
    if family == "nvenc":
        nvenc_preset = preset.get("video_nvenc_preset", "p5")
        nvenc_tune = preset.get("video_nvenc_tune", "hq")
        return [
            "-c:v", encoder_name,
            "-preset", nvenc_preset,   # p1 (fastest) .. p7 (slowest/best quality)
            "-tune", nvenc_tune,
            "-rc", "vbr",
            "-cq", str(crf),
            "-b:v", "0",
        ]
    if family == "amf":
        # AMF's quality dial is "quality" (speed/balanced/quality) plus
        # its own quantizer-based rate control; qp_i/qp_p approximate a
        # CRF-like fixed-quality target closely enough for this tool's
        # purposes (mod video assets, not mastering-grade encodes).
        amf_quality = preset.get("video_amf_quality", "quality")
        return [
            "-c:v", encoder_name,
            "-quality", amf_quality,
            "-rc", "cqp",
            "-qp_i", str(crf),
            "-qp_p", str(crf),
        ]
    if family == "qsv":
        # Intel QSV: global_quality is its closest CRF equivalent.
        return [
            "-c:v", encoder_name,
            "-preset", preset.get("video_qsv_preset", "medium"),
            "-global_quality", str(crf),
        ]
    if family == "videotoolbox":
        # VideoToolbox has no CRF concept - it's bitrate/quality (0-100)
        # based. Approximate: map crf (lower=better) onto q (higher=better).
        vt_quality = max(1, min(100, 100 - crf * 2))
        return [
            "-c:v", encoder_name,
            "-q:v", str(vt_quality),
        ]
    return None


def _resolve_codec_target(preset: dict, family: str = None):
    """Return the encoder name to use for the requested codec, for a
    given family ("cpu" or a GPU family name from GPU_ENCODERS)."""
    codec = preset.get("video_codec", "h264")
    if family is None or family == "cpu":
        if codec == "hevc":
            return "libx265" if tools.CPU_HEVC_AVAILABLE else None
        return "libx264"
    entry = tools.GPU_ENCODERS.get(family, {})
    return entry.get("hevc") if codec == "hevc" else entry.get("h264")


def _probe(path: str):
    """Return (width, height, fps) using ffprobe, or (None, None, None)
    if it fails."""
    if not tools.FFPROBE_PATH:
        return None, None, None
    try:
        cmd = [
            tools.FFPROBE_PATH, "-v", "error",
            "-select_streams", "v:0",
            "-show_entries", "stream=width,height,r_frame_rate",
            "-of", "csv=s=x:p=0",
            path,
        ]
        out = subprocess.run(
            cmd, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=30,
        ).stdout.strip()
        parts = out.split("x")
        width, height, rate = int(parts[0]), int(parts[1]), parts[2]
        if "/" in rate:
            num, den = rate.split("/")
            fps = float(num) / float(den) if float(den) != 0 else 24.0
        else:
            fps = float(rate)
        return width, height, fps
    except Exception:
        return None, None, None


def _build_common_args(path: str, preset: dict):
    """Compute the scale/fps filter args shared by every encode path
    (GPU or CPU, any codec), so they always produce comparable output."""
    max_width = preset.get("video_max_width", 960)
    max_height = preset.get("video_max_height", 540)
    max_fps = preset.get("video_max_fps", 24)

    width, height, fps = _probe(path)

    filters = []
    if width and height and (width > max_width or height > max_height):
        filters.append(
            f"scale={max_width}:{max_height}:"
            f"force_original_aspect_ratio=decrease:force_divisible_by=2"
        )

    target_fps = max_fps
    if fps and fps <= max_fps:
        target_fps = None  # never raise the frame rate

    return filters, target_fps


def _build_cmd(path: str, tmp_path: str, preset: dict, filters, target_fps,
               family: str, encoder_name: str):
    """Build the full ffmpeg command for one specific target: either
    family="cpu" (libx264/libx265) or a GPU family name with its
    resolved hardware encoder name."""
    crf = preset.get("video_crf", 28)
    audio_bitrate = preset.get("video_audio_bitrate", "96k")

    cmd = [tools.FFMPEG_PATH, "-y", "-nostdin", "-threads", str(threads_per_worker(preset, 2)), "-i", path]
    if filters:
        cmd += ["-vf", ",".join(filters)]
    if target_fps:
        cmd += ["-r", str(target_fps)]

    if family == "cpu":
        ffmpeg_preset = preset.get("video_preset", "veryfast")
        cmd += [
            "-c:v", encoder_name,
            "-preset", ffmpeg_preset,
            "-crf", str(crf),
            "-threads", str(threads_per_worker(preset, 2)),
        ]
    else:
        gpu_args = _build_gpu_encode_args(family, encoder_name, preset, crf)
        if gpu_args is None:
            return None
        cmd += gpu_args

    cmd += [
        "-c:a", "aac",
        "-b:a", audio_bitrate,
        "-movflags", "+faststart",
        tmp_path,
    ]
    return cmd


def _run_ffmpeg(cmd) -> bool:
    try:
        source = cmd[cmd.index("-i") + 1]
        return ffrun.run_ffmpeg(cmd, source, FFMPEG_TIMEOUT, "encode") == 0   # reports real progress
    except Exception:
        return False


def _encode_candidates(path: str, preset: dict):
    """Yield (family, encoder_name) pairs to try, in priority order:
    the preset's preferred GPU family first (if use_gpu is on and it
    was detected + supports the requested codec), then every other
    detected GPU family, then CPU last. On a machine with no GPU
    encoders at all (e.g. most Termux setups) this yields just one
    ("cpu", ...) candidate, same as the tool's original behavior."""
    want_gpu = preset.get("use_gpu", False) and bool(tools.GPU_ENCODERS)
    tried_families = []

    if want_gpu:
        preferred = preset.get("video_gpu_family")  # None = no preference
        family_order = list(tools.GPU_ENCODERS.keys())
        if preferred in tools.GPU_ENCODERS:
            family_order.remove(preferred)
            family_order.insert(0, preferred)

        for family in family_order:
            encoder_name = _resolve_codec_target(preset, family)
            if encoder_name:
                tried_families.append(family)
                yield family, encoder_name

    cpu_encoder = _resolve_codec_target(preset, "cpu")
    if cpu_encoder:
        yield "cpu", cpu_encoder
    elif preset.get("video_codec") == "hevc":
        # Asked for HEVC but neither a GPU HEVC encoder nor libx265 is
        # available anywhere on this machine - fall back to CPU H.264
        # rather than failing the file outright.
        yield "cpu", "libx264"


def optimize_video(path: str, preset: dict):
    """
    Optimize a single MP4 in place using the given preset.

    Tries hardware encoders (in the order described by
    _encode_candidates) before falling back to CPU encoding, and always
    falls back automatically if a given attempt fails for any reason
    (wrong GPU vendor, driver issue, GPU busy, encoder not actually
    supported by this hardware despite FFmpeg listing it, etc).

    Returns a tuple: (status, original_size, final_size)
    status is one of "SUCCESS", "SKIPPED", "FAILED"
    """
    original_size = os.path.getsize(path)

    if not tools.FFMPEG_PATH or not tools.FFPROBE_PATH:
        return "FAILED", original_size, original_size

    tmp_path = path + TEMP_SUFFIX + ".mp4"

    filters, target_fps = _build_common_args(path, preset)

    try:
        ok = False
        for family, encoder_name in _encode_candidates(path, preset):
            cmd = _build_cmd(path, tmp_path, preset, filters, target_fps,
                              family, encoder_name)
            if cmd is None:
                continue

            if family == "cpu":
                ok = _run_ffmpeg(cmd)
            else:
                with _gpu_slot:
                    ok = _run_ffmpeg(cmd)

            if ok:
                break
            _cleanup(tmp_path)

        if not ok or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            _cleanup(tmp_path)
            return "FAILED", original_size, original_size

        new_size = os.path.getsize(tmp_path)

        if new_size >= original_size:
            _cleanup(tmp_path)
            return "SKIPPED", original_size, original_size

        os.replace(tmp_path, path)
        return "SUCCESS", original_size, new_size

    except Exception:
        _cleanup(tmp_path)
        return "FAILED", original_size, original_size


def _cleanup(tmp_path: str) -> None:
    try:
        if os.path.exists(tmp_path):
            os.remove(tmp_path)
    except OSError:
        pass
