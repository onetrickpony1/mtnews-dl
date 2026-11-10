"""Command-line interface for talkmt-dl."""

import argparse
import datetime as dt
import json
import re
import signal
import sys
import threading
from pathlib import Path

from ... import __version__
from ...common.cliargs import (EXIT_AUTH, EXIT_NOTHING, EXIT_REVIEW, EXIT_STOPPED, add_destination,
                               add_pacing, add_selection, check_selection, delay_seconds,
                               destination, destination_flag, expand_dates, interruptible,
                               issue_date, period, printer)
from .discovery import PAPERS, PUBLICATIONS

PUB_ALIASES = {"l-orizzont": "l-orizzont", "orizzont": "l-orizzont", "lorizzont": "l-orizzont",
               "it-torca": "it-torca", "torca": "it-torca", "it-torċa": "it-torca", "torċa": "it-torca",
               "supplements": "supplements", "supplement": "supplements", "all": "all"}

def publication(value: str) -> str:
    key = value.strip().lower()
    if key not in PUB_ALIASES:
        raise argparse.ArgumentTypeError("choose l-orizzont, it-torca, supplements or all")
    return PUB_ALIASES[key]


def review_key(value: str) -> str:
    date, sep, slug = value.partition("@")
    if date != "undated":
        issue_date(date)
    if sep and not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,180}\.pdf", slug):
        raise argparse.ArgumentTypeError("use DATE or DATE@FILENAME.pdf as shown by review")
    return value


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="talkmt-dl",
        description="Download your subscribed subscribe.mt e-newspaper PDFs (l-orizzont, it-Torċa, supplements).")
    parser.add_argument("--version", action="version", version=f"talkmt-dl {__version__}")
    parser.add_argument("--easy", action="store_true", help="interactive step-by-step guide")
    commands = parser.add_subparsers(dest="command")

    auth = commands.add_parser("auth", help="store, check or remove your logged-in session cookies")
    group = auth.add_mutually_exclusive_group(required=True)
    group.add_argument("--cookies", type=Path, metavar="FILE",
                       help="import a Netscape cookies.txt exported from your logged-in browser")
    group.add_argument("--status", action="store_true", help="show whether a usable session is stored")
    group.add_argument("--forget", action="store_true", help="delete the stored session")
    group.add_argument("--help-cookies", action="store_true", help="explain how to export cookies")
    auth.add_argument("--offline", action="store_true", help="with --cookies/--status: do not contact the site")

    listing = commands.add_parser("list", help="show the PDFs currently linked on the e-Newspapers page")
    listing.add_argument("--json", action="store_true")

    commands.add_parser("patterns", help="show learned filename patterns and their evidence")

    cov = commands.add_parser("coverage", help="survey which sampled dates are served as PDFs (no downloads)")
    cov.add_argument("--from", dest="from_month", type=period, required=True, metavar="YYYY-MM")
    cov.add_argument("--to", dest="to_month", type=period, metavar="YYYY-MM", help="default: this month")
    cov.add_argument("--every", type=int, default=1, metavar="N", help="sample every Nth month (default 1)")
    cov.add_argument("--publication", "-p", dest="publications", nargs="+", type=publication,
                     default=[], metavar="NAME", help="l-orizzont and/or it-torca (default both)")
    cov.add_argument("--formats", choices=("observed", "all"), default="observed",
                     help="filename candidates per sample (default observed: fewer requests)")
    cov.add_argument("--delay", type=delay_seconds, default=3)
    cov.add_argument("--max-requests", type=int, default=150, help="stop after this many requests (default 150)")
    cov.add_argument("--refresh", action="store_true", help="probe again even if a result is cached")
    cov.add_argument("--report-only", action="store_true", help="print the cached results; no requests")

    download = commands.add_parser("download", help="download by date, month, year or range; resumes automatically")
    add_selection(download, listed_help="download everything currently linked on the e-Newspapers page")
    download.add_argument("--publication", "-p", dest="publications", nargs="+", action="extend",
                          type=publication, default=[], metavar="NAME",
                          help="l-orizzont, it-torca, supplements or all (default: all)")
    download.add_argument("--formats", choices=("all", "observed"), default="all",
                          help="for unlisted paper dates: 'all' (default) tries observed patterns, then "
                               "the bounded fallback date formats before reporting missing; "
                               "'observed' tries learned patterns only (fewer requests)")
    add_pacing(download, delay=3)
    review = commands.add_parser("review", help="show PDFs needing inspection, missing and failed issues")
    review.add_argument("--json", action="store_true")
    review.add_argument("--all", action="store_true", help="include missing/failed/not-entitled records")
    commands.add_parser("recheck", help="re-check pending local PDFs (no network)")
    accept = commands.add_parser("accept", help="accept a pending PDF after checking its printed date")
    accept.add_argument("key", type=review_key, metavar="DATE[@FILENAME.pdf]")
    accept.add_argument("--yes", action="store_true", help="confirm you inspected the printed date")
    for command in (download, review, accept, commands.choices["recheck"]):
        add_destination(command, library_layout="PATH/editions/PUBLICATION/YYYY/")
    return parser


