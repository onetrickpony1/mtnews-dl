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
EXTENSION_FILES = ("manifest.json", "background.js", "bridge.html", "bridge.js")


def extension_dir() -> Path:
    return data_dir("tom-dl") / "chrome-extension"


def install_extension() -> Path:
    """Copy the extension to a stable folder and create its pairing token (kept on reinstall)."""
    target = extension_dir()
    ensure_private_dir(target.parent)
    target.mkdir(parents=True, exist_ok=True)
    source = files(__package__) / "extension"
    for name in EXTENSION_FILES:
        (target / name).write_bytes((source / name).read_bytes())
    token_file = target / "connection.js"
    if not token_file.exists():
        token_file.write_text("const BRIDGE_TOKEN = " + json.dumps(secrets.token_urlsafe(32)) + ";\n")
        if os.name != "nt":
            token_file.chmod(0o600)
    return target


def read_token():
    path = extension_dir() / "connection.js"
    if not path.exists():
        return None
    return json.loads(path.read_text().split("=", 1)[1].strip().rstrip(";"))


class ChromeBridge:
    def __init__(self, stop, token=None, port=PORT):
        self.stop = stop
        self.token = token or read_token()
        if not self.token:
            raise RuntimeError("The Chrome extension is not set up yet. Run: tom-dl connect")
        self.commands, self.results = queue.Queue(), queue.Queue()
        self.connected = threading.Event()
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def reply(self, code, value):
                self.send_response(code)
                origin = self.headers.get('Origin', '')
                if origin.startswith('chrome-extension://'):
                    self.send_header('Access-Control-Allow-Origin', origin)
                    self.send_header('Access-Control-Allow-Headers', 'X-Bridge-Token, Content-Type')
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


CONNECT_HELP = """The tom-dl extension in Chrome is not connected. To connect:
  1. Open Chrome (the profile where you are signed in to timesofmalta.com).
  2. If you have not yet installed the extension: run `tom-dl connect` and follow its steps.
  3. Click the tom-dl icon in Chrome's toolbar (puzzle-piece menu): a "tom-dl connection" tab
     opens. Keep it open, then run the command again."""


from contextlib import contextmanager  # noqa: E402


@contextmanager
def chrome_page(stop, emit, timeout=60):
    """Start the loopback bridge and wait for the extension's connection tab."""
    try:
        bridge = ChromeBridge(stop)
    except OSError as exc:
        raise NotConnected(f"Port {PORT} is in use (another tom-dl run?): {exc}") from None
    try:
        emit("Waiting for the tom-dl connection tab in Chrome…")
        deadline = time.monotonic() + timeout
        while not bridge.connected.wait(.2):
            if stop.is_set():
                raise InterruptedError
            if time.monotonic() >= deadline:
                raise NotConnected(CONNECT_HELP)
        emit("Chrome connected.")
        yield ChromePage(bridge)
    finally:
        bridge.close()
