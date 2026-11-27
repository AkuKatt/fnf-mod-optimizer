"""
setup.py
--------
One-click setup for the FNF Mod Optimizer.

Run it with:

    python setup.py

It will:
  1. Detect your platform (Windows / Termux-Android / Linux / macOS).
  2. Detect your hardware (CPU cores, RAM, GPU vendor if any).
  3. Install Pillow (required, for image optimization).
  4. Install/locate FFmpeg + ffprobe (required, for video/audio).
  5. Try to install/build astcenc (optional, only used for .astc textures).
  6. Offer to install oxipng (optional, extra lossless PNG squeeze -
     always asks for confirmation first since it downloads/builds a
     third-party binary; never installed silently).
  7. Print a clear summary of what's ready and what (if anything) still
     needs a manual step, with a direct link/command for it.

Nothing here requires you to already have FFmpeg or astcenc on PATH -
that's the whole point of running this once before using the tool.
Safe to re-run any time; every step skips itself if it's already done.
"""

import os
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile

# ------------------------------------------------------------------
# Small terminal UI helpers (no third-party dependency - this has to
# run *before* anything is installed, including Pillow)
# ------------------------------------------------------------------
_SUPPORTS_COLOR = (
    os.environ.get("NO_COLOR") is None
    and (sys.stdout.isatty() or os.environ.get("FORCE_COLOR"))
)


def _c(code: str, text: str) -> str:
    if not _SUPPORTS_COLOR:
        return text
    return f"\033[{code}m{text}\033[0m"


def bold(t):    return _c("1", t)
def dim(t):     return _c("2", t)
def green(t):   return _c("32", t)
def yellow(t):  return _c("33", t)
def red(t):     return _c("31", t)
def cyan(t):    return _c("36", t)


def _enable_windows_ansi():
    """Windows terminals need this poked once before ANSI codes work."""
    if os.name != "nt":
        return
    try:
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            kernel32.SetConsoleMode(handle, mode.value | 0x0004)
    except Exception:
        pass


def banner():
    line = "=" * 52
    print(cyan(line))
    print(cyan(bold("        FNF MOD OPTIMIZER - ONE-CLICK SETUP")))
    print(cyan(line))
    print()


def step_header(n, total, title):
    print(f"\n{bold(cyan(f'[{n}/{total}]'))} {bold(title)}")


def ok(msg):
    print(f"  {green('OK')}    {msg}")


def warn(msg):
    print(f"  {yellow('WARN')}  {msg}")


def fail(msg):
    print(f"  {red('FAIL')}  {msg}")


def info(msg):
    print(f"  {dim('..')}    {msg}")


