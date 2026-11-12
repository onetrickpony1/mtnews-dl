"""Loopback bridge to the tom-dl extension installed in the user's normal, signed-in Chrome.

tom-dl never reads cookies or passwords: it asks the extension to open archive pages, read
their links and click the site's own download controls. The bridge listens on 127.0.0.1
only while a run is active, and every request must carry a random pairing token that
`tom-dl connect` writes into the extension folder.
"""
import json
import os
import queue
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from types import SimpleNamespace

from ...common.paths import data_dir, ensure_private_dir

PORT = 18769
EXTENSION_ID = "bahlcnkdemiaaeaphadjidhhdoohpdod"   # fixed by the "key" in extension/manifest.json
BRIDGE_URL = f"chrome-extension://{EXTENSION_ID}/bridge.html"
BRIDGE_VERSION = "4"   # must match bridge.js; an older connection tab is told to reload
EXTENSION_FILES = ("manifest.json", "background.js", "bridge.html", "bridge.js")


def extension_dir() -> Path:
    return data_dir("tom-dl") / "chrome-extension"


def own_extension_dir() -> Path:
    return data_dir("tom-dl") / "own-browser-extension"


def install_extension() -> Path:
    """Copy the extension to stable folders and create its pairing token (kept on reinstall).

    Returns the folder for your own Chrome; tom-dl's own browser uses a copy that also opens
    the connection tab at start-up (own_extension_dir)."""
    target = extension_dir()
    ensure_private_dir(target.parent)
    target.mkdir(parents=True, exist_ok=True)
    source = files(__package__) / "extension"
    for name in EXTENSION_FILES:
        (target / name).write_bytes((source / name).read_bytes())
    token_file = target / "connection.js"
    if not token_file.exists():
        token_file.write_text("const BRIDGE_TOKEN = " + json.dumps(secrets.token_urlsafe(32)) + ";\n", encoding="utf-8")
        if os.name != "nt":
            token_file.chmod(0o600)
    own = own_extension_dir()
    own.mkdir(parents=True, exist_ok=True)
    for name in EXTENSION_FILES + ("connection.js",):
        (own / name).write_bytes((target / name).read_bytes())
    (own / "autostart.js").write_text("self.TOM_DL_AUTOSTART = true;\n", encoding="utf-8")
    if os.name != "nt":
        (own / "connection.js").chmod(0o600)
    return target


def settings_path() -> Path:
    return data_dir("tom-dl") / "browser.json"


def browser_mode():
    """'own' (tom-dl's Chrome for Testing window), 'chrome' (your Chrome) or None (not set up)."""
    try:
        return json.loads(settings_path().read_text(encoding="utf-8")).get("mode")
    except (OSError, ValueError):
        return None


def set_browser_mode(mode):
    settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings_path().write_text(json.dumps(dict(mode=mode)) + "\n", encoding="utf-8")


def read_token():
    path = extension_dir() / "connection.js"
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8").split("=", 1)[1].strip().rstrip(";"))


