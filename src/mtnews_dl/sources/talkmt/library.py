"""Persistent state, resume, atomic downloads and verification (modelled on mttd-dl's Library)."""

import datetime as dt
import json
import os
import re
import tempfile
import time
from pathlib import Path

from .discovery import PAPERS, candidate_slugs, file_url
from .net import (AuthRequired, BadResponse, Forbidden, IssueMissing, NotEntitled, RateLimited,
                  Temporary, with_retries)
from ...common.lock import library_lock
from ...common.pdfcheck import check_structure, inspect_safe, sha
from .papercheck import publication_check

STATE = ".talkmt-dl"
DONE = ("verified", "accepted_by_user")
REVIEW = ("uncertain", "wrong_date", "wrong_publication")
UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def safe_title(title: str) -> str:
    words = UNSAFE.sub(" ", title).replace(".pdf", " ").split()
    return "-".join(words)[:80].strip("-.") or "Supplement"


class Library:
    def __init__(self, root, *, flat=True):
        self.root = Path(root).expanduser().resolve()
        self.flat = flat
        self.state_dir = self.root / STATE
        self.state_file = self.state_dir / "state.json"
        self.rows = {}
        self.selected = set()

    # ---- state ---------------------------------------------------------
    def _load(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.rows = json.loads(self.state_file.read_text()) if self.state_file.exists() else {}

    def save(self):
        fd, temp = tempfile.mkstemp(dir=self.state_dir, prefix=".state-")
        with os.fdopen(fd, "w") as out:
            json.dump(self.rows, out, indent=2, ensure_ascii=False)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, self.state_file)

    def lock(self):
        self.state_dir.mkdir(parents=True, exist_ok=True)
        return library_lock(self.state_dir / "run.lock", "talkmt-dl")

    # ---- naming --------------------------------------------------------
    def final(self, key) -> Path:
        row = self.rows[key]
        date = row.get("user_corrected_date") or row.get("issue_date") or "undated"
        pub = row["publication"]
        if pub in PAPERS:
            name = f"{PAPERS[pub]['label']}-{date}.pdf"
        else:
            name = f"Supplement-{date}-{safe_title(row.get('title') or row['slug'])}.pdf"
        folder = self.root if self.flat else self.root / "editions" / pub / date[:4]
        path = folder / name
        owners = {Path(r.get("path", "")) for k, r in self.rows.items() if k != key and r.get("path")}
        if path in owners:  # A second file for the same publication and date keeps its source name.
            path = folder / f"{path.stem}-{row['slug'][:-4]}.pdf"
        return path

    def promote(self, key, path) -> Path:
        dest = self.final(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if Path(path) == dest:
            return dest
        if dest.exists():
            backup = self.state_dir / "replaced" / f"{key.replace('@', '_')}-{time.time_ns()}.pdf"
            backup.parent.mkdir(exist_ok=True)
            os.replace(dest, backup)
        os.replace(path, dest)
        sidecar = Path(path).with_suffix(".review.json")
        sidecar.unlink(missing_ok=True)
        return dest

    def _write_review(self, key, path):
        row = self.rows[key]
        review = {k: row.get(k) for k in ("status", "publication", "title", "slug", "source",
                                          "issue_date", "date_source", "detected_dates", "method",
                                          "pages", "sha256", "checked_at", "listed")}
        review["key"] = key
        review["how_to_accept"] = f"talkmt-dl accept {key} --yes  (after checking the printed date)"
        Path(path).with_suffix(".review.json").write_text(json.dumps(review, indent=2, ensure_ascii=False) + "\n")

    def record(self, key, path, inspection):
        self.rows[key].update(inspection, path=str(path),
                              checked_at=dt.datetime.now(dt.timezone.utc).isoformat())
        self.rows[key].pop("error", None)
        self.save()
        if self.rows[key]["status"] in REVIEW:
            self._write_review(key, path)

    # ---- planning --------------------------------------------------------
    @staticmethod
    def plan(listing, patterns, dates, publications, *, listed_only=False, formats="all"):
        """Groups of candidates; within a group the first PDF found wins."""
        groups = []
        wanted = set(dates)
        for item in listing:
            if item["publication"] not in publications:
                continue
            if listed_only or item["date"] in wanted:
                groups.append(dict(publication=item["publication"], date=item["date"],
                                   candidates=[dict(item, listed=True)]))
        if listed_only:
            return groups
        listed_slugs = {item["slug"] for item in listing}
        for date in sorted(wanted):
            for pub in publications:
                for slug, template, origin in candidate_slugs(patterns, pub, date, formats):
                    if slug in listed_slugs:
                        continue
                    titles = (patterns.get(pub, {}).get(template) or {}).get("titles") or []
                    cand = dict(slug=slug, url=file_url(slug), template=template, date=date,
                                date_source="filename", publication=pub, listed=False, origin=origin,
                                title=titles[-1] if pub == "supplements" and titles else slug[:-4])
                    if pub in PAPERS:
                        group = next((g for g in groups if g["publication"] == pub and g["date"] == date),
                                     None)
                        if group:
                            group["candidates"].append(cand)
                            continue
                    groups.append(dict(publication=pub, date=date, candidates=[cand]))
        groups.sort(key=lambda g: (g["date"] or "9999", list(PAPERS).index(g["publication"])
                                   if g["publication"] in PAPERS else 9))
        return groups

    # ---- running ---------------------------------------------------------
    def _have_issue(self, pub, date):
        """A verified/accepted file for this paper and date already exists (any URL)."""
        for key, row in self.rows.items():
            if (row.get("publication") == pub
                    and (row.get("user_corrected_date") or row.get("issue_date")) == date
                    and row.get("status") in DONE and row.get("path") and Path(row["path"]).is_file()):
                return key
        return None

    def _printed_elsewhere(self, pub, date):
        """A pending file of this paper whose printed date is exactly `date` (filename was wrong)."""
        for key, row in self.rows.items():
            if (row.get("publication") == pub and row.get("status") == "wrong_date"
                    and row.get("detected_dates") == [date] and row.get("path")
                    and Path(row["path"]).is_file()):
                return key
        return None

    def _log(self, key, url, outcome, origin):
        """Append one attempt to attempts.jsonl (URL and outcome only, never credentials)."""
        line = json.dumps(dict(at=_now(), key=key, url=url, outcome=outcome, origin=origin),
                          ensure_ascii=False)
        with (self.state_dir / "attempts.jsonl").open("a", encoding="utf-8") as out:
            out.write(line + "\n")

    def run(self, session, groups, *, delay, stop, emit, retries=3, retry_missing=False,
            recheck_session=None, patterns=None):
        """Returns a summary dict. Raises AuthRequired/RateLimited/Forbidden/NotEntitled to stop.

        Within a group (one paper, one date) candidates are tried in order. Only a missing-file
        response moves on to the next filename; a wrong printed date also moves on (the right
        issue may sit under another format). A verified file, a refusal while logged in, or a
        temporary/server failure ends the group: those never cause more filename guessing.
        """
        summary = dict(downloaded=0, skipped=0, missing=0, review=0, failed=0, not_entitled=0,
                       requests=0)
        with self.lock():
            self._load()
            temp_failures = refused = 0
            for group in groups:
                pub, date = group["publication"], group["date"]
                if pub in PAPERS and date:
                    elsewhere = self._printed_elsewhere(pub, date)
                    if elsewhere:
                        self.selected.add(elsewhere)
                        emit(f"{date} {pub}: already downloaded under a mislabelled filename "
                             f"({elsewhere}, printed {date}); awaiting review, no new requests. "
                             f"Accept it with: talkmt-dl accept {elsewhere} --yes")
                        summary["review"] += 1
                        continue
                    have = self._have_issue(pub, date)
                    if have:
                        self.selected.add(have)
                        emit(f"{date} {pub}: already saved ({have}); skipped.")
                        summary["skipped"] += 1
                        continue
                tried, outcomes = [], []
                for cand in group["candidates"]:
                    if stop.is_set():
                        return summary
                    key = f"{cand['date'] or 'undated'}@{cand['slug']}"
                    self.selected.add(key)
                    try:
                        outcome = self._candidate(session, key, cand, stop, emit, retries, retry_missing)
                    except AuthRequired:
                        self._log(key, cand["url"], "login redirect", cand.get("origin"))
                        if recheck_session is None:
                            self.rows[key].update(status="auth_required", path="", checked_at=_now())
                            self.save()
                            raise
                        emit(f"{key}: refused; checking whether the session is still logged in…")
                        if stop.wait(delay):
                            return summary
                        try:
                            recheck_session()
                        except AuthRequired:  # the session has ended: stop the whole run
                            self.rows[key].update(status="auth_required", path="", checked_at=_now())
                            self.save()
                            raise
                        self.rows[key].update(status="not_entitled", path="", checked_at=_now(),
                                              error="Logged in, but the server refused this file.")
                        self.save()
                        emit(f"{key}: logged in, but this file was refused (not covered?).")
                        outcome = "not_entitled"
                    if self._used_network:
                        summary["requests"] += 1
                        self._log(key, cand["url"], outcome, cand.get("origin"))
                    tried.append(cand["slug"])
                    outcomes.append(outcome)
                    refused = refused + 1 if outcome == "not_entitled" else 0
                    temp_failures = temp_failures + 1 if outcome == "failed" else 0
                    if refused >= 3:
                        raise NotEntitled("Three files in a row were refused while logged in; your "
                                          "subscription may not cover them. Stopped.")
                    if temp_failures >= 3:
                        raise Temporary("Three consecutive failures; stopped. Rerun later to resume.")
                    if outcome == "pdf" and patterns is not None and not cand.get("listed"):
                        info = patterns.setdefault(pub, {}).setdefault(cand["template"], dict(
                            evidence=f"{cand.get('origin')} confirmed by a verified download",
                            listing_dates=[], unlisted_confirmed=[], titles=[]))
                        if cand["date"] not in info.setdefault("unlisted_confirmed", []):
                            info["unlisted_confirmed"] = sorted(info["unlisted_confirmed"] + [cand["date"]])
                    if self._used_network and stop.wait(delay):
                        return summary
                    if outcome not in ("missing", "known", "wrong_date"):
                        break
                final = next((o for o in outcomes if o not in ("missing", "known", "wrong_date")), None)
                if final in ("pdf",):
                    summary["downloaded"] += 1
                elif final in ("have", "known_refused"):
                    summary["skipped"] += 1
                elif final in ("review",) or "wrong_date" in outcomes:
                    summary["review"] += 1
                elif final in ("failed", "not_entitled"):
                    summary[final] += 1
                elif outcomes:
                    summary["missing"] += 1
                    if len(tried) > 1:
                        emit(f"{date} {pub}: not found under {len(tried)} filename variants "
                             f"({', '.join(tried)}).")
            self.save()
        return summary

    _used_network = False

    @staticmethod
    def _base(cand):
        return dict(publication=cand["publication"], slug=cand["slug"], source=cand["url"],
                    title=cand.get("title") or cand["slug"][:-4], issue_date=cand.get("date"),
                    date_source=cand.get("date_source"), listed=bool(cand.get("listed")),
                    template=cand.get("template"))

    def _candidate(self, session, key, cand, stop, emit, retries, retry_missing):
        """Returns pdf | review | have | known | missing | failed (raises on stop conditions)."""
        self._used_network = False
        row = self.rows.get(key, {})
        path = Path(row.get("path") or "/nonexistent")
        if path.is_file() and row.get("sha256"):
            if sha(path) == row["sha256"]:
                if row.get("status") in DONE:
                    emit(f"{key}: skipped, already verified.")
                    return "have"
                if row.get("status") in REVIEW:
                    emit(f"{key}: awaiting your review ({row['status']}); not downloaded again.")
                    # A wrong-date file says nothing about the requested issue: keep looking.
                    return "wrong_date" if row["status"] == "wrong_date" else "have"
            else:
                emit(f"{key}: saved file changed; checking it again.")
                self.rows[key].update(self._base(cand))
                self.record(key, path, inspect_safe(path, cand.get("date")))
                return "review" if self.rows[key]["status"] in REVIEW else "have"
        twin = self.rows.get(row.get("duplicate_of") or "", {})
        if row.get("status") == "duplicate" and twin.get("path") and Path(twin["path"]).is_file():
            emit(f"{key}: same PDF as {row['duplicate_of']}; skipped.")
            return "have"
        if row.get("status") in ("missing", "not_entitled") and not retry_missing and cand.get("date"):
            checked = (row.get("checked_at") or "")[:10]
            if checked and checked > cand["date"] and (dt.date.fromisoformat(checked)
                                                       - dt.date.fromisoformat(cand["date"])).days >= 3:
                emit(f"{key}: previously {row['status']} (checked {checked}); skipped. Use --retry-missing to check again.")
                return "known" if row["status"] == "missing" else "known_refused"
        self.rows[key] = dict(row, **self._base(cand))
        dest = self.final(key)
        if dest.exists() and not row.get("sha256"):
            emit(f"{key}: found existing {dest.name}; checking it instead of downloading.")
            self.record(key, dest, inspect_safe(dest, cand.get("date")))
            return "have" if self.rows[key]["status"] in DONE else "review"
        pending = self.state_dir / "pending"
        pending.mkdir(exist_ok=True)
        stem = key.replace("@", "_").removesuffix(".pdf")
        part = pending / f"{stem}.part"
        target = pending / f"{stem}.pdf"
        emit(f"{key}: {'listed' if cand.get('listed') else 'unlisted date'}; requesting…")
        self._used_network = True
        try:
            with_retries(lambda: session.get(cand["url"], dest=part, stop=stop, expect_pdf=True),
                         retries=retries, stop=stop, emit=emit)
            try:
                check_structure(part)
            except (ValueError, OSError) as exc:
                raise BadResponse(f"Downloaded PDF is damaged or incomplete: {exc}") from None
            check = inspect_safe(part, cand.get("date"))
            check["publication_check"] = publication_check(part, cand["publication"])
            if check["status"] == "verified" and check["publication_check"] == "mismatch":
                check["status"] = "wrong_publication"
            os.replace(part, target)
        except IssueMissing as exc:
            self.rows[key].update(status="missing", path="", error=str(exc), checked_at=_now())
            self.save()
            emit(f"{key}: not available ({exc})")
            return "missing"
        except (Temporary, BadResponse) as exc:
            self.rows[key].update(status="failed", path="", error=str(exc), checked_at=_now())
            self.save()
            if isinstance(exc, RateLimited):
                raise
            emit(f"{key}: failed: {exc}")
            return "failed"
        except Forbidden as exc:
            self.rows[key].update(status="failed", path="", error=str(exc), checked_at=_now())
            self.save()
            raise
        finally:
            part.unlink(missing_ok=True)  # never leave partial downloads behind
        twin = next((k for k, r in self.rows.items() if k != key and r.get("sha256") == check["sha256"]
                     and r.get("path") and Path(r["path"]).is_file()), None)
        if twin:
            target.unlink(missing_ok=True)
            self.rows[key].update(status="duplicate", path="", duplicate_of=twin,
                                  sha256=check["sha256"], checked_at=_now())
            self.save()
            emit(f"{key}: identical to {twin}; not saved twice.")
            return "wrong_date" if self.rows[twin].get("status") == "wrong_date" else "have"
        final = self.promote(key, target) if check["status"] in DONE else target
        self.record(key, final, check)
        shown = final.name if check["status"] in DONE else f"{final} (pending review)"
        emit(f"{key}: {check['status']}, {check['pages']} pages -> {shown}")
        return ("pdf" if check["status"] in DONE else
                "wrong_date" if check["status"] == "wrong_date" else "review")

    # ---- review --------------------------------------------------------
    def accept(self, key):
        with self.lock():
            self._load()
            row = self.rows[key]
            path = Path(row["path"])
            if sha(path) != row["sha256"]:
                raise ValueError("File changed since inspection; run recheck before accepting.")
            if row.get("status") == "wrong_date" and len(row.get("detected_dates") or []) == 1:
                # Name it by what is printed, never by the (wrong) filename date.
                row["user_corrected_date"] = row["detected_dates"][0]
            dest = self.promote(key, path)
            row.update(status="accepted_by_user", path=str(dest))
            self.save()
            return dest

    def recheck(self, emit=print):
        """Re-inspect pending local PDFs only; never touches the network."""
        with self.lock():
            self._load()
            for key, row in list(self.rows.items()):
                path = Path(row.get("path") or "/nonexistent")
                if row.get("status") not in REVIEW:
                    continue
                if not path.is_file():
                    emit(f"{key}: pending file is missing; left for review.")
                    continue
                check = inspect_safe(path, row.get("issue_date"))
                check["publication_check"] = publication_check(path, row.get("publication"))
                if check["status"] == "verified" and check["publication_check"] == "mismatch":
                    check["status"] = "wrong_publication"
                if check["status"] in DONE:
                    self.rows[key].update(check)
                    path = self.promote(key, path)
                self.record(key, path, check)
                emit(f"{key}: rechecked, {check['status']}.")


def _now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def read_rows(root) -> dict:
    """Read state without locking or creating anything."""
    state = Path(root).expanduser() / STATE / "state.json"
    return json.loads(state.read_text()) if state.exists() else {}
