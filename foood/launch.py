"""``foood up``: the API plus the three Delivery Plus apps as static web builds.

Each app is exported once with ``expo export --platform web`` (the way the apps'
own end-to-end runner serves them) against this API's address, then served by a
small static server with Expo Router's file conventions: ``/login`` is
``login.html``, ``/order/abc`` is ``order/[id].html``, anything else falls back
to the shell. The export is redone only when its inputs change.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
from dataclasses import dataclass
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "build" / "web"
STAMP = ".foood-export.json"
TYPES = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".mjs": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".json": "application/json",
    ".map": "application/json",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".svg": "image/svg+xml",
    ".ico": "image/x-icon",
    ".ttf": "font/ttf",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
DYNAMIC_FILE = re.compile(r"^\[.+\]\.html$")
DYNAMIC_DIR = re.compile(r"^\[.+\]$")


@dataclass(frozen=True)
class App:
    name: str
    port: int
    directory: Path
    extra_env: dict[str, str]


#: Fixed browser positions so no app asks for geolocation: the customer at home by
#: Union Square, the driver around the corner from the kitchens. Values are degrees.
CUSTOMER_HOME = "37.7849,-122.4094"
DRIVER_START = "37.7760,-122.4180"

APPS = [
    App(
        "customer", 8081, ROOT / "apps" / "customer", {"EXPO_PUBLIC_FIXED_LOCATION": CUSTOMER_HOME}
    ),
    App("driver", 8082, ROOT / "apps" / "driver", {"EXPO_PUBLIC_FIXED_LOCATION": DRIVER_START}),
    App("restaurant", 8083, ROOT / "apps" / "restaurant", {}),
]

CREDENTIALS = [
    ("customer", "demo.customer@example.com", "password123"),
    ("driver", "driver@example.com", "password123"),
    ("restaurant", "owner@example.com", "password123"),
    ("restaurant (QA Kitchen)", "qa.restaurant@delivery-plus.test", "Qa-Only-Passw0rd!"),
    ("admin", "admin@example.com", "password123"),
]


# --- static serving ---------------------------------------------------------------------------


class Site:
    """Resolves request paths the way Expo Router's static output expects."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()

    def resolve(self, url: str) -> Path | None:
        clean = unquote(urlsplit(url).path).strip("/")
        if not clean:
            return self.root / "index.html"
        direct = (self.root / clean).resolve()
        if self.root not in direct.parents and direct != self.root:
            return None  # path traversal
        if direct.is_file():
            return direct
        if direct.with_name(direct.name + ".html").is_file():
            return direct.with_name(direct.name + ".html")
        if (direct / "index.html").is_file():
            return direct / "index.html"
        return self._dynamic(clean.split("/")) or self.root / "index.html"

    def _dynamic(self, segments: list[str]) -> Path | None:
        here = self.root
        for index, segment in enumerate(segments):
            last = index == len(segments) - 1
            names = [p.name for p in here.iterdir()] if here.is_dir() else []
            exact = f"{segment}.html" if last else segment
            if exact in names:
                here = here / exact
                continue
            pattern = DYNAMIC_FILE if last else DYNAMIC_DIR
            match = next((n for n in names if pattern.match(n)), None)
            if match is None:
                return None
            here = here / match
        return here if here.is_file() else None

    @staticmethod
    def content_type(path: Path) -> str:
        return TYPES.get(path.suffix, "application/octet-stream")


class _Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args: object, site: Site, **kwargs: object) -> None:
        self.site = site
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]

    def do_GET(self) -> None:  # noqa: N802 (http.server naming)
        path = self.site.resolve(self.path)
        if path is None:
            self.send_error(400)
            return
        body = path.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", self.site.content_type(path))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # quiet
        return


