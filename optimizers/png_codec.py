"""
optimizers/png_codec.py
-----------------------
Shared PNG helpers used by the image, atlas and ASTC optimizers, so all
three squeeze pixels exactly the same way:

  * clear_dirty_alpha()  - RGB hidden under alpha==0 is never visible,
                           so it is replaced by (0,0,0,0). Fewer unique
                           colors + far better deflate ratio.
  * quantize_rgba()      - palette quantization that keeps alpha.
  * exact_palette()      - LOSSLESS indexed conversion when the image has
                           <= 256 unique RGBA colors (verified exact).
  * save_png()           - writes RGBA / RGB / indexed PNG with no
                           metadata, compact bit depth for tiny palettes.
  * oxipng_squeeze()     - optional extra lossless pass (external tool),
                           written to a separate file and verified before
                           it replaces anything.
  * visible_mae()        - full-resolution, alpha-premultiplied error
                           metric limited to visible pixels.

Nothing here changes an image's width/height.
"""

import os
import subprocess
import sys
from array import array

import tools
from config import threads_per_worker

try:
    from PIL import Image, ImageChops, ImageStat
    PIL_AVAILABLE = True
except ImportError:  # pragma: no cover
    PIL_AVAILABLE = False

_ZERO_ALPHA_MASK = [255] + [0] * 255
_NONZERO_MASK = [0] + [255] * 255


# ----------------------------------------------------------------------
# Pixel helpers
# ----------------------------------------------------------------------
def clear_dirty_alpha(img):
    """Return `img` with every fully-transparent pixel set to (0,0,0,0).
    Only alpha==0 pixels are touched - partially transparent pixels keep
    their color. No-op (returns the same object) for non-RGBA images or
    images without any fully transparent pixel."""
    if img.mode != "RGBA":
        return img
    alpha = img.getchannel("A")
    if alpha.getextrema()[0] != 0:
        return img
    out = img.copy()
    out.paste((0, 0, 0, 0), None, alpha.point(_ZERO_ALPHA_MASK))
    return out


def _premultiplied(img_rgba):
    r, g, b, a = img_rgba.split()
    return Image.merge("RGBA", (
        ImageChops.multiply(r, a),
        ImageChops.multiply(g, a),
        ImageChops.multiply(b, a),
        a,
    ))


def visible_mae(a, b) -> float:
    """Mean absolute error (0-255 scale, averaged over the 4 premultiplied
    RGBA bands) measured ONLY over pixels visible in either image.
    Returns a huge number if sizes differ. Cheap: all work is done by
    Pillow's C code, no per-pixel Python loops."""
    if a.size != b.size:
        return 1e9
    a4 = a if a.mode == "RGBA" else a.convert("RGBA")
    b4 = b if b.mode == "RGBA" else b.convert("RGBA")
    visible = ImageChops.lighter(a4.getchannel("A"), b4.getchannel("A")).point(_NONZERO_MASK)
    if visible.getbbox() is None:
        return 0.0
    diff = ImageChops.difference(_premultiplied(a4), _premultiplied(b4))
    means = ImageStat.Stat(diff, mask=visible).mean
    return sum(means) / len(means)


def visible_bad_fraction(a, b, threshold: int = 16) -> float:
    """Fraction of visible pixels whose premultiplied RGBA differs by
    more than `threshold` in ANY band. A low average error can hide a
    rare-but-wrong color (quantizers merge rare colors first); this
    catches exactly that."""
    if a.size != b.size:
        return 1.0
    a4 = a if a.mode == "RGBA" else a.convert("RGBA")
    b4 = b if b.mode == "RGBA" else b.convert("RGBA")
    visible = ImageChops.lighter(a4.getchannel("A"), b4.getchannel("A")).point(_NONZERO_MASK)
    total = ImageStat.Stat(visible).sum[0] / 255.0
    if total <= 0:
        return 0.0
    diff = ImageChops.difference(_premultiplied(a4), _premultiplied(b4))
    over = _THRESH_CACHE.get(threshold)
    if over is None:
        over = [0] * (threshold + 1) + [255] * (255 - threshold)
        _THRESH_CACHE[threshold] = over
    bad = None
    for band in diff.split():
        m = band.point(over)
        bad = m if bad is None else ImageChops.lighter(bad, m)
    count = ImageStat.Stat(bad, mask=visible).sum[0] / 255.0
    return count / total


_THRESH_CACHE = {}


def identical_rgba(a, b) -> bool:
    """True when both images have identical size and pixel data."""
    if a.size != b.size:
        return False
    a4 = a if a.mode == "RGBA" else a.convert("RGBA")
    b4 = b if b.mode == "RGBA" else b.convert("RGBA")
    return ImageChops.difference(a4, b4).getbbox() is None


# ----------------------------------------------------------------------
# Palettes
# ----------------------------------------------------------------------
def quantize_rgba(img, colors: int):
    """Quantize an RGBA (or RGB) image to `colors` palette entries."""
    colors = max(2, min(256, int(colors)))
    if img.mode == "RGBA":
        return img.quantize(colors=colors, method=Image.FASTOCTREE)
    return img.quantize(colors=colors, method=Image.MEDIANCUT)


