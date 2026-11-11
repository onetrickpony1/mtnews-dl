# mtnews-dl

Download Maltese newspapers as whole-issue PDFs into your own archive: resumable, paced, and
with every PDF's **printed front-page date checked** before it is filed.

| Source | Command | Papers | Login |
|---|---|---|---|
| `tom` | `tom-dl` | Times of Malta, The Sunday Times of Malta | e-paper (2024-11-19 →): none · archive (older, `--archive`): premium, through your Chrome |
| `mttd` | `mttd-dl` | MaltaToday, Illum, BusinessToday, other MediaToday titles; pre-Oct-2013 issues rebuilt from web archives | none |
| `talkmt` | `talkmt-dl` | l-orizzont, it-Torċa, supplements (subscribe.mt) | your subscription: a cookies file |

`mtnews-dl` runs any or all of them with one command. Each tool also works on its own, with
the same options.

## Install (Windows, macOS, Linux)

Download this repository (green **Code** button → *Download ZIP*, or `gh repo clone`), then:

- **Windows:** double-click `install.cmd`
- **macOS:** double-click `install.command` (if blocked: right-click → *Open*)
- **Linux:** `sh install.sh` (or double-click it and choose *Run*)

No admin rights are needed. The installer:
1. installs [uv](https://docs.astral.sh/uv/), which downloads its own Python if needed;
2. installs mtnews-dl and its Python packages in an isolated environment, and puts
   `mtnews-dl`, `tom-dl`, `mttd-dl` and `talkmt-dl` on your PATH;
3. runs `mtnews-dl doctor`, offering to install the rest: the OCR engine (Tesseract, via
   Homebrew, winget or your Linux package manager) with English and Maltese language data,
   and, if no Chrome is installed, a private browser for the Times of Malta archive
   (Google's Chrome for Testing, about 150 MB, kept in mtnews-dl's own folder).

Run the installer again to update. Uninstall: `uv tool uninstall mtnews-dl`.
To install straight from GitHub instead (needs git access to this private repo), set
`MTNEWS_DL_SOURCE=git+https://github.com/onetrickpony1/mtnews-dl` before running it.

PDF reading uses [pypdfium2](https://github.com/pypdfium2-team/pypdfium2) (bundled; no Poppler).
OCR is only used when a front page has no readable date text.

## Quick start

```sh
mtnews-dl --easy                                  # guided
mtnews-dl sources                                 # what is covered, what is ready
mtnews-dl download 2026-10                        # every ready source, into this folder
mtnews-dl download --year 2025 --root ~/News      # ~/News/ToM/2025, MTTD/2025, TALKMT/2025
mtnews-dl download --date 2026-11-03 -s tom talkmt
mtnews-dl review                                  # anything that needs your eyes
mtnews-dl doctor                                  # is everything installed and ready?
```

## The same commands everywhere

```
<tool> download  [YYYY | YYYY-MM ...] [--year Y ...] [--date D ...] [--from D [--to D]]
                 [-P DIR | --library DIR] [--delay S] [--retries N] [--retry-missing] [--dry-run]
<tool> list      [--json]
<tool> review    [--all] [--json]            PDFs held for review, gaps, failures
<tool> recheck                               re-check pending PDFs (no network)
<tool> accept    DATE[@ID] --yes             file a pending PDF after you checked it
<tool> --easy | --version | --help
```

- **Selection:** years/months, one or more `--date`, or a `--from/--to` range (inclusive).
- **Destination:** `-P DIR` puts PDFs directly in DIR (default: the current folder);
  `--library DIR` uses `DIR/editions/YYYY/`. Use the same option for `review`/`accept`.
- **Resume:** run the same command again. Verified files (SHA-256) are skipped; existing files
  with the right name are checked rather than downloaded; dates found missing are skipped
  unless `--retry-missing`.
- **Date check:** the printed date is read from the masthead (top 28% of page 1; English and
  Maltese month names), with OCR as a fallback. Matching → filed; anything else → kept in the
  tool's hidden `.tom-dl`/`.mttd-dl`/`.talkmt-dl/pending` folder for `review`/`accept`.

Source-specific extras:

| | |
|---|---|
| `tom-dl download --archive [--max-pages N] [--ceiling N] [--wait]` | use the digital archive for dates without an e-paper PDF |
| `tom-dl connect` | one-time Chrome extension setup for the archive, and a sign-in check |
| `tom-dl feed update \| import FILES \| status` | the e-paper link record |
| `tom-dl archive status \| import OLD_LIBRARY \| add-usage N` | archive page allowance |
| `mttd-dl collections`, `download --collection NAME…` | whole Uberflip collections (incl. undated issues) |
| `mttd-dl download --gap-only \| --legacy-only \| --no-legacy \| --legacy-url URL \| --no-wayback` | web-archive reconstruction (before Oct 2013) |
| `talkmt-dl auth --cookies FILE \| --status \| --forget` | your subscribe.mt session |
| `talkmt-dl download -p l-orizzont it-torca supplements \| --listed \| --formats observed` | |
| `talkmt-dl patterns`, `talkmt-dl coverage --from YYYY-MM` | filename patterns, coverage survey |

**Exit codes (all tools):** `0` ok · `1` error · `2` bad arguments · `3` PDFs need review ·
`4` single date, nothing found · `5` login / cookies / Chrome connection needed ·
`6` stopped by the site (rate limit, refusal, quota, server errors) · `130` interrupted.
`mtnews-dl` returns the most serious code of the tools it ran.

## Sources in detail

**Times of Malta e-paper** — the publisher's public feed lists whole-issue PDFs for about the
last two years and drops older ones, but their links keep working. tom-dl keeps every link it
has seen in a link record (seeded with all links since 2024-11-19), so older e-paper issues
stay downloadable. Only links the publisher listed are used.

**Times of Malta archive** (`--archive`, premium subscription) — the archive serves one PDF per
page with a daily page allowance. tom-dl drives a browser you are signed in to through a
small extension (`tom-dl connect`); it never reads cookies or passwords. With Chrome
installed you load the extension once into your normal Chrome; without it (or with
`tom-dl connect --own-browser`) tom-dl uses its own window (Chrome for Testing, separate
profile, extension preloaded) where you sign in once. Every page
click is counted in a local rolling 24-hour ledger *before* it happens (default 302 pages; change with `--ceiling N`).
The count is per user, not per folder, so every library and year folder (and two runs at
once) share one allowance. Uncertain attempts stay counted, and an issue is joined into one PDF only when every listed
page is saved and verified. Security checks, sign-in pages and quota notices pause the run.

**MediaToday (mttd)** — publisher-linked issue PDFs from the Uberflip archives. Year selections
use the *issue* year, falling back to the posting date for issues without a usable year. For
June 2010–September 2013 and earlier, issues are rebuilt from the publisher's web archives
(and, unless `--no-wayback`, the Internet Archive) as searchable PDFs; these are marked as
reconstructions, not printed issues.

**talk.mt / subscribe.mt (talkmt)** — needs your logged-in session as a Netscape `cookies.txt`
export (`talkmt-dl auth --help-cookies`). Cookies are filtered to subscribe.mt, stored
privately, and never printed or logged. See [docs/talkmt-validation.md](docs/talkmt-validation.md).

## Upgrading from the separate tools

Existing libraries keep working as they are: state folders (`.mttd-dl`, `.talkmt-dl`,
`.tom-dl`, or `epaper/` from tom-dl 0.1.0), talkmt's stored session and mttd's catalog cache
are in the same places. Bring the old Times of Malta archive downloader's quota history and
pages over with `tom-dl archive import PATH/TO/times-of-malta-downloader/library`. Saved feed
snapshots (e.g. from the Windows feed archiver) can be added with `tom-dl feed import *.xml`.

## Responsible use

For your personal archive of papers you can access: public feeds, and your own subscriptions
through your own login. The tools pace requests (default 2–3 s), stop on refusals and
security checks rather than retrying, never guess credentials, and keep within the archive's
page allowance. Respect the publishers' terms.

## Development

```sh
python -m venv .venv && .venv/bin/pip install -e '.[test]'
.venv/bin/python -m pytest
```

Layout: `src/mtnews_dl/common` (date checks, OCR, CLI options, paths, locks),
`src/mtnews_dl/sources/{tom,tom_archive,mttd,talkmt}`, `umbrella.py` (`mtnews-dl`).
