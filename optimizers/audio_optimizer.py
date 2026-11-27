"""
optimizers/audio_optimizer.py
-----------------------------
Preset-driven audio compression via FFmpeg.

Rules enforced:
- The file extension never changes.
- The original file is only replaced if the new file is valid & smaller.
"""

import os
import subprocess

import ffrun
import tools
from config import FFMPEG_TIMEOUT, TEMP_SUFFIX, threads_per_worker


def optimize_audio(path: str, preset: dict):
    """
    Optimize a single audio file in place using the given preset.

    Returns a tuple: (status, original_size, final_size)
    status is one of "SUCCESS", "SKIPPED", "FAILED"
    """
    original_size = os.path.getsize(path)

    if not tools.FFMPEG_PATH:
        return "FAILED", original_size, original_size

    ext = os.path.splitext(path)[1].lower()
    tmp_path = path + TEMP_SUFFIX + ext

    mp3_bitrate = preset.get("audio_mp3_bitrate", "96k")
    ogg_quality = preset.get("audio_ogg_quality", "3")
    wav_sample_rate = preset.get("audio_wav_sample_rate", 32000)
    force_mono = preset.get("audio_mono", False)

    # -vn drops embedded cover art (a whole JPEG/PNG carried inside many
    # mp3/ogg files). Tags such as loop points are kept: ffmpeg copies
    # global metadata by default.
    cmd = [tools.FFMPEG_PATH, "-y", "-nostdin", "-threads", str(threads_per_worker(preset, 2)), "-i", path, "-vn"]

    if ext == ".mp3":
        cmd += ["-c:a", "libmp3lame", "-b:a", mp3_bitrate]
    elif ext == ".ogg":
        cmd += ["-c:a", "libvorbis", "-q:a", ogg_quality]
    elif ext == ".wav":
        cmd += ["-c:a", "pcm_s16le", "-ar", str(wav_sample_rate)]
    else:
        return "FAILED", original_size, original_size

    if force_mono:
        cmd += ["-ac", "1"]

    cmd += [tmp_path]

    try:
        returncode = ffrun.run_ffmpeg(cmd, path, FFMPEG_TIMEOUT, "encode")   # reports real progress

        if returncode != 0 or not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
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
