"""
scanner.py
----------
Recursively scans a folder or a ZIP archive and classifies every
supported file (images / videos / audio / astc) without loading any
file content into memory beyond what's strictly needed - only paths,
ZipInfo entries, and sizes are collected for most categories.

Additionally detects "atlas" pairs: a spritesheet image together with
a valid, matching Sparrow/Starling XML atlas next to it. An image that
IS part of a valid atlas pair is routed into the `atlas` category
(handled by optimizers/atlas_optimizer.py, which trims transparent
padding and repacks the sheet tighter) instead of the plain `images`
category. An image that has no XML, or whose XML isn't a recognized
Sparrow atlas, is left in `images` exactly as before (compress-only) -
this is a deliberate, safe fallback, never a failure.

A .xml file is never treated as its own "supported file" - it's only
ever consulted to see if it validly pairs with an image. An unpaired
.xml is left completely untouched either way.
"""

import fnmatch
import os
import re
import zipfile

from config import IMAGE_EXTS, VIDEO_EXTS, AUDIO_EXTS, ASTC_EXTS, FONT_EXTS, SHADER_EXTS, SCRIPT_EXTS, JSON_EXTS, XML_EXT
from optimizers.atlas_optimizer import detect_sparrow_pair, detect_sparrow_pair_bytes


class ScanResult:
    """Container for scan statistics. `items` holds the raw file
    references so the optimizer can process them later."""

    def __init__(self):
        self.images = []
        self.videos = []
        self.audio = []
        self.astc = []
        self.fonts = []
        self.shaders = []
        self.scripts = []
        self.json = []
        self.atlas = []   # list of (image_ref, xml_ref) pairs
        self.total_size = 0

    @property
    def total_files(self):
        return (
            len(self.images) + len(self.videos) + len(self.audio)
            + len(self.astc) + len(self.fonts) + len(self.shaders)
            + len(self.scripts) + len(self.json) + len(self.atlas)
        )

    def add(self, category: str, ref, size: int):
        getattr(self, category).append(ref)
        self.total_size += size

    def add_atlas(self, image_ref, xml_ref, size: int):
        self.atlas.append((image_ref, xml_ref))
        self.total_size += size


def parse_excludes(text) -> list:
    """'data/*.json, scripts/ ; readme.txt' -> ['data/*.json', 'scripts/', 'readme.txt']"""
    if isinstance(text, (list, tuple)):
        text = ",".join(text)
    out = []
    for raw in re.split(r"[\n,;]+", text or ""):
        p = raw.strip().replace("\\", "/").lower()
        while p.startswith("./"):
            p = p[2:]
        p = p.lstrip("/")
        if p:
            out.append(p)
    return out


def build_exclude_rules(settings: dict) -> list:
    """Everything the user asked to leave alone, as one list of rules:
    path patterns (exclude_patterns) + words that must not appear in a
    file name (exclude_names)."""
    rules = parse_excludes((settings or {}).get("exclude_patterns"))
    prefix = "namep:" if (settings or {}).get("exclude_name_scope") == "path" else "name:"
    for word in re.split(r"[\n,;]+", (settings or {}).get("exclude_names") or ""):
        word = word.strip().lower()
        if word:
            rules.append(prefix + word)
    return rules


def is_excluded(rel: str, patterns) -> bool:
    """True if a mod-relative path matches any pattern:
       'folder/'   -> that folder (anywhere) and everything inside it
       '*.json'    -> glob on the whole path or on the file name
       'name.ext'  -> a file or folder with exactly that name"""
    if not patterns:
        return False
    rel = rel.replace("\\", "/").lower().lstrip("/")
    parts = rel.split("/")
    for pat in patterns:
        if pat.startswith("name:"):               # word in the file NAME
            if pat[5:] in parts[-1]:
                return True
            continue
        if pat.startswith("namep:"):              # word anywhere in the path
            if pat[6:] in rel:
                return True
            continue
        if pat.endswith("/"):
            folder = pat.rstrip("/")
            if not folder:
                continue
            if "/" in folder:
                if rel.startswith(folder + "/") or ("/" + folder + "/") in ("/" + rel):
                    return True
            elif folder in parts[:-1]:
                return True
        elif any(c in pat for c in "*?["):
            if fnmatch.fnmatchcase(rel, pat) or fnmatch.fnmatchcase(parts[-1], pat):
                return True
        elif pat == rel or pat in parts or rel.endswith("/" + pat):
            return True
    return False


