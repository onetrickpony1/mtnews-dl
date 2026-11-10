"""Per-user locations. Session material lives only in the private config folder
(~/.config/talkmt-dl on Linux, as in talkmt-dl 0.1.0, so existing sessions keep working)."""

from ...common.paths import config_dir as _config_dir
from ...common.paths import ensure_private_dir  # noqa: F401  (re-exported)


def config_dir():
    return _config_dir("talkmt-dl")


def cookies_path():
    return config_dir() / "cookies.txt"


def patterns_path():
    return config_dir() / "patterns.json"
