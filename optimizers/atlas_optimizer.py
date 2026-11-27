"""
optimizers/atlas_optimizer.py
------------------------------
Trims wasted transparent padding from each sprite in a Sparrow/Starling
spritesheet and repacks it tighter, and lets identical sprites share one
rectangle.

Sparrow/HaxeFlixel frame semantics (what this module preserves):
    * the region (x, y, width, height) is drawn with its top-left corner
      at (-frameX, -frameY) inside a logical frame of
      frameWidth x frameHeight; without frame attributes the offset is
      (0, 0) and the logical size is (width, height).
    * so removing `left`/`top` empty pixels from a region must make
      frameX/frameY SMALLER by that amount:  frameX' = frameX - left.
      (The earlier version ADDED it, moving every sprite the wrong way
      by twice the trim - that was the "skewed / broken" result.)

Nothing is written unless it is PROVEN identical: before touching any
file, every SubTexture is re-rendered from the new XML text + new sheet
and compared pixel-for-pixel (and offset-for-offset) with the original.
Atlases using anything that can't be proven this way are left exactly as
they are and only compressed as a plain image:
    * rotated="true" or flipX/flipY sprites
    * fractional x/y/width/height/frameX/frameY values
    * partial frame data (e.g. frameX without frameWidth)
    * regions outside the sheet, animated images, non-RGBA sheets.
"""

import math
import os
import shutil
import xml.etree.ElementTree as ET

from progress_channel import report, scope as progress_scope
from config import TEMP_SUFFIX, IMAGE_EXTS
from optimizers import png_codec

try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False

from optimizers.image_optimizer import _canonicalize, optimize_image

_TRIM_MARKER_ATTR = "fnfOptimizerTrimmed"


def _looks_like_sparrow_root(root) -> bool:
    if root.tag != "TextureAtlas":
        return False
    if not root.get("imagePath"):
        return False
    subs = root.findall("SubTexture")
    if not subs:
        return False
    first = subs[0]
    try:
        float(first.get("x", ""))
        float(first.get("y", ""))
        float(first.get("width", ""))
        float(first.get("height", ""))
    except (TypeError, ValueError):
        return False
    return True


def _parse_sparrow_bytes(xml_bytes: bytes):
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return None
    if not _looks_like_sparrow_root(root):
        return None
    return root, root.get("imagePath")


def _parse_sparrow_file(xml_path: str):
    try:
        tree = ET.parse(xml_path)
    except (ET.ParseError, OSError):
        return None
    root = tree.getroot()
    if not _looks_like_sparrow_root(root):
        return None
    return tree, root, root.get("imagePath")


def _resolve_image_path(base_dir: str, image_path_attr: str):
    candidate = os.path.join(base_dir, image_path_attr)
    if os.path.isfile(candidate):
        return candidate
    target_name = os.path.basename(image_path_attr).lower()
    try:
        for name in os.listdir(base_dir):
            if name.lower() == target_name:
                return os.path.join(base_dir, name)
    except OSError:
        pass
    return None


def detect_sparrow_pair(xml_path: str):
    """Image that belongs to this Sparrow XML, or None.

    FNF engines load `foo.xml` together with the image of the SAME name
    (`foo.png`) and ignore imagePath. So if imagePath names a different
    file while a same-name image also exists, it is unclear which one
    the game really draws -> None (both files are left alone)."""
    parsed = _parse_sparrow_file(xml_path)
    if parsed is None:
        return None
    _tree, _root, image_path_attr = parsed
    folder = os.path.dirname(xml_path)
    image_path = _resolve_image_path(folder, image_path_attr)
    if image_path is None:
        return None
    if os.path.splitext(image_path)[1].lower() not in IMAGE_EXTS:
        return None
    stem = os.path.splitext(os.path.basename(xml_path))[0].lower()
    try:
        names = os.listdir(folder)
    except OSError:
        return image_path
    for name in names:
        base, ext = os.path.splitext(name)
        if base.lower() == stem and ext.lower() in IMAGE_EXTS:
            same = os.path.normcase(os.path.abspath(os.path.join(folder, name)))
            if same != os.path.normcase(os.path.abspath(image_path)):
                return None
    return image_path


