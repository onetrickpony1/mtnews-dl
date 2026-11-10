"""Optional Tesseract OCR for printed-date checks.

Tesseract is found on PATH or in its usual install folders. Language data is taken from
mtnews-dl's own tessdata folder when it has eng (the installer puts eng + mlt there), so
Maltese works even when the system package has English only.
"""

import functools
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import paths

LANGS = ("eng", "mlt")
TESSDATA_URL = "https://github.com/tesseract-ocr/tessdata_fast/raw/main/{lang}.traineddata"


class OcrError(Exception):
    pass


def find_tesseract():
    found = shutil.which("tesseract")
    if found:
        return found
    candidates = []
    if os.name == "nt":
        for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),
                     os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                     os.environ.get("LOCALAPPDATA", "")):
            if base:
                candidates.append(Path(base) / "Tesseract-OCR" / "tesseract.exe")
                candidates.append(Path(base) / "Programs" / "Tesseract-OCR" / "tesseract.exe")
    elif sys.platform == "darwin":
        candidates += [Path("/opt/homebrew/bin/tesseract"), Path("/usr/local/bin/tesseract")]
    for path in candidates:
        if path.is_file():
            return str(path)
    return None


def tessdata_dir() -> Path:
    return paths.data_dir() / "tessdata"


class Engine:
    def __init__(self, binary, langs, tessdata=None):
        self.binary, self.langs, self.tessdata = binary, langs, tessdata

    def _cmd(self, image, psm):
        cmd = [self.binary, str(image), "stdout", "-l", self.langs, "--psm", psm]
        if self.tessdata:
            cmd[1:1] = ["--tessdata-dir", str(self.tessdata)]
        return cmd

    def read_region(self, pdf, box, dpi, psm) -> str:
        from .pdfcheck import render_region
        with tempfile.TemporaryDirectory(prefix="mtnews-ocr-") as temp:
            image = Path(temp) / "region.png"
            render_region(pdf, box, dpi, image)
            try:
                return subprocess.run(self._cmd(image, psm), capture_output=True, text=True,
                                      check=True, timeout=180).stdout
            except subprocess.SubprocessError as exc:
                raise OcrError(str(exc)) from None


def _languages(binary, tessdata=None):
    cmd = [binary, "--list-langs"]
    if tessdata:
        cmd[1:1] = ["--tessdata-dir", str(tessdata)]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        return set()
    return set((out.stdout + out.stderr).split())


@functools.lru_cache(maxsize=1)
def engine():
    """The Tesseract engine to use, or None when OCR is unavailable."""
    binary = find_tesseract()
    if not binary:
        return None
    own = tessdata_dir()
    if (own / "eng.traineddata").is_file():
        langs = _languages(binary, own)
        tessdata = own
    else:
        langs, tessdata = _languages(binary), None
    chosen = "+".join(lang for lang in LANGS if lang in langs)
    return Engine(binary, chosen, tessdata) if chosen else None


def status() -> dict:
    binary = find_tesseract()
    eng = engine.__wrapped__() if binary else None
    return dict(binary=binary, langs=eng.langs if eng else "",
                tessdata=str(eng.tessdata) if eng and eng.tessdata else "")


def install_languages(emit=print) -> bool:
    """Download eng + mlt traineddata into mtnews-dl's own tessdata folder."""
    import urllib.request
    target = tessdata_dir()
    target.mkdir(parents=True, exist_ok=True)
    ok = True
    for lang in LANGS:
        dest = target / f"{lang}.traineddata"
        if dest.is_file() and dest.stat().st_size > 100_000:
            continue
        url = TESSDATA_URL.format(lang=lang)
        emit(f"Downloading Tesseract language data: {lang}")
        part = dest.with_suffix(".part")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "mtnews-dl"})
            with urllib.request.urlopen(req, timeout=120) as response, part.open("wb") as out:
                shutil.copyfileobj(response, out)
            if part.stat().st_size < 100_000:
                raise OSError("file too small")
            os.replace(part, dest)
        except OSError as exc:
            part.unlink(missing_ok=True)
            emit(f"Could not download {lang} language data: {exc}")
            ok = False
    engine.cache_clear()
    return ok
