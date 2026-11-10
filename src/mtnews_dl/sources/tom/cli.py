"""tom-dl: Times of Malta issues as whole PDFs.

  e-paper (no login):  2024-11-19 onward, from the publisher's feed and tom-dl's link record
  archive (--archive): older issues from timesofmalta.com/archive, through your signed-in
                       Chrome (premium subscription; `tom-dl connect` once)
"""

import argparse
import datetime as dt
import json
import shutil
import sqlite3
import sys
from pathlib import Path

from ... import __version__
from ...common.cliargs import (EXIT_AUTH, EXIT_ERROR, EXIT_INTERRUPTED, EXIT_NOTHING, EXIT_REVIEW,
                               EXIT_STOPPED, add_destination, add_pacing, add_selection,
                               check_selection, confirm_accept, destination, destination_flag,
                               expand_dates, interruptible, issue_date, period, printer)
from ...common.paths import data_dir
from . import feed
from ..tom_archive.runner import DEFAULT_CEILING
from .library import DONE, EPAPER_START, REVIEW, Library, StopRun, read_rows

PROG = "tom-dl"
_err = printer(PROG)


def ceiling(value):
    if not value.isdigit() or not 1 <= int(value) <= 1000:
        raise argparse.ArgumentTypeError("use a number of pages from 1 to 1000")
    return int(value)


def build_parser():
    parser = argparse.ArgumentParser(
        prog=PROG, description="Download Times of Malta issues as whole PDFs: the e-paper (2024-11-19 "
        "onward, no login) and, with --archive, older issues from the digital archive via your "
        "signed-in Chrome.", epilog="Start with: tom-dl --easy")
    parser.add_argument("--version", action="version", version=f"{PROG} {__version__}")
    parser.add_argument("--easy", action="store_true", help="interactive step-by-step guide")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")

    download = commands.add_parser("download", help="download by year, month, date or range; resumes automatically")
    add_selection(download)
    download.add_argument("--archive", action="store_true",
                          help="use the digital archive (Chrome) for dates without an e-paper PDF")
    download.add_argument("--max-pages", type=ceiling, metavar="N",
                          help="archive: stop after requesting N pages in this run")
    download.add_argument("--ceiling", type=ceiling, default=DEFAULT_CEILING, metavar="N",
                          help=f"archive: local page allowance per rolling 24 h (default {DEFAULT_CEILING})")
    download.add_argument("--wait", action="store_true",
                          help="archive: wait for the local allowance to free up instead of stopping")
    download.add_argument("--offline", action="store_true",
                          help="do not read the live feed; use the saved link record only")
    add_pacing(download, delay=2)

    listing = commands.add_parser("list", help="dates with an e-paper PDF (live feed + link record)")
    listing.add_argument("periods", nargs="*", type=period, metavar="YYYY[-MM]", help="filter")
    listing.add_argument("--json", action="store_true")
    listing.add_argument("--offline", action="store_true", help="do not read the live feed")

    review = commands.add_parser("review", help="show PDFs needing inspection and unavailable dates")
    review.add_argument("--json", action="store_true")
    review.add_argument("--all", action="store_true", help="include unavailable/missing dates")
    recheck = commands.add_parser("recheck", help="re-check pending PDFs (no network)")
    accept = commands.add_parser("accept", help="accept a pending PDF after checking its printed date")
    accept.add_argument("date", type=issue_date, metavar="YYYY-MM-DD")
    accept.add_argument("--yes", action="store_true", help="confirm you inspected the printed date")
    for command in (download, review, recheck, accept):
        add_destination(command)

    feed_cmd = commands.add_parser("feed", help="the e-paper link record: refresh, import snapshots, status")
    feed_sub = feed_cmd.add_subparsers(dest="feed_command", metavar="ACTION", required=True)
    feed_sub.add_parser("update", help="read the live feed now and add its links to the record")
    imp = feed_sub.add_parser("import", help="add links from saved feed XML files (e.g. Windows feed archiver)")
    imp.add_argument("files", nargs="+", type=Path)
    feed_sub.add_parser("status", help="how many dates the record covers")

    connect = commands.add_parser("connect", help="set up Chrome for the archive and check your sign-in")
    which = connect.add_mutually_exclusive_group()
    which.add_argument("--own-browser", action="store_true",
                       help="use tom-dl's own browser window (downloaded if needed), not your Chrome")
    which.add_argument("--my-chrome", action="store_true", help="use your installed Chrome")
    arch = commands.add_parser("archive", help="archive allowance and earlier archive-downloader libraries")
    arch_sub = arch.add_subparsers(dest="archive_command", metavar="ACTION", required=True)
    status = arch_sub.add_parser("status", help="pages used in the last 24 hours, saved pages")
    old = arch_sub.add_parser("import", help="bring in a library from the old Times of Malta archive "
                              "downloader (its progress.sqlite3 and pages/), keeping quota history")
    old.add_argument("old_library", type=Path)
    used = arch_sub.add_parser("add-usage", help="count archive pages downloaded outside tom-dl")
    used.add_argument("pages", type=int)
    pages = arch_sub.add_parser("pages", help="list an issue's archive pages without downloading "
                                "(uses no page allowance)")
    pages.add_argument("dates", nargs="+", type=issue_date, metavar="YYYY-MM-DD")
    for command in (status, old, used, pages):
        add_destination(command)
    return parser


