"""Interactive terminal guide that delegates to the ordinary CLI."""

import argparse
import datetime as dt
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .cli import delay_seconds, download_date, month


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


def validated(prompt, validator, default=""):
    while True:
        value = answer(prompt, default)
        try:
            return validator(value)
        except (ValueError, argparse.ArgumentTypeError) as exc:
            print(f"Please try again: {exc}")


def months(value):
    values = value.replace(",", " ").split()
    if not values:
        raise ValueError("Enter at least one month, for example 2026-10.")
    return sorted({month(item) for item in values})


def output_folder(value):
    if not value:
        raise ValueError("Enter a folder path.")
    path = Path(value).expanduser().resolve()
    if path.exists() and not path.is_dir():
        raise ValueError("That path is a file, not a folder.")
    for parent in path.parents:
        if parent.exists() and not parent.is_dir():
            raise ValueError("A parent path is a file, not a folder.")
    return path


def run_easy(run_command):
    if not sys.stdin.isatty():
        print(
            "tom-dl --easy needs an interactive terminal. Use 'tom-dl --help' for scripting.",
            file=sys.stderr,
        )
        return 1
    try:
        print(
            "Welcome to tom-dl! Press Enter for the shown default; type Q to quit at any question."
        )
        print("Only issues in the publisher's current feed are available.")
        action = choice(
            "What would you like? [S]ingle issue / [M]onth(s) / [L]ist available issues [S]: ",
            {
                "s": "single",
                "single": "single",
                "m": "months",
                "months": "months",
                "l": "list",
                "list": "list",
            },
            "s",
        )
        today = dt.date.today()
        if action == "list":
            selected = validated(
                "Filter by month (YYYY-MM), or Enter for all: ",
                lambda value: month(value) if value else "",
            )
            args = ["list"] + (["--month", selected] if selected else [])
            description = f"List available issues: {selected or 'all dates'}"
        else:
            args = ["download"]
            if action == "single":
                default = today.isoformat()
                selected = validated(
                    f"Issue date (YYYY-MM-DD) [{default}]: ", download_date, default
                )
                args += ["--date", selected]
                description = f"Download one issue: {selected}"
            else:
                default = today.strftime("%Y-%m")
                selected = validated(
                    f"Month(s), separated by spaces or commas (YYYY-MM) [{default}]: ",
                    months,
                    default,
                )
                args += selected
                description = f"Download month(s): {', '.join(selected)}"
            here = choice(
                f"Save PDFs directly in {Path.cwd()}? [Y/n]: ",
                {"y": True, "yes": True, "n": False, "no": False},
                "y",
            )
            folder = Path.cwd() if here else validated("Destination folder: ", output_folder)
            if not here:
                args += ["--output-dir", str(folder)]
            description += f"\nDestination: {folder}\nPDFs will be saved directly in this folder."
            if action == "months":
                delay = validated("Seconds between downloads (1–60) [2]: ", delay_seconds, "2")
                args += ["--delay", str(delay)]
                description += f"\nDelay between issues: {delay:g} seconds"
            print("Existing verified PDFs are skipped. Uncertain files are kept for review.")
        command = ["tom-dl", *args]
        rendered = subprocess.list2cmdline(command) if os.name == "nt" else shlex.join(command)
        print(f"\n{description}\nEquivalent command: {rendered}")
        if not choice("Proceed? [y/N]: ", {"y": True, "yes": True, "n": False, "no": False}, "n"):
            print("Cancelled. Nothing downloaded.")
            return 0
        result = run_command(args)
        if result == 3:
            print("Some issues need review. Use 'tom-dl review' in the destination folder.")
        elif result == 4:
            print(
                "That issue is not in the current feed. Use 'tom-dl --easy' and choose L to list available dates."
            )
        return result
    except (Cancelled, EOFError):
        print("\nCancelled. Nothing downloaded.")
        return 0
    except KeyboardInterrupt:
        print("\nCancelled.")
        return 130
