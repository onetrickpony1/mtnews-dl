"""Local state and conservative rolling-24-hour accounting. No network access.

Issue and page records live with each library (``<library>/.tom-dl/archive/progress.sqlite3``).
The page allowance belongs to the timesofmalta.com account, not to a folder, so usage is
counted once per user (``<tom-dl data dir>/archive-usage.sqlite3``) and shared by every
library, e.g. the per-year folders that ``mtnews-dl download --root`` uses.
"""
import hashlib
import os
import socket
import sqlite3
import time
from pathlib import Path

from ...common.paths import data_dir

WINDOW = 24 * 60 * 60
LOCK_WAIT = 15
USAGE_DB = 'archive-usage.sqlite3'
USAGE_TABLE = (
    'CREATE TABLE IF NOT EXISTS usage ('
    ' id INTEGER PRIMARY KEY, ts REAL NOT NULL, pages INTEGER NOT NULL,'
    ' page_id TEXT, status TEXT NOT NULL);'
)


def usage_path() -> Path:
    return data_dir('tom-dl') / USAGE_DB


class LibraryBusy(RuntimeError):
    pass


def _alive(pid):
    if os.name == 'nt':   # os.kill(pid, 0) would terminate the process on Windows
        import ctypes
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)   # PROCESS_QUERY_LIMITED_INFORMATION
        if not handle:
            return kernel32.GetLastError() == 5   # access denied: it exists
        code = ctypes.c_ulong()
        try:
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == 259
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except (PermissionError, OSError):
        return True
    return True


def _take_lock(lock: Path):
    """Exclusive-create lock file: works on SMB/NFS shares where SQLite's byte-range locks don't."""
    me = f'{socket.gethostname()} {os.getpid()}'
    for _ in range(2):
        try:
            fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o664)
        except FileExistsError:
            try:
                host, pid = lock.read_text(encoding='utf-8').split()
                stale = host == socket.gethostname() and not _alive(int(pid))
            except (OSError, ValueError):
                stale = False
            if stale:
                lock.unlink(missing_ok=True)
                continue
            raise LibraryBusy(f'Another tom-dl run is using this library ({lock}). If none is running, '
                              'delete that file and try again.')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(me)
        return lock
    raise LibraryBusy(f'Could not lock {lock}.')


def _folder_locks(folder: Path) -> bool:
    """Can SQLite commit in this folder? Tried on a new file nobody else uses, so a failure
    means the file system (e.g. a CIFS share) refuses SQLite's locks, not that it is busy."""
    probe = folder / f'.lock-probe-{socket.gethostname()}-{os.getpid()}.sqlite3'
    db = sqlite3.connect(probe, timeout=0)
    try:
        db.execute('CREATE TABLE t (x)')
        db.commit()
        return True
    except sqlite3.OperationalError as exc:
        if 'locked' in str(exc) or 'I/O' in str(exc):
            return False
        raise
    finally:
        db.close()
        for leftover in (probe, probe.with_name(probe.name + '-journal')):
            try:
                leftover.unlink()
            except FileNotFoundError:
                pass


def connect_library_db(path: Path):
    """Open a library database. Network shares (e.g. CIFS mounts) often refuse SQLite's file
    locks ("database is locked"); there tom-dl opens it without them and keeps other tom-dl runs
    out with a lock file instead. Returns (connection, lock file or None)."""
    if _folder_locks(path.parent):
        return sqlite3.connect(path, timeout=LOCK_WAIT), None
    lock = _take_lock(path.with_name(path.name + '.lock'))
    try:
        return sqlite3.connect(path.resolve().as_uri() + '?nolock=1', uri=True), lock
    except Exception:
        lock.unlink(missing_ok=True)
        raise