def _destination(args):
    return destination(args)


_err = printer("talkmt-dl")


def _auth_failure(exc) -> int:
    from .auth import COOKIE_HELP
    _err(f"authentication needed: {exc}")
    print("\nNo further requests were made. To fix this:\n" + COOKIE_HELP, file=sys.stderr)
    return EXIT_AUTH


def _session():
    from .auth import load_jar
    from .net import Session
    return Session(load_jar())


def review_rows(root, *, everything=False) -> dict:
    from .library import DONE, read_rows
    rows = read_rows(root)
    hidden = DONE + ("duplicate",) if everything else DONE + ("missing", "not_entitled", "failed", "auth_required", "duplicate")
    return {k: r for k, r in sorted(rows.items()) if r.get("status") not in hidden}


def cmd_auth(args) -> int:
    from . import auth
    from .net import AuthRequired, Session, TalkError
    if args.help_cookies:
        print(auth.COOKIE_HELP)
        return 0
    if args.forget:
        print("Stored session removed." if auth.forget() else "No stored session.")
        return 0
    try:
        if args.cookies:
            info = auth.import_cookies(args.cookies)
            print(f"Stored {info['kept']} subscribe.mt cookie(s) in {info['stored']} (private, mode 600).")
            if info["dropped_other_domains"]:
                print(f"Ignored {info['dropped_other_domains']} cookie(s) for other websites.")
            if info["login_cookie"] == "no_login_cookie":
                print("Warning: no WordPress login cookie found; export while logged in.")
            print("You can now delete the exported file you imported from.")
        else:
            status = auth.offline_status()
            for k, v in status.items():
                print(f"{k}: {v}")
            if status["state"] != "ok":
                print("\n" + auth.COOKIE_HELP)
                return EXIT_AUTH
        if args.offline:
            return 0
        html = auth.check_online(Session(auth.load_jar()))
        from .discovery import parse_listing
        items = parse_listing(html)
        print(f"Online check: logged in; the e-Newspapers page links {len(items)} PDF(s).")
        return 0
    except AuthRequired as exc:
        return _auth_failure(exc)
    except (TalkError, ValueError, OSError) as exc:
        _err(str(exc))
        return 1


def fetch_listing(session, stop):
    from .auth import check_online
    from .discovery import learn, load_patterns, parse_listing, save_patterns
    from .net import with_retries
    html = with_retries(lambda: check_online(session, stop), retries=2, stop=stop,
                        emit=lambda line: print(line, file=sys.stderr))
    items = parse_listing(html)
    from .coverage import load_results
    from .discovery import merge_probe_evidence
    patterns = merge_probe_evidence(learn(load_patterns(), items), load_results())
    save_patterns(patterns)
    return items, patterns


def cmd_list(args) -> int:
    from .net import AuthRequired, TalkError
    try:
        items, _patterns = fetch_listing(_session(), threading.Event())
    except AuthRequired as exc:
        return _auth_failure(exc)
    except TalkError as exc:
        _err(str(exc))
        return 1
    if args.json:
        print(json.dumps(items, indent=2, ensure_ascii=False))
    else:
        for item in sorted(items, key=lambda i: (i["date"] or "", i["publication"])):
            print(f"{item['date'] or 'undated':10}  {item['publication']:11}  {item['slug']}  {item['title']}")
        print(f"{len(items)} linked PDF(s).", file=sys.stderr)
    return 0


def cmd_patterns(_args) -> int:
    from .discovery import load_patterns, verified_templates
    patterns = load_patterns()
    for pub in PUBLICATIONS:
        trusted = set(verified_templates(patterns, pub))
        for template, info in patterns.get(pub, {}).items():
            flag = "used for unlisted dates" if template in trusted else "listed issues only"
            print(f"{pub:11} {template}  [{flag}]")
            print(f"            evidence: {info.get('evidence')}; listing dates: "
                  f"{len(info.get('listing_dates', []))}; probe dates: {len(info.get('probe_dates', []))}; "
                  f"confirmed unlisted: "
                  f"{', '.join(info.get('unlisted_confirmed', [])[-5:]) or 'none'}")
    return 0


