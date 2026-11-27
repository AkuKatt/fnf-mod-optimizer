"""
optimizers/font_optimizer.py
-----------------------------
Subsets .ttf/.otf fonts down to a chosen character set (Settings ->
Fonts: upper/lower case, digits, symbols, accents, punctuation, other
scripts, characters the mod's own text uses, plus any extra characters
typed in) using fontTools, a
widely-used, battle-tested library for this exact task - not hand-
rolled binary font parsing.

Subsetting only discards glyphs (and glyph-only data) for characters
OUTSIDE the kept set. Every kept character's glyph is untouched
pixel-for-pixel. The tradeoff: any character actually used in the
mod's text that ISN'T in the kept set will render as a missing-glyph
box in-game - that's inherent to subsetting, not a bug, which is why
this is configurable and validated before being kept.
"""

import os

try:
    from fontTools.ttLib import TTFont
    from fontTools import subset as ft_subset
    FONTTOOLS_AVAILABLE = True
except ImportError:
    FONTTOOLS_AVAILABLE = False

from progress_channel import report
from config import TEMP_SUFFIX


def _cleanup(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass


def default_unicodes():
    """Printable ASCII (space through tilde): covers A-Z, a-z, 0-9,
    and every standard keyboard punctuation/symbol. No other scripts."""
    return set(range(0x20, 0x7F))


# Ranges behind the Settings switches (inclusive code point ranges).
_RANGES = {
    "font_keep_upper": [(0x41, 0x5A)],
    "font_keep_lower": [(0x61, 0x7A)],
    "font_keep_digits": [(0x30, 0x39)],
    "font_keep_ascii_symbols": [(0x21, 0x2F), (0x3A, 0x40), (0x5B, 0x60), (0x7B, 0x7E)],
    "font_keep_latin_accents": [(0xA1, 0x24F), (0x1E00, 0x1EFF)],
    "font_keep_punctuation": [(0x2010, 0x2027), (0x2030, 0x205E), (0x20A0, 0x20CF), (0x2116, 0x2116), (0x2122, 0x2122)],
    "font_keep_symbols": [(0x2190, 0x21FF), (0x2200, 0x22FF), (0x25A0, 0x25FF), (0x2600, 0x27BF), (0x2B00, 0x2BFF)],
    "font_keep_cyrillic": [(0x400, 0x52F)],
    "font_keep_greek": [(0x370, 0x3FF)],
}
_DEFAULT_ON = {"font_keep_upper", "font_keep_lower", "font_keep_digits", "font_keep_ascii_symbols",
               "font_keep_latin_accents", "font_keep_punctuation"}


def build_charset(preset: dict, used=None) -> set:
    """Code points to keep, from the Settings switches.

    `used` = every character the mod's text files contain; only those
    above ASCII are added (so switching a letter group off really drops
    it, while accents the mod's own text needs always survive)."""
    chars = {0x20, 0xA0}                         # space + no-break space are always kept
    for key, ranges in _RANGES.items():
        if bool(preset.get(key, key in _DEFAULT_ON)):
            for lo, hi in ranges:
                chars.update(range(lo, hi + 1))
    if bool(preset.get("font_keep_used", True)) and used:
        chars.update(c for c in used if c > 0x7F)
    extra = preset.get("font_extra_chars") or ""
    chars.update(ord(c) for c in str(extra) if not c.isspace() or c in " \u00a0")
    chars.discard(0)
    return chars


def optimize_font(path: str, preset: dict):
    before = os.path.getsize(path)

    if not FONTTOOLS_AVAILABLE:
        return "FAILED", before, before
    if not bool(preset.get("font_subset_enabled", True)):
        return "SKIPPED", before, before

    unicodes = preset.get("font_subset_unicodes") or build_charset(preset)
    tmp_path = path + TEMP_SUFFIX

    try:
        report(0.10, "read")
        font = TTFont(path, fontNumber=0)

        options = ft_subset.Options()
        # copyright(0) family(1) style(2) id(3) full(4) version(5) ps(6)
        # + license(13/14): everything else (designer bios, sample text,
        # trademark notes, in many languages) is dead weight in-game.
        options.name_IDs = [0, 1, 2, 3, 4, 5, 6, 13, 14]
        options.name_legacy = True
        options.notdef_glyph = True
        options.notdef_outline = True
        options.recalc_bounds = True
        options.recalc_timestamp = False
        options.layout_features = ["*"]
        options.legacy_kern = True
        if bool(preset.get("font_drop_hinting", False)):
            options.hinting = False

        report(0.35, "compress")
        subsetter = ft_subset.Subsetter(options=options)
        subsetter.populate(unicodes=unicodes)
        subsetter.subset(font)
        report(0.85, "write")
        font.save(tmp_path)
        font.close()
    except Exception:
        _cleanup(tmp_path)
        return "FAILED", before, before

    # Validate: the subsetted font must still load cleanly and have at
    # least one real glyph left, or something went badly wrong.
    try:
        check = TTFont(tmp_path, fontNumber=0)
        num_glyphs = check["maxp"].numGlyphs
        check.close()
        if num_glyphs <= 1:
            _cleanup(tmp_path)
            return "FAILED", before, before
    except Exception:
        _cleanup(tmp_path)
        return "FAILED", before, before

    after = os.path.getsize(tmp_path)
    if after < before:
        os.replace(tmp_path, path)
        return "SUCCESS", before, after

    _cleanup(tmp_path)
    return "SKIPPED", before, before
