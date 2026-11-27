"""
deps.py
-------
Dependency checking and one-click installing for the web UI.

Roles
  required     Pillow (images, atlases) and fontTools (the font
               optimizer subsets fonts with it). Small pure-Python
               packages: the first-run screen offers to install them.
               Only Pillow BLOCKS a run; without fontTools the fonts are
               skipped (and the log says so) instead.
  conditional  FFmpeg / astcenc - big downloads, only offered when the
               scanned mod really contains video+audio / .astc files.
  optional     oxipng - a bonus. NEVER installed unless the user clicks
               Install for it in Settings.

Safety rules
  * Nothing is ever downloaded or installed without the user pressing
    Install (the first-run screen only offers the REQUIRED ones).
  * No sudo, no password prompts, nothing outside the user's own
    folders: programs go to ~/.fnf_mod_optimizer/bin (searched by
    tools.py), Python packages go through the user's own pip.
  * Archives are never "extractall"-ed: only the one or two wanted
    members are copied out by base name (no path traversal).
  * Downloads use HTTPS, have timeouts and are checked against a
    published checksum when one exists; the installed program must
    actually run (`-version`) before it counts as installed.
  * Every step is written to the process log (eventlog) as it happens,
    including each line of pip / pkg / cmake output.
"""

import hashlib
import importlib
import importlib.metadata
import importlib.util
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile

import tools
from config import APP_DIR, APP_BIN
from eventlog import log

USER_AGENT = "fnf-mod-optimizer"
_TMP_DIR = os.path.join(APP_DIR, "tmp")
_NET_TIMEOUT = 30
_RETRIES = 2

# id, display name, kind, role, categories that need it, homepage
DEPS = [
    {"id": "pillow", "name": "Pillow", "kind": "pip", "pkg": "Pillow", "module": "PIL",
     "role": "required", "blocking": True, "needed_for": ["images", "atlas", "astc"],
     "home": "https://python-pillow.org"},
    {"id": "fonttools", "name": "fontTools", "kind": "pip", "pkg": "fonttools", "module": "fontTools",
     "role": "required", "needed_for": ["fonts"],
     "home": "https://github.com/fonttools/fonttools"},
    {"id": "ffmpeg", "name": "FFmpeg", "kind": "binary", "role": "conditional",
     "needed_for": ["videos", "audio"], "home": "https://ffmpeg.org"},
    {"id": "astcenc", "name": "astcenc", "kind": "binary", "role": "conditional",
     "needed_for": ["astc"], "home": "https://github.com/ARM-software/astc-encoder"},
    {"id": "oxipng", "name": "oxipng", "kind": "binary", "role": "optional",
     "needed_for": ["images"], "home": "https://github.com/oxipng/oxipng"},
]
_BY_ID = {d["id"]: d for d in DEPS}


class InstallError(Exception):
    """A step failed; the message is shown to the user as the reason."""

    def __init__(self, message, hint=None):
        super().__init__(message)
        self.hint = hint


class Cancelled(Exception):
    pass


# ----------------------------------------------------------------------
# Platform
# ----------------------------------------------------------------------
def platform_id() -> str:
    if tools.IS_TERMUX:
        return "termux"
    system = platform.system()
    return {"Windows": "windows", "Darwin": "macos"}.get(system, "linux")


def arch_id() -> str:
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        return "x64"
    if machine in ("aarch64", "arm64", "armv8l"):
        return "arm64"
    if machine.startswith("armv7") or machine == "armhf":
        return "armhf"
    if machine in ("i386", "i686", "x86"):
        return "x86"
    return machine or "unknown"


def _exe(name: str) -> str:
    return name + (".exe" if os.name == "nt" else "")


# ----------------------------------------------------------------------
# Status
# ----------------------------------------------------------------------
_version_cache = {}