def classify_ext(ext: str):
    ext = ext.lower()
    if ext in IMAGE_EXTS:
        return "images"
    if ext in VIDEO_EXTS:
        return "videos"
    if ext in AUDIO_EXTS:
        return "audio"
    if ext in ASTC_EXTS:
        return "astc"
    if ext in FONT_EXTS:
        return "fonts"
    if ext in SHADER_EXTS:
        return "shaders"
    if ext in SCRIPT_EXTS:
        return "scripts"
    if ext in JSON_EXTS:
        return "json"
    return None


def scan_folder(root: str, exclude=None) -> ScanResult:
    """Walk a directory tree, pair up any valid Sparrow atlas (.xml +
    its image) first, then classify everything else that's left."""
    result = ScanResult()
    patterns = parse_excludes(exclude)

    all_files = []   # (full_path, ext)
    xml_paths = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full_path = os.path.join(dirpath, name)
            if patterns and is_excluded(os.path.relpath(full_path, root), patterns):
                continue
            ext = os.path.splitext(name)[1].lower()
            all_files.append((full_path, ext))
            if ext == XML_EXT:
                xml_paths.append(full_path)

    # image -> every XML that points at it. An image used by 2+ atlases
    # must NOT be repacked (the other XML would then describe a sheet that
    # no longer exists), so it is only compressed as a plain image.
    owners = {}
    for xml_path in xml_paths:
        image_path = detect_sparrow_pair(xml_path)
        if image_path is None:
            continue
        if patterns and is_excluded(os.path.relpath(image_path, root), patterns):
            continue                                  # the sheet itself is blacklisted
        key = os.path.normcase(os.path.abspath(image_path))
        owners.setdefault(key, []).append((image_path, xml_path))

    claimed_images = set()
    for key, pairs in owners.items():
        if len(pairs) != 1:
            continue
        image_path, xml_path = pairs[0]
        try:
            img_size = os.path.getsize(image_path)
            xml_size = os.path.getsize(xml_path)
        except OSError:
            continue
        claimed_images.add(key)
        result.add_atlas(image_path, xml_path, img_size + xml_size)

    for full_path, ext in all_files:
        if os.path.normcase(os.path.abspath(full_path)) in claimed_images:
            continue
        category = classify_ext(ext)
        if category is None:
            continue
        try:
            size = os.path.getsize(full_path)
        except OSError:
            continue
        result.add(category, full_path, size)

    return result


def scan_zip(zip_path: str, exclude=None) -> ScanResult:
    """Read a ZIP's entries (extracting only small .xml candidates
    into memory to sniff them), pair up any valid Sparrow atlas, then
    classify everything else that's left."""
    result = ScanResult()

    with zipfile.ZipFile(zip_path, "r") as zf:
        patterns = parse_excludes(exclude)
        infos = [i for i in zf.infolist() if not i.is_dir() and not is_excluded(i.filename, patterns)]
        by_name = {i.filename: i for i in infos}

        by_dir = {}
        for i in infos:
            by_dir.setdefault(os.path.dirname(i.filename), []).append(i.filename)

        owners = {}          # image name -> list of xml infos that use it
        for info in infos:
            ext = os.path.splitext(info.filename)[1].lower()
            if ext != XML_EXT:
                continue
            try:
                xml_bytes = zf.read(info)
            except Exception:
                continue

            siblings = [n for n in by_dir.get(os.path.dirname(info.filename), [])
                        if n != info.filename]
            match_name = detect_sparrow_pair_bytes(xml_bytes, siblings, info.filename)
            if match_name is None:
                continue
            if by_name.get(match_name) is None:
                continue
            if os.path.splitext(match_name)[1].lower() not in IMAGE_EXTS:
                continue
            owners.setdefault(match_name, []).append(info)

        claimed = set()
        for match_name, xml_infos in owners.items():
            if len(xml_infos) != 1:      # shared sheet: compress only, never repack
                continue
            image_info = by_name[match_name]
            claimed.add(match_name)
            result.add_atlas(image_info, xml_infos[0], image_info.file_size + xml_infos[0].file_size)

        for info in infos:
            if info.filename in claimed:
                continue
            ext = os.path.splitext(info.filename)[1]
            category = classify_ext(ext)
            if category is None:
                continue
            result.add(category, info, info.file_size)

    return result
