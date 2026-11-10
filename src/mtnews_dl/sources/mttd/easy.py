"""mttd-dl --easy: a guided way to build and run an mttd-dl command."""

from ...common.easy import (ask_dates, ask_delay, ask_destination, choice, confirm_and_run, guard,
                            validated)
from .catalog import ARCHIVE_NAMES


def collections(value):
    lookup = {str(i): str(i) for i in ARCHIVE_NAMES}
    lookup.update({name.lower(): str(i) for i, name in ARCHIVE_NAMES.items()})
    values = value.replace(",", " ").split()
    if values == ["all"]:
        return ["all"]
    if not values or any(v.lower() not in lookup for v in values):
        raise ValueError("enter names or IDs from the list, or 'all'")
    return [lookup[v.lower()] for v in values]


def run_easy(run_command):
    def body():
        print("Downloads combine the MediaToday PDF archives with older issues rebuilt from web "
              "archives (before October 2013). No login is needed.")
        action = choice("What would you like? [D]ownload by date / [C]ollection(s) / [L]ist available "
                        "issues [D]: ", {"d": "dates", "c": "collections", "l": "list"}, "d")
        if action == "list":
            return run_command(["list"])
        if action == "collections":
            for archive_id, name in ARCHIVE_NAMES.items():
                print(f"  {archive_id}: {name}")
            chosen = validated("Collection names/IDs (space separated), or all: ", collections)
            args, description = ["download", "--collection", *chosen], f"Collections: {', '.join(chosen)}"
        else:
            dates, description = ask_dates()
            args = ["download", *dates]
            if any(a[:4].isdigit() and a < "2013-10" for a in dates):
                print("June 2010–September 2013 web articles are grouped into one searchable PDF per "
                      "day; the first scan can take hours (progress is cached). These are not printed issues.")
                source = choice("Sources: [A]ll / [H]TML reconstruction only / [P]DFs only [A]: ",
                                {"a": "", "h": "--legacy-only", "p": "--no-legacy"}, "a")
                if source:
                    args.append(source)
        dest_args, dest_text = ask_destination()
        delay_args, delay_text = ask_delay(2)
        result = confirm_and_run("mttd-dl", args + dest_args + delay_args,
                                 "\n".join((description, dest_text, delay_text)), run_command)
        if result == 3:
            print("Some issues need review: mttd-dl review (in the destination folder).")
        elif result == 4:
            print("Nothing found for that date: mttd-dl list shows available dates.")
        return result
    return guard("mttd-dl", body)