# ---- commands ---------------------------------------------------------------------------------

def _links(offline, stop=None, emit=print):
    record = feed.LinkRecord()
    if not offline:
        try:
            entries = record.refresh(stop)
            emit(f"Live feed: {len(entries)} dated PDF(s); link record now covers {len(record.dates())} date(s).")
        except feed.FeedError as exc:
            emit(f"Could not read the live feed ({exc}); using the saved link record "
                 f"({len(record.dates())} dates).")
    return record


def cmd_list(args):
    record = _links(args.offline, emit=lambda m: print(m, file=sys.stderr))
    dates = [d for d in record.dates() if not args.periods or any(d.startswith(p) for p in args.periods)]
    if args.json:
        print(json.dumps({d: record.links(d) for d in dates}, indent=2))
    else:
        for d in dates:
            print(f"{d}\t{'live feed' if d in record.live else 'link record'}\t{record.links(d)[0]}")
        print(f"{len(dates)} date(s) with an e-paper PDF. Earlier dates (before {EPAPER_START}): "
              "tom-dl download --archive.", file=sys.stderr)
    return 0


def cmd_download(args, parser):
    check_selection(parser, args)
    try:
        dates = expand_dates(args)
    except ValueError as exc:
        parser.error(str(exc))
    root, flat = destination(args)
    library = Library(root, flat=flat)
    emit = lambda line: print(line, flush=True)  # noqa: E731
    archive = (dict(ceiling=args.ceiling, wait=args.wait, max_pages=args.max_pages)
               if args.archive else None)
    with interruptible() as stop:
        record = _links(args.offline, stop, emit)
        if args.dry_run:
            library.load()
            for date, route, detail in library.plan(dates, record, archive=bool(archive)):
                print(f"{date}\t{route}\t{detail}")
            return 0
        try:
            summary = library.run(dates, record, delay=args.delay, stop=stop, emit=emit,
                                  retries=args.retries, retry_missing=args.retry_missing,
                                  archive=archive)
        except StopRun as exc:
            _err(f"{exc}\nProgress is saved; rerun the same command to resume.")
            return exc.code
        except InterruptedError:
            return EXIT_INTERRUPTED
    if stop.is_set():
        return EXIT_INTERRUPTED
    emit("Summary: " + ", ".join(f"{k} {v}" for k, v in summary.items() if v or k == "downloaded"))
    rows = {d: library.rows.get(d, {}) for d in library.selected}
    if any(r.get("status") in REVIEW + ("ambiguous_feed",) for r in rows.values()):
        print(f"Some PDFs need review: {PROG} review{destination_flag(args)}")
        return EXIT_REVIEW
    if len(dates) == 1 and not any(r.get("status") in DONE for r in rows.values()):
        return EXIT_NOTHING
    return 0


def review_rows(root, flat, everything=False):
    hidden = DONE if everything else DONE + ("unavailable", "archive_not_listed", "missing")
    return {d: r for d, r in sorted(read_rows(root, flat=flat).items()) if r.get("status") not in hidden}


