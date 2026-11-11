"""Chrome for the Times of Malta archive.

Two ways, chosen automatically:
  * your own Google Chrome (or Chromium/Edge/Brave): you load the tom-dl extension once with
    "Load unpacked" and use your normal sign-in;
  * tom-dl's own browser window, when no Chrome is installed (or with `tom-dl connect
    --own-browser`): Google's "Chrome for Testing" build, downloaded into mtnews-dl's data folder
    (no admin rights), started with its own profile and the extension already loaded. You sign
    in to timesofmalta.com in that window once; the profile remembers it.
"""

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from .paths import data_dir

CFT_JSON = ("https://googlechromelabs.github.io/chrome-for-testing/"
            "last-known-good-versions-with-downloads.json")


def platform_key():
    if sys.platform == "darwin":
        return "mac-arm64" if os.uname().machine == "arm64" else "mac-x64"
    if os.name == "nt":
        return "win64" if sys.maxsize > 2 ** 32 else "win32"
    if sys.platform.startswith("linux"):
        machine = os.uname().machine
        return "linux64" if machine in ("x86_64", "amd64") else None
    return None


def browser_root() -> Path:
    return data_dir() / "chrome"


def _executable_in(folder: Path, key: str) -> Path:
    if key.startswith("mac"):
        return (folder / f"chrome-{key}" / "Google Chrome for Testing.app" / "Contents" / "MacOS"
                / "Google Chrome for Testing")
    return folder / f"chrome-{key}" / ("chrome.exe" if key.startswith("win") else "chrome")


def own_browser():
    """The installed Chrome for Testing executable, or None."""
    key = platform_key()
    marker = browser_root() / "current.json"
    if not key or not marker.is_file():
        return None
    try:
        exe = Path(json.loads(marker.read_text(encoding="utf-8"))["executable"])
    except (ValueError, KeyError):
        return None
    return str(exe) if exe.is_file() else None


def _extract(archive: Path, target: Path):
    if sys.platform == "darwin" and shutil.which("ditto"):
        # keeps the app bundle's symlinks and permissions
        subprocess.run(["ditto", "-x", "-k", str(archive), str(target)], check=True)
        return
    if os.name != "nt" and shutil.which("unzip"):
        subprocess.run(["unzip", "-q", "-o", str(archive), "-d", str(target)], check=True)
        return
    with zipfile.ZipFile(archive) as z:
        for info in z.infolist():
            path = z.extract(info, target)
            mode = info.external_attr >> 16
            if mode & 0o111 and os.name != "nt":
                os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def install_own_browser(emit=print) -> bool:
    """Download Chrome for Testing (about 150 MB) for tom-dl's archive window."""
    key = platform_key()
    if not key:
        emit("No Chrome for Testing build for this computer; install Google Chrome or Chromium.")
        return False
    try:
        req = urllib.request.Request(CFT_JSON, headers={"User-Agent": "mtnews-dl"})
        with urllib.request.urlopen(req, timeout=60) as response:
            info = json.load(response)["channels"]["Stable"]
        version = info["version"]
        url = next(d["url"] for d in info["downloads"]["chrome"] if d["platform"] == key)
    except (OSError, ValueError, KeyError, StopIteration) as exc:
        emit(f"Could not look up the Chrome for Testing download: {exc}")
        return False
    root = browser_root()
    target = root / version
    exe = _executable_in(target, key)
    if not exe.is_file():
        emit(f"Downloading Chrome for Testing {version} for tom-dl's archive window (about 150 MB)…")
        root.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(dir=root, suffix=".zip")
        os.close(fd)
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mtnews-dl"})
            with urllib.request.urlopen(req, timeout=120) as response, open(temp, "wb") as out:
                shutil.copyfileobj(response, out, 1024 * 1024)
            partial = root / f".{version}.partial"
            shutil.rmtree(partial, ignore_errors=True)
            _extract(Path(temp), partial)
            shutil.rmtree(target, ignore_errors=True)
            os.replace(partial, target)
        except (OSError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
            emit(f"Downloading Chrome for Testing failed: {exc}")
            return False
        finally:
            Path(temp).unlink(missing_ok=True)
    if not exe.is_file():
        emit(f"Chrome for Testing was unpacked but {exe} is missing.")
        return False
    (root / "current.json").write_text(json.dumps(dict(version=version, executable=str(exe))) + "\n", encoding="utf-8")
    for old in root.iterdir():   # keep only the current version
        if old.is_dir() and old.name not in (version,) and not old.name.startswith("."):
            shutil.rmtree(old, ignore_errors=True)
    if sys.platform.startswith("linux") and not _starts(exe):
        emit("Chrome for Testing is installed but may be missing system libraries. Install Google "
             "Chrome or Chromium from your distribution, or its libraries (e.g. Debian/Ubuntu: "
             "sudo apt-get install -y libnss3 libatk-bridge2.0-0 libgbm1 libasound2 libxkbcommon0).")
    emit(f"tom-dl's browser is ready: {exe}")
    return True


def _starts(exe) -> bool:
    try:
        out = subprocess.run([str(exe), "--version"], capture_output=True, text=True, timeout=30)
        return out.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def profile_dir() -> Path:
    return data_dir("tom-dl") / "browser-profile"


def own_browser_running() -> bool:
    """Is tom-dl's browser window open? Checks that the profile lock's process is alive
    (a lock left behind by a crash or a killed window does not count)."""
    profile = profile_dir()
    if os.name == "nt":
        lock = profile / "lockfile"
        if not lock.exists():
            return False
        try:
            lock.unlink()          # only possible when no browser holds it
            return False
        except OSError:
            return True
    lock = profile / "SingletonLock"
    try:
        target = os.readlink(lock)           # "<hostname>-<pid>"
    except OSError:
        return False
    try:
        pid = int(target.rsplit("-", 1)[1])
        os.kill(pid, 0)
        return True
    except (ValueError, IndexError, ProcessLookupError):
        return False
    except PermissionError:
        return True


def launch_own_browser(extension: Path, urls) -> bool:
    """Start tom-dl's browser window with its own profile and the extension loaded.

    chrome-extension:// URLs only open once the extension is loaded, so they are passed only
    to a window that is already running; a new window opens the connection tab by itself."""
    exe = own_browser()
    if not exe:
        return False
    profile = profile_dir()
    profile.mkdir(parents=True, exist_ok=True)
    if not own_browser_running():
        urls = [u for u in urls if not u.startswith("chrome-extension://")]
    args = [exe, f"--user-data-dir={profile}", f"--load-extension={extension}",
            "--no-first-run", "--no-default-browser-check",
            # The connection tab works in the background; keep Chrome from slowing its timers.
            "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
            "--disable-backgrounding-occluded-windows", *urls]
    kw = dict(stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
    if os.name == "nt":
        kw["creationflags"] = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    else:
        kw["start_new_session"] = True
    subprocess.Popen(args, **kw)
    return True
