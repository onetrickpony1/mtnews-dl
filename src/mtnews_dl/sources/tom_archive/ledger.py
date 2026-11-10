"""Local state and conservative rolling-24-hour accounting. No network access."""
import hashlib
import sqlite3
import time
from pathlib import Path

WINDOW = 24 * 60 * 60


class Ledger:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'progress.sqlite3')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS issues (
                date TEXT PRIMARY KEY, status TEXT NOT NULL, count INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS pages (
                id TEXT PRIMARY KEY, date TEXT NOT NULL, url TEXT NOT NULL,
                source TEXT NOT NULL, path TEXT, sha256 TEXT, bytes INTEGER);
            CREATE TABLE IF NOT EXISTS usage (
                id INTEGER PRIMARY KEY, ts REAL NOT NULL, pages INTEGER NOT NULL,
                page_id TEXT, status TEXT NOT NULL);
        ''')

    def close(self):
        self.db.close()

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
        return self.db.execute('SELECT COALESCE(SUM(pages),0) FROM usage WHERE ts>?',
                               (now - WINDOW,)).fetchone()[0]

    def reserve(self, page_id, ceiling, now=None):
        now = time.time() if now is None else now
        if not 1 <= ceiling <= 1000:
            raise ValueError('Local 24-hour ceiling must be between 1 and 1000.')
        # Include uncertain/failed attempts: a lost response may still consume quota.
        self.db.execute('BEGIN IMMEDIATE')
        try:
            if self.used(now) + 1 > ceiling:
                raise RuntimeError('Local rolling-24-hour budget exhausted.')
            cur = self.db.execute('INSERT INTO usage(ts,pages,page_id,status) VALUES(?,?,?,?)',
                                  (now, 1, page_id, 'attempted'))
            self.db.commit()
            return cur.lastrowid
        except Exception:
            self.db.rollback()
            raise

    def manual(self, pages, now=None):
        if pages < 1:
            raise ValueError('Enter a positive number of additional pages.')
        with self.db:
            self.db.execute('INSERT INTO usage(ts,pages,status) VALUES(?,?,?)',
                            (time.time() if now is None else now, pages, 'manual'))

    def next_release(self):
        row = self.db.execute('SELECT MIN(ts) FROM usage WHERE ts>?',
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
            self.db.execute('UPDATE usage SET status=? WHERE id=?', ('saved', usage_id))

    def recover(self, page_id, path, downloaded_at=None):
        # Reuse the existing reservation even if the app died before marking it saved.
        # A historical Chrome download is accounted at its real completion time.
        row = self.db.execute('SELECT id FROM usage WHERE page_id=? ORDER BY id DESC LIMIT 1',
                              (page_id,)).fetchone()
        usage_id = row[0] if row else None
        if usage_id is None:
            with self.db:
                cur = self.db.execute('INSERT INTO usage(ts,pages,page_id,status) VALUES(?,?,?,?)',
                    (time.time() if downloaded_at is None else downloaded_at, 1, page_id, 'saved'))
                usage_id = cur.lastrowid
        self.complete(page_id, path, usage_id)
        with self.db:
            self.db.execute("UPDATE usage SET status='saved' WHERE page_id=? AND status='attempted'", (page_id,))

    def uncertain(self, page_id):
        return self.db.execute("SELECT COUNT(*) FROM usage WHERE page_id=? AND status='attempted' AND ts>?",
                               (page_id, time.time() - WINDOW)).fetchone()[0] > 0

    def stats(self):
        return {'listed_issues': self.db.execute('SELECT COUNT(*) FROM issues').fetchone()[0],
                'saved_pages': self.db.execute('SELECT COUNT(*) FROM pages WHERE path IS NOT NULL').fetchone()[0],
                'bytes': self.db.execute('SELECT COALESCE(SUM(bytes),0) FROM pages').fetchone()[0],
                'used_24h': self.used()}
