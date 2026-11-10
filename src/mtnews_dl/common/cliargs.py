"""Argument types and option groups shared by every *-dl command, so they all read alike.

Selection (download):  YYYY | YYYY-MM ...   --year Y ...   --date D ...   --from D [--to D]
Destination:           -P/--output-dir DIR (flat, default: current folder) | --library DIR
Pacing:                --delay S (1-60)   --retries N (0-5)   --retry-missing   --dry-run
"""

import argparse
import datetime as dt
import math
import re
import signal
import sys
import threading
from contextlib import contextmanager
from pathlib import Path

# Exit codes, identical for every tool (documented in README):
EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
EXIT_REVIEW = 3      # some PDFs need your review
EXIT_NOTHING = 4     # a single requested date: nothing found
EXIT_AUTH = 5        # login / cookies / Chrome connection needed
EXIT_STOPPED = 6     # stopped by the site: rate limit, refusal, server errors, quota
EXIT_INTERRUPTED = 130

FIRST_YEAR = 1930


def _date(value: str) -> dt.date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value or ""):
        raise argparse.ArgumentTypeError("use YYYY-MM-DD, for example 2026-11-03")
    try:
        date = dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    if not dt.date(FIRST_YEAR, 1, 1) <= date <= dt.date.today():
        raise argparse.ArgumentTypeError(f"date must be between {FIRST_YEAR}-01-01 and today")
    return date


def issue_date(value: str) -> str:
    return _date(value).isoformat()


def year(value: str) -> str:
    if not re.fullmatch(r"\d{4}", value or "") or not FIRST_YEAR <= int(value) <= dt.date.today().year:
        raise argparse.ArgumentTypeError(f"use YYYY, between {FIRST_YEAR} and the current year")
    return value