def _run_version(path: str, flag: str = "-version"):
    try:
        res = subprocess.run([path, flag], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, text=True, timeout=8,
                             creationflags=_NO_WINDOW)
        first = (res.stdout or "").strip().splitlines()
        return res.returncode, (first[0][:80] if first else "")
    except Exception:
        return -1, ""


_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def _binary_version(dep_id: str, path: str) -> str:
    key = (dep_id, path)
    if key in _version_cache:
        return _version_cache[key]
    flag = "--version" if dep_id == "oxipng" else "-version"
    code, line = _run_version(path, flag)
    version = ""
    if line:
        parts = line.replace(",", " ").split()
        if dep_id == "ffmpeg" and "version" in parts:
            version = parts[parts.index("version") + 1]
        elif dep_id == "astcenc":
            version = next((p for p in parts if p.lower().startswith("v") and p[1:2].isdigit()), parts[-1] if parts else "")
        else:
            version = parts[-1] if parts else ""
    _version_cache[key] = version[:40]
    return _version_cache[key]


def _pip_info(dep: dict):
    importlib.invalidate_caches()
    try:
        found = importlib.util.find_spec(dep["module"]) is not None
    except (ImportError, ValueError):
        found = False
    version = ""
    if found:
        try:
            version = importlib.metadata.version(dep["pkg"])
        except Exception:
            version = ""
    return found, version


def manual_hint(dep_id: str) -> dict:
    """What the user can do by hand when the automatic install can't."""
    dep = _BY_ID[dep_id]
    plat = platform_id()
    cmd = None
    if dep["kind"] == "pip":
        cmd = f"{os.path.basename(sys.executable) or 'python'} -m pip install {dep['pkg']}"
        if plat == "termux" and dep_id == "pillow":
            cmd = "pkg install python-pillow"
    elif dep_id == "ffmpeg":
        cmd = {"termux": "pkg install ffmpeg", "macos": "brew install ffmpeg",
               "linux": "sudo apt install ffmpeg", "windows": "winget install Gyan.FFmpeg"}.get(plat)
    elif dep_id == "oxipng":
        cmd = {"termux": "pkg install oxipng", "macos": "brew install oxipng",
               "linux": "cargo install oxipng", "windows": "winget install oxipng"}.get(plat)
    elif dep_id == "astcenc":
        cmd = {"macos": "brew install astc-encoder"}.get(plat)
    return {"url": dep["home"], "command": cmd}


def status(dep_id: str = None) -> list:
    """Current state of every dependency (or one)."""
    out = []
    for dep in DEPS:
        if dep_id and dep["id"] != dep_id:
            continue
        item = {"id": dep["id"], "name": dep["name"], "role": dep["role"],
                "blocking": bool(dep.get("blocking")),
                "needed_for": dep["needed_for"], "kind": dep["kind"],
                "installed": False, "version": "", "path": None, "source": None,
                "can_install": can_install(dep["id"]), "manual": manual_hint(dep["id"])}
        if dep["kind"] == "pip":
            found, version = _pip_info(dep)
            item.update(installed=found, version=version,
                        path=None, source="python" if found else None)
        else:
            path = {"ffmpeg": tools.FFMPEG_PATH if tools.FFMPEG_AVAILABLE else None,
                    "astcenc": tools.ASTCENC_PATH, "oxipng": tools.OXIPNG_PATH}[dep["id"]]
            if path:
                item.update(installed=True, path=path,
                            version=_binary_version(dep["id"], path),
                            source="app" if os.path.abspath(path).startswith(os.path.abspath(APP_BIN)) else "system")
        out.append(item)
    return out


def can_install(dep_id: str) -> bool:
    dep = _BY_ID[dep_id]
    if dep["kind"] == "pip":
        return True
    plat, arch = platform_id(), arch_id()
    if dep_id == "ffmpeg":
        return plat in ("termux", "windows", "macos") or (plat == "linux" and arch in ("x64", "arm64", "armhf", "x86"))
    if dep_id == "astcenc":
        if plat == "termux":
            return True
        if plat in ("windows", "macos"):
            return True
        return plat == "linux" and (arch == "x64" or _have_build_tools())
    if dep_id == "oxipng":
        return plat in ("termux", "windows", "macos") or (plat == "linux" and arch in ("x64", "arm64"))
    return False


