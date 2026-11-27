"""
optimizers/image_optimizer.py
-----------------------------
Preset-driven image optimization for PNG / JPG / WEBP.

Rules enforced:
- Resolution (width/height) NEVER changes -> TextureAtlas / XML atlas
  coordinates stay valid.
- The original file is only replaced if the optimized version is both
  valid (same dimensions, passes content validation) and smaller.
- Animated PNG/WEBP (more than one frame) and 16-bit / float images are
  never touched - re-saving them through Pillow would flatten or
  degrade them.

Every image is first "baked" into plain RGBA/RGB for analysis. That is
only the WORKING copy: what gets written can still be an indexed/palette
PNG (see png_quantize_keep_indexed), which is 2-3x smaller than the same
colors stored as RGBA.

PNG candidates (all validated, the SMALLEST valid one wins):
  1. quantized palette (lossy, optional adaptive palette size)
  2. exact palette      (lossless, only if <= 256 unique colors)
  3. plain RGBA/RGB     (lossless fallback)
Then the winner gets the optional external oxipng pass.
"""

import os
import shutil

from progress_channel import report
from config import TEMP_SUFFIX
from optimizers import png_codec

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

# Fixed-size, low-memory "fingerprint" used to catch content corruption
# (washed-out colors, bled color under semi-transparent pixels, etc.).
_FINGERPRINT_SIZE = 48
_DEFAULT_LOSSLESS_TOLERANCE = 2
_DEFAULT_LOSSY_TOLERANCE = 22

_UNSUPPORTED_MODES = ("I", "F", "I;16", "I;16L", "I;16B", "I;16N")

# Adaptive palette ladder (upper bound = png_quantize_colors)
_PALETTE_LADDER = (8, 16, 24, 32, 48, 64, 96, 128, 192, 256)


def _fingerprint(img, mode: str) -> bytes:
    """Fixed-size content sample, alpha-premultiplied for RGBA so that
    "don't care" RGB under fully-transparent pixels (a normal, invisible
    artifact of some codecs) never registers as a false difference."""
    thumb = img.convert(mode).resize(
        (_FINGERPRINT_SIZE, _FINGERPRINT_SIZE), Image.NEAREST
    )
    raw = thumb.tobytes()
    if mode != "RGBA":
        return raw
    out = bytearray(len(raw))
    for i in range(0, len(raw), 4):
        a = raw[i + 3]
        out[i] = raw[i] * a // 255
        out[i + 1] = raw[i + 1] * a // 255
        out[i + 2] = raw[i + 2] * a // 255
        out[i + 3] = a
    return bytes(out)


def _fingerprints_close(before: bytes, after: bytes, tolerance: float) -> bool:
    if len(before) != len(after):
        return False
    diff = sum(abs(b - a) for b, a in zip(before, after))
    return (diff / len(before)) <= tolerance


def _canonicalize(img):
    """Return a copy of `img` in plain RGBA (if any transparency is
    present) or RGB (otherwise) - never P, PA, LA, L, 1, or CMYK."""
    mode = img.mode
    has_alpha = mode in ("RGBA", "LA", "PA") or "transparency" in img.info
    target = "RGBA" if has_alpha else "RGB"
    if mode == target:
        return img.copy()
    return img.convert(target)


def _validate(tmp_path, original_dims, original_fp, fp_mode, tolerance,
              original_had_alpha, original_alpha_extrema,
              reference=None, max_mae=None):
    """Return True if tmp_path is a safe, valid replacement. With
    `reference` + `max_mae`, the full-resolution visible-pixel error
    against the original must also stay under max_mae (the 48x48
    fingerprint alone can miss local damage)."""
    if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
        return False
    try:
        with Image.open(tmp_path) as check_img:
            if check_img.size != original_dims:
                return False
            if getattr(check_img, "n_frames", 1) != 1:
                return False
            if original_had_alpha:
                new_alpha_extrema = check_img.convert("RGBA").getchannel("A").getextrema()
                if original_alpha_extrema != (255, 255) and new_alpha_extrema == (255, 255):
                    return False
            new_fp = _fingerprint(check_img, fp_mode)
            if not _fingerprints_close(original_fp, new_fp, tolerance):
                return False
            if reference is not None and max_mae is not None:
                if png_codec.visible_mae(reference, check_img.convert("RGBA")) > max_mae:
                    return False
    except Exception:
        return False
    return True