def confirm(question: str, default_yes: bool = True) -> bool:
    """Ask a plain yes/no question. Never assumes consent for a
    download/install - if the input stream isn't interactive (rare,
    e.g. piped input) it safely defaults to "no" rather than silently
    downloading something the person never agreed to."""
    suffix = "[Y/n]" if default_yes else "[y/N]"
    try:
        answer = input(f"  {bold('?')}     {question} {suffix} ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        return False
    if not answer:
        return default_yes
    return answer in ("y", "yes")


class Spinner:
    """Minimal progress indicator for long-running steps (downloads,
    builds) that don't have a natural percentage to report."""
    _FRAMES = "|/-\\"

    def __init__(self, label: str):
        self.label = label
        self._i = 0

    def tick(self):
        frame = self._FRAMES[self._i % len(self._FRAMES)]
        sys.stdout.write(f"\r  {cyan(frame)}    {self.label}...   ")
        sys.stdout.flush()
        self._i += 1

    def done(self, ok_: bool, final_msg: str = None):
        sys.stdout.write("\r" + " " * (len(self.label) + 20) + "\r")
        if ok_:
            ok(final_msg or self.label)
        else:
            fail(final_msg or self.label)


def download_with_progress(url: str, dest_path: str, label: str) -> bool:
    """Download a file, printing a simple percentage progress bar.
    Returns True on success."""
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "fnf-mod-optimizer-setup"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            total = int(resp.headers.get("Content-Length", 0))
            downloaded = 0
            chunk_size = 1024 * 256
            with open(dest_path, "wb") as f:
                while True:
                    chunk = resp.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    downloaded += len(chunk)
                    if total:
                        pct = downloaded / total
                        bar_width = 28
                        filled = int(bar_width * pct)
                        bar = "#" * filled + "-" * (bar_width - filled)
                        sys.stdout.write(
                            f"\r  {cyan('..')}    {label} [{bar}] {pct*100:5.1f}%"
                        )
                    else:
                        sys.stdout.write(f"\r  {cyan('..')}    {label} ({downloaded // 1024} KB)")
                    sys.stdout.flush()
        sys.stdout.write("\n")
        return True
    except Exception as e:
        sys.stdout.write("\n")
        fail(f"{label} failed: {e}")
        return False


# ------------------------------------------------------------------
# Platform / hardware detection
# ------------------------------------------------------------------
def is_termux() -> bool:
    return "com.termux" in os.environ.get("PREFIX", "") or os.path.exists("/data/data/com.termux")


def detect_platform() -> str:
    if is_termux():
        return "termux"
    system = platform.system()
    if system == "Windows":
        return "windows"
    if system == "Darwin":
        return "macos"
    return "linux"


def detect_gpu_vendor() -> str:
    """Best-effort GPU vendor sniff, informational only - the actual
    optimizer probes FFmpeg's compiled encoders at run time, this is
    just to show the user something useful during setup."""
    system = platform.system()
    try:
        if system == "Windows":
            out = subprocess.run(
                ["wmic", "path", "win32_VideoController", "get", "name"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=10,
            ).stdout.lower()
        elif system == "Linux" and not is_termux():
            out = subprocess.run(
                ["lspci"], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=10,
            ).stdout.lower()
        elif system == "Darwin":
            out = subprocess.run(
                ["system_profiler", "SPDisplaysDataType"],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                text=True, timeout=15,
            ).stdout.lower()
        else:
            return "unknown"
    except Exception:
        return "unknown"

    found = []
    if "nvidia" in out:
        found.append("NVIDIA")
    if "amd" in out or "radeon" in out or "advanced micro" in out:
        found.append("AMD")
    if "intel" in out:
        found.append("Intel")
    if "apple" in out:
        found.append("Apple")
    return ", ".join(found) if found else "none detected / integrated only"


def detect_hardware_summary():
    cores = os.cpu_count() or "unknown"
    ram_gb = "unknown"
    try:
        if platform.system() == "Linux":
            with open("/proc/meminfo") as f:
                for line in f:
                    if line.startswith("MemTotal:"):
                        kb = int(line.split()[1])
                        ram_gb = f"{kb / 1024 / 1024:.1f} GB"
                        break
        elif platform.system() == "Windows":
            import ctypes

            class MEMORYSTATUSEX(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]
            stat = MEMORYSTATUSEX()
            stat.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))
            ram_gb = f"{stat.ullTotalPhys / (1024**3):.1f} GB"
        elif platform.system() == "Darwin":
            out = subprocess.run(["sysctl", "-n", "hw.memsize"], stdout=subprocess.PIPE, text=True).stdout.strip()
            ram_gb = f"{int(out) / (1024**3):.1f} GB"
    except Exception:
        pass
    return cores, ram_gb


# ------------------------------------------------------------------
# Step: Python package deps (Pillow)
# ------------------------------------------------------------------
def install_pillow() -> bool:
    try:
        import PIL  # noqa: F401
        import fontTools  # noqa: F401  (the font optimizer needs it)
        ok(f"Pillow (v{PIL.__version__}) and fontTools already installed")
        return True
    except ImportError:
        pass

    info("Installing Pillow + fontTools via pip...")
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "Pillow", "fonttools"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300,
        )
        if result.returncode == 0:
            ok("Pillow installed")
            return True
        fail("pip install Pillow failed:")
        print(dim(result.stdout[-800:]))
        return False
    except Exception as e:
        fail(f"pip install Pillow failed: {e}")
        return False


# ------------------------------------------------------------------
# Step: FFmpeg (platform-specific)
# ------------------------------------------------------------------
def ffmpeg_present() -> bool:
    return shutil.which("ffmpeg") is not None and shutil.which("ffprobe") is not None


