"""Per-user folders.

Each tool keeps its own config folder (cookies, learned patterns) so existing installs of
talkmt-dl keep working: e.g. ~/.config/talkmt-dl on Linux. Shared data (OCR language files,
the Times of Malta link record) lives under mtnews-dl.
"""

import os
import sys
from pathlib import Path


def _base(kind: str) -> Path:
    if os.name == "nt":
        if kind == "config":
            return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming")
        return Path(os.environ.get("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    if sys.platform == "darwin":
        lib = Path.home() / "Library"
        return lib / {"config": "Application Support", "data": "Application Support",
                      "cache": "Caches"}[kind]
    env, default = {"config": ("XDG_CONFIG_HOME", ".config"),
                    "data": ("XDG_DATA_HOME", ".local/share"),
                    "cache": ("XDG_CACHE_HOME", ".cache")}[kind]
    return Path(os.environ.get(env) or Path.home() / default).expanduser()


def config_dir(tool: str = "mtnews-dl") -> Path:
    override = os.environ.get(tool.upper().replace("-", "_") + "_CONFIG")
    return Path(override).expanduser() if override else _base("config") / tool


def data_dir(tool: str = "mtnews-dl") -> Path:
    override = os.environ.get(tool.upper().replace("-", "_") + "_DATA")
    return Path(override).expanduser() if override else _base("data") / tool


def cache_dir(tool: str = "mtnews-dl") -> Path:
    return _base("cache") / tool


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, 0o700)
    return path