def optimize_image(path: str, preset: dict):
    """
    Optimize a single image in place using the given preset.

    Returns a tuple: (status, original_size, final_size)
    status is one of "SUCCESS", "SKIPPED", "FAILED"
    """
    original_size = os.path.getsize(path)

    if not PIL_AVAILABLE:
        return "FAILED", original_size, original_size

    ext = os.path.splitext(path)[1].lower()
    tmp_path = path + TEMP_SUFFIX
    made = []  # every temp file created, always cleaned up

    try:
        with Image.open(path) as raw_img:
            if getattr(raw_img, "n_frames", 1) > 1 or getattr(raw_img, "is_animated", False):
                return "SKIPPED", original_size, original_size
            if raw_img.mode in _UNSUPPORTED_MODES:
                return "SKIPPED", original_size, original_size
            raw_img.load()
            original_dims = raw_img.size
            img = _canonicalize(raw_img)
            # RGB/L PNG whose transparency is a single "color key" (tRNS):
            # keep that source so it can compete as an exact candidate
            # (1 tRNS chunk instead of a full alpha channel).
            colorkey_src = None
            if (ext == ".png" and raw_img.mode in ("RGB", "L")
                    and "transparency" in raw_img.info):
                colorkey_src = (raw_img.copy(), raw_img.info["transparency"])

        report(0.08, "read")
        if preset.get("png_strip_metadata", True):
            img.info.clear()

        original_had_alpha = img.mode == "RGBA"
        original_alpha_extrema = img.getchannel("A").getextrema() if original_had_alpha else None
        fp_mode = "RGBA" if original_had_alpha else "RGB"
        original_fp = _fingerprint(img, fp_mode)

        candidates = _build_candidates(img, ext, original_had_alpha, preset, colorkey_src)
        if not candidates:
            return "FAILED", original_size, original_size

        report(0.30, "analyze")
        lossless_tolerance = preset.get("image_lossless_tolerance", _DEFAULT_LOSSLESS_TOLERANCE)
        lossy_tolerance = preset.get("image_lossy_tolerance", _DEFAULT_LOSSY_TOLERANCE)

        lossy_max_mae = preset.get("image_lossy_max_mae", 14.0)
        valid = []  # (size, is_lossy, path) of every candidate that passed
        produced = False   # at least one candidate was written to disk
        n_cand = max(1, len(candidates))
        for i, (save_fn, is_lossy) in enumerate(candidates):
            report(0.30 + 0.45 * i / n_cand, "encode")
            cand_path = f"{tmp_path}.{i}"
            made.append(cand_path)
            try:
                save_fn(cand_path)
            except Exception:
                _cleanup(cand_path)
                continue
            produced = True

            tolerance = lossy_tolerance if is_lossy else lossless_tolerance
            if not _validate(cand_path, original_dims, original_fp, fp_mode,
                             tolerance, original_had_alpha, original_alpha_extrema,
                             reference=img if is_lossy else None,
                             max_mae=lossy_max_mae if is_lossy else None):
                _cleanup(cand_path)
                continue
            valid.append((os.path.getsize(cand_path), is_lossy, cand_path))

        # Smallest valid candidate wins - except that a LOSSLESS one is
        # preferred when it is within ~3% of the best lossy one: nearly
        # the same size with zero pixel change is the better deal.
        best_size = best_lossy = best_path = None
        if valid:
            best_size, best_lossy, best_path = min(valid, key=lambda v: v[0])
            if best_lossy:
                lossless = [v for v in valid if not v[1]]
                if lossless:
                    cand = min(lossless, key=lambda v: v[0])
                    if cand[0] <= best_size * 1.03 + 16:
                        best_size, best_lossy, best_path = cand

        report(0.78, "squeeze")
        if ext == ".png":
            if best_path is not None:
                # Extra lossless pass on the winner only (oxipng is slow).
                png_codec.oxipng_squeeze(best_path, preset)
                best_size = os.path.getsize(best_path)

            # The ORIGINAL file is a candidate too: many PNGs are already
            # indexed and squeezed by a stronger encoder than Pillow's,
            # so a re-encode from pixels can't beat them - but oxipng run
            # directly on the original still can (still bit-exact).
            if png_codec.oxipng_available(preset):
                report(0.90, "squeeze")
                orig_copy = f"{tmp_path}.orig"
                made.append(orig_copy)
                try:
                    shutil.copyfile(path, orig_copy)
                    png_codec.oxipng_squeeze(orig_copy, preset)
                    osz = os.path.getsize(orig_copy)
                    take = osz < original_size and (
                        best_path is None or osz < best_size
                        or (best_lossy and osz <= best_size * 1.03 + 16))
                    if take:
                        if best_path:
                            _cleanup(best_path)
                        best_path, best_size, best_lossy = orig_copy, osz, False
                except OSError:
                    pass

        if best_path is None:
            # A re-encode was produced but it would have visibly changed
            # the image -> leave the file alone (SKIPPED). FAILED is only
            # for "no candidate could even be written".
            if produced or ext == ".png":
                return "SKIPPED", original_size, original_size
            return "FAILED", original_size, original_size

        if best_size < original_size:
            report(0.99, "write")
            os.replace(best_path, path)
            return "SUCCESS", original_size, best_size

        # A safe, faithful re-encode exists - it just isn't smaller.
        return "SKIPPED", original_size, original_size

    except Exception:
        return "FAILED", original_size, original_size
    finally:
        for p in made:
            _cleanup(p)


