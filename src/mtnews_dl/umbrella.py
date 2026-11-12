"""mtnews-dl: one command for every Maltese newspaper source.

  mtnews-dl sources                      what each source covers and whether it is ready
  mtnews-dl download 2026-10             every ready source, into the current folder
  mtnews-dl download --year 2025 --root ~/News         ->  ToM/2025, MTTD/2025, TALKMT/2025
  mtnews-dl download --date 2026-11-03 -s tom talkmt
  mtnews-dl review | recheck             across all sources
  mtnews-dl doctor [--fix]               check (and install) what is needed
  mtnews-dl --easy                       guided

Each source is the matching *-dl command run with the same options, so anything shown here
can also be done with tom-dl, mttd-dl or talkmt-dl directly.
"""

import argparse
import datetime as dt
import sys
from dataclasses import dataclass
from pathlib import Path

from . import __version__
from .common.cliargs import (EXIT_AUTH, EXIT_ERROR, EXIT_INTERRUPTED, EXIT_NOTHING, EXIT_OK,
                             EXIT_REVIEW, EXIT_STOPPED, add_selection, check_selection,
                             delay_seconds, expand_dates, printer, was_interrupted)

_err = printer("mtnews-dl")


@dataclass(frozen=True)
class Source:
    key: str
    tool: str
    folder: str           # subfolder name under --root / --library
    papers: str
    login: str            # "" when none is needed

    def main(self):
        if self.key == "tom":
            from .sources.tom.cli import main
        elif self.key == "mttd":
            from .sources.mttd.cli import main
        else:
            from .sources.talkmt.cli import main
        return main

    def ready(self):
        """(ready, note) without network access."""
        if self.key == "talkmt":
            from .sources.talkmt.auth import offline_status
            status = offline_status()
            if status.get("state") == "ok":
                return True, "session stored"
            return False, "no session: mtnews-dl auth talkmt --cookies FILE"
        return True, "no login needed"


SOURCES = {
    "tom": Source("tom", "tom-dl", "ToM", "Times of Malta, The Sunday Times of Malta (e-paper "
                  "2024-11-19 on; older issues with --archive)", "archive only: premium, via Chrome"),
    "mttd": Source("mttd", "mttd-dl", "MTTD", "MaltaToday, Illum, BusinessToday and other MediaToday "
                   "titles (web-archive rebuilds before Oct 2013)", ""),
    "talkmt": Source("talkmt", "talkmt-dl", "TALKMT", "l-orizzont, it-Torca, supplements (subscribe.mt)",
                     "subscribe.mt subscription: cookies file"),
}
ALIASES = {"tom": "tom", "times": "tom", "timesofmalta": "tom", "tom-dl": "tom",
           "mttd": "mttd", "maltatoday": "mttd", "mediatoday": "mttd", "mttd-dl": "mttd",
           "talkmt": "talkmt", "talk": "talkmt", "orizzont": "talkmt", "torca": "talkmt",
           "talkmt-dl": "talkmt", "all": "all"}
# when several sources finish differently, report the most serious outcome
SEVERITY = [EXIT_INTERRUPTED, EXIT_AUTH, EXIT_STOPPED, EXIT_ERROR, EXIT_USAGE := 2, EXIT_REVIEW,
            EXIT_NOTHING, EXIT_OK]


def source_name(value):
    key = ALIASES.get(value.lower())
    if not key:
        raise argparse.ArgumentTypeError("choose tom, mttd, talkmt or all")
    return key


def build_parser():
    parser = argparse.ArgumentParser(
        prog="mtnews-dl", description="Download Maltese newspaper PDFs from every supported source "
        "with one command.", epilog="Start with: mtnews-dl --easy   ·   Check setup: mtnews-dl doctor")
    parser.add_argument("--version", action="version", version=f"mtnews-dl {__version__}")
    parser.add_argument("--easy", action="store_true", help="interactive step-by-step guide")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND")
    commands.add_parser("sources", help="list sources, what they cover and whether they are ready")

    download = commands.add_parser("download", help="download from every ready source (or -s ...)")
    add_selection(download)
    _source_options(download)
    download.add_argument("--archive", action="store_true",
                          help="tom: use the Times of Malta digital archive (Chrome) for older dates")
    download.add_argument("--delay", type=delay_seconds, help="seconds between requests (each tool's default otherwise)")
    download.add_argument("--retry-missing", action="store_true", help="tom, talkmt: ask again for dates recorded as missing")
    download.add_argument("--dry-run", action="store_true", help="show what would be downloaded")

    for name, text in (("review", "PDFs needing your review, across sources"),
                       ("recheck", "re-check pending PDFs across sources (no network)")):
        cmd = commands.add_parser(name, help=text)
        _source_options(cmd)

    auth = commands.add_parser("auth", help="store the cookies a subscription source needs (talkmt)")
    auth.add_argument("source", type=source_name, metavar="SOURCE")
    auth.add_argument("rest", nargs=argparse.REMAINDER, help="passed on, e.g. --cookies FILE or --status")
    commands.add_parser("connect", help="set up Chrome for the Times of Malta archive (tom-dl connect)")
    doctor = commands.add_parser("doctor", help="check that everything needed is installed and ready")
    doctor.add_argument("--fix", action="store_true", help="download missing OCR language data")
    return parser


