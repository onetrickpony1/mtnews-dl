"""mttd-dl: MaltaToday, Illum, BusinessToday and the other MediaToday titles as PDFs.

Publisher-linked issue PDFs from the Uberflip archives, plus (before October 2013) issues
rebuilt from the publisher's web archives, with an optional Wayback Machine fallback.
"""

import argparse
import json
import re
import sys
from pathlib import Path

from ... import __version__
from ...common.cliargs import (EXIT_INTERRUPTED, EXIT_NOTHING, EXIT_REVIEW, add_destination,
                               add_pacing, add_selection, check_selection, confirm_accept,
                               destination, expand_dates, interruptible, issue_date, month,
                               months_of, period, printer)
from .catalog import ARCHIVE_NAMES, discover
from .core import Library, validate_download_date  # noqa: F401

_err = printer("mttd-dl")


def expand_periods(values):
    import datetime as dt
    today = dt.date.today()
    result = set()
    for value in values:
        if len(value) == 4:
            last = today.month if int(value) == today.year else 12
            result.update(f"{value}-{number:02d}" for number in range(1, last + 1))
        else:
            result.add(value)
    return sorted(result)


def review_key(value):
    parts = value.split('@')
    issue_date(parts[0])
    if len(parts) > 2 or (len(parts) == 2 and not re.fullmatch(r'(?:\d+|(?:legacy|cms)-[a-f0-9]{12})', parts[1])):
        raise argparse.ArgumentTypeError("use YYYY-MM-DD or YYYY-MM-DD@ISSUE_ID")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="mttd-dl", description="Download MaltaToday, Illum, BusinessToday and other MediaToday "
        "newspaper PDFs (no login).", epilog="Start with: mttd-dl --easy")
    parser.add_argument("--version", action="version", version=f"mttd-dl {__version__}")
    parser.add_argument("--easy", action="store_true", help="interactive step-by-step guide")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("collections", aliases=["archives"],
                        help="show collection names and IDs for download --collection")
    listing = commands.add_parser("list", help="list issue dates available in the combined archives")
    listing.add_argument("periods", nargs="*", type=period, metavar="YYYY[-MM]", help="filter")
    listing.add_argument("--month", type=month, help=argparse.SUPPRESS)
    listing.add_argument("--json", action="store_true", help="print dates and issue metadata URLs as JSON")
    download = commands.add_parser(
        "download", help="download by year (issue year), month, date or range; resumes automatically")
    add_selection(download)
    download.add_argument("--collection", "--collections", "--archive", "--archives", dest="archive",
                          nargs="+", default=[], metavar="NAME_OR_ID",
                          help="download whole Uberflip collections, including undated issues; 'all' for every one")
    legacy = download.add_mutually_exclusive_group()
    legacy.add_argument('--gap-only', action='store_true', help='collect daily web articles from June 2010 through September 2013 only')
    legacy.add_argument('--legacy-only', action='store_true', help='collect only HTML collections (including 2010–2013 daily articles)')
    legacy.add_argument('--no-legacy', action='store_true', help='disable automatic HTML reconstruction')
    download.add_argument('--legacy-url', action='append', default=[], metavar='ISSUE_INDEX_URL',
                          help='collect a specific dated legacy issue index (repeatable; replaces period selection)')
    download.add_argument('--no-wayback', action='store_true', help='disable public Wayback fallback for legacy sources')
    add_pacing(download, delay=2, retry_missing=False)
    review = commands.add_parser("review", help="show files needing inspection and unavailable dates")
    review.add_argument("--json", action="store_true", help="print full review records as JSON")
    review.add_argument("--all", action="store_true", help="include unavailable/failed records")
    recheck = commands.add_parser("recheck", help="recheck pending local PDFs without downloading")
    accept = commands.add_parser("accept", help="accept an issue after checking its printed date")
    accept.add_argument("date", type=review_key, metavar="DATE[@ISSUE_ID]")
    accept.add_argument("--yes", action="store_true", help="confirm you have inspected the PDF's printed date")
    for command in (listing, download):
        command.add_argument("--refresh", action="store_true", help="refresh all archive listings (cached for 6 hours)")
    for command in (download, review, accept, recheck):
        add_destination(command, library_layout="PATH/editions/YYYY/")
    return parser


DONE = ("verified", "verified_pdf", "accepted_by_user", "reconstructed")


