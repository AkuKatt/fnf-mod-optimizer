"""
optimizers/astc_optimizer.py
-----------------------------
Optimizes .astc texture files by round-tripping through astcenc.

ASTC is a *fixed-rate* format: for a given block footprint the file size
is fully determined by the pixel dimensions (16 bytes per block + 16
byte header). Two levers therefore exist:

  1. BLOCK SIZE (the only thing that shrinks the raw file): a larger
     footprint = fewer blocks = smaller file. `astc_max_block` is the
     largest block EDGE allowed (12 = the ASTC maximum, 12x12); the
     biggest footprint within that limit that still passes validation
     is found by binary search along config.ASTC_BLOCK_LADDER (fewer
     slow encodes than trying every size).
  2. PRE-PROCESSING of the pixels before encoding (does not change the
     raw size, but makes the data flatter/more repetitive, which is
     what ZIP / 7z / zstd of the mod exploit, and lets the encoder
     spend its bits better):
        .astc -> decode -> PNG -> clear dirty alpha + color
        quantization (same code as the PNG optimizer) -> encode again
     plus optional simplified-mode encoder flags ("low entropy"), only
     those the installed astcenc actually lists in `astcenc -help`.

Everything is validated against the DECODED ORIGINAL (not the
pre-processed image), so quantization error counts against the budget:
fingerprint check (`astc_tolerance`) AND a full-resolution visible-pixel
error cap (`astc_max_mae`). If nothing passes the file is left alone.
"""

import os
import re
import struct
import subprocess
import threading
import zlib

import tools
from progress_channel import report
from config import ASTC_BLOCK_LADDER, ASTCENC_TIMEOUT, TEMP_SUFFIX, threads_per_worker
from optimizers import png_codec

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

_DEFAULT_ASTC_TOLERANCE = 26
_DEFAULT_ASTC_MAX_MAE = 30.0

_MAGIC = 0x5CA1AB13

# Simplified-mode flags (candidates, used only if listed by `-help`).
# Fewer partitions / block modes -> more regular bitstream.
_LOW_ENTROPY_FLAGS = [
    ("-partitioncountlimit", "1"),
    ("-blockmodelimit", "50"),
]

_flags_lock = threading.Lock()
_supported_flags = None


def _astc_supported_flags() -> set:
    """Flags mentioned in `astcenc -help` (cached). Empty set if the
    help text can't be read - every optional flag is then skipped."""
    global _supported_flags
    with _flags_lock:
        if _supported_flags is not None:
            return _supported_flags
        found = set()
        if tools.ASTCENC_PATH:
            try:
                res = subprocess.run(
                    [tools.ASTCENC_PATH, "-help"], stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    timeout=20,
                )
                text = res.stdout.decode("utf-8", "replace")
                found = set(re.findall(r"(?<![\w-])(-[A-Za-z][A-Za-z0-9]*)", text))
            except Exception:
                found = set()
        _supported_flags = found
        return found


def _read_header(path: str):
    """Return (block_x, block_y, block_z, dim_x, dim_y, dim_z) from an
    .astc file's 16-byte header, or None if it doesn't look valid."""
    try:
        with open(path, "rb") as f:
            header = f.read(16)
        if len(header) != 16:
            return None
        magic = struct.unpack("<I", header[0:4])[0]
        if magic != _MAGIC:
            return None
        block_x, block_y, block_z = header[4], header[5], header[6]
        dim_x = header[7] | (header[8] << 8) | (header[9] << 16)
        dim_y = header[10] | (header[11] << 8) | (header[12] << 16)
        dim_z = header[13] | (header[14] << 8) | (header[15] << 16)
        return block_x, block_y, block_z, dim_x, dim_y, dim_z
    except OSError:
        return None


def _target_block_size(current: tuple, max_edge: int) -> tuple:
    """Largest ladder footprint whose width AND height are <= max_edge
    (never smaller than the current one). max_edge 0/None = keep."""
    if not max_edge:
        return current
    try:
        idx = ASTC_BLOCK_LADDER.index(current)
    except ValueError:
        return current
    best = idx
    for j in range(idx + 1, len(ASTC_BLOCK_LADDER)):
        bx, by = ASTC_BLOCK_LADDER[j]
        if bx <= max_edge and by <= max_edge:
            best = j
    return ASTC_BLOCK_LADDER[best]


def _run(cmd) -> bool:
    try:
        result = subprocess.run(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=ASTCENC_TIMEOUT,
        )
        return result.returncode == 0
    except Exception:
        return False


def _decompress(astc_path: str, png_path: str, profile: str) -> bool:
    return _run([tools.ASTCENC_PATH, f"-d{profile}", astc_path, png_path])