def install_ffmpeg_windows(plat_dir: str) -> bool:
    """Download a static Windows FFmpeg build and drop it in a local
    ./ffmpeg-bin folder, adding it to THIS PROCESS's PATH so the rest
    of setup (and a subsequent `python main.py` in the same shell,
    once the folder is on PATH) can find it. Also prints how to add it
    to PATH permanently."""
    bin_dir = os.path.join(plat_dir, "ffmpeg-bin")
    exe = os.path.join(bin_dir, "ffmpeg.exe")
    probe = os.path.join(bin_dir, "ffprobe.exe")
    if os.path.exists(exe) and os.path.exists(probe):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
        ok(f"FFmpeg already downloaded ({bin_dir})")
        return True

    url = "https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
    with tempfile.TemporaryDirectory() as tmp:
        zip_path = os.path.join(tmp, "ffmpeg.zip")
        if not download_with_progress(url, zip_path, "Downloading FFmpeg (essentials build)"):
            warn("Automatic FFmpeg download failed - see manual step below.")
            return False

        info("Extracting FFmpeg...")
        try:
            with zipfile.ZipFile(zip_path) as z:
                names = z.namelist()
                bin_entries = [n for n in names if n.endswith(("ffmpeg.exe", "ffprobe.exe"))]
                if len(bin_entries) < 2:
                    fail("Downloaded archive didn't contain ffmpeg.exe/ffprobe.exe as expected.")
                    return False
                os.makedirs(bin_dir, exist_ok=True)
                for entry in bin_entries:
                    with z.open(entry) as src, open(os.path.join(bin_dir, os.path.basename(entry)), "wb") as dst:
                        shutil.copyfileobj(src, dst)
        except Exception as e:
            fail(f"Extracting FFmpeg failed: {e}")
            return False

    os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
    ok(f"FFmpeg installed to {bin_dir}")
    warn("This only adds FFmpeg to PATH for this setup run.")
    print(f"       To use it every time, add this folder to your PATH permanently:")
    print(f"       {bold(bin_dir)}")
    print(f"       (Windows Settings -> search \"env\" -> Edit environment variables ->")
    print(f"        Path -> New -> paste the folder above)")
    return True