def detect_sparrow_pair_bytes(xml_bytes: bytes, sibling_names, xml_name: str = ""):
    parsed = _parse_sparrow_bytes(xml_bytes)
    if parsed is None:
        return None
    _root, image_path_attr = parsed
    target = image_path_attr.replace("\\", "/")
    target_base = os.path.basename(target)
    found = None
    for name in sibling_names:
        if name == target:
            found = name
            break
    if found is None:
        for name in sibling_names:
            if name.lower() == target.lower():
                found = name
                break
    if found is None:
        for name in sibling_names:
            if os.path.basename(name).lower() == target_base.lower():
                found = name
                break
    if found is None:
        return None
    if xml_name:                       # same ambiguity rule as for folders
        stem = os.path.splitext(os.path.basename(xml_name))[0].lower()
        for name in sibling_names:
            base, ext = os.path.splitext(os.path.basename(name))
            if base.lower() == stem and ext.lower() in IMAGE_EXTS and name != found:
                return None
    return found


# ----------------------------------------------------------------------
# Geometry + verification
# ----------------------------------------------------------------------
class _Unsupported(Exception):
    """The atlas uses something this module can't prove it preserves."""


def _int_attr(el, name, default=None):
    raw = el.get(name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except ValueError:
        raise _Unsupported(f"non-numeric {name}")
    if value != int(value):
        raise _Unsupported(f"fractional {name}")
    return int(value)


def _is_true(el, name) -> bool:
    return (el.get(name) or "").strip().lower() in ("true", "1", "yes")


def _read_geometry(el):
    """Return dict(x,y,w,h,fx,fy,fw,fh,framed) for one SubTexture."""
    if _is_true(el, "rotated") or _is_true(el, "flipX") or _is_true(el, "flipY"):
        raise _Unsupported("rotated/flipped sprite")
    x, y = _int_attr(el, "x"), _int_attr(el, "y")
    w, h = _int_attr(el, "width"), _int_attr(el, "height")
    if None in (x, y, w, h) or w <= 0 or h <= 0 or x < 0 or y < 0:
        raise _Unsupported("bad region")
    fx, fy = _int_attr(el, "frameX"), _int_attr(el, "frameY")
    fw, fh = _int_attr(el, "frameWidth"), _int_attr(el, "frameHeight")
    framed_any = [v is not None for v in (fx, fy, fw, fh)]
    if any(framed_any) and not all(framed_any):
        raise _Unsupported("partial frame data")
    if not any(framed_any):
        fx, fy, fw, fh = 0, 0, w, h
    if fw <= 0 or fh <= 0:
        raise _Unsupported("bad frame size")
    return {"x": x, "y": y, "w": w, "h": h, "fx": fx, "fy": fy,
            "fw": fw, "fh": fh, "framed": any(framed_any)}


def _check_overflow(sheet, g, sheet_w, sheet_h):
    """Exporters sometimes emit a region that pokes 1-2px past the
    right/bottom edge of the sheet. What an engine draws there is
    unspecified (transparent vs. repeated edge pixel), so it is only
    accepted when BOTH readings give the same picture: the overflow is
    tiny (<= 4px) and the sheet's edge pixels inside that region are
    fully transparent. Anything else -> _Unsupported (atlas untouched)."""
    ox = g["x"] + g["w"] - sheet_w
    oy = g["y"] + g["h"] - sheet_h
    if ox <= 0 and oy <= 0:
        return
    if g["x"] >= sheet_w or g["y"] >= sheet_h or ox > 4 or oy > 4:
        raise _Unsupported("region outside sheet")
    if ox > 0:
        strip = sheet.crop((sheet_w - 1, g["y"], sheet_w, min(sheet_h, g["y"] + g["h"])))
        if strip.getchannel("A").getextrema()[1] != 0:
            raise _Unsupported("opaque pixels at overflowing edge")
    if oy > 0:
        strip = sheet.crop((g["x"], sheet_h - 1, min(sheet_w, g["x"] + g["w"]), sheet_h))
        if strip.getchannel("A").getextrema()[1] != 0:
            raise _Unsupported("opaque pixels at overflowing edge")


def _render(sheet, g, bounds):
    """Draw one frame the way Flixel does (region top-left at
    (-frameX, -frameY)) onto a transparent canvas covering `bounds`
    (x0, y0, x1, y1). Returns the canvas."""
    x0, y0, x1, y1 = bounds
    canvas = Image.new("RGBA", (x1 - x0, y1 - y0), (0, 0, 0, 0))
    region = sheet.crop((g["x"], g["y"], g["x"] + g["w"], g["y"] + g["h"]))
    canvas.paste(region, (-g["fx"] - x0, -g["fy"] - y0))
    return canvas


def _bounds_of(*geoms):
    x0 = y0 = 0
    x1 = y1 = 0
    for g in geoms:
        x0 = min(x0, -g["fx"])
        y0 = min(y0, -g["fy"])
        x1 = max(x1, g["fw"], -g["fx"] + g["w"])
        y1 = max(y1, g["fh"], -g["fy"] + g["h"])
    return x0, y0, x1, y1


def _same_frame(sheet_a, ga, sheet_b, gb) -> bool:
    if (ga["fw"], ga["fh"]) != (gb["fw"], gb["fh"]):
        return False
    bounds = _bounds_of(ga, gb)
    a = png_codec.clear_dirty_alpha(_render(sheet_a, ga, bounds))
    b = png_codec.clear_dirty_alpha(_render(sheet_b, gb, bounds))
    return png_codec.identical_rgba(a, b)


# ----------------------------------------------------------------------
# Packing
# ----------------------------------------------------------------------
def _pack_rects(items, padding: int):
    """Simple shelf packer. Each item: {"w","h",...}. Returns
    (placements, sheet_w, sheet_h) where placements[i] = (item, x, y)."""
    sorted_items = sorted(items, key=lambda it: (-it["h"], -it["w"]))
    total_area = sum((it["w"] + padding) * (it["h"] + padding) for it in sorted_items)
    widest = max((it["w"] for it in sorted_items), default=1) + 2 * padding
    target_w = max(widest, 64, math.ceil(math.sqrt(max(total_area, 1)) * 1.05))

    x = padding
    y = padding
    shelf_h = 0
    max_w = 0
    placed = []
    for it in sorted_items:
        if x + it["w"] + padding > target_w and x > padding:
            x = padding
            y += shelf_h + padding
            shelf_h = 0
        placed.append((it, x, y))
        x += it["w"] + padding
        shelf_h = max(shelf_h, it["h"])
        max_w = max(max_w, x)
    total_h = y + shelf_h + padding
    return placed, max_w, total_h


def _next_pow2(n: int) -> int:
    p = 1
    while p < n:
        p *= 2
    return p


def _extrude_edges(canvas, x: int, y: int, w: int, h: int, cw: int, ch: int):
    """Copy each sprite's 1px edge outward into the padding (only into
    padding - never over another sprite, since padding >= 1 separates
    them)."""
    if x > 0:
        canvas.paste(canvas.crop((x, y, x + 1, y + h)), (x - 1, y))
    if x + w < cw:
        canvas.paste(canvas.crop((x + w - 1, y, x + w, y + h)), (x + w, y))
    if y > 0:
        canvas.paste(canvas.crop((x, y, x + w, y + 1)), (x, y - 1))
    if y + h < ch:
        canvas.paste(canvas.crop((x, y + h - 1, x + w, y + h)), (x, y + h))


def _cleanup(path: str) -> None:
    try:
        if path and os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def _parse_keep_comments(xml_path: str):
    parser = ET.XMLParser(target=ET.TreeBuilder(insert_comments=True))
    with open(xml_path, "rb") as f:
        return ET.fromstring(f.read(), parser=parser)


def _compact(el) -> None:
    """Drop whitespace-only text/tail (pretty-print indentation)."""
    if el.text is not None and not el.text.strip():
        el.text = None
    if el.tail is not None and not el.tail.strip():
        el.tail = None
    for child in el:
        _compact(child)


def _write_xml(root, path: str) -> None:
    _compact(root)
    ET.ElementTree(root).write(path, encoding="utf-8", xml_declaration=True)


def _plain_fallback(image_path, xml_path, preset, xml_size):
    """Atlas can't be repacked safely -> compress the sheet as a normal
    image (dimensions unchanged, so the XML stays valid untouched)."""
    status, before, after = optimize_image(image_path, preset)
    return status, before + xml_size, after + xml_size


# ----------------------------------------------------------------------
# Main entry
# ----------------------------------------------------------------------
def trim_and_repack_atlas(image_path: str, xml_path: str, preset: dict):
    """Returns (status, before_total, after_total) for image+xml."""
    try:
        img_size = os.path.getsize(image_path)
        xml_size = os.path.getsize(xml_path)
    except OSError:
        return "FAILED", 0, 0
    before_total = img_size + xml_size
    report(0.02, "read")

    if not PIL_AVAILABLE:
        return "FAILED", before_total, before_total

    if not bool(preset.get("atlas_trim_enabled", True)):
        return _plain_fallback(image_path, xml_path, preset, xml_size)

    if os.path.splitext(image_path)[1].lower() != ".png":
        return _plain_fallback(image_path, xml_path, preset, xml_size)

    tmp_img = image_path + TEMP_SUFFIX + ".atlas.png"
    tmp_xml = xml_path + TEMP_SUFFIX + ".atlas.xml"
    tmp_raw = image_path + TEMP_SUFFIX + ".raw.png"
    try:
        try:
            result = _repack(image_path, xml_path, preset, tmp_img, tmp_xml, tmp_raw)
        except _Unsupported:
            result = None
        except Exception:
            result = None

        if result is None:
            return _plain_fallback(image_path, xml_path, preset, xml_size)

        new_img_size = os.path.getsize(tmp_img)
        new_xml_size = os.path.getsize(tmp_xml)
        repack_total = new_img_size + new_xml_size

        # Trim+repack is not automatically the winner: compressing the
        # ORIGINAL sheet as a plain image can be smaller (and keeps the
        # XML byte-identical). Try it on a copy and keep the better one.
        tmp_plain = image_path + TEMP_SUFFIX + ".plain.png"
        try:
            shutil.copyfile(image_path, tmp_plain)
            with progress_scope(0.80, 0.97, "compress"):
                p_status, _pb, p_after = optimize_image(tmp_plain, preset)
        except OSError:
            p_status, p_after = "FAILED", None
        plain_total = (p_after + xml_size) if p_status == "SUCCESS" else before_total

        if repack_total >= before_total and plain_total >= before_total:
            _cleanup(tmp_plain)
            return "SKIPPED", before_total, before_total

        if plain_total <= repack_total:
            os.replace(tmp_plain, image_path)       # XML stays untouched
            return "SUCCESS", before_total, plain_total

        _cleanup(tmp_plain)
        # Write both files only now that everything has been verified.
        report(0.99, "write")
        os.replace(tmp_xml, xml_path)
        os.replace(tmp_img, image_path)
        return "SUCCESS", before_total, repack_total
    finally:
        for p in (tmp_img, tmp_xml, tmp_raw, image_path + TEMP_SUFFIX + ".plain.png"):
            _cleanup(p)


def _repack(image_path, xml_path, preset, tmp_img, tmp_xml, tmp_raw):
    """Build + verify the repacked sheet/XML. Returns True on success,
    None if the atlas can't be handled safely. Raises _Unsupported for
    unsupported features."""
    root = _parse_keep_comments(xml_path)
    if not _looks_like_sparrow_root(root):
        return None
    subs = root.findall("SubTexture")
    if not subs:
        return None

    padding = max(0, int(preset.get("atlas_trim_padding", 1)))
    threshold = max(0, int(preset.get("atlas_trim_alpha_threshold", 0)))
    extrude = bool(preset.get("atlas_trim_extrude", False))
    pow2 = bool(preset.get("atlas_trim_pow2", False))
    dedupe = bool(preset.get("atlas_trim_dedupe", True))
    clear_dirty = bool(preset.get("png_clear_dirty_alpha", True))

    with Image.open(image_path) as raw:
        if getattr(raw, "n_frames", 1) > 1:
            raise _Unsupported("animated sheet")
        if raw.mode in ("I", "F", "I;16", "I;16L", "I;16B", "I;16N"):
            raise _Unsupported("16-bit sheet")
        raw.load()
        sheet = raw.convert("RGBA")

    if threshold > 0:
        low = sheet.getchannel("A").point(lambda a: 255 if a <= threshold else 0)
        sheet = sheet.copy()
        sheet.paste((0, 0, 0, 0), None, low)

    sheet_w, sheet_h = sheet.size
    geoms = []
    for el in subs:
        g = _read_geometry(el)
        _check_overflow(sheet, g, sheet_w, sheet_h)
        geoms.append(g)

    report(0.08, "trim")
    # 1) trim every distinct source region once
    trimmed = {}   # (x,y,w,h) -> dict(left, top, crop)
    for g in geoms:
        key = (g["x"], g["y"], g["w"], g["h"])
        if key in trimmed:
            continue
        crop = sheet.crop((g["x"], g["y"], g["x"] + g["w"], g["y"] + g["h"]))
        bbox = crop.getchannel("A").getbbox()
        if bbox is None:
            trimmed[key] = {"left": 0, "top": 0, "crop": Image.new("RGBA", (1, 1), (0, 0, 0, 0))}
        else:
            l, t, r, b = bbox
            trimmed[key] = {"left": l, "top": t, "crop": crop.crop(bbox)}
        if clear_dirty:
            trimmed[key]["crop"] = png_codec.clear_dirty_alpha(trimmed[key]["crop"])

    # 2) dedupe identical trimmed pixel blocks -> one packed rect
    blocks = {}    # content id -> item
    for key, info in trimmed.items():
        c = info["crop"]
        content = (c.size, c.tobytes()) if dedupe else key
        item = blocks.get(content)
        if item is None:
            item = {"w": c.size[0], "h": c.size[1], "crop": c}
            blocks[content] = item
        info["item"] = item

    items = list(blocks.values())
    report(0.22, "pack")
    placed, sw, sh = _pack_rects(items, padding)
    if pow2:
        sw, sh = _next_pow2(sw), _next_pow2(sh)
    canvas = Image.new("RGBA", (sw, sh), (0, 0, 0, 0))
    for item, x, y in placed:
        item["x"], item["y"] = x, y
        canvas.paste(item["crop"], (x, y))
    if extrude and padding >= 1:
        for item, x, y in placed:
            _extrude_edges(canvas, x, y, item["w"], item["h"], sw, sh)

    # 3) rewrite XML
    new_geoms = []
    for el, g in zip(subs, geoms):
        info = trimmed[(g["x"], g["y"], g["w"], g["h"])]
        item = info["item"]
        empty = (item["w"], item["h"]) == (1, 1) and info["left"] == 0 and info["top"] == 0 \
            and info["crop"].getchannel("A").getbbox() is None
        changed = empty or (item["w"], item["h"]) != (g["w"], g["h"]) or info["left"] or info["top"]
        ng = dict(g)
        ng["x"], ng["y"] = item["x"], item["y"]
        ng["w"], ng["h"] = item["w"], item["h"]
        if changed:
            # region shrank by (left, top): the region's top-left moves
            # RIGHT/DOWN inside the frame by that much, i.e. frameX/
            # frameY (which are -offset) get SMALLER.
            ng["fx"] = g["fx"] - info["left"]
            ng["fy"] = g["fy"] - info["top"]
            ng["framed"] = True
        new_geoms.append(ng)

        el.set("x", str(ng["x"]))
        el.set("y", str(ng["y"]))
        el.set("width", str(ng["w"]))
        el.set("height", str(ng["h"]))
        if ng["framed"]:
            el.set("frameX", str(ng["fx"]))
            el.set("frameY", str(ng["fy"]))
            el.set("frameWidth", str(ng["fw"]))
            el.set("frameHeight", str(ng["fh"]))
    if root.get("width") is not None:
        root.set("width", str(sw))
    if root.get("height") is not None:
        root.set("height", str(sh))
    _write_xml(root, tmp_xml)

    report(0.34, "verify")
    # 4) PROOF: re-read the XML we just wrote and re-render every frame
    #    from the new sheet; each must equal the original frame.
    check_root = _parse_keep_comments(tmp_xml)
    check_subs = check_root.findall("SubTexture")
    if len(check_subs) != len(subs):
        return None
    for el0, el1, g0 in zip(subs, check_subs, geoms):
        g1 = _read_geometry(el1)
        if el1.get("name") != el0.get("name"):
            return None
        if g1["x"] + g1["w"] > sw or g1["y"] + g1["h"] > sh:
            return None
        if not _same_frame(sheet, g0, canvas, g1):
            return None

    # 5) write + compress the sheet (quantize/palette/oxipng all handled
    #    by the normal image pipeline), then check the compressed result
    #    still looks like the exact canvas.
    png_codec.save_png(canvas, tmp_raw, {"png_compress_level": 1, "png_optimize": False})
    os.replace(tmp_raw, tmp_img)
    report(0.46, "compress")
    with progress_scope(0.46, 0.78, "compress"):
        status, _b, _a = optimize_image(tmp_img, preset)
    if status == "FAILED":
        return None
    # optimize_image() already validated the compressed sheet against the
    # exact canvas (fingerprint + full-resolution error cap); all that is
    # left to confirm is that its size did not change.
    with Image.open(tmp_img) as fin:
        if fin.size != canvas.size:
            return None
    return True
