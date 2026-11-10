"""Operating-system helpers: finding Chrome, and installing the optional Tesseract OCR engine
with the platform's package manager (Homebrew, winget, apt/dnf/pacman/zypper/apk).

Everything else mtnews-dl needs is a Python package installed with it.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def find_chrome():
    """Path of Google Chrome (or Chromium/Brave/Edge, which can load the extension too), or None."""
    names = ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser", "brave-browser",
             "microsoft-edge")
    for name in names:
        found = shutil.which(name)
        if found:
            return found
    candidates = []
    if sys.platform == "darwin":
        for app in ("Google Chrome", "Chromium", "Brave Browser", "Microsoft Edge"):
            for base in (Path("/Applications"), Path.home() / "Applications"):
                candidates.append(base / f"{app}.app" / "Contents" / "MacOS" / app)
    elif os.name == "nt":
        for env in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
            base = os.environ.get(env)
            if base:
                candidates.append(Path(base) / "Google" / "Chrome" / "Application" / "chrome.exe")
                candidates.append(Path(base) / "Microsoft" / "Edge" / "Application" / "msedge.exe")
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def tesseract_install_command():
    """(argv, description) for this machine's package manager, or (None, advice)."""
    if sys.platform == "darwin":
        if shutil.which("brew"):
            return ["brew", "install", "tesseract"], "Homebrew"
        return None, ("Install Homebrew from https://brew.sh, then run: brew install tesseract "
                      "(or skip OCR: it is only used when a PDF has no readable date text).")
    if os.name == "nt":
        if shutil.which("winget"):
            return (["winget", "install", "-e", "--id", "UB-Mannheim.TesseractOCR",
                     "--accept-package-agreements", "--accept-source-agreements"], "winget")
        return None, ("Install Tesseract from https://github.com/UB-Mannheim/tesseract/wiki "
                      "(or skip OCR: it is only used when a PDF has no readable date text).")
    sudo = [] if os.geteuid() == 0 else (["sudo"] if shutil.which("sudo") else [])
    for tool, argv in (("apt-get", ["apt-get", "install", "-y", "tesseract-ocr"]),
                       ("dnf", ["dnf", "install", "-y", "tesseract"]),
                       ("pacman", ["pacman", "-S", "--needed", "--noconfirm", "tesseract"]),
                       ("zypper", ["zypper", "--non-interactive", "install", "tesseract-ocr"]),
                       ("apk", ["apk", "add", "tesseract-ocr"])):
        if shutil.which(tool):
            return sudo + argv, tool
    return None, "Install the 'tesseract' package with your system's package manager."


def install_tesseract(emit=print, interactive=True) -> bool:
    argv, how = tesseract_install_command()
    if argv is None:
        emit(how)
        return False
    if argv[0] == "sudo" and not interactive:
        emit(f"Tesseract needs an administrator install; run: {' '.join(argv)}")
        return False
    emit(f"Installing Tesseract OCR with {how}: {' '.join(argv)}")
    try:
        result = subprocess.run(argv, timeout=1800)
    except (OSError, subprocess.SubprocessError) as exc:
        emit(f"Could not run {how}: {exc}")
        return False
    if result.returncode != 0:
        emit(f"{how} did not finish (exit {result.returncode}). OCR stays optional; you can retry with: "
             "mtnews-dl doctor --fix")
        return False
    return True
