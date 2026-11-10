"""tom-dl library: one Times of Malta issue per date, from the e-paper feed or the archive.

For every selected date, in order:
  1. an existing verified file (state + SHA-256), or an existing file with the final name
     (checked, not downloaded again);
  2. the e-paper PDF the publisher listed for that date (live feed or link record);
  3. with --archive: the digital archive through the user's Chrome (pages joined locally).
Every new PDF has its printed front-page date checked; anything doubtful stays in
.tom-dl/pending for review.
"""

import datetime as dt
import json
import os
import tempfile
import time
from contextlib import ExitStack
from pathlib import Path

from ...common.lock import library_lock
from ...common.pdfcheck import check_structure, inspect_safe, sha
from . import feed

DONE = ("verified", "accepted_by_user")
REVIEW = ("uncertain", "wrong_date")
# not worth asking again within a run; --retry-missing asks again
KNOWN_GAPS = ("unavailable", "archive_not_listed", "archive_redirected", "archive_incomplete", "missing")
EPAPER_START = "2024-11-19"   # first issue in the publisher's e-paper feed


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


class StopRun(Exception):
    """Ends the whole run (site refused, quota, sign-in, Chrome): exit code 5 or 6."""

    def __init__(self, message, code):
        super().__init__(message)
        self.code = code


