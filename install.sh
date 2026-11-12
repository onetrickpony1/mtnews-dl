#!/bin/sh
# mtnews-dl installer for Linux and macOS. No admin rights needed (except, if you agree,
# for the optional OCR engine through your package manager).
#
#   One line:   curl -LsSf https://raw.githubusercontent.com/onetrickpony1/mtnews-dl/main/install.sh | sh
#   From a downloaded copy of the repository:   sh install.sh   (macOS: double-click install.command)
#
# What it does:
#   1. installs uv (https://docs.astral.sh/uv/) if missing: it brings its own Python
#   2. installs mtnews-dl with its Python packages in an isolated environment
#   3. puts mtnews-dl, tom-dl, mttd-dl and talkmt-dl on your PATH
#   4. checks everything with `mtnews-dl doctor`, offering to install the OCR engine
#      (Tesseract) with English + Maltese data and, if no Chrome is installed, a private
#      Chrome for Testing for the Times of Malta archive
# Run it again to update. Uninstall: uv tool uninstall mtnews-dl
set -eu

HERE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if [ -n "${MTNEWS_DL_SOURCE:-}" ]; then
    SOURCE="$MTNEWS_DL_SOURCE"
elif [ -f "$HERE/pyproject.toml" ] && grep -q '^name = "mtnews-dl"' "$HERE/pyproject.toml"; then
    SOURCE="$HERE"
else
    SOURCE="mtnews-dl @ https://github.com/onetrickpony1/mtnews-dl/archive/refs/heads/main.zip"
fi
PYTHON="${MTNEWS_DL_PYTHON:-3.12}"

say() { printf '\n==> %s\n' "$*"; }
fail() { printf '\nmtnews-dl installer: %s\n' "$*" >&2; exit 1; }
ask() {   # ask "question" default(y/n) -> 0 for yes; no terminal -> default
    if ( : < /dev/tty ) 2>/dev/null; then
        printf '%s ' "$1" > /dev/tty
        read -r ans < /dev/tty || ans=""
    else
        ans=""
    fi
    [ -z "$ans" ] && ans="$2"
    case "$ans" in [Yy]*) return 0 ;; *) return 1 ;; esac
}

say "Installing mtnews-dl from $SOURCE"

# 1. uv
if command -v uv >/dev/null 2>&1; then
    UV="$(command -v uv)"
elif [ -x "$HOME/.local/bin/uv" ]; then
    UV="$HOME/.local/bin/uv"
else
    say "Installing uv (Python manager, from astral.sh)"
    if command -v curl >/dev/null 2>&1; then
        curl -LsSf https://astral.sh/uv/install.sh | sh
    elif command -v wget >/dev/null 2>&1; then
        wget -qO- https://astral.sh/uv/install.sh | sh
    else
        fail "needs curl or wget to download uv"
    fi
    for c in "$HOME/.local/bin/uv" "${XDG_BIN_HOME:-$HOME/.local/bin}/uv" "$HOME/.cargo/bin/uv"; do
        [ -x "$c" ] && UV="$c" && break
    done
    [ -n "${UV:-}" ] || fail "uv was installed but can't be found; open a new terminal and run this again"
fi

# 2. mtnews-dl (+ Python if needed)
say "Installing mtnews-dl and its packages (Python $PYTHON is downloaded if needed)"
"$UV" tool install --force --python "$PYTHON" "$SOURCE" || fail "installing mtnews-dl failed (see above)"

# 3. PATH
"$UV" tool update-shell >/dev/null 2>&1 || true
BIN="$("$UV" tool dir --bin)"
MT="$BIN/mtnews-dl"
[ -x "$MT" ] || fail "mtnews-dl was installed but $MT is missing"

# 4. dependency check (+ optional OCR)
say "Checking the installation"
if [ -z "${CI:-}" ] && ask "Install the remaining parts: OCR engine (Tesseract) + English/Maltese data, and a browser for the Times of Malta archive if no Chrome is installed? [Y/n]" y; then
    # Package managers may ask questions: give them the keyboard, not the piped script
    if ( : < /dev/tty ) 2>/dev/null; then
        "$MT" doctor --fix < /dev/tty || true
    else
        "$MT" doctor --fix < /dev/null || true
    fi
else
    "$MT" doctor || true
fi

say "Done: $("$MT" --version)"
case ":$PATH:" in
    *":$BIN:"*) ;;
    *) printf 'Open a NEW terminal so the commands are found (they are in %s).\n' "$BIN" ;;
esac
cat <<'EOF'

Start with:   mtnews-dl --easy        (guided: sources, dates, folder)
Sources:      mtnews-dl sources
Check setup:  mtnews-dl doctor
Each paper on its own: tom-dl, mttd-dl, talkmt-dl (same options; add --help)
EOF
