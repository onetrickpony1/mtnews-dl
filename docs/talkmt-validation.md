# Validation – talkmt-dl 0.1.0

## Done (2026-11-03)

Repository inspection: mttd-dl 0.8.1 at `~/Documents/Codex/2026-11-02/che/outputs/mttd-dl`
(no AGENTS.md there or in parent folders). talkmt-dl is a separate project in
`~/Apps/talkmt-dl`; mttd-dl's installed files were checksummed before and after installing
talkmt-dl and are unchanged.

Anonymous live requests (3 in total, no cookies):

| Request | Result |
| --- | --- |
| `GET /newspapers/daily/` | 200, MemberPress login form, no `/mp-files/` links |
| `GET /newspapers/mp-files/l-orizzont-2026-11-03.pdf/` | 302 → `/newspapers/login/?action=mepr_unauthorized&mepr-unauth-page=…` |
| `GET /newspapers/mp-files/l-orizzont-1999-01-01.pdf/` (made-up date) | **200** HTML: the WordPress post listing (`<body class="blog …">`), not a PDF |

So "unauthorised" and "no such file" look different, and a missing file returns HTTP 200.

Automated tests: 69 passing (52 at first; regression tests added for both filename formats) on Python 3.14 (system) and 3.12 (mttd-dl runtime): auth
redirects (302 and HTTP 200 login pages), cookie import/filtering/permissions/expiry, cookies
never sent to other or look-alike hosts, missing dates, filename/printed-date mismatch, malformed
responses and URLs, Retry-After, bounded retries, truncation, interrupted downloads and resume,
existing-file preservation, library lock, recheck/accept, supplement titles, CLI exit codes,
and `--easy` flows.

Live OCR check: an image-only PDF reading "Il-Hadd, 1 ta' Novembru 2026" verified as
2026-11-01 through `tesseract -l eng+mlt` from mttd-dl's runtime.

Installation: `./install.sh` → `~/.local/bin/talkmt-dl`, `talkmt-dl --version` = 0.1.0.

## Live checks with the owner's session (2026-11-03, ~20 requests in total)

`auth --cookies`: 1 subscribe.mt cookie kept, stored mode 600 (folder 700). The page links 21 PDFs:
7 l-orizzont (ISO names), 6 it-Torċa (**two** conventions: `it-torca-DD-MM-YYYY` and
`it-torca-YYYY-MM-DD`), 7 supplements (Ekonomija, Kurrenti, Il-Gazzetta Tfal; some undated).

| Request | Result |
| --- | --- |
| l-orizzont 2026-11-03 (listed) | verified, 20 pp, after fixing Poppler bbox XML (control chars) |
| l-orizzont 2026-10-20, 2026-01-06, 2025-11-05 (unlisted, ISO) | verified |
| l-orizzont 2025-11-04, 2026-06-02 ISO name | HTTP 200 site HTML → missing |
| same dates, DD-MM-YYYY name | **verified** (owner's discovery confirmed) |
| l-orizzont 2026-11-01 (Sunday), ISO | missing (DD-MM-YYYY not tried for this date) |
| it-Torċa 2026-11-01 (listed) | verified by OCR (text layer has hidden older mastheads) |
| `it-torca-04-09-2026.pdf` (listed) | printed **4 Oct 2026** (No. 5338) → `wrong_date`, pending: the filename is wrong |
| Kurrenti 2025-10-25 (listed supplement) | monthly "Ottubru 2025", no day → uncertain, pending |

Not seen live: session expiry, 429/5xx, a file refused while logged in.

## Live checks, round 3 (owner findings on mixed formats; 11 requests)

| Request | Result |
| --- | --- |
| l-orizzont 2026-11-01 (Sunday) | all 9 variants tried, each HTTP 200 site HTML → reported missing only then |
| it-Torċa 2026-10-11 (listed `11-10-2026`) | verified by OCR, publication check confirmed |
| it-Torċa 2026-10-04 | 0 requests: found as the mislabelled `it-torca-04-09-2026.pdf` (printed 4 Oct) |
| `ekonomija-2025-07-26.pdf` (listed) | 12 pp, printed "#36 Lulju 2025" (month only) → uncertain, pending |

The owner's anonymous probes (protected-page IDs 10398, 10350, 9138, 9382) match the listed
filenames; authenticated downloads above confirm two of them (Torċa 01-11-2026, Ekonomija).
Mixed formats are shown for l-orizzont and it-Torċa, not for supplements.

## Coverage survey (2026-11-03, `talkmt-dl coverage`, 234 probe requests, no downloads)

Probes read only the first bytes, so "served" means a PDF exists under that name. The
printed date of a probed file was not checked.

* Quarterly samples 2018-01 → 2026-10 (observed formats): both papers served at every sample
  from **2020-04** onward, except l-orizzont 2022-04-15. That date is still not found after
  all 9 formats; it was Good Friday, a likely but unconfirmed no-paper day.
* Monthly 2019-10 → 2020-03, all formats: nothing served for l-orizzont through 2020-03-16.
  it-Torċa is served from **2020-03-15**; 2020-02-16 and earlier were not found.
* So the reachable archive starts around March/April 2020: it-Torċa 2020-03-15,
  l-orizzont between 2020-03-17 and 2020-04-15 (not narrowed further). This is a sample:
  individual days inside the range can still be missing.
* Text: all 11 fully downloaded issues have an embedded text layer, ~10–18k words per l-orizzont
  issue and ~40–55k per it-Torċa issue (it-Torċa's count includes hidden leftover template text).
* File sizes vary (3–52 MB); 2022–23 issues are often smaller. Quality was not inspected.

## Remaining

- [ ] How far back each convention goes (no range scan done; deliberately)
- [ ] Whether any l-orizzont/Torċa issue really uses a fallback-only format (none seen yet)
- [ ] Live session expiry, 429/5xx, a refusal while logged in
- [ ] Behaviour after the session expires