def cmd_review(args):
    root, flat = destination(args)
    rows = review_rows(root, flat, args.all)
    if args.json:
        print(json.dumps({d: {k: v for k, v in r.items() if k != "front_text"} for d, r in rows.items()},
                         indent=2))
    elif not rows:
        print("No issues need review.")
    else:
        for date, row in rows.items():
            print(f"{date}\t{row.get('status')}\t{row.get('path') or row.get('front_text', '')[:150]}")
            if row.get("status") in REVIEW:
                print(f"  Printed dates found: {', '.join(row.get('detected_dates') or []) or 'none'} "
                      f"({row.get('method')}). Accept after checking: {PROG} accept {date} --yes")
    return 0


def cmd_accept(args):
    root, flat = destination(args)
    row = review_rows(root, flat).get(args.date)
    if not row or not row.get("path"):
        _err(f"no pending PDF for {args.date}; run '{PROG} review' first.")
        return EXIT_ERROR
    if not confirm_accept(row["path"], args.date, row.get("detected_dates"), yes=args.yes, err=_err):
        return EXIT_ERROR
    print(f"Accepted {args.date} -> {Library(root, flat=flat).accept(args.date)}")
    return 0


def cmd_recheck(args):
    root, flat = destination(args)
    Library(root, flat=flat).recheck()
    return EXIT_REVIEW if any(r.get("path") for r in review_rows(root, flat).values()) else 0


def cmd_feed(args):
    record = feed.LinkRecord()
    if args.feed_command == "update":
        entries = record.refresh()
        print(f"Live feed: {len(entries)} dated PDF(s), {min(entries)} to {max(entries)}. "
              f"Link record: {len(record.dates())} dates ({record.path}).")
    elif args.feed_command == "import":
        total = 0
        for path in args.files:
            try:
                entries = feed.parse_feed(path.read_bytes())
            except (OSError, feed.FeedError) as exc:
                _err(f"{path}: {exc}")
                continue
            total += record.add_feed(entries)
            print(f"{path.name}: {len(entries)} dated PDF(s)" + (f", {min(entries)} to {max(entries)}" if entries else ""))
        record.save()
        print(f"Link record: {len(record.dates())} dates.")
    else:
        dates = record.dates()
        print(f"Link record: {record.path}\nDates with an e-paper PDF: {len(dates)}"
              + (f" ({dates[0]} to {dates[-1]})" if dates else ""))
        print("Every tom-dl run also reads the live feed (about the last two years) and adds new links.")
    return 0


def cmd_connect(args):
    from ...common import browser, system
    from ..tom_archive.chrome_bridge import (BRIDGE_URL, NotConnected, chrome_page, install_extension,
                                             own_extension_dir, set_browser_mode)
    from ..tom_archive.runner import Runner
    folder = install_extension()
    chrome = system.find_chrome()
    own = args.own_browser or (not args.my_chrome and (chrome is None or browser.own_browser()))
    if own:
        if not browser.own_browser() and not browser.install_own_browser():
            _err("tom-dl's browser could not be installed; install Google Chrome and run tom-dl connect again.")
            return EXIT_ERROR
        set_browser_mode("own")
        print("Using tom-dl's own browser window (Chrome for Testing, separate profile). Sign in to "
              "timesofmalta.com in that window once; it is remembered for later runs.\n"
              "tom-dl never sees your password: the window does the downloading with your sign-in.")
        browser.launch_own_browser(own_extension_dir(), [BRIDGE_URL, "https://timesofmalta.com/archive"])
    else:
        set_browser_mode("chrome")
        print(f"""Using your Chrome ({chrome}). The tom-dl extension is in:
  {folder}

One-time setup in Chrome (the profile where you are signed in to timesofmalta.com):
  1. Open chrome://extensions and switch on "Developer mode" (top right).
  2. Click "Load unpacked" and choose the folder above.
  3. Click the extensions (puzzle-piece) icon, pin "tom-dl archive connection", then click it:
     a "tom-dl connection" tab opens. Keep it open while tom-dl uses the archive.
tom-dl never reads your cookies or password; Chrome does the downloading with your own sign-in.
Tip: in Chrome's download settings, turn off "Ask where to save each file".
(Prefer a separate window instead? tom-dl connect --own-browser)
""")
        if not sys.stdin.isatty():
            return 0
        input("Press Enter once the connection tab is open (Ctrl+C to finish later)… ")
    with interruptible() as stop:
        try:
            with chrome_page(stop, print, timeout=120) as page:
                temp = data_dir("tom-dl") / "connect-check"
                runner = Runner(dict(folder=str(temp), interval=1, ceiling=DEFAULT_CEILING,
                                     auto_resume=False), print, stop)
                try:
                    runner.wait_for_sign_in(page)
                finally:
                    runner.ledger.close()
        except NotConnected as exc:
            _err(str(exc))
            return EXIT_AUTH
        except (RuntimeError, InterruptedError) as exc:
            _err(str(exc) or "stopped")
            return EXIT_AUTH
    print("Ready: use  tom-dl download --archive ...  for issues before 2024-11-19.")
    return 0


