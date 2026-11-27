"""
optimizers/shader_optimizer.py
--------------------------------
Token-based GLSL minifier (.frag/.fsh/.vert/.vsh/.glsl).

What it does:
  * removes // and /* */ comments (a comment is never detected inside
    a preprocessor line's string or in the middle of another token),
  * removes every space/newline that isn't needed to keep two tokens
    apart (e.g. "a + b" -> "a+b", but "a + +b" -> "a+ +b" and
    "float x" keeps its space),
  * keeps each preprocessor directive (#version, #define, #ifdef,
    #pragma ...) on its OWN line with its content left as-is apart from
    comment/whitespace trimming, because GLSL preprocessing is
    line-based; line continuations (\\) are preserved,
  * puts nothing else on a directive's line: normal code following a
    directive always starts on a new line.

What it never does: rename identifiers, reorder or inline code, touch
numbers (1.0 stays 1.0), or change strings/directives' meaning.

Safety proof before anything is written: the minified text is tokenized
again and must produce EXACTLY the same token sequence (and the same
directive lines) as the original. Otherwise the file is left untouched.
"""

import os
import re

from config import TEMP_SUFFIX

# Longest-first punctuators (GLSL operators)
_PUNCT = [
    "<<=", ">>=", "...",
    "++", "--", "+=", "-=", "*=", "/=", "%=", "&=", "|=", "^=",
    "==", "!=", "<=", ">=", "&&", "||", "^^", "<<", ">>",
]

_IDENT = re.compile(r"[A-Za-z_][A-Za-z_0-9]*")
# Numbers incl. floats, exponents, hex, suffixes (u, U, f, F, lf, LF)
_NUMBER = re.compile(
    r"(?:0[xX][0-9A-Fa-f]+[uU]?)"
    r"|(?:(?:\d+\.\d*|\.\d+|\d+)(?:[eE][+-]?\d+)?(?:[fF]|[lL][fF]|[uU])?)"
)


class _LexError(Exception):
    pass


def _skip_line_comment(src: str, i: int, n: int) -> int:
    """Index of the newline ending a // comment. A backslash right
    before the newline continues the comment onto the next line (GLSL
    line splicing), so that case keeps scanning."""
    while True:
        nl = src.find("\n", i)
        if nl == -1:
            return n
        k = nl - 1
        if k >= i and src[k] == "\r":
            k -= 1
        if k >= i and src[k] == "\\":
            i = nl + 1
            continue
        return nl


def _tokenize(src: str):
    """Return a list of tokens: ("dir", text) for a whole preprocessor
    directive line, ("id"|"num"|"op", text) otherwise. Comments are
    dropped. Raises _LexError on unterminated comments."""
    tokens = []
    i, n = 0, len(src)
    at_line_start = True   # only whitespace/comments seen since last \n
    while i < n:
        c = src[i]
        if c == "\n":
            at_line_start = True
            i += 1
            continue
        if c in " \t\r\f\v":
            i += 1
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "/":
            i = _skip_line_comment(src, i, n)
            continue
        if c == "/" and i + 1 < n and src[i + 1] == "*":
            end = src.find("*/", i + 2)
            if end == -1:
                raise _LexError("unterminated block comment")
            comment = src[i:end + 2]
            i = end + 2
            # A block comment spanning lines counts as a line break for
            # directive detection purposes.
            if "\n" in comment:
                at_line_start = True
            continue
        if c == "#" and at_line_start:
            # whole directive: up to newline, honoring backslash-newline
            # continuations; comments inside are removed.
            parts = []
            j = i
            while j < n:
                ch = src[j]
                if ch == "\\" and j + 1 < n and src[j + 1] == "\n":
                    parts.append("\\\n")
                    j += 2
                    continue
                if ch == "\\" and src[j + 1:j + 3] == "\r\n":
                    parts.append("\\\n")
                    j += 3
                    continue
                if ch == "\n":
                    break
                if ch == "/" and j + 1 < n and src[j + 1] == "/":
                    j = _skip_line_comment(src, j, n)
                    break
                if ch == "/" and j + 1 < n and src[j + 1] == "*":
                    end = src.find("*/", j + 2)
                    if end == -1:
                        raise _LexError("unterminated block comment")
                    parts.append(" ")
                    j = end + 2
                    continue
                parts.append(ch)
                j += 1
            text = "".join(parts).replace("\r", "")
            text = re.sub(r"[ \t]+", " ", text).strip()
            # "# define" -> keep exactly as the author wrote apart from
            # whitespace runs; the token sequence check covers it.
            tokens.append(("dir", text))
            i = j
            at_line_start = True
            continue
        at_line_start = False
        m = _NUMBER.match(src, i)
        if m and (c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit())):
            tokens.append(("num", m.group(0)))
            i = m.end()
            continue
        m = _IDENT.match(src, i)
        if m:
            tokens.append(("id", m.group(0)))
            i = m.end()
            continue
        for p in _PUNCT:
            if src.startswith(p, i):
                tokens.append(("op", p))
                i += len(p)
                break
        else:
            tokens.append(("op", c))
            i += 1
    return tokens


_SEP_OPS = set("(){}[],;")
_SPACE_CACHE = {}


def _needs_space(prev, cur) -> bool:
    """True if `prev` and `cur` can't be written back-to-back. Generic
    rule: the pair is glued only if re-lexing the glued text gives back
    exactly the same two tokens. On top of that, two operator tokens are
    only glued when one of them is a bracket/separator (belt and braces
    against an operator this lexer doesn't know about), and an operator
    is never glued to a number that starts with '.'."""
    key = (prev, cur)
    hit = _SPACE_CACHE.get(key)
    if hit is not None:
        return hit
    pk, pt = prev
    ck, ct = cur
    need = False
    if pk == "op" and ck == "op":
        need = not (pt in _SEP_OPS or ct in _SEP_OPS)
    elif pk == "op" and ck == "num" and ct.startswith("."):
        need = True
    elif pk == "op" and pt == "." and ck in ("num", "id") and ct[:1].isdigit():
        need = True
    if not need:
        try:
            need = _tokenize(pt + ct) != [prev, cur]
        except _LexError:
            need = True
    if len(_SPACE_CACHE) < 50000:
        _SPACE_CACHE[key] = need
    return need


def _emit(tokens) -> str:
    out = []
    prev = None
    for tok in tokens:
        kind, text = tok
        if kind == "dir":
            if out and not out[-1].endswith("\n"):
                out.append("\n")
            out.append(text + "\n")
            prev = None
            continue
        if prev is not None and _needs_space(prev, tok):
            out.append(" ")
        out.append(text)
        prev = tok
    return "".join(out).rstrip("\n") + ("\n" if out else "")


def minify_glsl(src: str) -> str:
    """Return the minified source. Raises _LexError if it can't be
    proven identical."""
    tokens = _tokenize(src)
    out = _emit(tokens)
    if _tokenize(out) != tokens:
        raise _LexError("token stream changed")
    return out


def optimize_shader(path: str, preset: dict):
    before = os.path.getsize(path)

    if not bool(preset.get("shader_minify_enabled", True)):
        return "SKIPPED", before, before

    try:
        with open(path, "rb") as f:
            raw = f.read()
        src = raw.decode("utf-8-sig") if raw.startswith(b"\xef\xbb\xbf") else raw.decode("utf-8")
    except (OSError, UnicodeDecodeError):
        return "FAILED", before, before

    try:
        minified = minify_glsl(src)
    except Exception:
        return "FAILED", before, before

    data = minified.encode("utf-8")
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
