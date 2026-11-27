"""
server.py
---------
Tiny local web server for the UI. Standard library only.

Security
  * Listens on 127.0.0.1 only. Every /api call needs the per-run random
    token (header X-Token); requests whose Host header isn't local are
    rejected - so a random web page open in the same browser can't drive
    the optimizer, install programs or read your files.
  * Static files come from a fixed whitelist (no path traversal).
  * A strict Content-Security-Policy: only this server's own scripts and
    styles run, nothing is loaded from elsewhere.
  * Every setting the browser sends is validated again here.
"""

import json
import os
import secrets
import shutil
import string
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

import config
import deps
import engine
from scanner import build_exclude_rules
import tools
from config import APP_NAME, APP_VERSION, IS_WINDOWS, DEFAULT_BACKUP
from eventlog import log
from settings_store import SettingsStore

WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
_MAX_BODY = 64 * 1024

_STATIC = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/i18n.js": ("i18n.js", "text/javascript; charset=utf-8"),
    "/static/style.css": ("style.css", "text/css; charset=utf-8"),
}
_CSP = ("default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'")


# ----------------------------------------------------------------------
# File browser
# ----------------------------------------------------------------------
def _roots() -> list:
    roots = []
    home = os.path.expanduser("~")
    for key, p in (
        ("internal", "/storage/emulated/0"),
        ("internal", os.path.join(home, "storage", "shared")),
        ("downloads", "/storage/emulated/0/Download"),
        ("home", home),
        ("cwd", os.getcwd()),
    ):
        if os.path.isdir(p) and all(r["path"] != p for r in roots):
            roots.append({"key": key, "name": key, "path": p})
    if IS_WINDOWS:
        for letter in string.ascii_uppercase:
            drive = f"{letter}:\\"
            if os.path.isdir(drive):
                roots.append({"key": "drive", "name": f"{letter}:", "path": drive})
    elif os.path.isdir("/"):
        roots.append({"key": "root", "name": "/", "path": "/"})
    return roots


def browse(path: str) -> dict:
    path = os.path.abspath(os.path.expanduser(path or "~"))
    if os.path.isfile(path):
        path = os.path.dirname(path)
    if not os.path.isdir(path):
        raise engine.UserError("folder_missing", "Folder not found")
    entries = []
    try:
        names = sorted(os.listdir(path), key=str.lower)
    except OSError as exc:
        raise engine.UserError("folder_denied", f"Can't open this folder: {exc.strerror or exc}")
    for name in names:
        if name.startswith("."):
            continue
        full = os.path.join(path, name)
        try:
            if os.path.isdir(full):
                entries.append({"name": name, "path": full, "type": "dir",
                                "mod": os.path.exists(os.path.join(full, "_polymod_meta.json"))})
            elif name.lower().endswith(".zip"):
                entries.append({"name": name, "path": full, "type": "zip", "size": os.path.getsize(full)})
        except OSError:
            continue
    parent = os.path.dirname(path)
    return {"path": path, "parent": parent if parent != path else None,
            "entries": entries, "roots": _roots()}