def month(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", value or ""):
        raise argparse.ArgumentTypeError("use YYYY-MM, for example 2026-10")
    try:
        first = dt.date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from None
    if first.year < FIRST_YEAR or first > dt.date.today():
        raise argparse.ArgumentTypeError(f"month must be between January {FIRST_YEAR} and this month")
    return value


def period(value: str) -> str:
    return year(value) if re.fullmatch(r"\d{4}", value or "") else month(value)


def delay_seconds(value: str) -> float:
    try:
        delay = float(value)
    except ValueError:
        raise argparse.ArgumentTypeError("delay must be a number") from None
    if not math.isfinite(delay) or not 1 <= delay <= 60:
        raise argparse.ArgumentTypeError("delay must be between 1 and 60 seconds")
    return delay


def retries(value: str) -> int:
    if not value.isdigit() or int(value) > 5:
        raise argparse.ArgumentTypeError("retries must be 0–5")
    return int(value)


def add_selection(parser, *, listed_help=None):
    """Positional periods plus --year/--date/--from/--to (and --listed when the source has one)."""
    parser.add_argument("periods", nargs="*", type=period, metavar="YYYY[-MM]",
                        help="years or months to download, e.g. 2025 or 2026-10")
    parser.add_argument("--year", "--years", dest="years", nargs="+", action="extend", type=year,
                        default=[], metavar="YYYY", help="whole years (same as giving YYYY)")
    parser.add_argument("--date", type=issue_date, action="append", default=[], metavar="YYYY-MM-DD",
                        help="one issue date (repeatable)")
    parser.add_argument("--from", dest="from_date", type=issue_date, metavar="YYYY-MM-DD",
                        help="first date of a range (inclusive)")
    parser.add_argument("--to", dest="to_date", type=issue_date, metavar="YYYY-MM-DD",
                        help="last date of the range (inclusive; default today)")
    if listed_help:
        parser.add_argument("--listed", action="store_true", help=listed_help)
    else:
        parser.set_defaults(listed=False)


def add_pacing(parser, *, delay=2, retries_default=3, dry_run=True, retry_missing=True):
    parser.add_argument("--delay", type=delay_seconds, default=delay,
                        help=f"seconds between requests (1–60; default {delay:g})")
    parser.add_argument("--retries", type=retries, default=retries_default,
                        help=f"retries for temporary errors (0–5; default {retries_default})")
    if retry_missing:
        parser.add_argument("--retry-missing", action="store_true",
                            help="check dates recorded as missing again")
    if dry_run:
        parser.add_argument("--dry-run", action="store_true",
                            help="show what would be downloaded; no PDF requests")


def add_destination(parser, *, library_layout="PATH/editions/YYYY/"):
    dest = parser.add_mutually_exclusive_group()
    dest.add_argument("-P", "--output-dir", type=Path,
                      help="save PDFs directly in this folder (default: current folder)")
    dest.add_argument("--library", type=Path, help=f"structured library: {library_layout}")


def destination(args):
    """(root, flat) from -P/--library; flat (PDFs directly in the folder) is the default."""
    library = getattr(args, "library", None)
    root = library or getattr(args, "output_dir", None) or Path.cwd()
    return Path(root).expanduser(), library is None


def destination_flag(args) -> str:
    if getattr(args, "library", None):
        return f" --library {args.library}"
    if getattr(args, "output_dir", None):
        return f" -P {args.output_dir}"
    return ""


def selection_count(args) -> int:
    return sum((bool(args.periods or args.years), bool(args.date), bool(args.from_date),
                bool(getattr(args, "listed", False))))


def check_selection(parser, args, *, required=True):
    if args.to_date and not args.from_date:
        parser.error("--to needs --from")
    count = selection_count(args)
    if count > 1:
        parser.error("choose one selection: YYYY[-MM]/--year, --date, --from/--to"
                     + (", or --listed" if hasattr(args, "listed") else ""))
    if required and count == 0:
        parser.error("choose what to download: YYYY[-MM], --year, --date or --from/--to")


def expand_dates(args) -> list:
    """Every calendar date selected, up to today, as sorted YYYY-MM-DD strings."""
    today = dt.date.today()
    dates = set()

    def span(first, last):
        last = min(last, today)
        while first <= last:
            dates.add(first.isoformat())
            first += dt.timedelta(days=1)

    for value in list(args.periods) + list(args.years):
        if len(value) == 4:
            span(dt.date(int(value), 1, 1), dt.date(int(value), 12, 31))
        else:
            first = dt.date.fromisoformat(value + "-01")
            nxt = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            span(first, nxt - dt.timedelta(days=1))
    dates.update(args.date or [])
    if args.from_date:
        first = _date(args.from_date)
        last = _date(args.to_date) if args.to_date else today
        if first > last:
            raise ValueError("--from must not be after --to")
        span(first, last)
    return sorted(dates)


def months_of(dates) -> list:
    return sorted({d[:7] for d in dates})


def safe_console():
    """Never crash on characters the console's code page lacks (e.g. Maltese letters on a
    redirected Windows console): show a replacement instead."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="backslashreplace")
        except (AttributeError, ValueError):
            pass


def printer(tool):
    def err(message):
        print(f"{tool}: {message}", file=sys.stderr)
    return err


@contextmanager
def interruptible(message="Stopping after the current network operation…"):
    """Yields a threading.Event set by Ctrl+C (the first press stops cleanly)."""
    stop = threading.Event()

    def handler(_signum, _frame):
        if stop.is_set():
            raise KeyboardInterrupt
        stop.set()
        print(message, file=sys.stderr)

    try:
        previous = signal.signal(signal.SIGINT, handler)
    except ValueError:  # not the main thread
        previous = None
    try:
        yield stop
    finally:
        if previous is not None:
            signal.signal(signal.SIGINT, previous)


def confirm_accept(row_path, expected, detected, *, yes, err) -> bool:
    if yes:
        return True
    print(f"File: {row_path}\nExpected date: {expected}\nDetected dates: {detected}")
    if not sys.stdin.isatty():
        err("use --yes only after inspecting the PDF.")
        return False
    if input("Have you confirmed this PDF is the right issue? [y/N] ").strip().lower() not in ("y", "yes"):
        print("Not accepted.")
        return False
    return True
