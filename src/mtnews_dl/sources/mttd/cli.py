"""Command-line interface; importing it does not require Tkinter."""

import argparse
import datetime as dt
import json
import math
import re
import shutil
import signal
import sys
import threading
from pathlib import Path

from . import __version__
from .core import Library, validate_download_date
from .catalog import discover, ARCHIVE_NAMES


def month(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", value):
        raise argparse.ArgumentTypeError("use YYYY-MM, for example 2026-10")
    try:
        date = dt.date.fromisoformat(value + "-01")
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    if date.year < 1930 or date > dt.date.today().replace(day=1):
        raise argparse.ArgumentTypeError("month must be between January 1930 and this month")
    return value


def year(value: str) -> str:
    if not re.fullmatch(r"\d{4}", value) or not 1930 <= int(value) <= dt.date.today().year:
        raise argparse.ArgumentTypeError("use YYYY, between 1930 and the current year")
    return value


def period(value: str) -> str:
    return year(value) if re.fullmatch(r"\d{4}", value) else month(value)


def expand_periods(values):
    today = dt.date.today()
    result = set()
    for value in values:
        if len(value) == 4:
            last = today.month if int(value) == today.year else 12
            result.update(f"{value}-{number:02d}" for number in range(1, last + 1))
        else:
            result.add(value)
    return sorted(result)


def issue_date(value: str) -> str:
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            raise ValueError("use YYYY-MM-DD")
        dt.date.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc
    return value


def download_date(value):
    try:
        return validate_download_date(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def review_key(value):
    parts = value.split('@')
    issue_date(parts[0])
    if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r'(?:\d+|(?:legacy|cms)-[a-f0-9]{12})', parts[1])):
        raise argparse.ArgumentTypeError("use YYYY-MM-DD or YYYY-MM-DD@ISSUE_ID")
    return value


def delay_seconds(value: str) -> float:
    try:
        delay = float(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("delay must be a number") from exc
    if not math.isfinite(delay) or not 1 <= delay <= 60:
        raise argparse.ArgumentTypeError("delay must be between 1 and 60 seconds")
    return delay


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mttd-dl", description="Download publisher-linked MediaToday newspaper PDFs."
    )
    parser.add_argument("--version", action="version", version=f"mttd-dl {__version__}")
    parser.add_argument("--easy", action="store_true", help="interactive step-by-step guide")
    commands = parser.add_subparsers(dest="command")
    commands.add_parser("archives", help="show archive names and IDs for download --archive")
    listing = commands.add_parser("list", help="list issue dates available in the combined archives")
    listing.add_argument("--month", type=month, help="filter to YYYY-MM")
    listing.add_argument("--json", action="store_true", help="print dates and issue metadata URLs as JSON")
    download = commands.add_parser(
        "download", help="download years, months, or all issues on one date; resume automatically"
    )
    download.add_argument("--archive", "--archives", nargs="+", default=[], metavar="NAME_OR_ID",
                          help="download whole archives, including undated issues; use 'all' for every archive")
    download.add_argument("months", nargs="*", type=period, metavar="YYYY[-MM]")
    download.add_argument("--year", "--years", dest="years", nargs="+", action="extend", type=year,
                          default=[], metavar="YYYY", help="download all archives by issue year, with posting date fallback for yearless issues")
    download.add_argument(
        "--date",
        type=download_date,
        metavar="YYYY-MM-DD",
        help="download every listed newspaper on this date instead of months",
    )
    download.add_argument(
        "--delay", type=delay_seconds, default=2, help="seconds between issues (1–60; default: 2)"
    )
    legacy = download.add_mutually_exclusive_group()
    legacy.add_argument('--gap-only', action='store_true', help='collect daily web articles from June 2010 through September 2013 only')
    legacy.add_argument('--legacy-only', action='store_true', help='collect only HTML collections (including 2010–2013 daily articles)')
    legacy.add_argument('--no-legacy', action='store_true', help='disable automatic HTML reconstruction')
    download.add_argument('--legacy-url', action='append', default=[], metavar='ISSUE_INDEX_URL',
                          help='collect a specific dated legacy issue index (repeatable; replaces period selection)')
    download.add_argument('--no-wayback', action='store_true', help='disable public Wayback fallback for legacy sources')
    review = commands.add_parser(
        "review", help="show files needing inspection and unavailable dates"
    )
    review.add_argument("--json", action="store_true", help="print full review records as JSON")
    recheck = commands.add_parser("recheck", help="recheck pending local PDFs without downloading")
    accept = commands.add_parser("accept", help="accept an issue after checking its printed date")
    accept.add_argument("date", type=review_key, metavar="DATE[@ISSUE_ID]")
    accept.add_argument(
        "--yes", action="store_true", help="confirm you have inspected the PDF's printed date"
    )
    for command in (listing, download):
        command.add_argument("--refresh", action="store_true", help="refresh all archive listings (cached for 6 hours)")
    for command in (download, review, accept, recheck):
        destination = command.add_mutually_exclusive_group()
        destination.add_argument(
            "-P",
            "--output-dir",
            type=Path,
            help="save PDFs directly in this folder (default: current directory)",
        )
        destination.add_argument(
            "--library",
            type=Path,
            default=None,
            help="use a structured library; default: PDFs directly in the current directory",
        )
    return parser