# ----------------------------------------------------------------------
# App state shared by handler threads
# ----------------------------------------------------------------------
class App:
    def __init__(self, token: str, store: SettingsStore = None):
        self.token = token
        self.job = None
        self.lock = threading.Lock()
        self.store = store or SettingsStore()
        self.httpd = None
        self._client_errors = []          # timestamps, for rate limiting

    def app_info(self) -> dict:
        import platform
        return {"name": APP_NAME, "version": APP_VERSION, "platform": deps.platform_id(),
                "arch": deps.arch_id(), "python": platform.python_version(),
                "app_bin": config.APP_BIN, "data_dir": config.APP_DIR,
                "cpus": os.cpu_count() or 1, "default_workers": config.MAX_WORKERS,
                "is_default_backup": DEFAULT_BACKUP}

    def state(self) -> dict:
        return {
            "app": self.app_info(),
            "deps": deps.manager.status(force=False),
            "install": deps.manager.snapshot(),
            "gpu": engine.gpu_info(),
            "prefs": dict(self.store.prefs),
            "settings": self.store.describe(),
            "job": self.job.snapshot() if self.job else None,
        }

    def events(self, since: int) -> dict:
        return {"log": log.since(since),
                "job": self.job.snapshot() if self.job else None,
                "install": deps.manager.snapshot()}

    def allow_client_log(self) -> bool:
        now = time.time()
        self._client_errors = [t for t in self._client_errors if now - t < 60]
        if len(self._client_errors) >= 30:
            return False
        self._client_errors.append(now)
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = "FNFOptimizer"
    app: App = None  # set by make_server

    def log_message(self, *args):  # keep the console quiet
        pass

    # -- helpers --------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", _CSP)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, data, code: int = 200) -> None:
        self._send(code, json.dumps(data).encode("utf-8"), "application/json; charset=utf-8")

    def _error(self, message: str, code: int = 400, error_code: str = None, **args) -> None:
        self._json({"error": message, "code": error_code, "args": args}, code)

    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1") or host == self.server.allowed_host

    def _authed(self) -> bool:
        supplied = self.headers.get("X-Token") or ""
        return secrets.compare_digest(supplied, self.app.token)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > _MAX_BODY:
            raise engine.UserError("too_large", "Request too large")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise engine.UserError("bad_request", "Body must be a JSON object")
        return data

    def _fail(self, exc: Exception) -> None:
        if isinstance(exc, engine.UserError):
            return self._error(str(exc), 400, exc.code, **exc.args_)
        if isinstance(exc, ValueError):
            return self._error(str(exc), 400, "invalid")
        log.error(f"Server error: {type(exc).__name__}: {exc}", key=None)
        return self._error(f"{type(exc).__name__}: {exc}", 500, "internal")

    # -- routing --------------------------------------------------------
    def do_GET(self):
        if not self._host_ok():
            return self._error("Host not allowed", 403, "host")
        url = urlparse(self.path)
        if url.path in _STATIC:
            name, ctype = _STATIC[url.path]
            try:
                with open(os.path.join(WEB_DIR, name), "rb") as f:
                    return self._send(200, f.read(), ctype)
            except OSError:
                return self._error(f"{name} not found", 500, "internal")
        if not url.path.startswith("/api/"):
            return self._error("Not found", 404, "not_found")
        if not self._authed():
            return self._error("Wrong token. Open the full address shown in the terminal.", 401, "token")
        query = parse_qs(url.query)
        app = self.app
        try:
            if url.path == "/api/state":
                return self._json(app.state())
            if url.path == "/api/events":
                since = int((query.get("since") or ["0"])[0] or 0)
                return self._json(app.events(since))
            if url.path == "/api/browse":
                return self._json(browse((query.get("path") or [""])[0]))
            if url.path == "/api/job":
                job = app.job
                if job is None:
                    return self._json({"job": None})
                full = (query.get("results") or ["0"])[0] == "1"
                return self._json({"job": job.snapshot(with_results=full)})
            if url.path == "/api/deps":
                return self._json({"deps": deps.manager.status(force=False),
                                   "install": deps.manager.snapshot()})
        except Exception as exc:
            return self._fail(exc)
        return self._error("Not found", 404, "not_found")

    def do_POST(self):
        if not self._host_ok():
            return self._error("Host not allowed", 403, "host")
        if not self.path.startswith("/api/") or not self._authed():
            return self._error("Wrong token", 401, "token")
        app = self.app
        try:
            body = self._body()
            path = urlparse(self.path).path

            if path == "/api/scan":
                summary = engine.scan_path(str(body.get("path", "")), build_exclude_rules(app.store.effective()))
                app.store.set_last_path(summary["path"])
                log.info(f"Scanned {summary['name']}: {summary['total_files']} files",
                         key="log.scan.done", name=summary["name"], files=summary["total_files"])
                return self._json(summary)

            if path == "/api/start":
                with app.lock:
                    if app.job is not None and app.job.active:
                        return self._error("A run is already in progress", 409, "busy")
                    if deps.manager.running:
                        return self._error("Wait for the installation to finish first", 409, "install_busy")
                    target = os.path.abspath(os.path.expanduser(str(body.get("path", ""))))
                    summary = engine.scan_path(target, build_exclude_rules(app.store.effective()))   # validates folder/zip first
                    if any(m["blocking"] for m in summary["missing"]):
                        return self._error("A required component is missing", 409, "missing_required")
                    prefs = app.store.prefs
                    backup = bool(body.get("backup", prefs.get("backup", True)))
                    gpu = bool(body.get("gpu", prefs.get("gpu", True)))
                    app.store.update_prefs({"backup": backup, "gpu": gpu})
                    app.store.set_last_path(target)
                    workers = prefs.get("workers")
                    app.job = engine.Job(
                        target, backup=backup, gpu=gpu, settings=app.store.effective(),
                        workers=workers if isinstance(workers, int) else None,
                        parallel_mode=prefs.get("parallel_mode"))
                    app.job.start()
                return self._json({"ok": True})

            if path == "/api/cancel":
                if app.job is not None:
                    app.job.cancel()
                return self._json({"ok": True})

            if path == "/api/reset":
                if app.job is not None and app.job.active:
                    return self._error("A run is still in progress", 409, "busy")
                app.job = None
                return self._json({"ok": True})

            # ---- settings ------------------------------------------------
            if path == "/api/settings":
                values = app.store.update_settings(body.get("changes"))
                return self._json({"values": values, "settings": app.store.describe()})

            if path == "/api/settings/reset":
                if body.get("all"):
                    app.store.reset_settings()
                else:
                    group, keys = body.get("group"), body.get("keys")
                    if not group and not keys:
                        raise engine.UserError("invalid", "Nothing to reset")
                    app.store.reset_settings(keys=keys if isinstance(keys, list) else None,
                                             group=group if isinstance(group, str) else None)
                log.info("Settings reset to defaults", key="log.settings.reset")
                return self._json({"settings": app.store.describe()})

            if path == "/api/prefs":
                prefs = app.store.update_prefs(body.get("changes"))
                return self._json({"prefs": prefs})

            # ---- dependencies ----------------------------------------------------
            if path == "/api/deps/check":
                items = deps.manager.check()
                return self._json({"deps": items, "install": deps.manager.snapshot(),
                                   "gpu": engine.gpu_info()})

            if path == "/api/deps/install":
                if app.job is not None and app.job.active:
                    return self._error("Wait for the current run to finish first", 409, "busy")
                deps.manager.start_install(str(body.get("id", "")))
                return self._json({"ok": True, "install": deps.manager.snapshot()})

            if path == "/api/deps/cancel":
                deps.manager.cancel()
                return self._json({"ok": True})

            if path == "/api/progress/clear":
                if app.job is not None and app.job.active:
                    return self._error("A run is still in progress", 409, "busy")
                shutil.rmtree(os.path.join(config.APP_DIR, "progress"), ignore_errors=True)
                log.info("Processed-files memory cleared", key=None)
                return self._json({"ok": True})

            # ---- log ---------------------------------------------------------------
            if path == "/api/log/clear":
                log.clear()
                return self._json({"ok": True})

            if path == "/api/clientlog":
                if app.allow_client_log():
                    level = "warn" if body.get("level") == "warn" else "error"
                    msg = str(body.get("message", ""))[:600]
                    where = str(body.get("where", ""))[:200]
                    log.add(level, f"[UI] {msg}" + (f" ({where})" if where else ""))
                return self._json({"ok": True})

            if path == "/api/quit":
                self._json({"ok": True})
                threading.Thread(target=app.httpd.shutdown, daemon=True).start()
                return
        except Exception as exc:
            return self._fail(exc)
        return self._error("Not found", 404, "not_found")


def make_server(host: str = "127.0.0.1", port: int = 8765, store: SettingsStore = None):
    """Create the server on the first free port >= `port`. Returns
    (httpd, token, actual_port). Also runs the first dependency check in
    the background so its result is already in the log when the page opens."""
    token = secrets.token_urlsafe(24)
    app = App(token, store)
    handler = type("BoundHandler", (Handler,), {"app": app})
    last_error = None
    for candidate in range(port, port + 40):
        try:
            httpd = ThreadingHTTPServer((host, candidate), handler)
            break
        except OSError as exc:
            last_error = exc
    else:
        raise OSError(f"No free port near {port}: {last_error}")
    httpd.daemon_threads = True
    httpd.allowed_host = host.lower()
    app.httpd = httpd
    log.info(f"{APP_NAME} {APP_VERSION} started", key="log.app.start", version=APP_VERSION)
    threading.Thread(target=deps.manager.check, name="dep-check", daemon=True).start()
    return httpd, token, candidate