def cmd_coverage(args) -> int:
    from .coverage import load_results, month_range, report, sample_date, survey
    from .net import AuthRequired, TalkError
    pubs = [p for p in (args.publications or ["l-orizzont", "it-torca"]) if p != "all"] or ["l-orizzont", "it-torca"]
    if "supplements" in pubs:
        _err("coverage samples dated papers only; supplements follow the listing.")
        return 2
    last = (args.to_month or dt.date.today().strftime("%Y-%m"))[:7]
    first = args.from_month[:7]
    if first > last or args.every < 1:
        _err("--from must not be after --to, and --every must be at least 1")
        return 2
    months = list(month_range(first, last))
    if args.report_only:
        print(report(load_results(), pubs, months, args.every))
        return 0
    samples = [(p, m) for i, m in enumerate(months) if i % args.every == 0 for p in pubs if sample_date(m, p)]
    per = 2 if args.formats == "observed" else 12
    print(f"{len(samples)} sample(s); at most {per} request(s) each, capped at {args.max_requests}; "
          f"delay {args.delay:g}s. Probes read only the first bytes of each file.")
    stop = threading.Event()
    previous = signal.signal(signal.SIGINT, lambda *_: stop.set())
    try:
        session = _session()
        items, patterns = fetch_listing(session, stop)
        counter = {"n": 0}

        def emit(line):
            print(line, flush=True)

        class Budget:
            def __init__(self, session):
                self.session = session

            def get(self, url, **kw):
                if counter["n"] >= args.max_requests:
                    stop.set()
                    raise InterruptedError("request budget reached")
                counter["n"] += 1
                return self.session.get(url, **kw)
        try:
            _results, _n = survey(Budget(session), patterns, items, pubs, months, every=args.every,
                                  formats=args.formats, delay=args.delay, stop=stop, emit=emit,
                                  refresh=args.refresh)
        except InterruptedError:
            print(f"Stopped at the request budget ({args.max_requests}); rerun to continue (results are cached).")
    except AuthRequired as exc:
        return _auth_failure(exc)
    except TalkError as exc:
        _err(str(exc))
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)
    print(f"\nRequests made: {counter['n']}\n")
    print(report(load_results(), pubs, months, args.every))
    return 0


def cmd_review(args) -> int:
    root, _flat = _destination(args)
    rows = review_rows(root, everything=args.all)
    if args.json:
        print(json.dumps({k: {f: v for f, v in r.items() if f != "front_text"} for k, r in rows.items()},
                         indent=2, ensure_ascii=False))
    elif not rows:
        print("No issues need review.")
    else:
        for key, row in rows.items():
            print(f"{key}\t{row.get('status')}\t{row.get('path') or row.get('error') or ''}")
            if row.get("status") in ("uncertain", "wrong_date", "wrong_publication"):
                print(f"  Expected {row.get('issue_date')}; printed dates found: "
                      f"{', '.join(row.get('detected_dates') or []) or 'none'} ({row.get('method')}); "
                      f"publication check: {row.get('publication_check', 'n/a')}")
    return 0


def cmd_accept(args) -> int:
    from .library import Library
    root, flat = _destination(args)
    rows = review_rows(root)
    key = args.key
    if key not in rows:
        matches = [k for k in rows if k.startswith(key + "@") and rows[k].get("path")]
        if len(matches) > 1:
            _err("several pending files for that date; use one of: " + ", ".join(matches))
            return 1
        key = matches[0] if matches else key
    row = rows.get(key)
    if not row or not row.get("path"):
        _err("no pending PDF with that key; run 'talkmt-dl review' first.")
        return 1
    if not args.yes:
        print(f"File: {row['path']}\nExpected date: {row.get('issue_date')}\n"
              f"Detected dates: {row.get('detected_dates')}")
        if not sys.stdin.isatty():
            _err("use --yes only after inspecting the PDF.")
            return 1
        if input("Have you confirmed this PDF is the right issue? [y/N] ").strip().lower() not in ("y", "yes"):
            print("Not accepted.")
            return 1
    dest = Library(root, flat=flat).accept(key)
    print(f"Accepted {key} -> {dest}")
    return 0


def cmd_recheck(args) -> int:
    from .library import Library
    root, flat = _destination(args)
    Library(root, flat=flat).recheck()
    return EXIT_REVIEW if any(r.get("path") for r in review_rows(root).values()) else 0