# ----------------------------------------------------------------------
# Candidates
# ----------------------------------------------------------------------
def _palette_ok(img, q, max_mae: float, max_bad: float) -> bool:
    back = q.convert("RGBA")
    return (png_codec.visible_mae(img, back) <= max_mae
            and png_codec.visible_bad_fraction(img, back) <= max_bad)


def pick_palette(img, max_colors: int, adaptive: bool, max_mae: float,
                 max_bad: float = 0.03, max_palette: int = 256):
    """Quantize `img` to a palette and return a P-mode image.

    Non-adaptive: exactly `max_colors` entries, and it must pass the
    quality gate.

    Adaptive: binary-search the ladder 8..`max_palette` for the SMALLEST
    palette that passes the gate. `max_colors` is therefore the size you
    WANT; if it is too coarse for a particular image, the search goes up
    (never above `max_palette`, 256 = the PNG limit) instead of giving
    up - a 200-color indexed PNG is still ~4x smaller than RGBA.

    Quality gate (visible pixels only, alpha-premultiplied): mean
    absolute error <= max_mae AND at most `max_bad` of the pixels are
    off by more than 16 in any band. When nothing passes, ValueError is
    raised and the caller simply gets no lossy candidate (lossless ones
    still compete).
    """
    max_colors = max(2, min(256, int(max_colors)))
    max_palette = max(max_colors, min(256, int(max_palette)))

    # Image already has few enough colors? A palette of exactly those
    # colors is lossless - and it is the ceiling of the search: only
    # SMALLER palettes are worth trying (they may still pass the gate).
    exact = None
    n_exact = None
    counted = img.getcolors(maxcolors=256)
    if counted is not None:
        exact = png_codec.exact_palette(img)
        n_exact = len(counted) if exact is not None else None

    if adaptive:
        ladder = sorted({c for c in _PALETTE_LADDER if c <= max_palette} | {max_colors, max_palette})
    else:
        ladder = [max_colors]
    if n_exact is not None:
        ladder = [c for c in ladder if c < n_exact]
        if not ladder:
            return exact

    def ok(q):
        back = q.convert("RGBA")
        return (png_codec.visible_mae(img, back) <= max_mae
                and png_codec.visible_bad_fraction(img, back) <= max_bad)

    lo, hi = 0, len(ladder) - 1
    best = None
    while lo <= hi:
        mid = (lo + hi) // 2
        q = png_codec.quantize_rgba(img, ladder[mid])
        if ok(q):
            best = q
            hi = mid - 1
        else:
            lo = mid + 1
    if best is not None:
        return best
    if exact is not None:
        return exact
    raise ValueError("palette quantization would visibly change the image")


