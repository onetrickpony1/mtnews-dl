"""Building blocks for the --easy guides: every question has a default (Enter) and Q quits.
The guides only assemble an ordinary command line, show it, and run it after confirmation."""

import argparse
import datetime as dt
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .cliargs import delay_seconds, issue_date, period


class Cancelled(Exception):
    pass


def answer(prompt, default=""):
    value = input(prompt).strip()
    if value.lower() in {"q", "quit"}:
        raise Cancelled
    return value or default


def choice(prompt, options, default):
    while True:
        value = answer(prompt, default).lower()
        if value in options:
            return options[value]
        print("Please choose one of the letters shown (or Q to quit).")


def yes(prompt, default=True):
    return choice(prompt + (" [Y/n]: " if default else " [y/N]: "),
                  {"y": True, "yes": True, "n": False, "no": False}, "y" if default else "n")


def validated(prompt, validator, default=""):
    while True:
        value = answer(prompt, default)
        try:
            return validator(value)
        except (ValueError, argparse.ArgumentTypeError) as exc:
            print(f"Please try again: {exc}")


def periods(value):
    values = value.replace(",", " ").split()
    if not values:
        raise ValueError("Enter at least one year or month, for example 2025 or 2026-10.")
    return sorted({period(item) for item in values})


def folder(value):
    if not value:
        raise ValueError("Enter a folder path.")
    path = Path(value).expanduser().resolve()
    if path.exists() and not path.is_dir():
        raise ValueError("That path is a file, not a folder.")
    return path


def ask_dates():
    """Returns (args, description) for the date selection."""
    today = dt.date.today()
    kind = choice("Which issues? [D]ate / [M]onths or years / [R]ange of dates [M]: ",
                  {"d": "date", "m": "periods", "r": "range"}, "m")
    if kind == "date":
        value = validated(f"Issue date (YYYY-MM-DD) [{today.isoformat()}]: ", issue_date, today.isoformat())
        return ["--date", value], f"Issue: {value}"
    if kind == "range":
        first = validated("First date (YYYY-MM-DD): ", issue_date)
        last = validated(f"Last date (YYYY-MM-DD) [{today.isoformat()}]: ", issue_date, today.isoformat())
        if last < first:
            first, last = last, first
        return ["--from", first, "--to", last], f"Dates: {first} to {last}"
    default = today.strftime("%Y-%m")
    values = validated(f"Years and/or months, separated by spaces (e.g. 2025 2026-10) [{default}]: ",
                       periods, default)
    return values, f"Periods: {', '.join(values)}"


def ask_destination(layout_note="PDFs are saved directly in this folder."):
    here = Path.cwd()
    if yes(f"Save PDFs in {here}?"):
        return [], f"Destination: {here}\n{layout_note}"
    path = validated("Destination folder: ", folder)
    return ["-P", str(path)], f"Destination: {path}\n{layout_note}"


def ask_delay(default):
    value = validated(f"Seconds between requests (1–60) [{default:g}]: ", delay_seconds, str(default))
    return (["--delay", f"{value:g}"] if value != default else []), f"Delay: {value:g} s"


def confirm_and_run(prog, args, description, run_command):
    command = [prog, *args]
    rendered = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
    print(f"\n{description}\nEquivalent command: {rendered}")
    if not yes("Start now?", default=False):
        print("Cancelled. Nothing downloaded.")
        return 0
    return run_command(args)


def guard(prog, body):
    """Run an --easy guide body with the usual terminal checks and cancellation handling."""
    if not sys.stdin.isatty():
        print(f"{prog} --easy needs an interactive terminal. Use '{prog} --help' for scripting.",
              file=sys.stderr)
        return 1
    try:
        print(f"Welcome to {prog}! Press Enter for the default shown in [brackets]; Q quits.")
        return body()
    except (Cancelled, EOFError):
        print("\nCancelled. Nothing downloaded.")
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
