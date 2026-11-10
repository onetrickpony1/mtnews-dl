"""Cookie-file import and session checks. Cookie values are never printed or logged."""

import http.cookiejar
import os
import tempfile
import time
from pathlib import Path

from .config import config_dir, cookies_path, ensure_private_dir
from .net import DAILY_URL, HOST, AuthRequired, SessionExpired, Session, looks_like_login

LOGIN_PREFIX = "wordpress_logged_in_"
HEADER = "# Netscape HTTP Cookie File\n# Written by talkmt-dl. Private: do not share.\n"

COOKIE_HELP = f"""\
How to give talkmt-dl your session (never paste cookies into a chat):
  1. In your normal browser, log in at https://subscribe.mt/newspapers/login/ and
     open https://subscribe.mt/newspapers/daily/ to check the PDFs are listed.
  2. Export cookies for subscribe.mt in Netscape "cookies.txt" format with a local
     exporter extension (for example "Get cookies.txt LOCALLY"), choosing
     "current site" only. Save it somewhere private, e.g. ~/Downloads/subscribe.mt_cookies.txt
  3. Run:  talkmt-dl auth --cookies ~/Downloads/subscribe.mt_cookies.txt
     talkmt-dl keeps only subscribe.mt cookies in {cookies_path()} (mode 600).
  4. Delete the exported file afterwards:  rm ~/Downloads/subscribe.mt_cookies.txt
Logging out of the website ends the session; export again after logging back in."""


def domain_matches(domain: str) -> bool:
    domain = domain.lower().lstrip(".")
    return domain == HOST or domain.endswith("." + HOST)


def parse_cookie_file(path):
    """Return (kept_lines, dropped_count, login_expiries). Raises ValueError on bad format."""
    text = Path(path).read_text(encoding="utf-8", errors="strict")
    kept, dropped, expiries, seen = [], 0, [], 0
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip("\r")
        httponly = line.startswith("#HttpOnly_")
        if httponly:
            line = line[len("#HttpOnly_"):]
        if not line.strip() or (line.startswith("#") and not httponly):
            continue
        fields = line.split("\t")
        if len(fields) != 7:
            raise ValueError(f"Line {number} is not a Netscape cookie line (expected 7 tab-separated "
                             "fields). Export in 'Netscape/cookies.txt' format, not JSON.")
        domain, flag, path_, secure, expires, name, _value = fields
        if flag.upper() not in ("TRUE", "FALSE") or secure.upper() not in ("TRUE", "FALSE"):
            raise ValueError(f"Line {number} has invalid TRUE/FALSE fields.")
        try:
            int(expires or "0")
        except ValueError:
            raise ValueError(f"Line {number} has a non-numeric expiry.") from None
        seen += 1
        if not domain_matches(domain):
            dropped += 1
            continue
        if name.startswith(LOGIN_PREFIX):
            expiries.append(int(expires or "0"))
        kept.append(("#HttpOnly_" if httponly else "") + "\t".join(fields))
    if not seen:
        raise ValueError("No cookies found in that file.")
    return kept, dropped, expiries


def login_state(expiries, now=None):
    now = time.time() if now is None else now
    if not expiries:
        return "no_login_cookie"
    if all(0 < e <= now for e in expiries):
        return "expired"
    return "ok"


def write_private(path: Path, text: str):
    ensure_private_dir(path.parent)
    fd, temp = tempfile.mkstemp(prefix=".cookies-", dir=path.parent)
    try:
        if os.name != "nt":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def import_cookies(source) -> dict:
    source = Path(source).expanduser()
    if not source.is_file():
        raise ValueError(f"Cookie file not found: {source}")
    kept, dropped, expiries = parse_cookie_file(source)
    if not kept:
        raise ValueError("The file has no subscribe.mt cookies. Export while on subscribe.mt.")
    state = login_state(expiries)
    if state == "expired":
        raise SessionExpired("The WordPress login cookie in that file has expired. Log in again and re-export.")
    write_private(cookies_path(), HEADER + "\n".join(kept) + "\n")
    return dict(kept=len(kept), dropped_other_domains=dropped, login_cookie=state,
                stored=str(cookies_path()))


def load_jar(path=None):
    path = Path(path or cookies_path())
    if not path.is_file():
        raise AuthRequired("No session stored yet.")
    if os.name != "nt" and path.stat().st_mode & 0o077:
        os.chmod(path, 0o600)
    _kept, _dropped, expiries = parse_cookie_file(path)
    if login_state(expiries) == "expired":
        raise SessionExpired("The stored login cookie has expired.")
    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (http.cookiejar.LoadError, OSError) as exc:
        raise AuthRequired(f"Stored cookie file could not be read: {exc.__class__.__name__}") from None
    for cookie in jar:  # expiry 0 means "session cookie" (older Pythons treat it as expired)
        if cookie.expires == 0:
            cookie.expires, cookie.discard = None, True
    jar.clear_expired_cookies()
    for cookie in list(jar):  # Defence in depth: only subscribe.mt cookies may be sent.
        if not domain_matches(cookie.domain):
            jar.clear(cookie.domain, cookie.path, cookie.name)
    return jar


def offline_status() -> dict:
    path = cookies_path()
    if not path.is_file():
        return dict(stored=False, state="missing", path=str(path))
    try:
        kept, _dropped, expiries = parse_cookie_file(path)
    except (ValueError, OSError):
        return dict(stored=True, state="unreadable", path=str(path))
    mode = oct(path.stat().st_mode & 0o777) if os.name != "nt" else "n/a"
    soonest = min((e for e in expiries if e > 0), default=None)
    return dict(stored=True, state=login_state(expiries), path=str(path), cookies=len(kept),
                mode=mode, login_cookie_expires=(time.strftime("%Y-%m-%d %H:%M", time.localtime(soonest))
                                                 if soonest else None))


def check_online(session: Session, stop=None):
    """One request to the e-Newspapers page; returns its HTML when logged in."""
    result = session.get(DAILY_URL, stop=stop)
    if looks_like_login(result.text):
        raise AuthRequired("The e-Newspapers page shows the login form: the session is not logged in.")
    if "/mp-files/" not in result.text:
        raise AuthRequired("Logged-in page has no download links; the session may lack an active subscription.")
    return result.text


def forget() -> bool:
    path = cookies_path()
    if path.exists():
        path.unlink()
        return True
    return False


__all__ = ["COOKIE_HELP", "import_cookies", "load_jar", "offline_status", "check_online", "forget",
           "config_dir"]
