"""mtnews-dl doctor: is everything ready? With --fix, install what can be installed."""

import os
import platform
import shutil
import sys
import tempfile

from . import __version__
from .common import ocr, system


def _line(ok, label, detail):
    mark = {True: "ok  ", False: "FIX ", None: "info"}[ok]
    print(f"[{mark}] {label}: {detail}")


def doctor(fix=False, interactive=None) -> int:
    interactive = sys.stdin.isatty() if interactive is None else interactive
    problems = 0
    print(f"mtnews-dl {__version__} · Python {platform.python_version()} · {platform.system()} {platform.machine()}")

    try:
        import pypdfium2
        _line(True, "PDF engine", f"pypdfium2 {getattr(pypdfium2, '__version__', '')}".strip())
    except ImportError as exc:
        problems += 1
        _line(False, "PDF engine", f"pypdfium2 missing ({exc}); reinstall mtnews-dl")

    # OCR (optional): engine + English/Maltese language data
    if fix and not ocr.find_tesseract():
        system.install_tesseract(interactive=interactive)
        ocr.engine.cache_clear()
    if fix and ocr.find_tesseract():
        status = ocr.status()
        if "mlt" not in status["langs"] or "eng" not in status["langs"]:
            ocr.install_languages()
    status = ocr.status()
    if not status["binary"]:
        cmd, how = system.tesseract_install_command()
        _line(None, "OCR (optional)", "Tesseract not installed; dates are read from the PDF text, and only "
              "PDFs without a readable date need it. Install: mtnews-dl doctor --fix"
              + (f"  ({' '.join(cmd)})" if cmd else f"  ({how})"))
    elif "eng" in status["langs"] and "mlt" in status["langs"]:
        _line(True, "OCR (optional)", f"Tesseract with {status['langs']} ({status['binary']})")
    else:
        _line(None, "OCR (optional)", f"Tesseract found, languages: {status['langs'] or 'none usable'}. "
              "Add English + Maltese data: mtnews-dl doctor --fix")

    # sources
    from .sources.tom import feed
    record = feed.LinkRecord()
    dates = record.dates()
    _line(True, "tom (e-paper)", f"no login needed; link record covers {len(dates)} dates"
          + (f" ({dates[0]} to {dates[-1]})" if dates else ""))
    from .sources.tom_archive.chrome_bridge import extension_dir
    chrome = system.find_chrome()
    if (extension_dir() / "connection.js").exists():
        _line(True, "tom (archive)", f"Chrome extension prepared in {extension_dir()}"
              + ("" if chrome else "; Chrome not found"))
    else:
        _line(None, "tom (archive)", "optional, premium: run  tom-dl connect  once"
              + ("" if chrome else " (needs Google Chrome)"))
    _line(True, "mttd", "no login needed")
    from .sources.talkmt.auth import offline_status
    talk = offline_status()
    if talk.get("state") == "ok":
        _line(True, "talkmt", f"session stored; login cookie expires {talk.get('login_cookie_expires') or 'n/a'}")
    else:
        _line(None, "talkmt", f"subscription session {talk.get('state')}: "
              "mtnews-dl auth talkmt --cookies FILE  (talkmt-dl auth --help-cookies explains)")

    # commands on PATH, writable working folder
    missing = [c for c in ("mtnews-dl", "tom-dl", "mttd-dl", "talkmt-dl") if not shutil.which(c)]
    if missing:
        _line(None, "commands", f"not on PATH in this terminal: {', '.join(missing)} (open a new terminal "
              "after installing)")
    else:
        _line(True, "commands", "mtnews-dl, tom-dl, mttd-dl, talkmt-dl")
    try:
        with tempfile.NamedTemporaryFile(dir=os.getcwd()):
            pass
        _line(True, "current folder", f"writable ({os.getcwd()})")
    except OSError as exc:
        _line(None, "current folder", f"not writable ({exc}); use -P DIR")
    print("\nAll required parts are ready." if not problems else f"\n{problems} problem(s) need fixing.")
    return 0 if not problems else 1