def cmd_archive(args):
    from ..tom_archive.ledger import Ledger
    root, flat = destination(args)
    library = Library(root, flat=flat)
    target = library.archive_root()
    if args.archive_command == "import":
        old = args.old_library.expanduser()
        if not (old / "progress.sqlite3").is_file():
            _err(f"{old} has no progress.sqlite3 (choose the old downloader's 'library' folder).")
            return EXIT_ERROR
        if (target / "progress.sqlite3").exists():
            _err(f"{target} already has archive progress; not overwritten.")
            return EXIT_ERROR
        target.mkdir(parents=True, exist_ok=True)
        src = sqlite3.connect(old / "progress.sqlite3")
        dst = sqlite3.connect(target / "progress.sqlite3")
        with dst:
            src.backup(dst)
        src.close()
        dst.close()
        if (old / "pages").is_dir():
            shutil.copytree(old / "pages", target / "pages", dirs_exist_ok=True)
        ledger = Ledger(target)
        print(f"Imported into {target}: {ledger.stats()}")
        ledger.close()
        if (old / "editions").is_dir():
            print(f"Merged issues in {old / 'editions'} were not copied; move them into {root} and "
                  "tom-dl will check and keep them.")
        return 0
    if args.archive_command == "pages":
        return _archive_pages(args, target)
    ledger = Ledger(target)
    try:
        if args.archive_command == "add-usage":
            ledger.manual(args.pages)
        stats = ledger.stats()
        print(f"Archive state: {target}\nPages used in the last 24 hours: {stats['used_24h']} "
              f"(local count; default allowance {DEFAULT_CEILING})\nSaved pages: {stats['saved_pages']} "
              f"({stats['bytes'] / 1e6:.0f} MB); issues inventoried: {stats['listed_issues']}")
        if stats["used_24h"]:
            free = dt.datetime.fromtimestamp(ledger.next_release()).strftime("%Y-%m-%d %H:%M")
            print(f"Next page frees up at about {free}.")
    finally:
        ledger.close()
    return 0


def _archive_pages(args, target):
    from ..tom_archive.chrome_bridge import NotConnected, chrome_page
    from ..tom_archive.runner import IssueUnavailable, Runner
    with interruptible() as stop:
        try:
            with chrome_page(stop, print) as page:
                runner = Runner(dict(folder=str(target), interval=2, ceiling=DEFAULT_CEILING,
                                     auto_resume=False), print, stop)
                try:
                    for date in args.dates:
                        try:
                            items = runner.list_issue(page, date)
                        except IssueUnavailable as exc:
                            print(exc)
                            continue
                        for page_id, url, _ in items:
                            saved = "saved" if runner.ledger.valid(page_id) else "not saved"
                            print(f"{date}\t{page_id}\t{saved}\t{url}")
                finally:
                    runner.close()
        except NotConnected as exc:
            _err(str(exc))
            return EXIT_AUTH
        except RuntimeError as exc:
            _err(str(exc))
            return EXIT_STOPPED
    return 0


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.easy:
        if args.command:
            parser.error("use --easy by itself")
        from .easy import run_easy
        return run_easy(main)
    if args.command is None:
        parser.print_help()
        return 2
    try:
        handler = dict(list=cmd_list, review=cmd_review, accept=cmd_accept, recheck=cmd_recheck,
                       feed=cmd_feed, connect=cmd_connect, archive=cmd_archive).get(args.command)
        return handler(args) if handler else cmd_download(args, parser)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    except (RuntimeError, ValueError, OSError, feed.FeedError) as exc:
        _err(str(exc))
        return EXIT_ERROR
    except Exception as exc:  # last resort: short message, state is saved
        _err(f"unexpected error ({exc.__class__.__name__}: {str(exc)[:200]}). Progress is saved; "
             "rerun the same command to resume. Please report this message.")
        return EXIT_ERROR


if __name__ == "__main__":
    raise SystemExit(main())