def _have_build_tools() -> bool:
    return bool(shutil.which("git") and shutil.which("cmake") and (shutil.which("c++") or shutil.which("g++") or shutil.which("clang++")))


def log_status(items: list = None) -> list:
    """Write one log line per dependency (this is the 'checking
    dependencies' output the user sees in the terminal panel)."""
    log.info("Checking dependencies…", key="log.check.start")
    items = items if items is not None else status()
    for it in items:
        if it["installed"]:
            log.ok(f"{it['name']} {it['version']} found", key="log.dep.ok",
                   name=it["name"], version=it["version"] or "", where=it["source"] or "")
        else:
            level = log.warn if it["role"] == "required" else log.info
            level(f"{it['name']} not found ({it['role']})", key="log.dep.missing",
                  name=it["name"], role=it["role"])
    return items


def hot_load_python_deps() -> None:
    """After pip installs Pillow/fontTools, make the already-imported
    optimizer modules see them without restarting the program."""
    importlib.invalidate_caches()
    try:
        from PIL import Image, ImageChops, ImageStat
        for name in ("optimizers.png_codec", "optimizers.image_optimizer",
                     "optimizers.atlas_optimizer", "optimizers.astc_optimizer"):
            mod = sys.modules.get(name)
            if mod is not None:
                mod.PIL_AVAILABLE = True
                mod.Image, mod.ImageChops, mod.ImageStat = Image, ImageChops, ImageStat
    except ImportError:
        pass
    try:
        from fontTools.ttLib import TTFont
        from fontTools import subset as ft_subset
        mod = sys.modules.get("optimizers.font_optimizer")
        if mod is not None:
            mod.TTFont, mod.ft_subset, mod.FONTTOOLS_AVAILABLE = TTFont, ft_subset, True
    except ImportError:
        pass


# ----------------------------------------------------------------------
# Install machinery
# ----------------------------------------------------------------------
class _Progress:
    """Maps step-local 0..1 progress onto one overall 0..1 bar."""

    def __init__(self, manager, steps):
        self.manager = manager
        self.steps = steps                       # [(name, weight), ...]
        self.total = float(sum(w for _n, w in steps)) or 1.0
        self.done_weight = 0.0
        self.current = None
        self.current_weight = 0.0

    def begin(self, name):
        if self.current is not None:
            self.done_weight += self.current_weight
        self.current = name
        self.current_weight = dict(self.steps).get(name, 0.0)
        self.manager._set(step=name, progress=min(1.0, self.done_weight / self.total), detail="")

    def update(self, fraction, detail=""):
        fraction = max(0.0, min(1.0, fraction))
        overall = (self.done_weight + self.current_weight * fraction) / self.total
        self.manager._set(progress=min(1.0, overall), detail=detail)

    def indeterminate(self, detail=""):
        self.manager._set(progress=None, detail=detail)


class DepManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = {"running": False, "dep": None, "state": "idle", "step": None,
                       "progress": 0.0, "detail": "", "error": None, "hint": None,
                       "started": None, "finished": None}
        self._cancel = threading.Event()
        self._proc = None
        self._thread = None
        self._status_cache = None

    # -- state ------------------------------------------------------------
    def snapshot(self) -> dict:
        with self._lock:
            return dict(self._state)

    def _set(self, **kw) -> None:
        with self._lock:
            self._state.update(kw)

    @property
    def running(self) -> bool:
        return bool(self._state["running"])

    def status(self, force: bool = True) -> list:
        if force or self._status_cache is None:
            self._status_cache = status()
        return self._status_cache

    def check(self) -> list:
        """Re-detect everything, log the result, return it."""
        tools.refresh()
        _version_cache.clear()
        self._status_cache = None
        return log_status(self.status(force=True))

    # -- control ------------------------------------------------------------
    def start_install(self, dep_id: str) -> None:
        dep = _BY_ID.get(dep_id)
        if dep is None:
            raise ValueError("Unknown dependency")
        if self._state["running"]:
            raise ValueError("Another installation is already running")
        if not can_install(dep_id):
            raise ValueError("Automatic install is not available on this system; use the manual instructions")
        if next(i for i in self.status(force=True) if i["id"] == dep_id)["installed"]:
            raise ValueError("Already installed")
        self._cancel.clear()
        with self._lock:
            self._state.update(running=True, dep=dep_id, state="running", step="prepare",
                               progress=0.0, detail="", error=None, hint=None,
                               started=time.time(), finished=None)
        self._thread = threading.Thread(target=self._run, args=(dep,), name=f"install-{dep_id}", daemon=True)
        self._thread.start()

    def cancel(self) -> None:
        self._cancel.set()
        proc = self._proc
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
            except OSError:
                pass

    def wait(self, timeout=None) -> None:
        if self._thread:
            self._thread.join(timeout)

    # -- worker ---------------------------------------------------------------
    def _run(self, dep: dict) -> None:
        name = dep["name"]
        log.info(f"Installing {name}…", key="log.install.start", name=name)
        try:
            {"pip": self._install_pip,
             "binary": self._install_binary}[dep["kind"]](dep)
            tools.refresh()
            _version_cache.clear()
            if dep["kind"] == "pip":
                hot_load_python_deps()
            self._status_cache = None
            now = next(i for i in self.status(force=True) if i["id"] == dep["id"])
            if not now["installed"]:
                raise InstallError("Finished, but the program was not found afterwards",
                                   hint=manual_hint(dep["id"]))
            self._set(running=False, state="done", progress=1.0, step="done",
                      detail=now["version"], finished=time.time())
            log.ok(f"{name} installed ({now['version']})" if now["version"] else f"{name} installed",
                   key="log.install.done", name=name, version=now["version"] or "")
        except Cancelled:
            self._set(running=False, state="cancelled", finished=time.time(), detail="")
            log.warn(f"{name} installation cancelled", key="log.install.cancel", name=name)
        except InstallError as exc:
            self._fail(name, str(exc), exc.hint or manual_hint(dep["id"]))
        except Exception as exc:                         # never let the thread die silently
            self._fail(name, f"{type(exc).__name__}: {exc}", manual_hint(dep["id"]))
        finally:
            self._proc = None
            self._cleanup_tmp()

    def _fail(self, name, reason, hint):
        self._set(running=False, state="error", error=reason, hint=hint, finished=time.time())
        log.error(f"{name} installation failed: {reason}", key="log.install.fail", name=name, reason=reason)

    def _check_cancel(self):
        if self._cancel.is_set():
            raise Cancelled()

    @staticmethod
    def _cleanup_tmp():
        shutil.rmtree(_TMP_DIR, ignore_errors=True)

    # -- subprocess with live output ------------------------------------------------
    def _stream(self, cmd, timeout=1800, cwd=None, env=None) -> int:
        """Run a command, pushing every output line to the log as it is
        printed. Returns the exit code (cancel kills the process)."""
        log.out("$ " + " ".join(cmd))
        try:
            proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, bufsize=1,
                                    errors="replace", cwd=cwd, env=env, creationflags=_NO_WINDOW)
        except FileNotFoundError:
            raise InstallError(f"Command not found: {cmd[0]}")
        self._proc = proc
        killer = threading.Timer(timeout, lambda: proc.poll() is None and proc.kill())
        killer.daemon = True
        killer.start()
        tail = []
        try:
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    log.out(line)
                    tail.append(line)
                    del tail[:-12]
                self._set(detail=line[:90])
                if self._cancel.is_set():
                    proc.terminate()
                    break
            proc.wait()
        finally:
            killer.cancel()
            self._proc = None
        self._check_cancel()
        self._last_output = "\n".join(tail)
        return proc.returncode

    # -- pip ----------------------------------------------------------------------------
    def _install_pip(self, dep: dict) -> None:
        prog = _Progress(self, [("pip", 1.0)])
        prog.begin("pip")
        prog.indeterminate()
        cmd = [sys.executable, "-m", "pip", "install", "--disable-pip-version-check",
               "--progress-bar", "off", dep["pkg"]]
        code = self._stream(cmd, timeout=900)
        if code == 0:
            return
        output = getattr(self, "_last_output", "")
        if platform_id() == "termux" and dep["id"] == "pillow" and shutil.which("pkg"):
            log.warn("pip could not build Pillow here; trying Termux's own package", key="log.install.pkgfallback")
            if self._stream(["pkg", "install", "-y", "python-pillow"], timeout=900) == 0:
                return
        if "externally-managed-environment" in output:
            raise InstallError("This Python is managed by the system, so pip refuses to install into it. "
                               "Use a virtual environment (python -m venv) or install it manually.",
                               hint=manual_hint(dep["id"]))
        raise InstallError(f"pip exited with code {code}", hint=manual_hint(dep["id"]))

    # -- downloads --------------------------------------------------------------------------
    def _http_get_json(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "application/vnd.github+json"})
        try:
            with urllib.request.urlopen(req, timeout=_NET_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 403:
                raise InstallError("GitHub refused the request (rate limit). Try again in a few minutes.")
            raise InstallError(f"Could not read the release list (HTTP {exc.code})")
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise InstallError(f"Network problem while contacting GitHub: {exc}")

    def _http_get_text(self, url: str):
        req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
        try:
            with urllib.request.urlopen(req, timeout=_NET_TIMEOUT) as resp:
                return resp.read(4096).decode("utf-8", "replace")
        except Exception:
            return None

    def _download(self, url: str, dest: str, label: str, prog: _Progress, expect=None) -> None:
        """Download with progress, retries and optional checksum
        (expect = ("sha256"|"md5", hexdigest))."""
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        part = dest + ".part"
        last_error = None
        for attempt in range(1, _RETRIES + 2):
            self._check_cancel()
            try:
                req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
                hasher = hashlib.new(expect[0]) if expect else None
                with urllib.request.urlopen(req, timeout=_NET_TIMEOUT) as resp, open(part, "wb") as fh:
                    total = int(resp.headers.get("Content-Length") or 0)
                    done, next_log = 0, 0.1
                    while True:
                        self._check_cancel()
                        chunk = resp.read(256 * 1024)
                        if not chunk:
                            break
                        fh.write(chunk)
                        if hasher:
                            hasher.update(chunk)
                        done += len(chunk)
                        if total:
                            frac = done / total
                            prog.update(frac, f"{done / 1048576:.1f} / {total / 1048576:.1f} MB")
                            if frac >= next_log:
                                log.out(f"  {label}: {int(frac * 100)}%  ({done / 1048576:.1f} / {total / 1048576:.1f} MB)")
                                next_log += 0.1
                        else:
                            prog.indeterminate(f"{done / 1048576:.1f} MB")
                if done < 1024:
                    raise InstallError("The download was empty")
                if total and done != total:
                    raise InstallError("The download was cut short")
                if expect:
                    if hasher.hexdigest().lower() != expect[1].lower():
                        raise InstallError("Checksum mismatch: the downloaded file is not what was published. Nothing was installed.")
                    log.ok(f"{expect[0].upper()} checksum verified", key="log.install.checksum", algo=expect[0].upper())
                os.replace(part, dest)
                return
            except (Cancelled, InstallError):
                self._rm(part)
                raise
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                last_error = exc
                self._rm(part)
                if attempt <= _RETRIES:
                    log.warn(f"Download failed ({exc}); retrying ({attempt}/{_RETRIES})…",
                             key="log.install.retry", reason=str(exc), n=attempt, max=_RETRIES)
                    time.sleep(1.5 * attempt)
        raise InstallError(f"Download failed: {last_error}")

    @staticmethod
    def _rm(path):
        try:
            os.remove(path)
        except OSError:
            pass

    # -- extraction --------------------------------------------------------------------------------
    @staticmethod
    def _extract_members(archive: str, wanted, dest_dir: str) -> list:
        """Copy members whose base name satisfies wanted(name) out of a
        zip/tar archive, flattened into dest_dir. Returns the paths."""
        os.makedirs(dest_dir, exist_ok=True)
        out = []
        if archive.endswith(".zip"):
            with zipfile.ZipFile(archive) as zf:
                for info in zf.infolist():
                    base = os.path.basename(info.filename)
                    if info.is_dir() or not base or not wanted(base):
                        continue
                    target = os.path.join(dest_dir, base)
                    with zf.open(info) as src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    out.append(target)
        else:
            with tarfile.open(archive) as tf:
                for member in tf:
                    base = os.path.basename(member.name)
                    if not member.isfile() or not base or not wanted(base):
                        continue
                    target = os.path.join(dest_dir, base)
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    with src, open(target, "wb") as dst:
                        shutil.copyfileobj(src, dst)
                    out.append(target)
        for path in out:
            try:
                os.chmod(path, 0o755)
            except OSError:
                pass
        return out

    def _verify_runs(self, path: str, flag: str = "-version", must_contain: str = None) -> str:
        code, line = _run_version(path, flag)
        if code != 0 or (must_contain and must_contain.lower() not in line.lower()):
            raise InstallError(f"{os.path.basename(path)} was installed but does not run on this system")
        return line

    # -- binary installs -------------------------------------------------------------------------------
    def _install_binary(self, dep: dict) -> None:
        os.makedirs(APP_BIN, exist_ok=True)
        os.makedirs(_TMP_DIR, exist_ok=True)
        getattr(self, f"_install_{dep['id']}")(dep)

    def _github_asset(self, repo: str, pick):
        release = self._http_get_json(f"https://api.github.com/repos/{repo}/releases/latest")
        for asset in release.get("assets", []):
            if pick(asset.get("name", "").lower()):
                digest = asset.get("digest") or ""
                expect = ("sha256", digest.split(":", 1)[1]) if digest.startswith("sha256:") else None
                return asset["browser_download_url"], asset["name"], expect, release.get("tag_name", "")
        raise InstallError("No download for this system was found in the latest release")

    def _fetch_and_unpack(self, url, name, expect, wanted, prog) -> list:
        archive = os.path.join(_TMP_DIR, name)
        prog.begin("download")
        self._download(url, archive, name, prog, expect)
        self._check_cancel()
        prog.begin("extract")
        prog.indeterminate()
        log.info(f"Unpacking {name}…", key="log.install.extract", name=name)
        try:
            found = self._extract_members(archive, wanted, os.path.join(_TMP_DIR, "unpacked"))
        except (zipfile.BadZipFile, tarfile.TarError, OSError) as exc:
            raise InstallError(f"The downloaded archive is damaged: {exc}")
        if not found:
            raise InstallError("The archive did not contain the expected program")
        return found

    def _place(self, src: str, dest_name: str) -> str:
        dest = os.path.join(APP_BIN, dest_name)
        tmp = dest + ".new"
        shutil.copyfile(src, tmp)
        os.chmod(tmp, 0o755)
        os.replace(tmp, dest)
        return dest

    # ffmpeg ----------------------------------------------------------------------------------------------
    def _install_ffmpeg(self, dep):
        plat, arch = platform_id(), arch_id()
        if plat == "termux":
            return self._pkg_install("ffmpeg")
        if plat == "macos" and shutil.which("brew"):
            prog = _Progress(self, [("brew", 1.0)])
            prog.begin("brew")
            prog.indeterminate()
            if self._stream(["brew", "install", "ffmpeg"], timeout=1800) != 0:
                raise InstallError("brew install ffmpeg failed")
            return
        prog = _Progress(self, [("download", 0.8), ("extract", 0.1), ("verify", 0.1)])
        if plat == "windows":
            url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
            expect = self._published_hash(url + ".sha256", "sha256")
            found = self._fetch_and_unpack(url, "ffmpeg-release-essentials.zip", expect,
                                           lambda b: b.lower() in ("ffmpeg.exe", "ffprobe.exe"), prog)
        elif plat == "macos":
            found = []
            for part, label in (("", "ffmpeg"), ("ffprobe/", "ffprobe")):
                url = f"https://evermeet.cx/ffmpeg/getrelease/{part}zip"
                found += self._fetch_and_unpack(url, f"{label}.zip", None,
                                                lambda b, l=label: b == l, prog)
        else:
            tag = {"x64": "amd64", "arm64": "arm64", "armhf": "armhf", "x86": "i686"}[arch]
            url = f"https://johnvansickle.com/ffmpeg/releases/ffmpeg-release-{tag}-static.tar.xz"
            expect = self._published_hash(url + ".md5", "md5")
            found = self._fetch_and_unpack(url, f"ffmpeg-{tag}-static.tar.xz", expect,
                                           lambda b: b in ("ffmpeg", "ffprobe"), prog)
        prog.begin("verify")
        names = {os.path.basename(p).lower(): p for p in found}
        for tool in ("ffmpeg", "ffprobe"):
            src = names.get(_exe(tool))
            if not src:
                raise InstallError(f"{tool} was missing from the archive")
            self._verify_runs(self._place(src, _exe(tool)))

    def _published_hash(self, url: str, algo: str):
        text = self._http_get_text(url)
        if text:
            token = text.split()[0] if text.split() else ""
            if len(token) == {"sha256": 64, "md5": 32}[algo] and all(c in "0123456789abcdefABCDEF" for c in token):
                return (algo, token)
        log.warn("No published checksum found; continuing over HTTPS only", key="log.install.nochecksum")
        return None

    # pkg (Termux) ------------------------------------------------------------------------------------------
    def _pkg_install(self, package: str):
        if not shutil.which("pkg"):
            raise InstallError("`pkg` was not found. Is this running inside Termux?")
        prog = _Progress(self, [("pkg", 1.0)])
        prog.begin("pkg")
        prog.indeterminate()
        if self._stream(["pkg", "install", "-y", package], timeout=1800) != 0:
            raise InstallError(f"pkg install {package} failed")

    # astcenc ---------------------------------------------------------------------------------------------------
    def _install_astcenc(self, dep):
        plat, arch = platform_id(), arch_id()
        if plat == "termux" or (plat == "linux" and arch != "x64"):
            return self._build_astcenc()
        keyword = {"windows": "windows", "macos": "macos", "linux": "linux"}[plat]
        prog = _Progress(self, [("download", 0.8), ("extract", 0.1), ("verify", 0.1)])
        prog.begin("download")
        url, name, expect, tag = self._github_asset(
            "ARM-software/astc-encoder",
            lambda n: keyword in n and (n.endswith(".zip") or n.endswith(".tar.gz")) and "debug" not in n)
        found = self._fetch_and_unpack(url, name, expect,
                                       lambda b: b.lower().startswith("astcenc") and not b.lower().endswith((".txt", ".md", ".pdb")), prog)
        prog.begin("verify")
        order = ["avx2", "sse4.1", "sse2", "neon", "universal", "none"]

        def rank(path):
            base = os.path.basename(path).lower()
            for i, tag_ in enumerate(order):
                if tag_ in base:
                    return i
            return len(order)

        for candidate in sorted(found, key=rank):
            code, line = _run_version(candidate, "-version")
            if code == 0 and "astcenc" in line.lower():
                log.info(f"Using {os.path.basename(candidate)} for this CPU", key="log.install.variant",
                         file=os.path.basename(candidate))
                self._place(candidate, _exe("astcenc"))
                return
        raise InstallError("None of the downloaded astcenc builds runs on this CPU")

    def _build_astcenc(self):
        """No prebuilt binary for this system: build from source."""
        plat = platform_id()
        if plat == "termux":
            need = [t for t in ("git", "cmake", "clang", "make") if not shutil.which(t)]
            if need:
                prog = _Progress(self, [("pkg", 1.0)])
                prog.begin("pkg")
                prog.indeterminate()
                if self._stream(["pkg", "install", "-y", "git", "cmake", "clang", "make"], timeout=1800) != 0:
                    raise InstallError("Could not install the build tools")
        elif not _have_build_tools():
            raise InstallError("No prebuilt astcenc for this CPU, and git/cmake/a C++ compiler are missing to build it")
        prog = _Progress(self, [("clone", 0.1), ("build", 0.85), ("verify", 0.05)])
        src = os.path.join(_TMP_DIR, "astc-encoder")
        prog.begin("clone")
        prog.indeterminate()
        if self._stream(["git", "clone", "--depth", "1", "https://github.com/ARM-software/astc-encoder.git", src], timeout=600) != 0:
            raise InstallError("git clone failed")
        build = os.path.join(src, "build")
        os.makedirs(build, exist_ok=True)
        prog.begin("build")
        prog.indeterminate("this takes a few minutes")
        isa = "-DASTCENC_ISA_NEON=ON" if arch_id() == "arm64" else "-DASTCENC_ISA_NONE=ON"
        if self._stream(["cmake", isa, "-DCMAKE_BUILD_TYPE=Release", "-DASTCENC_CLI=ON", ".."], cwd=build, timeout=600) != 0:
            raise InstallError("cmake configure failed")
        if self._stream(["cmake", "--build", ".", "--config", "Release", "-j", str(os.cpu_count() or 2)], cwd=build, timeout=3600) != 0:
            raise InstallError("The build failed")
        built = None
        for root, _dirs, files in os.walk(build):
            for fname in files:
                full = os.path.join(root, fname)
                if fname.startswith("astcenc") and "." not in fname and os.access(full, os.X_OK) and os.path.isfile(full):
                    built = full
        if not built:
            raise InstallError("The build finished but no astcenc program was produced")
        prog.begin("verify")
        self._verify_runs(self._place(built, _exe("astcenc")), "-version", "astcenc")

    # oxipng -------------------------------------------------------------------------------------------------------
    def _install_oxipng(self, dep):
        plat, arch = platform_id(), arch_id()
        if plat == "termux" and shutil.which("pkg"):
            try:
                self._pkg_install("oxipng")
                return
            except InstallError:
                log.warn("Termux has no oxipng package; downloading the static build instead", key="log.install.pkgfallback2")
        triple = {("windows", "x64"): "x86_64-pc-windows-msvc",
                  ("linux", "x64"): "x86_64-unknown-linux-musl",
                  ("linux", "arm64"): "aarch64-unknown-linux-musl",
                  ("termux", "arm64"): "aarch64-unknown-linux-musl",
                  ("macos", "x64"): "x86_64-apple-darwin",
                  ("macos", "arm64"): "aarch64-apple-darwin"}.get((plat, arch))
        if not triple:
            raise InstallError("No oxipng download exists for this system")
        prog = _Progress(self, [("download", 0.8), ("extract", 0.1), ("verify", 0.1)])
        prog.begin("download")
        url, name, expect, tag = self._github_asset(
            "oxipng/oxipng", lambda n: triple in n and (n.endswith(".zip") or n.endswith(".tar.gz")))
        found = self._fetch_and_unpack(url, name, expect, lambda b: b.lower() == _exe("oxipng"), prog)
        prog.begin("verify")
        self._verify_runs(self._place(found[0], _exe("oxipng")), "--version", "oxipng")


manager = DepManager()