class ChromeBridge:
    def __init__(self, stop, token=None, port=PORT):
        self.stop = stop
        self.token = token or read_token()
        if not self.token:
            raise RuntimeError("The Chrome extension is not set up yet. Run: tom-dl connect")
        self.commands, self.results = queue.Queue(), queue.Queue()
        self.connected = threading.Event()
        self.stale_told = False
        self.emit_stale = lambda: None
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, value):
                self.send_response(code)
                origin = self.headers.get('Origin', '')
                if origin.startswith('chrome-extension://'):
                    self.send_header('Access-Control-Allow-Origin', origin)
                    self.send_header('Access-Control-Allow-Headers', 'X-Bridge-Token, X-Bridge-Version, Content-Type')
                    self.send_header('Access-Control-Allow-Methods', 'GET, POST, OPTIONS')
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps(value).encode())

            def do_OPTIONS(self):
                self.reply(200, {})

            def authorized(self):
                return secrets.compare_digest(self.headers.get('X-Bridge-Token', ''), bridge.token)

            def do_GET(self):
                if not self.authorized():
                    return self.reply(403, {'error': 'Unauthorized'})
                if self.path != '/command':
                    return self.reply(404, {})
                if self.headers.get('X-Bridge-Version') != BRIDGE_VERSION:
                    # Connection tab still runs code from before an update; its files are
                    # already refreshed, so a reload picks up the new version.
                    if not bridge.stale_told:
                        bridge.stale_told = True
                        bridge.emit_stale()
                    return self.reply(200, {'id': 'reload', 'action': 'reload'})
                bridge.connected.set()
                try:
                    command = bridge.commands.get_nowait()
                except queue.Empty:
                    command = None
                self.reply(200, command)

            def do_POST(self):
                if not self.authorized():
                    return self.reply(403, {})
                if self.path != '/result':
                    return self.reply(404, {})
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 2_000_000:
                    return self.reply(413, {})
                try:
                    result = json.loads(self.rfile.read(length))
                except ValueError:
                    return self.reply(400, {})
                bridge.results.put(result)
                self.reply(200, {})

        self.server = ThreadingHTTPServer(('127.0.0.1', port), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def call(self, action, **kwargs):
        if self.stop.is_set():
            raise RuntimeError('Stopped; Chrome stays open.')
        identifier = secrets.token_hex(16)
        self.commands.put(dict(id=identifier, action=action, **kwargs))
        deadline = time.monotonic() + 75
        while time.monotonic() < deadline:
            if self.stop.is_set():
                raise RuntimeError('Stopped; any download already started in Chrome may finish. Its attempt remains counted.')
            try:
                result = self.results.get(timeout=.2)
            except queue.Empty:
                continue
            if result.get('id') != identifier:
                continue
            if result.get('error'):
                raise RuntimeError(result['error'])
            return result['value']
        raise RuntimeError('Chrome connection timed out. Keep the tom-dl connection tab open in Chrome. No automatic retry.')

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class ChromePage:
    def __init__(self, bridge):
        self.bridge = bridge
        self.state = {}
        self.pending = None

    @property
    def url(self):
        return self.state.get('url', '')

    def snapshot(self):
        self.state = self.bridge.call('snapshot')
        return self.state

    def goto(self, url, **kwargs):
        self.state = self.bridge.call('navigate', url=url)
        return None  # DOM challenge checks handle access failures; no invented HTTP status.

    def locator(self, selector):
        return ChromeLocator(self, selector)

    def get_by_role(self, role, name, exact=True):
        return ChromeLocator(self, 'heading', name)

    def is_closed(self):
        return False

    def title(self):
        return self.snapshot()['title']

    def wait_for_timeout(self, ms):
        self.bridge.stop.wait(ms / 1000)

    def completed_files(self, urls):
        return self.bridge.call('completed', urls=urls)

    def expect_download(self, **kwargs):
        page = self
        class Pending:
            def __enter__(self):
                page.pending = self
                return self
            def __exit__(self, *args):
                page.pending = None
        return Pending()


class ChromeLocator:
    def __init__(self, page, selector, name=None):
        self.page, self.selector, self.name = page, selector, name

    def inner_text(self, **kwargs):
        return self.page.snapshot()['body']

    def evaluate_all(self, expression):
        return self.page.snapshot()['links']

    def wait_for(self, **kwargs):
        if self.name not in self.page.snapshot()['headings']:
            raise RuntimeError('Digital Archive is not visible in Chrome. Check sign-in, site verification, or an access error in the archive tab.')

    def count(self):
        url = self.selector[len('a[href="'):-2]
        return sum(link['href'] == url for link in self.page.snapshot()['links'])

    def click(self, **kwargs):
        url = self.selector[len('a[href="'):-2]
        value = self.page.bridge.call('download', url=url)
        path = Path(value['filename'])
        if not path.is_absolute() or not path.is_file():
            raise RuntimeError('Chrome reported a download but its PDF file is unavailable locally.')
        self.page.pending.value = SimpleNamespace(failure=lambda: None, save_as=lambda dest: shutil.copyfile(path, dest))


class NotConnected(RuntimeError):
    pass


CONNECT_HELP = """tom-dl's Chrome connection is not open. To connect:
  * First time: run `tom-dl connect` and follow its steps.
  * Using your own Chrome: open it, click the tom-dl icon in the toolbar (puzzle-piece menu) so
    the "tom-dl connection" tab opens, keep it open, then run the command again.
  * Using tom-dl's own browser: it starts by itself; if it did not, run `tom-dl connect`."""


from contextlib import contextmanager  # noqa: E402


@contextmanager
def chrome_page(stop, emit, timeout=60):
    """Start the loopback bridge and wait for the extension's connection tab."""
    if read_token():
        install_extension()   # refresh the extension's files after an update (token is kept)
    try:
        bridge = ChromeBridge(stop)
    except OSError as exc:
        raise NotConnected(f"Port {PORT} is in use (another tom-dl run?): {exc}") from None
    bridge.emit_stale = lambda: emit(
        "The tom-dl connection tab is from an older version; asking it to reload. If it does not "
        "connect, reload that tab (F5) or close tom-dl's browser window and run the command again.")
    try:
        emit("Waiting for the tom-dl connection tab in Chrome…")
        deadline = time.monotonic() + timeout
        launch_at = time.monotonic() + 3 if browser_mode() == "own" else None
        while not bridge.connected.wait(.2):
            if stop.is_set():
                raise InterruptedError
            if launch_at and time.monotonic() >= launch_at:
                launch_at = None
                from ...common.browser import launch_own_browser
                if launch_own_browser(own_extension_dir(), [BRIDGE_URL]):
                    emit("Started tom-dl's browser window.")
                    deadline = time.monotonic() + timeout
            if time.monotonic() >= deadline:
                raise NotConnected(CONNECT_HELP)
        emit("Chrome connected.")
        yield ChromePage(bridge)
    finally:
        bridge.close()