def cmd_download(args, parser) -> int:
    from .library import REVIEW, Library
    from .net import AuthRequired, Forbidden, NotEntitled, Temporary, TalkError
    check_selection(parser, args)
    pubs = list(dict.fromkeys(args.publications or ["all"]))
    pubs = list(PUBLICATIONS) if "all" in pubs else pubs
    try:
        dates = [] if args.listed else expand_dates(args)
    except ValueError as exc:
        parser.error(str(exc))
    root, flat = _destination(args)
    stop = threading.Event()

    def interrupt(_signum, _frame):
        stop.set()
        print("Stopping after the current network operation…", file=sys.stderr)

    previous = signal.signal(signal.SIGINT, interrupt)
    emit = lambda line: print(line, flush=True)  # noqa: E731
    library = Library(root, flat=flat)
    try:
        session = _session()
        emit("Reading the e-Newspapers page…")
        items, patterns = fetch_listing(session, stop)
        groups = Library.plan(items, patterns, dates, pubs, listed_only=args.listed,
                              formats=args.formats)
        listed = sum(1 for g in groups for c in g["candidates"] if c.get("listed"))
        guessed = sum(1 for g in groups for c in g["candidates"] if not c.get("listed"))
        emit(f"Page lists {len(items)} PDF(s). Planned: {listed} listed file(s) and up to {guessed} "
             f"date-based URL(s) for {len(dates) if dates else 'listed'} date(s); publications: "
             f"{', '.join(pubs)}. Variants after the first are requested only while earlier ones are missing.")
        if "supplements" in pubs and not args.listed and not any(
                g["publication"] == "supplements" for g in groups):
            emit("Note: supplements are only fetched when listed or when a supplement filename pattern "
                 "has been seen on at least two dates.")
        if args.dry_run:
            for g in groups:
                for c in g["candidates"]:
                    print(f"{g['date'] or 'undated'}\t{g['publication']}\t"
                          f"{'listed' if c.get('listed') else c.get('origin', 'pattern')}\t{c['url']}")
            return 0

        def recheck_session():
            from .auth import check_online
            check_online(session, stop)

        summary = library.run(session, groups, delay=args.delay, stop=stop, emit=emit,
                              retries=args.retries, retry_missing=args.retry_missing,
                              recheck_session=recheck_session, patterns=patterns)
        from .discovery import save_patterns
        save_patterns(patterns)
        emit("Summary: " + ", ".join(f"{k} {v}" for k, v in summary.items()))
    except AuthRequired as exc:
        return _auth_failure(exc)
    except InterruptedError:
        return 130
    except (NotEntitled, Forbidden, Temporary) as exc:
        _err(f"{exc}\nProgress is saved; rerun the same command later to resume.")
        return EXIT_STOPPED
    except (TalkError, ValueError, RuntimeError, OSError) as exc:
        _err(str(exc))
        return 1
    finally:
        signal.signal(signal.SIGINT, previous)
    if stop.is_set():
        return 130
    rows = {k: library.rows.get(k, {}) for k in library.selected}
    if any(r.get("status") in REVIEW for r in rows.values()):
        print("Some PDFs need review: talkmt-dl review" + destination_flag(args))
        return EXIT_REVIEW
    if len(args.date) == 1 and not args.periods and not any(
            r.get("status") in ("verified", "accepted_by_user") for r in rows.values()):
        return EXIT_NOTHING
    return 0


def main(argv=None) -> int:
    from mtnews_dl.common.cliargs import safe_console
    safe_console()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.easy:
        if args.command:
            parser.error("use --easy by itself")
        from .easy import run_easy
        return run_easy(main)
    if args.command is None:
        parser.error("choose a command, or use --easy for guided mode")
    try:
        if args.command == "auth":
            if args.offline and not (args.cookies or args.status):
                parser.error("--offline goes with --cookies or --status")
            return cmd_auth(args)
        if args.command == "list":
            return cmd_list(args)
        if args.command == "patterns":
            return cmd_patterns(args)
        if args.command == "coverage":
            return cmd_coverage(args)
        if args.command == "review":
            return cmd_review(args)
        if args.command == "accept":
            return cmd_accept(args)
        if args.command == "recheck":
            return cmd_recheck(args)
        return cmd_download(args, parser)
    except KeyboardInterrupt:
        return 130
    except RuntimeError as exc:  # e.g. library lock held by another process
        _err(str(exc))
        return 1
    except Exception as exc:  # last resort: a short message, not a traceback; state is saved
        _err(f"unexpected error ({exc.__class__.__name__}: {str(exc)[:200]}). Progress is saved; "
             "rerun the same command to resume. Please report this message.")
        return 1


__all__ = ["main", "build_parser", "PAPERS"]
