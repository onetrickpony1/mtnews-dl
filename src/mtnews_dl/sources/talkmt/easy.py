"""Guided terminal mode that builds and runs an ordinary talkmt-dl command."""

import argparse
import datetime as dt
import os
import shlex
import subprocess
import sys
from pathlib import Path

from ...common.cliargs import delay_seconds, issue_date, period, year


class Cancelled(Exception):
    pass


def answer(prompt, default=""):
    value = input(prompt).strip()
    if value.lower() in ("q", "quit"):
        raise Cancelled
    return value or default


def choice(prompt, options, default):
    while True:
        value = answer(prompt, default).lower()
        if value in options:
            return options[value]
        print("Please choose one of the letters shown (or Q to quit).")


def validated(prompt, validator, default=""):
    while True:
        value = answer(prompt, default)
        try:
            return validator(value)
        except (ValueError, argparse.ArgumentTypeError) as exc:
            print(f"Please try again: {exc}")


def many(validator, example):
    def check(value):
        values = value.replace(",", " ").split()
        if not values:
            raise ValueError(f"enter at least one, for example {example}")
        return sorted({validator(v) for v in values})
    return check


def folder(value):
    if not value:
        raise ValueError("enter a folder path")
    path = Path(value).expanduser().resolve()
    if path.exists() and not path.is_dir():
        raise ValueError("that path is a file, not a folder")
    return path


def cookie_file(value):
    path = Path(value).expanduser()
    if not path.is_file():
        raise ValueError(f"no file at {path}")
    return path.resolve()


def render(args):
    command = ["talkmt-dl", *map(str, args)]
    return subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)


def auth_step(run_command):
    """Make sure a session is stored. Asks for a file path, never for cookie contents."""
    from .auth import COOKIE_HELP, offline_status
    status = offline_status()
    if status["state"] == "ok":
        expiry = status.get("login_cookie_expires")
        print(f"Session: stored ({status['cookies']} cookies"
              + (f", login cookie valid until {expiry}" if expiry else "") + "). It is checked online before downloading.")
        if choice("Use it? [Y]es / [R]eplace with a new cookies file [Y]: ",
                  {"y": True, "yes": True, "r": False}, "y"):
            return True
    else:
        print({"missing": "No session stored yet.", "expired": "The stored login has expired.",
               "no_login_cookie": "The stored cookies contain no login cookie.",
               "unreadable": "The stored cookie file cannot be read."}.get(status["state"], status["state"]))
        print(COOKIE_HELP)
    path = validated("Path to your exported cookies.txt (Enter to stop and do it later): ",
                     lambda v: cookie_file(v) if v else None)
    if path is None:
        raise Cancelled
    args = ["auth", "--cookies", str(path)]
    print(f"Equivalent command: {render(args)}")
    if run_command(args) != 0:
        print("The session could not be confirmed. Nothing downloaded.")
        return False
    return True


def run_easy(run_command):
    if not sys.stdin.isatty():
        print("talkmt-dl --easy needs an interactive terminal; see 'talkmt-dl --help'.", file=sys.stderr)
        return 1
    try:
        print("Welcome to talkmt-dl. Press Enter for the default shown in [brackets]; Q quits.")
        print("Step 1/4 - your subscribe.mt session")
        if not auth_step(run_command):
            return 5
        print("Step 2/4 - publication")
        pub = choice("[A]ll / [L]-orizzont / [T]orċa / [S]upplements [A]: ",
                     {"a": "all", "l": "l-orizzont", "t": "it-torca", "s": "supplements"}, "a")
        print("Step 3/4 - dates")
        mode = choice("[D]ate / [M]onth(s) / [Y]ear(s) / [R]ange / [C]urrently listed on the page [D]: ",
                      {"d": "date", "m": "months", "y": "years", "r": "range", "c": "listed"}, "d")
        today = dt.date.today()
        args = ["download"]
        if mode == "date":
            value = validated(f"Issue date YYYY-MM-DD [{today}]: ", issue_date, today.isoformat())
            args += ["--date", value]
            what = f"issue date {value}"
        elif mode == "months":
            values = validated(f"Month(s) YYYY-MM [{today:%Y-%m}]: ", many(period, "2026-10"),
                               f"{today:%Y-%m}")
            args += values
            what = "month(s) " + ", ".join(values)
        elif mode == "years":
            values = validated(f"Year(s) YYYY [{today.year}]: ", many(year, "2025"), str(today.year))
            args += ["--year", *values]
            what = "year(s) " + ", ".join(values) + " (at least one request per date and paper; more when a filename format misses)"
        elif mode == "range":
            first = validated("From YYYY-MM-DD: ", issue_date)
            while True:
                last = validated(f"To YYYY-MM-DD [{today}]: ", issue_date, today.isoformat())
                if last >= first:
                    break
                print("Please try again: the end must not be before the start.")
            args += ["--from", first, "--to", last]
            what = f"{first} to {last}"
        else:
            args += ["--listed"]
            what = "everything currently linked on the e-Newspapers page"
        if pub != "all":
            args += ["--publication", pub]
        print("Step 4/4 - destination")
        here = choice(f"Save PDFs directly in {Path.cwd()}? [Y/n]: ",
                      {"y": True, "yes": True, "n": False, "no": False}, "y")
        dest = Path.cwd() if here else validated("Destination folder: ", folder)
        if not here:
            args += ["--output-dir", str(dest)]
        if mode in ("months", "years", "range"):
            delay = validated("Seconds between requests (1–60) [3]: ", delay_seconds, "3")
            args += ["--delay", f"{delay:g}"]
        print(f"\nDownload: {what}\nPublication: {pub}\nDestination: {dest}\n"
              "Verified PDFs are skipped on resume; uncertain ones stay in .talkmt-dl/pending for review.")
        print(f"Equivalent command: {render(args)}")
        if not choice("Proceed? [y/N]: ", {"y": True, "yes": True, "n": False, "no": False}, "n"):
            print("Cancelled. Nothing downloaded.")
            return 0
        result = run_command(args)
        if result == 3:
            print("Some PDFs need review: run 'talkmt-dl review' in the destination folder.")
        elif result == 4:
            print("Nothing was found for that date (no issue, or not published that day).")
        return result
    except (Cancelled, EOFError):
        print("\nCancelled. Nothing downloaded.")
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