def read_review(root: Path, *, flat=False, everything=True) -> dict:
    """Read state without creating directories or acquiring a writer lock."""
    state = root.expanduser() / ".mttd-dl" / "state.json"
    rows = json.loads(state.read_text()) if state.exists() else {}
    hidden = DONE if everything else DONE + ("unavailable", "failed")
    return {date: row for date, row in sorted(rows.items()) if row["status"] not in hidden}


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.easy:
        if args.command:
            parser.error("Use --easy by itself, without a subcommand.")
        from .easy import run_easy

        return run_easy(main)
    if args.command is None:
        parser.print_help()
        return 2
    if args.command in ("collections", "archives"):
        for archive_id, name in ARCHIVE_NAMES.items():
            print(f"{archive_id}\t{name}")
        return 0
    if args.command == "download":
        check_selection(parser, args, required=False)
        if args.from_date or len(args.date) > 1:
            try:
                args.dates = expand_dates(args)
            except ValueError as exc:
                parser.error(str(exc))
            args.date = None
        else:
            args.dates = None
            args.date = args.date[0] if args.date else None
        args.months = list(args.periods)
        if sum((bool(args.months or args.years), bool(args.date or args.dates), bool(args.archive), bool(args.legacy_url))) != 1:
            parser.error("Choose one selection: YYYY[-MM]/--year, --date/--from, --collection, or --legacy-url.")
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
                    parser.error(f"Unknown collection {exc.args[0]!r}; run 'mttd-dl collections'.")
        args.selected_years = sorted(set(args.years + [p for p in args.months if len(p) == 4]))
        args.explicit_months = sorted({p for p in args.months if len(p) == 7 and p[:4] not in args.selected_years})
        args.months = expand_periods(args.months + args.years)
    root, flat = destination(args)
    try:
        if args.command == "list":
            filters = list(args.periods) + ([args.month] if args.month else [])
            entries = discover(refresh=args.refresh, emit=lambda line: print(line, file=sys.stderr))
            entries = {
                date: urls
                for date, urls in sorted(entries.items())
                if not filters or any(date.startswith(f) for f in filters)
            }
            if args.json:
                print(json.dumps(entries, indent=2))
            else:
                for date, urls in entries.items():
                    print(f"{date}\t{len(urls)} issue link(s)")
            return 0
        if args.command == "review":
            rows = read_review(root, flat=flat, everything=args.all)
            if args.json:
                print(json.dumps({k: {f: v for f, v in r.items() if f != "front_text"} for k, r in rows.items()}, indent=2))
            elif not rows:
                print("No issues need review.")
            else:
                for date, row in rows.items():
                    print(f"{date}\t{row['status']}\t{row.get('path') or 'No PDF'}")
                    print(f"  Detected dates: {', '.join(row.get('detected_dates', [])) or 'none'}")
            return 0
        if args.command == "recheck":
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
            if row.get('source_type') in ('legacy_html', 'cms_html') and not args.yes:
                print("This is a reconstructed collection: check its missing-content warnings first.")
            if not confirm_accept(row['path'], args.date, row.get('detected_dates', []), yes=args.yes, err=_err):
                return 1
            Library(root, flat=flat).accept(args.date)
            print(f"Accepted {args.date}.")
            return 0
        library = Library(root, flat=flat, refresh=args.refresh, dry_run=args.dry_run, retries=args.retries)
        legacy_result = 0
        emit = lambda line: print(line, flush=True)  # noqa: E731
        try:
            with interruptible() as stop:
                if args.legacy_only or args.gap_only or args.legacy_url:
                    pass
                elif args.archive:
                    library.run_archives(args.archive, args.delay, stop, emit)
                elif args.selected_years:
                    library.run_periods(args.selected_years, args.explicit_months, args.delay, stop, emit)
                elif args.date:
                    library.run_issue(args.date, stop, emit, delay=args.delay)
                elif args.dates:
                    library.run(months_of(args.dates), args.delay, stop, emit, only_dates=set(args.dates))
                    if args.dates[0] < '2013-10-01' and not args.no_legacy:
                        emit("Note: issues rebuilt from web archives (before October 2013) are collected for "
                             "YYYY[-MM] selections or a single --date, not for --from/--to or several --date.")
                else:
                    library.run(sorted(set(args.months)), args.delay, stop, emit)
                if args.dry_run:
                    early = ((args.dates or [args.date or "9999"])[0] < "2013-10"
                             or any(p < "2013-10" for p in args.months + args.years))
                    if early and not (args.archive or args.no_legacy):
                        emit("Dry run: web-archive reconstruction (before October 2013) is not previewed.")
                    return 0
                if args.dates:
                    args.no_legacy = True
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
            return EXIT_INTERRUPTED
        if stop.is_set():
            return EXIT_INTERRUPTED
        if legacy_result:
            return legacy_result
        if args.date and library.rows.get(args.date, {}).get("status") == "unavailable" and not any(
            k.startswith(args.date + '@') and r.get('path') for k, r in library.rows.items()):
            return 4
        # Missing feed dates are informational: the paper is not published every day.
        if any(
            row["status"] in {"wrong_date", "uncertain", "ambiguous_feed"}
            for date, row in library.rows.items()
            if (date in library.selected_keys if args.archive or args.selected_years or args.dates else
                date[:10] == args.date if args.date else date[:7] in args.months)
        ):
            print("Some PDFs need review: mttd-dl review" + (f" --library {args.library}" if args.library
                  else f" -P {args.output_dir}" if args.output_dir else ""))
            return EXIT_REVIEW
        return 0
    except KeyboardInterrupt:
        return 130
    except Exception as exc:
        print(f"mttd-dl: {exc}", file=sys.stderr)
        return 1