def exact_palette(img):
    """If `img` has <= 256 unique colors, return a P-mode image that is
    pixel-identical to it, else None. Built by direct color->index
    mapping (Pillow's quantize() merges similar colors even when there
    are fewer than 256, so it can't be used for a lossless conversion).
    Palette entries that are not fully opaque are placed first so the
    tRNS chunk stays short."""
    try:
        rgba = img if img.mode == "RGBA" else img.convert("RGBA")
        colors = rgba.getcolors(maxcolors=256)
        if colors is None:
            return None
        ordered = sorted(colors, key=lambda c: (c[1][3] == 255, -c[0]))
        palette = [c[1] for c in ordered]
        order = sys.byteorder
        lut = {int.from_bytes(bytes(col), order): i for i, col in enumerate(palette)}
        arr = array("I")
        arr.frombytes(rgba.tobytes())
        indices = bytes(map(lut.__getitem__, arr))
        out = Image.frombytes("P", rgba.size, indices)
        opaque = all(col[3] == 255 for col in palette)
        flat = []
        for col in palette:
            flat.extend(col[:3] if opaque else col)
        if opaque:
            flat.extend([0, 0, 0] * (256 - len(palette)))
            out.putpalette(flat, "RGB")
        else:
            flat.extend([0, 0, 0, 255] * (256 - len(palette)))
            out.putpalette(flat, "RGBA")
        if identical_rgba(out.convert("RGBA"), rgba):
            return out
    except Exception:
        pass
    return None


def _compact_palette(p_img):
    """Drop unused palette entries and put non-opaque entries first so the
    tRNS chunk stays as short as possible. Returns (image, n_colors)."""
    used = [idx for _count, idx in p_img.getcolors(256)]
    pal = p_img.getpalette("RGBA")
    if pal is None:
        return p_img, max(used) + 1

    def alpha_of(i):
        return pal[i * 4 + 3]

    order = sorted(used, key=lambda i: (alpha_of(i) == 255, i))
    try:
        remapped = p_img.remap_palette(order)
    except Exception:
        return p_img, max(used) + 1
    return remapped, len(order)


def _bits_for(n_colors: int) -> int:
    if n_colors <= 2:
        return 1
    if n_colors <= 4:
        return 2
    if n_colors <= 16:
        return 4
    return 8


# ----------------------------------------------------------------------
# Saving
# ----------------------------------------------------------------------
def save_png(img, path: str, preset: dict) -> None:
    """Write `img` as PNG with the preset's compression settings. Never
    embeds metadata when png_strip_metadata is on (default)."""
    kwargs = {
        "format": "PNG",
        "optimize": bool(preset.get("png_optimize", True)),
        "compress_level": int(preset.get("png_compress_level", 9)),
    }
    if preset.get("png_strip_metadata", True):
        kwargs["icc_profile"] = None

    if img.mode == "P":
        try:
            img, n = _compact_palette(img)
            kwargs["bits"] = _bits_for(n)
        except Exception:
            pass
    img.save(path, **kwargs)


# ----------------------------------------------------------------------
# oxipng (optional, external)
# ----------------------------------------------------------------------
def _oxipng_cmd(src, dst, level, preset, full: bool):
    cmd = [tools.OXIPNG_PATH, "-o", str(level), "-t", str(threads_per_worker(preset)), "-q"]
    if full:
        cmd += ["--strip", "all" if preset.get("png_strip_metadata", True) else "safe"]
        if preset.get("png_oxipng_alpha", True):
            # oxipng's own dirty-alpha optimization (flag is -a/--alpha)
            cmd.append("-a")
        if preset.get("png_zopfli", False):
            cmd.append("-Z")
    cmd += ["--out", dst, src]
    return cmd


def oxipng_available(preset: dict) -> bool:
    level = preset.get("png_oxipng_level", -1)
    return bool(tools.OXIPNG_PATH) and level is not None and level >= 0 and PIL_AVAILABLE


def oxipng_squeeze(path: str, preset: dict) -> None:
    """Run oxipng over `path` (extra lossless PNG pass). Output goes to a
    separate temp file that is verified (same size, same pixels, smaller)
    before it replaces `path`. Any failure leaves `path` untouched."""
    level = preset.get("png_oxipng_level", -1)
    if not tools.OXIPNG_PATH or level is None or level < 0 or not PIL_AVAILABLE:
        return
    dst = path + ".ox"
    try:
        size_before = os.path.getsize(path)
        timeout = min(900, 60 + size_before // (20 * 1024))
        if preset.get("png_zopfli", False):
            timeout = min(1800, timeout * 4)

        ok = False
        for full in (True, False):
            try:
                if os.path.exists(dst):
                    os.remove(dst)
                res = subprocess.run(
                    _oxipng_cmd(path, dst, level, preset, full),
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL, timeout=timeout,
                )
                if res.returncode == 0 and os.path.exists(dst) and os.path.getsize(dst) > 0:
                    ok = True
                    break
            except Exception:
                continue
        if not ok:
            return
        if os.path.getsize(dst) >= size_before:
            return

        with Image.open(path) as a, Image.open(dst) as b:
            a.load()
            b.load()
            a4 = a.convert("RGBA")
            b4 = b.convert("RGBA")
            same_alpha = ImageChops.difference(
                a4.getchannel("A"), b4.getchannel("A")).getbbox() is None
            if a4.size != b4.size or not same_alpha or visible_mae(a4, b4) != 0.0:
                return
        os.replace(dst, path)
    except Exception:
        pass
    finally:
        try:
            if os.path.exists(dst):
                os.remove(dst)
        except OSError:
            pass