def _compress(png_path: str, astc_path: str, block: tuple, quality: str, profile: str,
              alpha_weight_radius: int = 0, extra_flags=(), threads: int = 1) -> bool:
    block_str = f"{block[0]}x{block[1]}"
    supported = _astc_supported_flags()
    cmd = [tools.ASTCENC_PATH, f"-c{profile}", png_path, astc_path, block_str, quality]
    if alpha_weight_radius > 0:
        # Weight per-texel error by nearby alpha so bits are spent where
        # the texture is actually visible. Only passed for RGBA sources.
        cmd += ["-a", str(alpha_weight_radius)]
    if "-silent" in supported:
        cmd.append("-silent")
    if "-j" in supported and threads > 1:
        cmd += ["-j", str(threads)]
    for flag, value in extra_flags:
        cmd += [flag, value] if value is not None else [flag]
    return _run(cmd)


def _decompress_detect_profile(astc_path: str, png_path: str):
    for profile in ("s", "l"):
        if os.path.exists(png_path):
            os.remove(png_path)
        if _decompress(astc_path, png_path, profile) and os.path.exists(png_path) and os.path.getsize(png_path) > 0:
            return profile
    return None


def _deflated_size(path: str) -> int:
    with open(path, "rb") as f:
        return len(zlib.compress(f.read(), 9))


def _cleanup_prefix(prefix: str) -> None:
    folder = os.path.dirname(prefix) or "."
    base = os.path.basename(prefix)
    try:
        for name in os.listdir(folder):
            if name.startswith(base):
                try:
                    os.remove(os.path.join(folder, name))
                except OSError:
                    pass
    except OSError:
        pass