def install_ffmpeg_termux() -> bool:
    info("Installing FFmpeg via pkg...")
    try:
        subprocess.run(["pkg", "update", "-y"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
        result = subprocess.run(
            ["pkg", "install", "-y", "ffmpeg"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600,
        )
        if result.returncode == 0 and ffmpeg_present():
            ok("FFmpeg installed via pkg")
            return True
        fail("pkg install ffmpeg failed:")
        print(dim(result.stdout[-800:]))
        return False
    except FileNotFoundError:
        fail("`pkg` not found - is this actually running inside Termux?")
        return False
    except Exception as e:
        fail(f"pkg install ffmpeg failed: {e}")
        return False


def install_ffmpeg_linux() -> bool:
    """Try the distro package manager. Best-effort - Linux distros vary
    too much to fully automate, so this tries the common ones and
    otherwise hands back a clear manual command."""
    managers = [
        (["apt-get", "install", "-y", "ffmpeg"], ["sudo", "apt-get", "update"]),
        (["dnf", "install", "-y", "ffmpeg"], None),
        (["pacman", "-S", "--noconfirm", "ffmpeg"], None),
        (["zypper", "install", "-y", "ffmpeg"], None),
    ]
    for install_cmd, pre_cmd in managers:
        mgr = install_cmd[0]
        if shutil.which(mgr) is None:
            continue
        info(f"Installing FFmpeg via {mgr} (may ask for your sudo password)...")
        try:
            if pre_cmd:
                subprocess.run(["sudo"] + pre_cmd[1:] if pre_cmd[0] == "sudo" else pre_cmd,
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=180)
            full_cmd = ["sudo"] + install_cmd if os.geteuid() != 0 else install_cmd
            result = subprocess.run(full_cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                     text=True, timeout=600)
            if result.returncode == 0 and ffmpeg_present():
                ok(f"FFmpeg installed via {mgr}")
                return True
        except Exception:
            continue
    return False


def install_ffmpeg_macos() -> bool:
    if shutil.which("brew") is None:
        warn("Homebrew not found - can't auto-install FFmpeg on macOS without it.")
        return False
    info("Installing FFmpeg via Homebrew...")
    try:
        result = subprocess.run(
            ["brew", "install", "ffmpeg"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
        )
        if result.returncode == 0 and ffmpeg_present():
            ok("FFmpeg installed via Homebrew")
            return True
        fail("brew install ffmpeg failed:")
        print(dim(result.stdout[-800:]))
        return False
    except Exception as e:
        fail(f"brew install ffmpeg failed: {e}")
        return False


def setup_ffmpeg(plat: str, plat_dir: str) -> bool:
    if ffmpeg_present():
        ok(f"FFmpeg already on PATH ({shutil.which('ffmpeg')})")
        return True

    if plat == "windows":
        result = install_ffmpeg_windows(plat_dir)
    elif plat == "termux":
        result = install_ffmpeg_termux()
    elif plat == "macos":
        result = install_ffmpeg_macos()
    else:
        result = install_ffmpeg_linux()

    if not result and not ffmpeg_present():
        warn("Couldn't auto-install FFmpeg. Manual step:")
        if plat == "windows":
            print("       Download: https://www.gyan.dev/ffmpeg/builds/ (release essentials)")
            print("       Extract, then add its bin\\ folder (with ffmpeg.exe/ffprobe.exe) to PATH.")
        elif plat == "termux":
            print("       Run:  pkg install ffmpeg")
        elif plat == "macos":
            print("       Install Homebrew (https://brew.sh), then run:  brew install ffmpeg")
        else:
            print("       Debian/Ubuntu:  sudo apt install ffmpeg")
            print("       Fedora:         sudo dnf install ffmpeg")
            print("       Arch:           sudo pacman -S ffmpeg")
    return ffmpeg_present()


# ------------------------------------------------------------------
# Step: astcenc (optional)
# ------------------------------------------------------------------
def astcenc_present() -> bool:
    for name in ("astcenc", "astcenc-neon", "astcenc-avx2", "astcenc-sse4.1", "astcenc-sse2", "astcenc-none"):
        if shutil.which(name):
            return True
    termux_prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    for name in ("astcenc", "astcenc-neon"):
        if os.path.exists(os.path.join(termux_prefix, "bin", name)):
            return True
    return False


def install_astcenc_windows(plat_dir: str) -> bool:
    bin_dir = os.path.join(plat_dir, "astcenc-bin")
    exe = os.path.join(bin_dir, "astcenc.exe")
    if os.path.exists(exe):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
        ok(f"astcenc already downloaded ({bin_dir})")
        return True

    try:
        api_url = "https://api.github.com/repos/ARM-software/astc-encoder/releases/latest"
        req = urllib.request.Request(api_url, headers={"User-Agent": "fnf-mod-optimizer-setup"})
        import json
        with urllib.request.urlopen(req, timeout=20) as resp:
            release = json.loads(resp.read().decode())
        asset = next(
            (a for a in release.get("assets", [])
             if "windows" in a["name"].lower() and a["name"].lower().endswith(".zip")),
            None,
        )
        if asset is None:
            warn("No Windows astcenc release asset found automatically.")
            return False

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "astcenc.zip")
            if not download_with_progress(asset["browser_download_url"], zip_path, "Downloading astcenc"):
                return False
            info("Extracting astcenc...")
            os.makedirs(bin_dir, exist_ok=True)
            with zipfile.ZipFile(zip_path) as z:
                for entry in z.namelist():
                    if entry.lower().endswith(".exe") and "astcenc" in entry.lower():
                        with z.open(entry) as src, open(
                            os.path.join(bin_dir, os.path.basename(entry)), "wb"
                        ) as dst:
                            shutil.copyfileobj(src, dst)

        if not os.listdir(bin_dir):
            warn("astcenc archive didn't contain a recognizable .exe.")
            return False

        os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
        ok(f"astcenc installed to {bin_dir}")
        return True
    except Exception as e:
        warn(f"Automatic astcenc download failed: {e}")
        return False


def install_astcenc_termux() -> bool:
    """astcenc isn't in Termux's package repos - build it from source
    with NEON enabled. This is the slowest step by far (a few minutes)
    so it's clearly labelled."""
    if shutil.which("git") is None or shutil.which("cmake") is None:
        info("Installing build tools (git, cmake, clang) via pkg...")
        try:
            subprocess.run(
                ["pkg", "install", "-y", "git", "cmake", "clang", "make"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=600,
            )
        except Exception as e:
            warn(f"Couldn't install build tools: {e}")
            return False

    termux_prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    with tempfile.TemporaryDirectory() as tmp:
        src_dir = os.path.join(tmp, "astc-encoder")
        info("Cloning ARM-software/astc-encoder (this can take a minute)...")
        try:
            clone = subprocess.run(
                ["git", "clone", "--depth", "1",
                 "https://github.com/ARM-software/astc-encoder.git", src_dir],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300,
            )
            if clone.returncode != 0:
                warn("git clone failed:")
                print(dim(clone.stdout[-500:]))
                return False

            build_dir = os.path.join(src_dir, "build")
            os.makedirs(build_dir, exist_ok=True)
            info("Building astcenc with NEON support (this is the slow part)...")
            cmake_cfg = subprocess.run(
                ["cmake", "-DASTCENC_ISA_NEON=ON", "-DCMAKE_BUILD_TYPE=Release", ".."],
                cwd=build_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=180,
            )
            if cmake_cfg.returncode != 0:
                warn("cmake configure failed:")
                print(dim(cmake_cfg.stdout[-500:]))
                return False

            build = subprocess.run(
                ["cmake", "--build", ".", "--config", "Release", "-j", str(os.cpu_count() or 2)],
                cwd=build_dir, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, timeout=900,
            )
            if build.returncode != 0:
                warn("build failed:")
                print(dim(build.stdout[-500:]))
                return False

            built = None
            for root, _dirs, files in os.walk(build_dir):
                for fname in files:
                    if fname.startswith("astcenc") and os.access(os.path.join(root, fname), os.X_OK):
                        built = os.path.join(root, fname)
                        break
                if built:
                    break
            if not built:
                warn("Build finished but the astcenc binary wasn't found.")
                return False

            dest = os.path.join(termux_prefix, "bin", "astcenc")
            shutil.copy(built, dest)
            os.chmod(dest, 0o755)
            ok(f"astcenc built and installed to {dest}")
            return True
        except Exception as e:
            warn(f"Building astcenc failed: {e}")
            return False


def setup_astcenc(plat: str, plat_dir: str) -> bool:
    if astcenc_present():
        ok("astcenc already available")
        return True

    if plat == "windows":
        result = install_astcenc_windows(plat_dir)
    elif plat == "termux":
        result = install_astcenc_termux()
    else:
        result = False  # Linux/macOS: package availability varies too much to script reliably

    if not result and not astcenc_present():
        warn("astcenc not installed (optional - only needed for .astc textures).")
        if plat in ("linux", "macos"):
            print("       Build from source: https://github.com/ARM-software/astc-encoder")
        else:
            print("       Download manually: https://github.com/ARM-software/astc-encoder/releases")
    return astcenc_present()


# ------------------------------------------------------------------
# Step: oxipng (optional - extra lossless PNG squeeze)
# ------------------------------------------------------------------
# Pillow's own PNG encoder is a plain zlib wrapper - fast, but leaves
# real size on the table. oxipng is a dedicated PNG-only tool that:
#   1. Tries every PNG row-filter strategy (None/Sub/Up/Average/Paeth)
#      per scanline and keeps whichever compresses smallest, instead
#      of Pillow's single fixed strategy.
#   2. Recompresses with Zopfli, a much more exhaustive (and much
#      slower) deflate implementation than plain zlib - it searches
#      many more ways to encode the same bytes before picking the best.
#   3. Strips PNG metadata chunks that don't affect the pixels at all
#      (authoring tool info, timestamps, etc).
# All three are 100% lossless - not one pixel changes - which is why
# the optimizer only uses it as an extra pass on top of its existing
# validation, never as a replacement for it.
# This is also why it's noticeably slower per file than Pillow alone,
# especially on a phone CPU (Termux) - Zopfli trades time for size on
# purpose. It's optional for exactly this reason.
def oxipng_present() -> bool:
    if shutil.which("oxipng"):
        return True
    cargo_candidate = os.path.expanduser("~/.cargo/bin/oxipng")
    if os.path.exists(cargo_candidate):
        return True
    if not is_termux():
        return False
    termux_prefix = os.environ.get("PREFIX", "/data/data/com.termux/files/usr")
    return os.path.exists(os.path.join(termux_prefix, "bin", "oxipng"))


def install_oxipng_windows(plat_dir: str) -> bool:
    bin_dir = os.path.join(plat_dir)
    exe = os.path.join(bin_dir, "oxipng.exe")
    if os.path.exists(exe):
        os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
        ok(f"oxipng already downloaded ({exe})")
        return True

    try:
        api_url = "https://api.github.com/repos/oxipng/oxipng/releases/latest"
        req = urllib.request.Request(api_url, headers={"User-Agent": "fnf-mod-optimizer-setup"})
        import json
        with urllib.request.urlopen(req, timeout=20) as resp:
            release = json.loads(resp.read().decode())
        asset = next(
            (a for a in release.get("assets", [])
             if "x86_64-pc-windows-msvc" in a["name"] and a["name"].lower().endswith(".zip")),
            None,
        )
        if asset is None:
            warn("No Windows oxipng release asset found automatically.")
            print("       Get it manually: https://github.com/oxipng/oxipng/releases")
            return False

        with tempfile.TemporaryDirectory() as tmp:
            zip_path = os.path.join(tmp, "oxipng.zip")
            if not download_with_progress(asset["browser_download_url"], zip_path, "Downloading oxipng"):
                return False
            info("Extracting oxipng...")
            with zipfile.ZipFile(zip_path) as z:
                entry = next((n for n in z.namelist() if n.lower().endswith("oxipng.exe")), None)
                if entry is None:
                    warn("oxipng archive didn't contain oxipng.exe as expected.")
                    return False
                with z.open(entry) as src, open(exe, "wb") as dst:
                    shutil.copyfileobj(src, dst)

        os.environ["PATH"] = bin_dir + os.pathsep + os.environ["PATH"]
        ok(f"oxipng installed to {exe}")
        return True
    except Exception as e:
        warn(f"Automatic oxipng download failed: {e}")
        print("       Get it manually: https://github.com/oxipng/oxipng/releases")
        return False


def install_oxipng_termux() -> bool:
    info("Trying: pkg install oxipng ...")
    try:
        result = subprocess.run(
            ["pkg", "install", "-y", "oxipng"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=300,
        )
        if result.returncode == 0 and oxipng_present():
            ok("oxipng installed via pkg")
            return True
    except Exception:
        pass

    warn("pkg install oxipng didn't work (not in this Termux repo mirror) - falling back to")
    print("       building it with cargo instead. This compiles from source on your phone's")
    print(f"       CPU and can take a {bold('few minutes')} - slower than everything else in this setup,")
    print("       but it's a one-time cost.")
    if not confirm("Build oxipng from source with cargo now?", default_yes=True):
        info("Skipped - you can build it later with: pkg install rust && cargo install oxipng")
        return False

    if shutil.which("cargo") is None:
        info("Installing rust (provides cargo) via pkg - this alone can take a minute or two...")
        try:
            result = subprocess.run(
                ["pkg", "install", "-y", "rust"],
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=900,
            )
            if result.returncode != 0:
                warn("Installing rust failed:")
                print(dim(result.stdout[-500:]))
                return False
        except Exception as e:
            warn(f"Installing rust failed: {e}")
            return False

    info("Building oxipng with cargo (this is the slow part - please wait)...")
    try:
        result = subprocess.run(
            ["cargo", "install", "oxipng"],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=1800,
        )
        if result.returncode == 0 and oxipng_present():
            ok("oxipng built and installed via cargo (~/.cargo/bin/oxipng)")
            return True
        warn("cargo install oxipng failed:")
        print(dim(result.stdout[-800:]))
        return False
    except Exception as e:
        warn(f"Building oxipng failed: {e}")
        return False


def setup_oxipng(plat: str, plat_dir: str) -> bool:
    if oxipng_present():
        ok("oxipng already available")
        return True

    print()
    info("oxipng is optional: it gives PNGs an extra, fully-lossless size")
    info("reduction pass on top of what Pillow alone can do. Without it the")
    info("tool still works exactly the same, just with a bit less PNG savings")
    info("on files that were already well-compressed before you got them.")

    if not confirm("Install oxipng now?", default_yes=True):
        info("Skipped. Install later any time - see README.md for instructions.")
        return False

    if plat == "windows":
        result = install_oxipng_windows(plat_dir)
    elif plat == "termux":
        result = install_oxipng_termux()
    else:
        info("Automatic install isn't scripted for this OS yet.")
        print("       Linux: check your package manager, or 'cargo install oxipng'")
        print("       macOS: 'brew install oxipng', or 'cargo install oxipng'")
        result = False

    if not result and not oxipng_present():
        warn("oxipng not installed (optional - the tool works fine without it).")
    return oxipng_present()


# ------------------------------------------------------------------
# Main
# ------------------------------------------------------------------
def main():
    _enable_windows_ansi()
    banner()

    plat_dir = os.path.dirname(os.path.abspath(__file__))
    plat = detect_platform()
    plat_labels = {
        "windows": "Windows", "termux": "Termux (Android)",
        "linux": "Linux", "macos": "macOS",
    }

    total_steps = 6
    step_header(1, total_steps, "Detecting your device")
    cores, ram = detect_hardware_summary()
    gpu = detect_gpu_vendor() if plat != "termux" else "n/a (mobile)"
    ok(f"Platform: {bold(plat_labels[plat])}")
    ok(f"Python:   {platform.python_version()} ({sys.executable})")
    ok(f"CPU:      {cores} logical cores")
    ok(f"RAM:      {ram}")
    ok(f"GPU:      {gpu}")
    if sys.version_info < (3, 9):
        fail(f"Python {platform.python_version()} is too old - this tool needs Python 3.9+.")
        sys.exit(1)

    step_header(2, total_steps, "Python dependencies (Pillow)")
    pillow_ok = install_pillow()

    step_header(3, total_steps, "FFmpeg (video/audio encoding)")
    ffmpeg_ok = setup_ffmpeg(plat, plat_dir)

    step_header(4, total_steps, "astcenc (optional, .astc textures)")
    astc_ok = setup_astcenc(plat, plat_dir)

    step_header(5, total_steps, "oxipng (optional, extra PNG compression)")
    oxipng_ok = setup_oxipng(plat, plat_dir)

    step_header(6, total_steps, "GPU encoder check")
    if ffmpeg_ok:
        try:
            sys.path.insert(0, plat_dir)
            import importlib
            import tools as _cfg
            importlib.reload(_cfg)  # re-probe now that FFmpeg may have just been installed
            if _cfg.GPU_ENCODERS:
                names = ", ".join(_cfg.GPU_ENCODERS.keys())
                ok(f"Hardware video encoder(s) detected: {names}")
            else:
                info("No GPU video encoder detected - videos will encode on CPU (still works fine).")
        except Exception as e:
            warn(f"Could not run the GPU probe yet: {e}")
    else:
        info("Skipped (FFmpeg isn't installed yet).")

    # ---- Summary ----
    print()
    print(cyan("=" * 52))
    print(bold("  SUMMARY"))
    print(cyan("=" * 52))
    print(f"  Pillow (images) ............. {green('ready') if pillow_ok else red('missing')}")
    print(f"  FFmpeg (video/audio) ........ {green('ready') if ffmpeg_ok else red('missing')}")
    print(f"  astcenc (.astc, optional) ... {green('ready') if astc_ok else yellow('not installed')}")
    print(f"  oxipng (PNG, optional) ...... {green('ready') if oxipng_ok else yellow('not installed')}")
    print()

    if pillow_ok and ffmpeg_ok:
        print(green(bold("  You're all set!")))
        print(f"  Run the optimizer with:  {bold('python main.py')}")
    else:
        print(yellow(bold("  Almost there - see the manual step(s) marked above.")))
        print(f"  Once resolved, run:  {bold('python main.py')}")
    print()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\nSetup cancelled.")
        sys.exit(1)
