"""
ffrun.py
--------
Run ffmpeg and report REAL progress (position in the media / total length)
through progress_channel. Falls back to a plain run when the length of the
file can't be read.
"""

import subprocess
import threading

import progress_channel
import tools


def media_seconds(path: str):
    """Length of a media file in seconds, or None."""
    exe = getattr(tools, "FFPROBE_PATH", None)
    if not exe:
        return None
    try:
        out = subprocess.run(
            [exe, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            text=True, timeout=30,
        ).stdout.strip()
        value = float(out.splitlines()[0])
        return value if value > 0 else None
    except Exception:
        return None


def run_ffmpeg(cmd, input_path: str, timeout: int, stage: str = "encode") -> int:
    """Like subprocess.run(cmd, ...).returncode, with live progress.
    Raises subprocess.TimeoutExpired / OSError exactly like subprocess.run."""
    total = media_seconds(input_path)
    full = [cmd[0], "-progress", "pipe:1", "-nostats"] + list(cmd[1:])
    proc = subprocess.Popen(full, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.DEVNULL, text=True, bufsize=1)
    timed_out = []

    def _kill():
        timed_out.append(True)
        try:
            proc.kill()
        except Exception:
            pass

    timer = threading.Timer(timeout, _kill)
    timer.daemon = True
    timer.start()
    try:
        for line in proc.stdout:
            if line.startswith(("out_time_us=", "out_time_ms=")):
                try:
                    seconds = int(line.split("=", 1)[1]) / 1_000_000.0
                except ValueError:
                    continue
                if total:
                    progress_channel.report(min(seconds / total, 0.99), stage)
            elif line.startswith("progress=end"):
                progress_channel.report(1.0, stage)
        proc.wait()
    finally:
        timer.cancel()
        try:
            proc.stdout.close()
        except Exception:
            pass
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    if timed_out:
        raise subprocess.TimeoutExpired(cmd, timeout)
    return proc.returncode
