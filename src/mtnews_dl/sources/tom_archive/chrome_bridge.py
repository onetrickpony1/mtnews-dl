"""Loopback bridge to a user-installed extension in their normal Chrome profile."""
import json
import queue
import secrets
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
PORT = 18769


def prepare_extension():
    path = HERE / 'chrome-extension' / 'connection.js'
    if not path.exists():
        path.write_text('const BRIDGE_TOKEN = ' + json.dumps(secrets.token_urlsafe(32)) + ';\n')
        path.chmod(0o600)
    return json.loads(path.read_text().split('=', 1)[1].strip().rstrip(';'))


class ChromeBridge:
    def __init__(self, stop, token=None, port=PORT, diagnostic_root=None):
        self.stop = stop
        self.diagnostics = None
        if diagnostic_root is not None:
            try:
                from diagnostics import Diagnostics
                self.diagnostics = Diagnostics(Path(diagnostic_root))
            except Exception:
                pass  # Logging must not prevent the browser connection.
        self.token = token or prepare_extension()
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
                if self.path not in ('/result', '/diagnostics'):
                    return self.reply(404, {})
                length = int(self.headers.get('Content-Length', 0))
                if not 0 < length <= 2_000_000:
                    return self.reply(413, {})
                try:
                    result = json.loads(self.rfile.read(length))
                except ValueError:
                    return self.reply(400, {})
                if self.path == '/diagnostics':
                    if bridge.diagnostics and isinstance(result, list):
                        bridge.diagnostics.submit(result)
                    return self.reply(200, {})
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
            if self.diagnostics and isinstance(result.get('diagnostics'), list):
                self.diagnostics.submit(result['diagnostics'])
            if result.get('error'):
                raise RuntimeError(result['error'])
            return result['value']
        raise RuntimeError('Chrome connection timed out. Keep the companion extension tab open. No automatic retry.')

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        if self.diagnostics:
            self.diagnostics.close()


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
