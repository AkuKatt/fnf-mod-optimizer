"""
optimizers/script_optimizer.py
--------------------------------
Comment stripping + whitespace minification for the scripts FNF engines
use: Lua (.lua) and Haxe (.hx, .hxc).

How it works (token based, NOT regex):
  1. A real lexer splits the source into tokens: identifiers, numbers,
     operators, strings (Lua short/long strings; Haxe "..." and '...'
     including ${...} interpolation with nested strings/braces), Haxe
     regex literals ~/.../flags, and Haxe preprocessor words (#if, #else,
     #elseif, #end, #error). Comments (--, --[==[ ]==], //, /* */) are
     dropped. Anything inside a string/regex is kept byte-for-byte.
  2. Tokens are written back with the fewest spaces that keep every
     token separate. Two tokens are only glued if re-lexing the glued
     text gives back the same two tokens.
  3. Line breaks are kept between statements (so #if/#end structure and
     rough line numbers survive); lines are only merged where the
     previous token can never end a statement (Lua: after `then`, `do`,
     `,`, `=`, `(` ... ; Haxe: after `;`, `{`, `}`) and never around a
     preprocessor word.
  4. PROOF before writing: the output is lexed again and must equal the
     original token list. A single difference, an unterminated
     string/comment, non-UTF-8 text, or a construct the lexer doesn't
     know -> the file is left untouched (reported FAILED, never
     half-processed).

Never renames variables, never reorders, never touches string content.
"""

import os
import re

from config import TEMP_SUFFIX


class _LexError(Exception):
    pass


# token = (kind, text, start_line, end_line, start_offset); compare only (kind, text)
_SEP_OPS = set("(){}[],;")
_EOL = re.compile(r"[\r\n]")


def _line_end(src: str, i: int, n: int) -> int:
    m = _EOL.search(src, i)
    return n if m is None else m.start()

_IDENT = re.compile(r"[A-Za-z_\u0080-\uffff][A-Za-z_0-9\u0080-\uffff]*")

_LUA_NUM = re.compile(
    r"0[xX](?:[0-9A-Fa-f]*\.?[0-9A-Fa-f]*)(?:[pP][+-]?\d+)?"
    r"|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
)
_HX_NUM = re.compile(
    r"0[xX][0-9A-Fa-f_]+\w*"
    r"|(?:\d[\d_]*(?:\.(?!\.)[\d_]*)?|\.\d[\d_]*)(?:[eE][+-]?\d+)?\w*"
)

_LUA_OPS = ["...", "..", "==", "~=", "<=", ">=", "<<", ">>", "//", "::"]
_HX_OPS = ["<<=", ">>=", ">>>=", ">>>", "...", "??=", "?.", "??", "&&=", "||=", "**=", "**", "@:",
           "++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
           "==", "!=", "<=", ">=", "&&", "||", "<<", ">>", "=>", "->", "::"]
_HX_OPS.sort(key=len, reverse=True)
_LUA_OPS.sort(key=len, reverse=True)


# ----------------------------------------------------------------------
# Lua
# ----------------------------------------------------------------------
def _lua_long_bracket(src, i, n):
    """If src[i] starts a long bracket [==[ return (level, content_start)."""
    if src[i] != "[":
        return None
    k = i + 1
    eq = 0
    while k < n and src[k] == "=":
        eq += 1
        k += 1
    if k < n and src[k] == "[":
        return eq, k + 1
    return None