def optimize_astc(path: str, preset: dict):
    """
    Optimize a single .astc file in place using the given preset.

    Returns a tuple: (status, original_size, final_size)
    status is one of "SUCCESS", "SKIPPED", "FAILED"
    """
    original_size = os.path.getsize(path)

    if not tools.ASTCENC_PATH or not PIL_AVAILABLE:
        return "FAILED", original_size, original_size

    header = _read_header(path)
    if header is None:
        return "FAILED", original_size, original_size
    block_x, block_y, block_z, dim_x, dim_y, dim_z = header

    if block_z != 1 or dim_z != 1:
        # 3D textures aren't something FNF mods use - leave untouched.
        return "FAILED", original_size, original_size

    max_block = int(preset.get("astc_max_block", 12) or 0)
    quality = preset.get("astc_quality", "-medium")
    tolerance = preset.get("astc_tolerance", _DEFAULT_ASTC_TOLERANCE)
    max_mae = float(preset.get("astc_max_mae", _DEFAULT_ASTC_MAX_MAE))
    alpha_weight_radius = int(preset.get("astc_alpha_weight_radius", 0))
    do_quantize = bool(preset.get("astc_quantize", False))
    do_clear = bool(preset.get("astc_clear_dirty_alpha", False))
    do_low_entropy = bool(preset.get("astc_low_entropy", False))
    target = _target_block_size((block_x, block_y), max_block)

    threads = threads_per_worker(preset)
    prefix = path + TEMP_SUFFIX + ".astcwork"
    ref_png = prefix + ".ref.png"
    prep_png = prefix + ".prep.png"
    cand_png = prefix + ".cand.png"

    try:
        report(0.04, "read")
        profile = _decompress_detect_profile(path, ref_png)
        report(0.14, "analyze")
        if profile is None:
            return "FAILED", original_size, original_size

        from optimizers.image_optimizer import (
            _canonicalize, _fingerprint, _fingerprints_close, pick_palette,
        )
        try:
            with Image.open(ref_png) as ref_img:
                ref_img.load()
                if ref_img.size != (dim_x, dim_y):
                    return "FAILED", original_size, original_size
                ref_canon = _canonicalize(ref_img)
        except Exception:
            return "FAILED", original_size, original_size

        has_alpha = ref_canon.mode == "RGBA"
        fp_mode = "RGBA" if has_alpha else "RGB"
        ref_fp = _fingerprint(ref_canon, fp_mode)
        effective_alpha_radius = alpha_weight_radius if has_alpha else 0

        # ---- pre-processing: dirty alpha + color quantization --------
        source_png = ref_png
        if do_quantize or (do_clear and has_alpha):
            prepared = ref_canon
            if do_clear and has_alpha:
                prepared = png_codec.clear_dirty_alpha(prepared)
            if do_quantize:
                try:
                    q = pick_palette(
                        prepared,
                        int(preset.get("astc_quantize_colors", 64)),
                        True,
                        float(preset.get("astc_quantize_max_mae", 3.0)),
                        float(preset.get("png_quantize_max_bad_fraction", 0.03)),
                        int(preset.get("png_quantize_max_palette", 256)),
                    )
                    # astcenc wants a normal RGB(A) PNG, not a palette one
                    prepared = q.convert("RGBA" if has_alpha else "RGB")
                except ValueError:
                    pass   # too detailed to quantize safely: keep as is
            prepared.save(prep_png, format="PNG", compress_level=1)
            source_png = prep_png

        report(0.26, "analyze")
        _enc = [0]

        def _valid(cand_astc_path: str) -> bool:
            if not os.path.exists(cand_astc_path) or os.path.getsize(cand_astc_path) == 0:
                return False
            hdr = _read_header(cand_astc_path)
            if hdr is None or (hdr[3], hdr[4]) != (dim_x, dim_y):
                return False
            if os.path.exists(cand_png):
                os.remove(cand_png)
            if not _decompress(cand_astc_path, cand_png, profile):
                return False
            try:
                with Image.open(cand_png) as cand_img:
                    cand_img.load()
                    if cand_img.size != (dim_x, dim_y):
                        return False
                    cand_canon = _canonicalize(cand_img)
                    if not _fingerprints_close(ref_fp, _fingerprint(cand_canon, fp_mode), tolerance):
                        return False
                    if png_codec.visible_mae(ref_canon, cand_canon) > max_mae:
                        return False
            except Exception:
                return False
            return True

        def _encode(block, tag, extra=()):
            _enc[0] += 1
            report(min(0.88, 0.26 + 0.14 * _enc[0]), "encode")
            out = f"{prefix}.{tag}.astc"
            if os.path.exists(out):
                os.remove(out)
            ok = _compress(source_png, out, block, quality, profile,
                           effective_alpha_radius, extra, threads)
            return out if ok and _valid(out) else None

        try:
            start_idx = ASTC_BLOCK_LADDER.index((block_x, block_y))
        except ValueError:
            start_idx = None
        try:
            target_idx = ASTC_BLOCK_LADDER.index(target)
        except ValueError:
            target_idx = start_idx

        best_path, best_block = None, None

        # 1) biggest passing block size (binary search, assumes quality
        #    only gets worse as the block grows)
        if start_idx is not None and target_idx is not None and target_idx > start_idx:
            lo, hi = start_idx + 1, target_idx
            while lo <= hi:
                mid = (lo + hi) // 2
                cand = _encode(ASTC_BLOCK_LADDER[mid], f"b{mid}")
                if cand is not None:
                    if best_path and best_path != cand:
                        os.remove(best_path)
                    best_path, best_block = cand, ASTC_BLOCK_LADDER[mid]
                    lo = mid + 1
                else:
                    hi = mid - 1

        # 2) no bigger block possible (e.g. already 12x12, the largest
        #    2D footprint) -> re-encode the pre-processed pixels at the
        #    SAME block size. Raw size can't change (fixed rate); it is
        #    only kept if it deflates clearly smaller, i.e. it only pays
        #    off inside a ZIP/7z. The source is already lossy, so this
        #    second generation gets a much stricter error cap than a
        #    block-size bump does.
        same_block_only = best_path is None
        if same_block_only:
            if not (do_quantize or (do_clear and has_alpha) or do_low_entropy):
                return "SKIPPED", original_size, original_size
            max_mae = min(max_mae, float(preset.get("astc_same_block_max_mae", 4.0)))
            cand = _encode((block_x, block_y), "same")
            if cand is None:
                return "SKIPPED", original_size, original_size
            best_path, best_block = cand, (block_x, block_y)

        # 3) low-entropy variant of the chosen block size; keep the one
        #    that deflates smaller (raw size is identical)
        if do_low_entropy:
            supported = _astc_supported_flags()
            extra = [(f, v) for f, v in _LOW_ENTROPY_FLAGS if f in supported]
            if extra:
                alt = _encode(best_block, "le", extra)
                if alt is not None:
                    if _deflated_size(alt) < _deflated_size(best_path):
                        os.remove(best_path)
                        best_path = alt
                    else:
                        os.remove(alt)

        new_size = os.path.getsize(best_path)
        if new_size < original_size:
            os.replace(best_path, path)
            return "SUCCESS", original_size, new_size

        if new_size == original_size and same_block_only:
            # same raw size: only worth replacing when it compresses
            # noticeably better inside a ZIP (>= 5% fewer deflated bytes)
            if _deflated_size(best_path) <= _deflated_size(path) * 0.95:
                os.replace(best_path, path)
                return "SUCCESS", original_size, new_size

        return "SKIPPED", original_size, original_size

    finally:
        _cleanup_prefix(prefix)
