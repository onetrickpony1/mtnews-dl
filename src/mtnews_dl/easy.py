"""mtnews-dl --easy: pick sources, dates and a folder; shows and runs the equivalent command."""

from pathlib import Path

from .common.easy import (ask_dates, ask_delay, choice, confirm_and_run, folder, guard, validated,
                          yes)


def run_easy(run_command):
    from .umbrella import SOURCES

    def body():
        print("Sources:")
        ready = []
        for src in SOURCES.values():
            ok, note = src.ready()
            print(f"  {src.key:7} {src.papers}  [{'ready' if ok else note}]")
            if ok:
                ready.append(src.key)
        action = choice("What would you like? [D]ownload / [S]et up talkmt cookies / [C]onnect Chrome "
                        "(Times of Malta archive) / [H]ealth check [D]: ",
                        {"d": "download", "s": "auth", "c": "connect", "h": "doctor"}, "d")
        if action == "auth":
            path = validated("Path of the cookies.txt you exported from subscribe.mt: ",
                             lambda v: str(Path(v).expanduser()) if Path(v).expanduser().is_file()
                             else (_ for _ in ()).throw(ValueError("file not found")))
            return run_command(["auth", "talkmt", "--cookies", path])
        if action in ("connect", "doctor"):
            return run_command([action])
        default = " ".join(ready)
        picked = validated(f"Sources (space separated) [{default}]: ",
                           lambda v: [s for s in v.replace(",", " ").split() if s in SOURCES]
                           or (_ for _ in ()).throw(ValueError("choose from: " + ", ".join(SOURCES))),
                           default)
        args, description = ask_dates()
        args = ["download", *args, "-s", *picked]
        description += f"\nSources: {', '.join(picked)}"
        if "tom" in picked and yes("Use the Times of Malta archive (premium, Chrome) for older dates?", False):
            args.append("--archive")
        layout = choice("Folders: [O]ne folder for everything / [S]ource and year subfolders "
                        "(ToM/2025, MTTD/2025, …) [S]: ", {"o": "one", "s": "root"}, "s")
        where = validated(f"Folder [{Path.cwd()}]: ", folder, str(Path.cwd()))
        args += ["-P" if layout == "one" else "--root", str(where)]
        description += f"\nDestination: {where}" + ("" if layout == "one" else " (ToM/YYYY, MTTD/YYYY, TALKMT/YYYY)")
        delay_args, delay_text = ask_delay(3)
        return confirm_and_run("mtnews-dl", args + delay_args, f"{description}\n{delay_text}", run_command)
    return guard("mtnews-dl", body)
