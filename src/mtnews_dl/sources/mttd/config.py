"""Portable defaults shared by the command line and desktop interfaces."""

import os
import sys
from pathlib import Path


def default_library() -> Path:
    """Use portable overrides or the platform's user-data directory."""
    if os.environ.get("MTTD_DL_LIBRARY"):
        return Path(os.environ["MTTD_DL_LIBRARY"]).expanduser()
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    elif sys.platform == "darwin":
        base = Path.home() / "Library/Application Support"
    else:
        base = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share").expanduser()
    return base / "mttd-dl" / "library"
