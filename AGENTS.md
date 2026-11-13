# mtnews-dl: notes for AI assistants

Project-specific rules for working on this repo.

## Layout

- `src/mtnews_dl/umbrella.py` is the `mtnews-dl` command. It runs the source tools and returns the most serious exit
  code of the tools it ran.
- `src/mtnews_dl/sources/` holds one package per source. `tom` (Times of Malta e-paper), `tom_archive` (the premium
  archive, through a browser extension), `mttd` (MediaToday) and `talkmt` (subscribe.mt) each have their own command.
- `src/mtnews_dl/common/` is shared code: printed-date checks and OCR, CLI options, paths and locks. Every tool takes
  the same options, so add new options here rather than to one tool.
- Exit codes are part of the interface (see the README). Don't change their meaning.

## Rules

- Never run a Times of Malta archive download (`tom-dl download --archive`, or anything that clicks archive page
  downloads) as a test without asking first. The daily page allowance (default 302) is for real downloads. When it is
  allowed, cap it with `--max-pages 1` or `2`. Listing (`tom-dl archive pages DATE`) and `--dry-run` are free.
- Only use links the publishers list. Stop on refusals, sign-in pages, security checks and quota notices. Never retry
  past them.
- Never commit downloads, cookies, browser profiles or state folders (`.tom-dl/`, `.mttd-dl/`, `.talkmt-dl/`). The
  `.gitignore` covers them. Keep it that way.
- Cookies and sessions are never printed or logged.
- Existing libraries must keep working: state folders and stored sessions stay where they are.

## Platforms

- All text files are read and written as UTF-8. Windows defaults to cp1252, and Maltese text broke the Windows
  build once.
- Libraries often live on network shares (SMB/CIFS), where SQLite's own locks can fail. Keep the exclusive-create
  lock-file fallback (`_take_lock` in `sources/tom_archive/ledger.py`) working.

## Tests

`pip install -e ".[test]"`, then `pytest`. Tests run offline. CI runs Linux, macOS and Windows × Python 3.10 and 3.13.
