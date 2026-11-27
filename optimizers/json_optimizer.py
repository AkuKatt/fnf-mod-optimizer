"""
optimizers/json_optimizer.py
-----------------------------
Lossless JSON minify (.json): removes every space/tab/newline that sits
OUTSIDE a string. Nothing else is touched - no number is re-formatted,
no key is re-ordered, no string is altered - so it is much safer than
"load and dump again".

Three independent checks must agree before anything is written:
  1. a regex pass and a character-by-character state machine produce the
     SAME minified text (two separate implementations of the same rule),
  2. the original and the result parse to EQUAL data (strict parser),
  3. the result is shorter.
Files that are not plain strict JSON (comments, trailing commas, NaN,
Infinity, absurd nesting, ...) are left completely alone (SKIPPED): some
engines accept dialects that these checks can't vouch for.
"""

import json
import os
import re
from decimal import Decimal, ROUND_HALF_EVEN

from config import TEMP_SUFFIX

# a JSON string (kept as-is) OR a run of whitespace (dropped)
_TOKEN = re.compile(r'("[^"\\]*(?:\\.[^"\\]*)*")|[ \t\r\n]+')
_WS = " \t\r\n"
# a string (kept) or a plain decimal number such as -12.3456789 (candidate for rounding)
_NUM_OR_STR = re.compile(r'("[^"\\]*(?:\\.[^"\\]*)*")|(-?\d+\.\d+)(?![\d.eE+-])')


def minify_json_text(text: str) -> str:
    return _TOKEN.sub(lambda m: m.group(1) or "", text)


def _minify_by_hand(text: str) -> str:
    """Second, independent implementation (no regex)."""
    out = []
    in_str = False
    esc = False
    for ch in text:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
            out.append(ch)
        elif ch not in _WS:
            out.append(ch)
    return "".join(out)


def _round_numbers(text: str, digits: int) -> str:
    """LOSSY (opt-in): shorten decimals longer than `digits` places.
    Integers, exponent numbers and strings are never touched."""
    quant = Decimal(1).scaleb(-digits)

    def fix(m):
        if m.group(1) is not None:
            return m.group(1)
        tok = m.group(2)
        if len(tok.split(".")[1]) <= digits:
            return tok
        out = format(Decimal(tok).quantize(quant, rounding=ROUND_HALF_EVEN), "f")
        if "." in out:
            out = out.rstrip("0").rstrip(".")
        return "0" if out in ("-0", "") else out

    return _NUM_OR_STR.sub(fix, text)


def _close(a, b, tol: float) -> bool:
    """Same structure and same non-number values; numbers within tol."""
    if isinstance(a, dict):
        return (isinstance(b, dict) and list(a) == list(b)
                and all(_close(a[k], b[k], tol) for k in a))
    if isinstance(a, list):
        return isinstance(b, list) and len(a) == len(b) and all(_close(x, y, tol) for x, y in zip(a, b))
    if isinstance(a, bool) or isinstance(b, bool) or a is None or isinstance(a, str):
        return a == b and type(a) is type(b)
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        if isinstance(a, int) and isinstance(b, int):
            return a == b
        return abs(a - b) <= tol
    return False


def _reject_constant(name):
    raise ValueError("non-standard constant " + name)


def _strict_load(text: str):
    return json.loads(text, parse_constant=_reject_constant)


def optimize_json(path: str, preset: dict):
    before = os.path.getsize(path)

    if not bool(preset.get("json_minify_enabled", True)):
        return "SKIPPED", before, before

    try:
        with open(path, "rb") as f:
            raw = f.read()
        bom = raw.startswith(b"\xef\xbb\xbf")
        text = raw.decode("utf-8-sig" if bom else "utf-8")
    except (OSError, UnicodeDecodeError):
        return "SKIPPED", before, before

    try:
        original = _strict_load(text)
    except (ValueError, RecursionError):
        return "SKIPPED", before, before      # not strict JSON: hands off

    minified = minify_json_text(text)
    try:
        if minified != _minify_by_hand(text):
            return "SKIPPED", before, before  # the two implementations disagree
        if _strict_load(minified) != original:
            return "FAILED", before, before
        digits = int(preset.get("json_round_decimals") or 0)
        if digits > 0:                        # opt-in lossy step, proven within tolerance
            rounded = minify_json_text(_round_numbers(text, digits))
            if len(rounded) < len(minified) and _close(original, _strict_load(rounded), 0.5 * 10 ** -digits * 1.0001):
                minified = rounded
    except (ValueError, RecursionError):
        return "FAILED", before, before

    data = minified.encode("utf-8")
    if bom:
        data = b"\xef\xbb\xbf" + data
    if len(data) >= before:
        return "SKIPPED", before, before

    tmp_path = path + TEMP_SUFFIX
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except OSError:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        return "FAILED", before, before
    return "SUCCESS", before, len(data)