def _lex_lua(src: str, shebang: bool = True):
    tokens = []
    i, n, line = 0, len(src), 1
    if shebang and src.startswith("#"):          # shebang line, Lua skips it
        nl = src.find("\n")
        first = src if nl == -1 else src[:nl]
        tokens.append(("raw", first, 1, 1, i))
        i = n if nl == -1 else nl
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r\f\v":
            i += 1
            continue
        if c == "-" and src.startswith("--", i):
            lb = _lua_long_bracket(src, i + 2, n) if i + 2 < n else None
            if lb:
                level, start = lb
                close = "]" + "=" * level + "]"
                end = src.find(close, start)
                if end == -1:
                    raise _LexError("unterminated long comment")
                line += src.count("\n", i, end + len(close))
                i = end + len(close)
            else:
                i = _line_end(src, i, n)
            continue
        if c == '"' or c == "'":
            j = i + 1
            while True:
                if j >= n:
                    raise _LexError("unterminated string")
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == c:
                    j += 1
                    break
                if ch == "\n":
                    raise _LexError("newline in string")
                j += 1
            text = src[i:j]
            nls = text.count("\n")          # only via "\<newline>"
            tokens.append(("str", text, line, line + nls, i))
            line += nls
            i = j
            continue
        if c == "[":
            lb = _lua_long_bracket(src, i, n)
            if lb:
                level, start = lb
                close = "]" + "=" * level + "]"
                end = src.find(close, start)
                if end == -1:
                    raise _LexError("unterminated long string")
                text = src[i:end + len(close)]
                nls = text.count("\n")
                tokens.append(("str", text, line, line + nls, i))
                line += nls
                i = end + len(close)
                continue
        m = _IDENT.match(src, i)
        if m:
            tokens.append(("id", m.group(0), line, line, i))
            i = m.end()
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _LUA_NUM.match(src, i)
            if m and m.end() > i:
                j = m.end()
                # a number glued to letters (e.g. 3x) is malformed Lua:
                # refuse rather than guess
                if j < n and (src[j].isalpha() or src[j] == "_"):
                    raise _LexError("malformed number")
                tokens.append(("num", m.group(0), line, line, i))
                i = j
                continue
        for op in _LUA_OPS:
            if src.startswith(op, i):
                tokens.append(("op", op, line, line, i))
                i += len(op)
                break
        else:
            tokens.append(("op", c, line, line, i))
            i += 1
    return tokens


_LUA_JOIN_AFTER = {
    "then", "do", "else", "repeat", "and", "or", "not", ",", "=", "(", "{", "[",
    "..", "+", "-", "*", "/", "%", "^", "==", "~=", "<", ">", "<=", ">=", "#", ";",
}


# ----------------------------------------------------------------------
# Haxe
# ----------------------------------------------------------------------
def _hx_scan_dq(src, i, n):
    j = i + 1
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        j += 1
        if ch == '"':
            return j
    raise _LexError("unterminated string")


def _hx_scan_sq(src, i, n):
    """Single-quoted string with ${expr} interpolation."""
    j = i + 1
    while j < n:
        ch = src[j]
        if ch == "\\":
            j += 2
            continue
        if ch == "$" and j + 1 < n and src[j + 1] == "{":
            j += 2
            depth = 1
            while j < n and depth:
                cc = src[j]
                if cc == "{":
                    depth += 1
                    j += 1
                elif cc == "}":
                    depth -= 1
                    j += 1
                elif cc == '"':
                    j = _hx_scan_dq(src, j, n)
                elif cc == "'":
                    j = _hx_scan_sq(src, j, n)
                else:
                    j += 1
            if depth:
                raise _LexError("unterminated interpolation")
            continue
        j += 1
        if ch == "'":
            return j
    raise _LexError("unterminated string")


def _lex_haxe(src: str):
    tokens = []
    i, n, line = 0, len(src), 1
    while i < n:
        c = src[i]
        if c == "\n":
            line += 1
            i += 1
            continue
        if c in " \t\r\f\v":
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            i = _line_end(src, i, n)
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            if end == -1:
                raise _LexError("unterminated block comment")
            line += src.count("\n", i, end + 2)
            i = end + 2
            continue
        if c == '"':
            j = _hx_scan_dq(src, i, n)
            text = src[i:j]
            nls = text.count("\n")
            tokens.append(("str", text, line, line + nls, i))
            line += nls
            i = j
            continue
        if c == "'":
            j = _hx_scan_sq(src, i, n)
            text = src[i:j]
            nls = text.count("\n")
            tokens.append(("str", text, line, line + nls, i))
            line += nls
            i = j
            continue
        if c == "~" and i + 1 < n and src[i + 1] == "/":
            j = i + 2
            while True:
                if j >= n or src[j] == "\n":
                    raise _LexError("unterminated regex")
                if src[j] == "\\":
                    j += 2
                    continue
                if src[j] == "/":
                    j += 1
                    break
                j += 1
            while j < n and src[j].isalpha():
                j += 1
            tokens.append(("str", src[i:j], line, line, i))
            i = j
            continue
        if c == "#":
            m = _IDENT.match(src, i + 1)
            if m:
                tokens.append(("pp", "#" + m.group(0), line, line, i))
                i = m.end()
                continue
        m = _IDENT.match(src, i)
        if m:
            tokens.append(("id", m.group(0), line, line, i))
            i = m.end()
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _HX_NUM.match(src, i)
            if m and m.end() > i:
                tokens.append(("num", m.group(0), line, line, i))
                i = m.end()
                continue
        for op in _HX_OPS:
            if src.startswith(op, i):
                tokens.append(("op", op, line, line, i))
                i += len(op)
                break
        else:
            tokens.append(("op", c, line, line, i))
            i += 1
    return tokens