def read_review(root: Path, *, flat=False) -> dict:
    """Read state without creating directories or acquiring a writer lock."""
    state = root.expanduser() / ".mttd-dl" / "state.json"
    rows = json.loads(state.read_text()) if state.exists() else {}
    return {
        date: row
        for date, row in sorted(rows.items())
        if row["status"] not in ("verified", "verified_pdf", "accepted_by_user", "reconstructed")
    }


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.easy:
        if args.command:
            parser.error("Use --easy by itself, without a subcommand.")
        from .easy import run_easy

        return run_easy(main)
    if args.command is None:
        parser.error("Choose a command or use --easy for guided setup.")
    if args.command == "archives":
        for archive_id, name in ARCHIVE_NAMES.items():
            print(f"{archive_id}\t{name}")
        return 0
    if args.command == "download":
        if sum((bool(args.months or args.years), bool(args.date), bool(args.archive), bool(args.legacy_url))) != 1:
            parser.error("Choose years/months, --date, --archive, or --legacy-url (one selection mode).")
        if args.archive and (args.legacy_only or args.gap_only):
            parser.error('--legacy-only supports years/months, --date or --legacy-url, not Uberflip archives.')
        if args.legacy_url and (args.no_legacy or args.gap_only):
            parser.error('--legacy-url cannot be combined with --no-legacy.')
        archive_lookup = {str(i): i for i in ARCHIVE_NAMES}
        archive_lookup.update({name: i for i, name in ARCHIVE_NAMES.items()})
        if args.archive:
            if args.archive == ['all']:
                args.archive = list(ARCHIVE_NAMES)
            else:
                try:
                    args.archive = list(dict.fromkeys(archive_lookup[a] for a in args.archive))
                except KeyError as exc:
                    parser.error(f"Unknown archive {exc.args[0]!r}; run 'mttd-dl archives'.")
        args.selected_years = sorted(set(args.years + [p for p in args.months if len(p) == 4]))
        args.explicit_months = sorted({p for p in args.months if len(p) == 7 and p[:4] not in args.selected_years})
        args.months = expand_periods(args.months + args.years)
    flat = getattr(args, "library", None) is None
    root = getattr(args, "library", None) or getattr(args, "output_dir", None) or Path.cwd()
    try:
        if args.command == "list":
            entries = discover(refresh=args.refresh, emit=lambda line: print(line, file=sys.stderr))
            entries = {
                date: urls
                for date, urls in sorted(entries.items())
                if not args.month or date.startswith(args.month)
            }
            if args.json:
                print(json.dumps(entries, indent=2))
            else:
                for date, urls in entries.items():
                    print(f"{date}\t{len(urls)} issue link(s)")
            return 0
        if args.command == "review":
            rows = read_review(root, flat=flat)
            if args.json:
                print(json.dumps(rows, indent=2))
            elif not rows:
                print("No issues need review.")
            else:
                for date, row in rows.items():
                    print(f"{date}\t{row['status']}\t{row.get('path') or 'No PDF'}")
                    print(f"  Detected dates: {', '.join(row.get('detected_dates', [])) or 'none'}")
            return 0
        if args.command == "recheck":
            if not all(shutil.which(tool) for tool in ("pdfinfo", "pdftotext")):
                raise RuntimeError("Install Poppler before rechecking PDFs.")
            Library(root, flat=flat).recheck()
            return 3 if any(row.get("path") for row in read_review(root, flat=flat).values()) else 0
        if args.command == "accept":
            rows = read_review(root, flat=flat)
            if args.date not in rows and '@' not in args.date:
                matches = [key for key in rows if key.startswith(args.date + '@') and rows[key].get('path')]
                if len(matches) > 1:
                    raise ValueError("Multiple pending issues: specify one of " + ', '.join(matches))
                if matches:
                    args.date = matches[0]
            row = rows.get(args.date)
            if not row or not row.get("path"):
                raise ValueError("No pending PDF for this date. Run 'mttd-dl review' first.")
            if not args.yes:
                print(f"File: {row['path']}\nDetected dates: {row.get('detected_dates', [])}")
                if not sys.stdin.isatty():
                    raise ValueError("Use --yes only after inspecting the PDF's printed date.")
                prompt = (f"Have you reviewed this reconstructed collection and its missing-content warnings for {args.date}? [y/N] "
                          if row.get('source_type') in ('legacy_html', 'cms_html') else
                          f"Have you confirmed the printed issue date is {args.date}? [y/N] ")
                answer = input(prompt)
                if answer.strip().lower() not in {"y", "yes"}:
                    print("Not accepted.")
                    return 1
            Library(root, flat=flat).accept(args.date)
            print(f"Accepted {args.date}.")
            return 0
        missing = [tool for tool in ("pdfinfo", "pdftotext") if not shutil.which(tool)]
        if missing:
            raise RuntimeError(f"Install Poppler before downloading; missing: {', '.join(missing)}")
        library = Library(root, flat=flat, refresh=args.refresh)
        stop = threading.Event()

        def interrupt(signum, frame):
            stop.set()
            print("Stopping; waiting for the current network operation…", file=sys.stderr)

        previous = signal.signal(signal.SIGINT, interrupt)
        legacy_result = 0
        try:
            if args.legacy_only or args.gap_only or args.legacy_url:
                pass
            elif args.archive:
                library.run_archives(args.archive, args.delay, stop, lambda line: print(line, flush=True))
            elif args.selected_years:
                library.run_periods(args.selected_years, args.explicit_months, args.delay, stop,
                                    lambda line: print(line, flush=True))
            elif args.date:
                library.run_issue(args.date, stop, lambda line: print(line, flush=True), delay=args.delay)
            else:
                library.run(
                    sorted(set(args.months)), args.delay, stop, lambda line: print(line, flush=True)
                )
            from .gap import gap_requested, run_gap
            legacy_years = [y for y in args.selected_years if y < '2010']
            legacy_months = [m for m in args.months if m <= '2010-05' and m[:4] not in legacy_years]
            legacy_date = args.date if args.date and args.date <= '2010-05-31' else None
            old_requested = bool(legacy_years or legacy_months or legacy_date or args.legacy_url)
            gap = gap_requested(args.selected_years, args.explicit_months, args.date)
            results = []
            if not args.no_legacy and not args.archive:
                if old_requested and not args.gap_only:
                    from .legacy import run_legacy
                    results.append(run_legacy(library, legacy_years, legacy_months, legacy_date,
                        args.delay, stop, lambda line: print(line, flush=True),
                        wayback=not args.no_wayback, seeds=args.legacy_url))
                if gap and not args.legacy_url:
                    results.append(run_gap(library, args.selected_years, args.explicit_months,
                        args.date, args.delay, stop, lambda line: print(line, flush=True),
                        wayback=not args.no_wayback))
                if args.gap_only and not gap:
                    raise ValueError('--gap-only requires June 2010 through September 2013.')
                if args.legacy_only and not (old_requested or gap):
                    raise ValueError('HTML selection must precede October 2013.')
            legacy_result = 3 if 3 in results else 4 if results and all(r == 4 for r in results) and (args.legacy_only or args.gap_only or args.legacy_url) else 0
        except InterruptedError:
            return 130
        finally:
            signal.signal(signal.SIGINT, previous)
        if stop.is_set():
            return 130
        if legacy_result:
            return legacy_result
        if args.date and library.rows.get(args.date, {}).get("status") == "unavailable" and not any(
            k.startswith(args.date + '@') and r.get('path') for k, r in library.rows.items()):
            return 4
        # Missing feed dates are informational: the paper is not published every day.
        if any(
            row["status"] in {"wrong_date", "uncertain", "ambiguous_feed"}
            for date, row in library.rows.items()
            if (date in library.selected_keys if args.archive or args.selected_years else
                date[:10] == args.date if args.date else date[:7] in args.months)
        ):
            return 3
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"mttd-dl: {exc}", file=sys.stderr)
        return 1