class Ledger:
    def __init__(self, root, usage_db=None):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db, self.lock = connect_library_db(self.root / 'progress.sqlite3')
        self.usage = None
        try:
            self._open(usage_db)
        except BaseException:
            self.close()
            raise

    def _open(self, usage_db):
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS issues (
                date TEXT PRIMARY KEY, status TEXT NOT NULL, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS pages (
                id TEXT PRIMARY KEY, date TEXT NOT NULL, url TEXT NOT NULL,
                source TEXT NOT NULL, path TEXT, sha256 TEXT, bytes INTEGER);
        ''' + USAGE_TABLE)
        usage_db = Path(usage_db) if usage_db else usage_path()
        usage_db.parent.mkdir(parents=True, exist_ok=True)
        # Autocommit; reserve() takes an explicit write lock so concurrent runs share one count.
        self.usage = sqlite3.connect(usage_db, timeout=30, isolation_level=None)
        self.usage.executescript(USAGE_TABLE)
        self._adopt_library_usage()

    def _adopt_library_usage(self):
        """Move usage recorded inside this library (older tom-dl, or a library brought in with
        `tom-dl archive import`) into the per-user count, once."""
        rows = self.db.execute('SELECT ts,pages,page_id,status FROM usage ORDER BY id').fetchall()
        if not rows:
            return
        self.usage.execute('BEGIN IMMEDIATE')
        try:
            self.usage.executemany('INSERT INTO usage(ts,pages,page_id,status) VALUES(?,?,?,?)', rows)
            self.usage.execute('COMMIT')
        except Exception:
            self.usage.execute('ROLLBACK')
            raise
        with self.db:
            self.db.execute('DELETE FROM usage')

    def close(self):
        self.db.close()
        if self.usage:
            self.usage.close()
        if self.lock:
            self.lock.unlink(missing_ok=True)
            self.lock = None

    def inventory(self, date, links):
        with self.db:
            self.db.execute('INSERT OR REPLACE INTO issues VALUES (?,?,?)',
                            (date, 'listed' if links else 'unavailable', len(links)))
            for page_id, url, source in links:
                self.db.execute('INSERT INTO pages(id,date,url,source) VALUES(?,?,?,?) '
                                'ON CONFLICT(id) DO UPDATE SET url=excluded.url, source=excluded.source',
                                (page_id, date, url, source))

    def used(self, now=None):
        now = time.time() if now is None else now
        return self.usage.execute('SELECT COALESCE(SUM(pages),0) FROM usage WHERE ts>?',
                                  (now - WINDOW,)).fetchone()[0]

    def reserve(self, page_id, ceiling, now=None):
        now = time.time() if now is None else now
        if not 1 <= ceiling <= 1000:
            raise ValueError('Local 24-hour ceiling must be between 1 and 1000.')
        # Include uncertain/failed attempts: a lost response may still consume quota.
        self.usage.execute('BEGIN IMMEDIATE')
        try:
            if self.used(now) + 1 > ceiling:
                raise RuntimeError('Local rolling-24-hour budget exhausted.')
            cur = self.usage.execute('INSERT INTO usage(ts,pages,page_id,status) VALUES(?,?,?,?)',
                                     (now, 1, page_id, 'attempted'))
            self.usage.execute('COMMIT')
            return cur.lastrowid
        except Exception:
            self.usage.execute('ROLLBACK')
            raise

    def manual(self, pages, now=None):
        if pages < 1:
            raise ValueError('Enter a positive number of additional pages.')
        self.usage.execute('INSERT INTO usage(ts,pages,status) VALUES(?,?,?)',
                           (time.time() if now is None else now, pages, 'manual'))

    def next_release(self):
        row = self.usage.execute('SELECT MIN(ts) FROM usage WHERE ts>?',
                                 (time.time() - WINDOW,)).fetchone()[0]
        return row + WINDOW + 1 if row else time.time()

    def valid(self, page_id):
        row = self.db.execute('SELECT path,sha256 FROM pages WHERE id=?', (page_id,)).fetchone()
        if not row or not row[0]:
            return False
        path = self.root / row[0]
        return path.is_file() and hashlib.sha256(path.read_bytes()).hexdigest() == row[1]

    def complete(self, page_id, path, usage_id):
        path = Path(path)
        with self.db:
            self.db.execute('UPDATE pages SET path=?,sha256=?,bytes=? WHERE id=?',
                            (str(path.relative_to(self.root)), hashlib.sha256(path.read_bytes()).hexdigest(),
                             path.stat().st_size, page_id))
        self.usage.execute('UPDATE usage SET status=? WHERE id=?', ('saved', usage_id))

    def refused(self, usage_id):
        """The site refused this attempt outright (e.g. its daily limit notice): no PDF can be
        in flight, so it is not uncertain. It stays in the local count to stay on the safe side."""
        self.usage.execute('UPDATE usage SET status=? WHERE id=?', ('refused', usage_id))

    def recover(self, page_id, path, downloaded_at=None):
        # Reuse the existing reservation even if the app died before marking it saved.
        # A historical Chrome download is accounted at its real completion time.
        row = self.usage.execute('SELECT id FROM usage WHERE page_id=? ORDER BY id DESC LIMIT 1',
                                 (page_id,)).fetchone()
        usage_id = row[0] if row else None
        if usage_id is None:
            cur = self.usage.execute('INSERT INTO usage(ts,pages,page_id,status) VALUES(?,?,?,?)',
                (time.time() if downloaded_at is None else downloaded_at, 1, page_id, 'saved'))
            usage_id = cur.lastrowid
        self.complete(page_id, path, usage_id)
        self.usage.execute("UPDATE usage SET status='saved' WHERE page_id=? AND status='attempted'",
                           (page_id,))

    def uncertain(self, page_id):
        return self.usage.execute("SELECT COUNT(*) FROM usage WHERE page_id=? AND status='attempted' AND ts>?",
                                  (page_id, time.time() - WINDOW)).fetchone()[0] > 0

    def stats(self):
        return {'listed_issues': self.db.execute('SELECT COUNT(*) FROM issues').fetchone()[0],
                'saved_pages': self.db.execute('SELECT COUNT(*) FROM pages WHERE path IS NOT NULL').fetchone()[0],
                'bytes': self.db.execute('SELECT COALESCE(SUM(bytes),0) FROM pages').fetchone()[0],
                'used_24h': self.used()}