_HX_JOIN_AFTER = {";", "{", "}"}


# ----------------------------------------------------------------------
# Emit
# ----------------------------------------------------------------------
_LEXERS = {".lua": _lex_lua, ".hx": _lex_haxe, ".hxc": _lex_haxe}
# used only to test whether two adjacent tokens can be glued: a leading
# "#" there is the length operator, not a shebang line
_PAIR_LEXERS = {".lua": lambda s: _lex_lua(s, False), ".hx": _lex_haxe, ".hxc": _lex_haxe}
_JOIN = {".lua": _LUA_JOIN_AFTER, ".hx": _HX_JOIN_AFTER, ".hxc": _HX_JOIN_AFTER}


def _needs_space(prev, cur, lex) -> bool:
    pk, pt = prev[0], prev[1]
    ck, ct = cur[0], cur[1]
    if pk == "raw" or ck == "raw":
        return True
    if pk == "op" and ck == "op" and not (pt in _SEP_OPS or ct in _SEP_OPS):
        return True
    if pt == "[" and pk == "op" and (ct.startswith("[") or ct == "="):
        return True    # would glue into a Lua long bracket [[ / [=[
    if pk == "op" and ck == "num" and ct.startswith("."):
        return True
    if pk == "pp" or ck == "pp":
        return True
    try:
        return [t[:2] for t in lex(pt + ct)] != [prev[:2], cur[:2]]
    except _LexError:
        return True


def _emit(tokens, ext: str) -> str:
    lex = _PAIR_LEXERS[ext]
    join_after = _JOIN[ext]
    out = []
    prev = None
    line_has_pp = False
    for tok in tokens:
        if prev is not None and prev[4] + len(prev[1]) == tok[4] and prev[0] != "raw":
            pass                                  # no gap in the source -> never add one
        elif prev is not None:
            newline_in_src = tok[2] > prev[3]
            if newline_in_src:
                joinable = (
                    prev[0] != "raw"
                    and prev[1] in join_after
                    and prev[0] in ("op", "id")
                    and not line_has_pp
                    and tok[0] != "pp"
                )
                if joinable:
                    if _needs_space(prev, tok, lex):
                        out.append(" ")
                else:
                    out.append("\n")
                    line_has_pp = False
            else:
                if _needs_space(prev, tok, lex):
                    out.append(" ")
        out.append(tok[1])
        if tok[0] == "pp":
            line_has_pp = True
        prev = tok
    return "".join(out) + ("\n" if out else "")


def minify_script(src: str, ext: str) -> str:
    """Return minified source; raises _LexError if it can't be proven
    identical at token level."""
    lex = _LEXERS[ext]
    tokens = lex(src)
    out = _emit(tokens, ext)
    if [t[:2] for t in lex(out)] != [t[:2] for t in tokens]:
        raise _LexError("token stream changed")
    return out


def optimize_script(path: str, preset: dict):
    before = os.path.getsize(path)

    if not bool(preset.get("script_strip_comments_enabled", False)):
        return "SKIPPED", before, before

    ext = os.path.splitext(path)[1].lower()
    if ext not in _LEXERS:
        return "SKIPPED", before, before

    try:
        with open(path, "rb") as f:
            raw = f.read()
        bom = raw.startswith(b"\xef\xbb\xbf")
        src = raw.decode("utf-8-sig" if bom else "utf-8")
    except (OSError, UnicodeDecodeError):
        return "FAILED", before, before

    try:
        result = minify_script(src, ext)
    except Exception:
        return "FAILED", before, before

    data = result.encode("utf-8")
    if len(data) >= before:
        return "SKIPPED", before, before

    tmp_path = path + TEMP_SUFFIX
    try:
        with open(tmp_path, "wb") as f:
            f.write(data)
        os.replace(tmp_path, path)
    except OSError:
        _cleanup(tmp_path)
        return "FAILED", before, before
    return "SUCCESS", before, len(data)


def _cleanup(path: str) -> None:
    try:
        if os.path.exists(path):
            os.remove(path)
    except OSError:
        pass