def serve_static(root: Path, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", port), partial(_Handler, site=Site(root)))
    threading.Thread(target=server.serve_forever, name=f"static:{port}", daemon=True).start()
    return server


# --- exporting the apps ------------------------------------------------------------------------


def _fingerprint(api_url: str, extra_env: dict[str, str]) -> str:
    raw = json.dumps({"api": api_url, "env": extra_env}, sort_keys=True)
    return hashlib.sha256(raw.encode()).hexdigest()


def write_stamp(out_dir: Path, api_url: str, extra_env: dict[str, str]) -> None:
    (out_dir / STAMP).write_text(json.dumps({"fingerprint": _fingerprint(api_url, extra_env)}))


#: What every web build is made with, besides the API address and its fixed position:
#: English, and dollars (the customer and driver apps come from our forks, which add the
#: currency switch; upstream labels every amount EGP).
PUBLIC_ENV = {"EXPO_PUBLIC_DEFAULT_LOCALE": "en", "EXPO_PUBLIC_CURRENCY": "USD"}


def public_env(app: App, api_url: str) -> dict[str, str]:
    """The build-time settings of *app*: they go into the export and into its stamp."""
    return {"EXPO_PUBLIC_API_BASE_URL": api_url, **PUBLIC_ENV, **app.extra_env}


def export_is_current(out_dir: Path, api_url: str, extra_env: dict[str, str]) -> bool:
    stamp = out_dir / STAMP
    if not stamp.is_file() or not (out_dir / "index.html").is_file():
        return False
    try:
        recorded = json.loads(stamp.read_text()).get("fingerprint")
    except json.JSONDecodeError:
        return False
    return bool(recorded == _fingerprint(api_url, extra_env))


def ensure_node_modules(app: App) -> None:
    if (app.directory / "node_modules").is_dir():
        return
    print(f"[{app.name}] installing dependencies (first run)", flush=True)
    subprocess.run(["npm", "ci"], cwd=app.directory, check=True)


def export_app(app: App, api_url: str, force: bool = False) -> Path:
    out = BUILD / app.name
    public = public_env(app, api_url)
    if not force and export_is_current(out, api_url, public):
        return out
    ensure_node_modules(app)
    print(f"[{app.name}] exporting the web build against {api_url}", flush=True)
    shutil.rmtree(out, ignore_errors=True)
    env = {**os.environ, "CI": "1", **public}
    subprocess.run(
        ["npx", "expo", "export", "--platform", "web", "--output-dir", str(out), "--clear"],
        cwd=app.directory,
        env=env,
        check=True,
        stdout=sys.stderr,
    )
    write_stamp(out, api_url, public)
    return out


# --- the command -------------------------------------------------------------------------------


def banner(api_url: str, apps: list[App]) -> str:
    lines = ["", "foood is up", "", f"  API            {api_url}  (docs at {api_url}/docs)"]
    engine = os.environ.get("FOOOD_ENGINE", "hand-wired")
    lines.append(f"  engine         {engine}  (decisions at {api_url}/api/_engine)")
    lines.append(f"  support        {api_url}/support  (sign in as admin@example.com)")
    lines.append(f"  map            {api_url}/map  (the evening on one map)")
    if os.environ.get("FOOOD_SCENARIO"):
        lines.append(
            f"  scenario       {os.environ['FOOOD_SCENARIO']}  (status at {api_url}/api/_scenario)"
        )
    for app in apps:
        lines.append(f"  {app.name:<14} http://localhost:{app.port}")
    lines += ["", "  Sign in with"]
    for role, email, password in CREDENTIALS:
        lines.append(f"  {role:<24} {email:<36} {password}")
    lines += ["", "  Ctrl-C stops everything.", ""]
    return "\n".join(lines)


def up(host: str, port: int, with_apps: bool, rebuild: bool) -> None:
    import uvicorn

    from foood.app import create_app

    api_url = f"http://localhost:{port}"
    os.environ.setdefault("FOOOD_SIMULATED_PHONES", "true")
    servers = []
    if with_apps:
        for app in APPS:
            servers.append(serve_static(export_app(app, api_url, force=rebuild), app.port))
    print(banner(api_url, APPS if with_apps else []), flush=True)
    try:
        uvicorn.run(create_app(), host=host, port=port, log_level="warning")
    finally:
        for server in servers:
            server.shutdown()
