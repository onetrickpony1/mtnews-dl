"""tom-dl --easy: a guided way to build and run a tom-dl command."""

from ...common.easy import (ask_dates, ask_delay, ask_destination, choice, confirm_and_run, guard,
                            yes)
from .library import EPAPER_START


def run_easy(run_command):
    def body():
        print(f"E-paper issues from {EPAPER_START} need no login. Older issues come from the digital "
              "archive (premium subscription, through your Chrome).")
        action = choice("What would you like? [D]ownload issues / [L]ist available e-paper dates / "
                        "[C]onnect Chrome for the archive [D]: ",
                        {"d": "download", "l": "list", "c": "connect"}, "d")
        if action == "list":
            return run_command(["list"])
        if action == "connect":
            return run_command(["connect"])
        args, description = ask_dates()
        args = ["download", *args]
        if yes("Also use the archive (Chrome, premium) for dates without an e-paper PDF?", default=False):
            args.append("--archive")
            description += "\nArchive: yes (needs `tom-dl connect` done once and the connection tab open)"
        dest_args, dest_text = ask_destination()
        delay_args, delay_text = ask_delay(2)
        print("Verified PDFs already saved are skipped; doubtful ones are kept for review.")
        result = confirm_and_run("tom-dl", args + dest_args + delay_args,
                                 "\n".join((description, dest_text, delay_text)), run_command)
        if result == 3:
            print("Some issues need review: tom-dl review (in the destination folder).")
        elif result == 5:
            print("The archive needs Chrome: run tom-dl connect.")
        return result
    return guard("tom-dl", body)