def _source_options(cmd):
    cmd.add_argument("-s", "--source", dest="sources", nargs="+", action="extend", type=source_name,
                     default=[], metavar="SOURCE", help="tom, mttd, talkmt or all (default: all ready)")
    dest = cmd.add_mutually_exclusive_group()
    dest.add_argument("-P", "--output-dir", type=Path, help="every source's PDFs in this folder (default: here)")
    dest.add_argument("--root", type=Path, help="ROOT/<ToM|MTTD|TALKMT>/<YYYY>/ (one folder per source and year)")
    dest.add_argument("--library", type=Path, help="ROOT/<ToM|MTTD|TALKMT>/ as each tool's structured library")


def chosen_sources(args, *, for_download):
    explicit = [s for s in args.sources if s != "all"]
    if "all" in args.sources or not explicit:
        picked, explicit_choice = list(SOURCES), False
    else:
        picked, explicit_choice = list(dict.fromkeys(explicit)), True
    result = []
    for key in picked:
        src = SOURCES[key]
        ready, note = src.ready()
        if for_download and not ready and not explicit_choice:
            print(f"[{src.tool}] skipped: {note}")
            continue
        result.append(src)
    return result


def year_chunks(args):
    """[(year or None, selection argv)] — per year with --root, else one chunk."""
    if not args.root:
        sel = [*args.periods]
        if args.years:
            sel += ["--year", *args.years]
        for d in args.date:
            sel += ["--date", d]
        if args.from_date:
            sel += ["--from", args.from_date] + (["--to", args.to_date] if args.to_date else [])
        return [(None, sel)]
    chunks = {}
    for value in list(args.periods) + list(args.years):
        chunks.setdefault(value[:4], []).append(value)
    for d in args.date:
        chunks.setdefault(d[:4], []).extend(["--date", d])
    if args.from_date:
        dates = expand_dates(argparse.Namespace(periods=[], years=[], date=[], from_date=args.from_date,
                                                to_date=args.to_date))
        for year in sorted({d[:4] for d in dates}):
            first = max(dates[0], f"{year}-01-01")
            last = min(dates[-1], f"{year}-12-31")
            chunks.setdefault(year, []).extend(["--from", first, "--to", last])
    return sorted(chunks.items())


def destination_for(args, src, year):
    if args.root:
        return ["-P", str(args.root.expanduser() / src.folder / year)]
    if args.library:
        return ["--library", str(args.library.expanduser() / src.folder)]
    if args.output_dir:
        return ["-P", str(args.output_dir.expanduser())]
    return []


def worst(codes):
    codes = [c if c in SEVERITY else EXIT_ERROR for c in codes] or [EXIT_OK]
    return min(codes, key=SEVERITY.index)


def run_tool(src, argv):
    print(f"\n[{src.tool}] {src.tool} {' '.join(argv)}", flush=True)
    try:
        code = src.main()(argv)
    except SystemExit as exc:          # argparse errors inside a tool
        code = exc.code if isinstance(exc.code, int) else EXIT_ERROR
    print(f"[{src.tool}] finished (exit {code}).", flush=True)
    return code


def cmd_download(args, parser):
    check_selection(parser, args)
    try:
        chunks = year_chunks(args)
    except ValueError as exc:
        parser.error(str(exc))
    codes = []
    for src in chosen_sources(args, for_download=True):
        for year, selection in chunks:
            argv = ["download", *selection, *destination_for(args, src, year)]
            if args.delay:
                argv += ["--delay", f"{args.delay:g}"]
            if args.dry_run:
                argv.append("--dry-run")
            if args.retry_missing and src.key in ("tom", "talkmt"):
                argv.append("--retry-missing")
            if args.archive and src.key == "tom":
                argv.append("--archive")
            code = run_tool(src, argv)
            codes.append(code)
            if code == EXIT_INTERRUPTED or was_interrupted():
                print("\nStopped (Ctrl+C); the remaining sources and years were not started.")
                return EXIT_INTERRUPTED
    result = worst(codes)
    print(f"\nAll sources done (exit {result}).")
    return result


def cmd_each(args, command):
    codes = []
    for src in chosen_sources(args, for_download=False):
        if args.root:
            base = args.root.expanduser() / src.folder
            years = sorted(p.name for p in base.iterdir() if p.is_dir() and p.name.isdigit()) if base.is_dir() else []
            targets = [["-P", str(base / y)] for y in years]
        else:
            targets = [destination_for(args, src, None)]
        for target in targets:
            codes.append(run_tool(src, [command, *target]))
            if was_interrupted():
                return EXIT_INTERRUPTED
    return worst(codes)


def cmd_sources(_args):
    for src in SOURCES.values():
        ready, note = src.ready()
        print(f"{src.key:7} {src.tool:10} {'ready' if ready else 'NOT READY':9} {note}")
        print(f"        {src.papers}")
        if src.login:
            print(f"        login: {src.login}")
    return 0


def main(argv=None):
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
        parser.print_help()
        return 2
    try:
        if args.command == "sources":
            return cmd_sources(args)
        if args.command == "download":
            return cmd_download(args, parser)
        if args.command in ("review", "recheck"):
            return cmd_each(args, args.command)
        if args.command == "auth":
            if args.source != "talkmt":
                print(f"{args.source}: no cookies needed" + (" (the archive uses Chrome: mtnews-dl connect)"
                                                            if args.source == "tom" else "."))
                return 0
            return SOURCES["talkmt"].main()(["auth", *(args.rest or ["--status"])])
        if args.command == "connect":
            return SOURCES["tom"].main()(["connect"])
        if args.command == "doctor":
            from .doctor import doctor
            return doctor(fix=args.fix)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return 2


if __name__ == "__main__":
    sys.exit(main())