class Library:
    def __init__(self, root, *, flat=True):
        self.root = Path(root).expanduser().resolve()
        self.flat = flat
        legacy = self.root / "epaper"   # tom-dl 0.1.0 / tom-epaper-downloader library layout
        if not flat and (legacy / "state.json").exists() and not (self.root / ".tom-dl").exists():
            self.state_dir = legacy
        else:
            self.state_dir = self.root / ".tom-dl"
        self.state_file = self.state_dir / "state.json"
        self.rows = {}
        self.selected = []

    # ---- state ------------------------------------------------------------------------------
    def load(self):
        self.rows = json.loads(self.state_file.read_text(encoding="utf-8")) if self.state_file.exists() else {}
        return self

    def save(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=self.state_dir, prefix=".state-")
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(self.rows, out, indent=2, ensure_ascii=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, self.state_file)

    def lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return library_lock(self.state_dir / "run.lock", "tom-dl")

    def final(self, date) -> Path:
        name = f"Times-of-Malta-{date}.pdf"
        return self.root / name if self.flat else self.root / "editions" / date[:4] / name

    def archive_root(self) -> Path:
        return self.state_dir / "archive"

    def record(self, date, path, check, **extra):
        self.rows[date] = dict(check, path=str(path), checked_at=_now(), **extra)
        self.save()
        if check.get("status") in REVIEW:
            review = {k: self.rows[date].get(k) for k in ("status", "detected_dates", "method", "pages",
                                                          "sha256", "source", "route")}
            review.update(date=date, how_to_accept=f"tom-dl accept {date} --yes  (after checking the printed date)")
            Path(path).with_suffix(".review.json").write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")

    def promote(self, date, path) -> Path:
        dest = self.final(date)
        dest.parent.mkdir(parents=True, exist_ok=True)
        path = Path(path)
        if path == dest:
            return dest
        if dest.exists():
            backup = self.state_dir / "replaced" / f"{date}-{time.time_ns()}.pdf"
            backup.parent.mkdir(parents=True, exist_ok=True)
            os.replace(dest, backup)
        os.replace(path, dest)
        path.with_suffix(".review.json").unlink(missing_ok=True)
        return dest

    def _gap(self, date, status, note, **extra):
        self.rows[date] = dict(status=status, path="", detected_dates=[], checked_at=_now(),
                               front_text=note, **extra)
        self.save()

    # ---- planning ---------------------------------------------------------------------------
    def plan(self, dates, links, *, archive=False):
        """[(date, route, detail)] where route is have | review | epaper | archive | none."""
        out = []
        for date in dates:
            row = self.rows.get(date, {})
            path = Path(row.get("path") or "/nonexistent")
            if row.get("status") in DONE and path.is_file():
                out.append((date, "have", str(path)))
            elif row.get("status") in REVIEW and path.is_file():
                out.append((date, "review", str(path)))
            elif self.final(date).exists():
                out.append((date, "have", f"{self.final(date)} (will be checked)"))
            elif links.links(date):
                out.append((date, "epaper", ", ".join(links.links(date))))
            elif archive:
                out.append((date, "archive", f"timesofmalta.com/archive/issue?from={date}"))
            else:
                out.append((date, "none", ""))
        return out

    # ---- running ----------------------------------------------------------------------------
    def run(self, dates, links, *, delay, stop, emit, retries=3, retry_missing=False,
            archive=None):
        """Download the selected dates. `archive` = dict(ceiling, wait, max_pages) to enable it.

        Returns a summary dict; raises StopRun to end early (progress is saved).
        """
        summary = dict(downloaded=0, skipped=0, review=0, unavailable=0, failed=0, archive_pages=0)
        with self.lock(), ExitStack() as stack:
            self.load()
            runner = page = None
            for date in dates:
                if stop.is_set():
                    break
                self.selected.append(date)
                outcome = self._existing(date, emit)
                if outcome:
                    summary["skipped" if outcome == "have" else "review"] += 1
                    continue
                row = self.rows.get(date, {})
                if row.get("status") in KNOWN_GAPS and not retry_missing and not (
                        archive and row.get("status") == "unavailable"):
                    checked = (row.get("checked_at") or "")[:10]
                    if checked and (dt.date.fromisoformat(checked) - dt.date.fromisoformat(date)).days >= 3:
                        emit(f"{date}: {row['status']} (checked {checked}); skipped. --retry-missing checks again.")
                        summary["unavailable"] += 1
                        continue
                urls = links.links(date)
                if len(urls) > 1:
                    self._gap(date, "ambiguous_feed", "The feed lists several PDFs for this date: "
                              + ", ".join(urls), source=urls)
                    emit(f"{date}: several PDFs listed; recorded for review (tom-dl review).")
                    summary["review"] += 1
                    continue
                if urls:
                    result = self._epaper(date, urls[0], stop, emit, retries)
                    summary[result] += 1
                    if result != "skipped" and stop.wait(delay):
                        break
                    continue
                if archive is None:
                    hint = (" Older issues come from the archive: add --archive (premium sign-in "
                            "in Chrome; see tom-dl connect)." if date < EPAPER_START else
                            " Not every day has an issue (e.g. some public holidays).")
                    self._gap(date, "unavailable", "No e-paper PDF listed for this date." + hint)
                    emit(f"{date}: no e-paper PDF listed.{hint}")
                    summary["unavailable"] += 1
                    continue
                if runner is None:
                    runner, page = self._open_archive(stack, archive, delay, stop, emit)
                result = self._archive(runner, page, date, archive, emit)
                summary[result] += 1
                summary["archive_pages"] = runner.pages_requested
            self.save()
        return summary

    def _existing(self, date, emit):
        """'have' / 'review' when the date needs no download, else None."""
        row = self.rows.get(date, {})
        path = Path(row.get("path") or "/nonexistent")
        if path.is_file() and row.get("sha256"):
            if sha(path) == row["sha256"]:
                if row.get("status") in DONE:
                    emit(f"{date}: skipped, already verified.")
                    return "have"
                if row.get("status") in REVIEW:
                    emit(f"{date}: awaiting your review ({row['status']}); not downloaded again.")
                    return "review"
            else:
                emit(f"{date}: saved file changed; checking it again.")
                return self._check_existing(date, path, row)
        dest = self.final(date)
        if dest.exists():
            emit(f"{date}: found {dest.name}; checking it instead of downloading.")
            return self._check_existing(date, dest, row)
        return None

    def _check_existing(self, date, path, row):
        check = inspect_safe(path, date)
        self.record(date, path, check, source=row.get("source"), route=row.get("route", "existing"))
        return "have" if check["status"] in DONE else "review"

    def _epaper(self, date, url, stop, emit, retries):
        pending = self.state_dir / "pending"
        pending.mkdir(parents=True, exist_ok=True)
        part, target = pending / f"{date}.part", pending / f"{date}.pdf"
        emit(f"{date}: downloading the e-paper issue…")
        try:
            for attempt in range(retries + 1):
                try:
                    feed.fetch(url, part, stop)
                    break
                except feed.Temporary as exc:
                    if attempt == retries:
                        raise
                    wait = min(300, 15 * 2 ** attempt)
                    emit(f"{date}: {exc}; retrying in {wait}s ({attempt + 1}/{retries}).")
                    if stop.wait(wait):
                        raise InterruptedError from None
            try:
                check_structure(part)
            except (ValueError, OSError) as exc:
                raise StopRun(f"{date}: the server returned something that is not a complete PDF "
                              f"({exc}). Stopped for review; rerun later.", 6) from None
            check = inspect_safe(part, date)
            os.replace(part, target)
        except feed.LinkMissing as exc:
            self._gap(date, "missing", f"Listed PDF no longer available ({exc}).", source=url)
            emit(f"{date}: listed PDF not available ({exc}).")
            return "unavailable"
        except feed.Temporary as exc:
            self._gap(date, "failed", f"Download failed: {exc}", source=url)
            raise StopRun(f"{date}: {exc}. Stopped after {retries} retries; rerun later to resume.", 6) from None
        except feed.FeedError as exc:
            raise StopRun(f"{date}: {exc}", 6) from None
        finally:
            part.unlink(missing_ok=True)
        final = self.promote(date, target) if check["status"] in DONE else target
        self.record(date, final, check, source=url, route="epaper")
        shown = final.name if check["status"] in DONE else f"{final} (pending review)"
        emit(f"{date}: {check['status']}, {check['pages']} pages -> {shown}")
        return "downloaded" if check["status"] in DONE else "review"

    # ---- archive ----------------------------------------------------------------------------
    def _open_archive(self, stack, archive, delay, stop, emit):
        from ..tom_archive.chrome_bridge import NotConnected, chrome_page
        from ..tom_archive.runner import Runner
        try:
            page = stack.enter_context(chrome_page(stop, emit))
        except NotConnected as exc:
            raise StopRun(str(exc), 5) from None
        except RuntimeError as exc:   # extension not set up
            raise StopRun(str(exc), 5) from None
        runner = Runner(dict(folder=str(self.archive_root()), interval=max(1.0, delay),
                             ceiling=archive["ceiling"], auto_resume=archive["wait"]), emit, stop)
        stack.callback(runner.close)
        emit(f"Archive allowance: {runner.ledger.used()} of {archive['ceiling']} pages used in the "
             "last 24 hours (local count).")
        return runner, page

    def _archive(self, runner, page, date, archive, emit):
        from ..tom_archive.runner import IssueDateRedirect, IssueUnavailable, PageLimit, Stopped
        try:
            merged = runner.issue(page, date, page_limit=archive.get("max_pages"))
        except IssueDateRedirect as exc:
            self._gap(date, "archive_redirected", str(exc), route="archive")
            emit(f"{date}: {exc}")
            return "unavailable"
        except IssueUnavailable as exc:
            self._gap(date, "archive_incomplete", str(exc), route="archive")
            emit(f"{date}: {exc}")
            return "unavailable"
        except PageLimit as exc:
            raise StopRun(str(exc), 6) from None
        except Stopped:
            raise InterruptedError from None
        except RuntimeError as exc:
            raise StopRun(f"{date}: {exc}", 6) from None
        if merged is None:
            self._gap(date, "archive_not_listed", "The archive lists no pages for this date "
                      "(non-publication day, archive gap or changed layout).", route="archive")
            return "unavailable"
        check = inspect_safe(merged, date)
        final = self.promote(date, merged) if check["status"] in DONE else self._pending_copy(date, merged)
        self.record(date, final, check, source=f"archive:{date}", route="archive")
        emit(f"{date}: {check['status']}, {check['pages']} pages (archive) -> "
             f"{final.name if check['status'] in DONE else str(final) + ' (pending review)'}")
        return "downloaded" if check["status"] in DONE else "review"

    def _pending_copy(self, date, merged):
        pending = self.state_dir / "pending"
        pending.mkdir(parents=True, exist_ok=True)
        target = pending / f"{date}.pdf"
        os.replace(merged, target)
        return target

    # ---- review -----------------------------------------------------------------------------
    def accept(self, date):
        with self.lock():
            self.load()
            row = self.rows[date]
            path = Path(row["path"])
            if sha(path) != row["sha256"]:
                raise ValueError("File changed since inspection; run tom-dl recheck first.")
            dest = self.promote(date, path)
            row.update(status="accepted_by_user", path=str(dest))
            self.save()
            return dest

    def recheck(self, emit=print):
        """Re-inspect pending PDFs; no network."""
        with self.lock():
            self.load()
            for date, row in sorted(self.rows.items()):
                if row.get("status") not in REVIEW:
                    continue
                path = Path(row.get("path") or "/nonexistent")
                if not path.is_file():
                    emit(f"{date}: pending file is missing; left for review.")
                    continue
                check = inspect_safe(path, date)
                if check["status"] in DONE:
                    path = self.promote(date, path)
                self.record(date, path, check, source=row.get("source"), route=row.get("route"))
                emit(f"{date}: rechecked, {check['status']}.")


def read_rows(root, *, flat=True) -> dict:
    return Library(root, flat=flat).load().rows
