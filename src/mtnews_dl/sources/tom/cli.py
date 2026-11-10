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
from .core import FEED, Library, fetch, parse_feed, validate_download_date


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
        prog="tom-dl", description="Download publisher-linked Times of Malta issue PDFs."
    )
    parser.add_argument("--version", action="version", version=f"tom-dl {__version__}")
    parser.add_argument("--easy", action="store_true", help="interactive step-by-step guide")
    commands = parser.add_subparsers(dest="command")
    listing = commands.add_parser("list", help="list issue dates available in the live feed")
    listing.add_argument("--month", type=month, help="filter to YYYY-MM")
    listing.add_argument("--json", action="store_true", help="print dates and PDF URLs as JSON")
    download = commands.add_parser(
        "download", help="download months or one issue with --date; resume automatically"
    )
    download.add_argument("months", nargs="*", type=month, metavar="YYYY-MM")
    download.add_argument(
        "--date",
        type=download_date,
        metavar="YYYY-MM-DD",
        help="download exactly one issue instead of months",
    )
    download.add_argument(
        "--delay", type=delay_seconds, default=2, help="seconds between issues (1–60; default: 2)"
    )
    review = commands.add_parser(
        "review", help="show files needing inspection and unavailable dates"
    )
    review.add_argument("--json", action="store_true", help="print full review records as JSON")
    accept = commands.add_parser("accept", help="accept an issue after checking its printed date")
    accept.add_argument("date", type=issue_date, metavar="YYYY-MM-DD")
    accept.add_argument(
        "--yes", action="store_true", help="confirm you have inspected the PDF's printed date"
    )
    commands.add_parser("gui", help="open the optional Tkinter desktop interface")
    for command in (download, review, accept):
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
    state = root.expanduser() / (".tom-dl" if flat else "epaper") / "state.json"
    rows = json.loads(state.read_text()) if state.exists() else {}
    return {
        date: row
        for date, row in sorted(rows.items())
        if row["status"] not in ("verified", "accepted_by_user")
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
    if args.command == "download" and bool(args.months) == bool(args.date):
        parser.error("Choose either one or more months or --date YYYY-MM-DD.")
    flat = getattr(args, "library", None) is None
    root = getattr(args, "library", None) or getattr(args, "output_dir", None) or Path.cwd()
    try:
        if args.command == "list":
            entries = parse_feed(fetch(FEED))
            entries = {
                date: urls
                for date, urls in sorted(entries.items())
                if not args.month or date.startswith(args.month)
            }
            if args.json:
                print(json.dumps(entries, indent=2))
            else:
                for date, urls in entries.items():
                    print(f"{date}\t{len(urls)} PDF link(s)")
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
        if args.command == "accept":
            row = read_review(root, flat=flat).get(args.date)
            if not row or not row.get("path"):
                raise ValueError("No pending PDF for this date. Run 'tom-dl review' first.")
            if not args.yes:
                print(f"File: {row['path']}\nDetected dates: {row.get('detected_dates', [])}")
                if not sys.stdin.isatty():
                    raise ValueError("Use --yes only after inspecting the PDF's printed date.")
                answer = input(f"Have you confirmed the printed issue date is {args.date}? [y/N] ")
                if answer.strip().lower() not in {"y", "yes"}:
                    print("Not accepted.")
                    return 1
            Library(root, flat=flat).accept(args.date)
            print(f"Accepted {args.date}.")
            return 0
        if args.command == "gui":
            from .gui import main as gui_main

            gui_main()
            return 0
        missing = [tool for tool in ("pdfinfo", "pdftotext") if not shutil.which(tool)]
        if missing:
            raise RuntimeError(f"Install Poppler before downloading; missing: {', '.join(missing)}")
        library = Library(root, flat=flat)
        stop = threading.Event()

        def interrupt(signum, frame):
            stop.set()
            print("Stopping; waiting for the current network operation…", file=sys.stderr)

        previous = signal.signal(signal.SIGINT, interrupt)
        try:
            if args.date:
                library.run_issue(args.date, stop, lambda line: print(line, flush=True))
            else:
                library.run(
                    sorted(set(args.months)), args.delay, stop, lambda line: print(line, flush=True)
                )
        except InterruptedError:
            return 130
        finally:
            signal.signal(signal.SIGINT, previous)
        if stop.is_set():
            return 130
        if args.date and library.rows.get(args.date, {}).get("status") == "unavailable":
            return 4
        # Missing feed dates are informational: the paper is not published every day.
        if any(
            row["status"] in {"wrong_date", "uncertain", "ambiguous_feed"}
            for date, row in library.rows.items()
            if (date == args.date if args.date else date[:7] in args.months)
        ):
            return 3
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"tom-dl: {exc}", file=sys.stderr)
        return 1