def _build_candidates(img, ext: str, has_alpha: bool, preset: dict, colorkey_src=None):
    """Return [(save_fn, is_lossy), ...]. Order doesn't matter - every
    candidate is validated and the smallest valid one wins."""
    candidates = []

    if ext == ".png":
        work = img
        if has_alpha and preset.get("png_clear_dirty_alpha", True):
            work = png_codec.clear_dirty_alpha(img)
        keep_indexed = bool(preset.get("png_quantize_keep_indexed", True))
        back_mode = "RGBA" if has_alpha else "RGB"

        if preset.get("png_quantize"):
            max_colors = preset.get("png_quantize_colors", 256)

            # (a) FIXED palette of exactly png_quantize_colors - what the
            #     earlier versions did. Judged only by the fingerprint
            #     tolerance (image_lossy_tolerance) + alpha sanity, so it
            #     stays as aggressive as the preset asks. Now written as
            #     an INDEXED png (and dirty-alpha cleared), which is the
            #     same pixels in far fewer bytes.
            def save_fixed_palette(tmp_path):
                q = png_codec.quantize_rgba(work, max_colors)
                if not keep_indexed:
                    q = q.convert(back_mode)
                png_codec.save_png(q, tmp_path, preset)

            candidates.append((save_fixed_palette, True))

            # (b) ADAPTIVE palette: smallest palette (8..256 colors) that
            #     passes a strict full-resolution error gate. Wins on
            #     simple images (16 colors are enough) - never required.
            if preset.get("png_quantize_adaptive", True):
                max_mae = float(preset.get("png_quantize_max_mae", 3.0))
                max_bad = float(preset.get("png_quantize_max_bad_fraction", 0.03))
                max_palette = int(preset.get("png_quantize_max_palette", 256))

                def save_adaptive_palette(tmp_path):
                    q = pick_palette(work, max_colors, True, max_mae, max_bad, max_palette)
                    if not keep_indexed:
                        q = q.convert(back_mode)
                    png_codec.save_png(q, tmp_path, preset)

                candidates.append((save_adaptive_palette, True))

        if preset.get("png_exact_palette", True):
            def save_exact_palette(tmp_path):
                q = png_codec.exact_palette(work)
                if q is None:
                    raise ValueError("more than 256 unique colors")
                if not keep_indexed:
                    q = q.convert(back_mode)
                png_codec.save_png(q, tmp_path, preset)

            candidates.append((save_exact_palette, False))

        def save_plain(tmp_path):
            png_codec.save_png(work, tmp_path, preset)

        candidates.append((save_plain, False))

        if colorkey_src is not None:
            key_img, key = colorkey_src

            def save_colorkey(tmp_path):
                key_img.save(tmp_path, format="PNG", transparency=key,
                             optimize=bool(preset.get("png_optimize", True)),
                             compress_level=int(preset.get("png_compress_level", 9)))

            candidates.append((save_colorkey, False))

    elif ext in (".jpg", ".jpeg"):
        jpeg_img = img.convert("RGB") if img.mode != "RGB" else img
        quality = preset.get("jpeg_quality", 82)
        jpeg_optimize_flag = preset.get("jpeg_optimize", True)
        jpeg_progressive_flag = preset.get("jpeg_progressive", True)

        def save_jpeg(tmp_path, _q=quality):
            jpeg_img.save(tmp_path, format="JPEG", quality=_q,
                          optimize=jpeg_optimize_flag,
                          progressive=jpeg_progressive_flag)

        candidates.append((save_jpeg, True))

    elif ext == ".webp":
        quality = preset.get("webp_quality", 78)
        allow_lossy_alpha = preset.get("webp_allow_lossy_alpha", False)
        webp_method = preset.get("webp_method", 6)

        if has_alpha and allow_lossy_alpha:
            def save_lossy_alpha(tmp_path, _q=quality):
                img.save(tmp_path, format="WEBP", quality=_q, method=webp_method)

            candidates.append((save_lossy_alpha, True))

        if has_alpha:
            def save_lossless(tmp_path):
                img.save(tmp_path, format="WEBP", lossless=True,
                         method=webp_method, quality=100)

            candidates.append((save_lossless, False))
        else:
            def save_lossy(tmp_path, _q=quality):
                img.save(tmp_path, format="WEBP", quality=_q, method=webp_method)

            candidates.append((save_lossy, True))

    return candidates


def _cleanup(tmp_path: str) -> None:
    try:
        if tmp_path and os.path.exists(tmp_path):
            os.remove(tmp_path)
    except OSError:
        pass
