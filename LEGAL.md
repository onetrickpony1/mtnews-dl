# Legal Notice, Terms of Use and Disclaimer

**Last updated: 12 November 2026 · Applies to mtnews-dl version 1.0.0 and later**

PLEASE READ THIS NOTICE CAREFULLY BEFORE INSTALLING, RUNNING, COPYING, MODIFYING OR DISTRIBUTING THE SOFTWARE.
BY DOING ANY OF THESE THINGS, YOU ACKNOWLEDGE THAT YOU HAVE READ, UNDERSTOOD AND AGREE TO BE BOUND BY THIS
NOTICE. IF YOU DO NOT AGREE, DO NOT USE THE SOFTWARE.

In this notice, "**the Software**" means mtnews-dl and the commands `mtnews-dl`, `tom-dl`, `mttd-dl` and
`talkmt-dl`, including the companion browser extension, installers, source code, documentation, bundled data
files and any copies or derivative works. "**the Authors**" means the copyright holders, authors, contributors
and maintainers of the Software, past and present. "**You**" means any person or entity that obtains,
installs, runs, copies, modifies or distributes the Software. "**Content**" means any newspaper issue, page,
PDF, article, photograph, advertisement, metadata or other material made available by a third party.
"**Third-Party Service**" means any website, feed, archive, platform or service operated by a party other than
You, including timesofmalta.com and its e-paper and digital archive, the MediaToday websites and their
publication archives (including those hosted on Uberflip), subscribe.mt and talk.mt, and the Internet Archive
(Wayback Machine).

---

## 1. Nature of the Software

1.1 The Software is a general-purpose, client-side tool. It runs on Your own computer, under Your own control,
and makes the same requests that You could make Yourself with a web browser. Depending on the source You
choose, it:

  (a) **Times of Malta e-paper** (`tom-dl`): reads the publisher's public e-paper feed and downloads the issue
      PDFs that the publisher itself links there, without any login;
  (b) **Times of Malta digital archive** (`tom-dl --archive`): operates a web browser that **You** have
      signed in to with **Your own** subscription, through a companion browser extension that talks only to
      the Software on Your own computer (the loopback address 127.0.0.1). The browser downloads archive pages
      with Your sign-in, one page at a time, and the Software joins them into issues;
  (c) **MediaToday titles** (`mttd-dl`): downloads publication PDFs that the publisher links from its public
      archives and, for older issues that are not available as PDFs, can rebuild an issue from the publisher's
      public web pages and from captures held by the Internet Archive (see section 3);
  (d) **subscribe.mt** (`talkmt-dl`): uses a session (cookies) that **You** export from Your own signed-in
      browser and give to the Software, to download issues included in **Your own** subscription.

1.2 The Software **does not** provide access to any Content. It does not host, store, mirror, index,
distribute, share or make available any Content to anyone. It holds no subscriptions, licences or accounts of
its own, and it does not grant, extend, transfer or create any right of access to any Content.

1.3 The Software is designed **not** to circumvent any technological protection measure, access control or
security measure. It does not bypass, disable, defeat, decrypt or evade paywalls, logins, CAPTCHAs or other
verification checks, bot-detection systems, download limits or digital rights management. When a Third-Party
Service presents a sign-in page, a verification check, a refusal or a limit notice, the Software stops (or
waits for You to act in Your own browser) and does not retry around it.

1.4 The Software never asks for, handles or stores passwords. For the Times of Malta archive, You sign in
Yourself in a browser window and the Software never sees Your password or cookies. For subscribe.mt, the
session cookies You supply are filtered to that site, stored only on Your computer with private file
permissions, sent only to subscribe.mt, and never printed or logged. You are responsible for keeping Your
computer, Your accounts and any exported cookie file secure, and for deleting an exported cookie file after
use.

1.5 The Software's safeguards (default pacing of a few seconds between requests, a local count of Times of
Malta archive pages in a rolling 24-hour window, stopping on refusals) are conservative technical defaults.
They are **not** a statement of what any Third-Party Service permits. In particular, the local page count
only covers downloads made through the Software on one computer; the service keeps its own count and its own
rules.

1.6 The Software is provided free of charge, for lawful personal, research and educational use. It is not a
commercial product or service, and no service relationship of any kind exists between You and the Authors.

## 2. Your sole responsibility for lawful use

2.1 **You alone are responsible for how You use the Software and for all consequences of that use.** You must
make sure that every use complies with:

  (a) the terms and conditions, subscription terms and acceptable-use policies of every Third-Party Service
      You access, including any limits on downloading, automated access, sharing or the number of pages or
      issues per day;
  (b) applicable copyright, related-rights, database-right, contract, computer-misuse, data-protection and
      other laws in every jurisdiction that applies to You or to Your use (in Malta, for example, the Copyright
      Act, Cap. 415, and the Data Protection Act, Cap. 586, together with the EU General Data Protection
      Regulation); and
  (c) any instruction, limit or restriction communicated to You by a Third-Party Service.

2.2 Newspapers, their photographs, illustrations, advertisements and layout are protected by copyright and
related rights, usually held by the publisher, the journalists, photographers and other contributors, or
agencies. Access to a page, or a subscription, gives You only the rights stated in the service's terms.
Subscription and website terms commonly **prohibit or restrict** systematic or bulk downloading, automated
access, keeping copies after a subscription ends, text and data mining, and sharing an account or its
downloads. Breaching them may lead the service to suspend or close Your account. **Read the terms of each
service before using the Software with it. If in doubt, ask the publisher and do not use the Software.**

2.3 Whether any exception to copyright (for example for private copying, quotation, research, archiving or
text and data mining) applies to You depends on Your jurisdiction, Your purpose and the facts. The Authors make
no statement that any exception applies to Your use.

2.4 The availability of a feature in the Software (including whole-year downloads, date ranges, scheduled or
resumed runs, configurable speeds and archive downloads) is **not** a representation, suggestion or
encouragement that using that feature is permitted for You. Defaults, presets, prompts and documentation
examples are technical illustrations only and are **not** legal advice or an assurance of compliance.

2.5 You must **not** use the Software:

  (a) to access any Content or service You are not authorised to access;
  (b) with any account, subscription, cookies or session that is not Your own or that You are not authorised
      to use, or to share one subscription among several people where its terms do not allow it;
  (c) to circumvent, or attempt to circumvent, any technological protection, access-control or rate-limiting
      measure, or to modify the Software to do so;
  (d) to download Content in breach of any terms of use, subscription terms or law, including systematic or
      bulk downloading where prohibited;
  (e) to share, publish, sell, upload, redistribute or otherwise make available Content to any person who is
      not entitled to it, including through websites, social media, file-sharing services, shadow libraries
      or public repositories;
  (f) to build or supply datasets, archives or collections in breach of any terms or law, including for
      training machine-learning models where this is not permitted or where the rights holder has reserved
      that use;
  (g) in a way that overloads, disrupts or harms any Third-Party Service, including the Internet Archive; or
  (h) for any commercial purpose where the applicable terms do not allow it.

2.6 You are solely responsible for the Content You download, store or use, for keeping it secure, and for
deleting it when Your right to keep it ends (for example when a subscription ends, if its terms require this).

## 3. Rebuilt issues from web archives

3.1 For some older MediaToday issues, the Software can assemble a PDF from articles published on the
publisher's website or captured by the Internet Archive. Such a PDF is a **reconstruction**: a compilation
made on Your computer from separately published web pages. It is **not** the printed issue, it may be
incomplete, it may contain articles from other dates or later edits, and it does not reproduce the original
layout, photographs or advertisements. The Software marks these files as reconstructions; You must not present
them as authentic copies of a printed newspaper.

3.2 Captures held by the Internet Archive are provided under the Internet Archive's own terms of use, and the
underlying Content remains the property of its rights holders. Use the Wayback Machine sparingly; the
`--no-wayback` option turns its use off.

## 4. Personal data

4.1 Newspapers contain personal data about identifiable people, including sensitive information (for example
about health, political opinions or criminal proceedings). Keeping copies for Your own purely personal use is
treated differently in data-protection law from processing them for any other purpose.

4.2 If You use downloaded Content for anything beyond purely personal or household activity (for example
research projects, publication, search services, datasets or making it available to others), You may be a
data controller with obligations under the GDPR and national law, and other rights (such as rights to
erasure or to object) may apply. These obligations are Yours alone.

4.3 The Software itself sends no data about You, Your downloads or Your use to the Authors or to anyone other
than the Third-Party Services You choose to contact. It has no telemetry.

## 5. No warranty

5.1 THE SOFTWARE IS PROVIDED "AS IS" AND "AS AVAILABLE", WITHOUT WARRANTY OF ANY KIND, EXPRESS, IMPLIED OR
STATUTORY, INCLUDING BUT NOT LIMITED TO WARRANTIES OF MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE,
TITLE, NON-INFRINGEMENT, ACCURACY, RELIABILITY, AVAILABILITY OR THAT THE SOFTWARE IS FREE OF ERRORS, DEFECTS
OR HARMFUL COMPONENTS.

5.2 Without limiting section 5.1, the Authors make no warranty or representation that: (a) any use of the
Software is lawful, licensed or permitted in Your circumstances; (b) the Software complies with the terms of
any Third-Party Service; (c) the Software's pacing, local counts, prompts or safeguards are sufficient to
prevent a breach of any terms, limit or law, or to prevent a Third-Party Service from limiting or closing Your
account; (d) any downloaded or rebuilt file is complete, correct, authentic or the issue it claims to be
(the printed-date check is automated, may be wrong, and files it cannot confirm are held for Your review);
or (e) the Software will continue to work with any Third-Party Service.

## 6. Limitation of liability

6.1 TO THE MAXIMUM EXTENT PERMITTED BY APPLICABLE LAW, IN NO EVENT SHALL THE AUTHORS BE LIABLE TO YOU OR TO ANY
THIRD PARTY FOR ANY CLAIM, LOSS, DAMAGES (WHETHER DIRECT, INDIRECT, INCIDENTAL, SPECIAL, CONSEQUENTIAL,
EXEMPLARY OR PUNITIVE), FINES, PENALTIES, COSTS OR OTHER LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT
(INCLUDING NEGLIGENCE), STRICT LIABILITY, STATUTE OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE
SOFTWARE OR THE USE OF OR OTHER DEALINGS IN THE SOFTWARE, INCLUDING WITHOUT LIMITATION: loss, suspension or
limitation of any subscription, account or access; use of a download allowance; breach of any contract or
terms of use; infringement of any copyright, related right, database right or other right; data-protection
claims; loss of data; or damage to any system, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGES.

6.2 The Authors have no control over, no knowledge of and no involvement in any particular use of the
Software, any Content You access, or any decision You make. Any use is made entirely at Your own risk and on
Your own initiative.

6.3 Where applicable law does not allow the exclusion or limitation of certain liability (including, where it
applies, liability that consumer-protection law does not allow to be excluded), the Authors' liability is
limited to the smallest extent permitted by that law. Nothing in this notice limits any liability that cannot
be limited by law.

## 7. Indemnification

To the maximum extent permitted by law, You agree to **defend, indemnify and hold harmless** the Authors from
and against any and all claims, demands, actions, proceedings, liabilities, damages, losses, fines, penalties,
costs and expenses (including reasonable legal fees) brought by any third party (including any publisher,
platform, rights holder, data subject or authority) and arising from or related to: (a) Your use or misuse of
the Software; (b) Your breach of this notice; (c) Your breach of any terms of use, subscription terms or law;
or (d) any Content You access, download, rebuild, store, use or distribute.

## 8. No affiliation; trademarks

The Software is an independent project. It is **not** affiliated with, endorsed by, sponsored by or approved by
Allied Newspapers Limited (Times of Malta, The Sunday Times of Malta), MediaToday Co. Ltd (MaltaToday, Illum,
BusinessToday and other titles), the publishers of l-orizzont and it-Torċa, subscribe.mt or talk.mt, Uberflip,
the Internet Archive, Google LLC (Chrome, Chrome for Testing), or any other Third-Party Service. All
newspaper titles, product names, trademarks and registered trademarks are the property of their respective
owners and are used only to identify the publications and services the Software interoperates with
(nominative use). Their use does not imply any endorsement or relationship.

## 9. Not legal advice

Nothing in the Software, its documentation, prompts or this notice is legal advice. The Authors do not advise
on whether any use is permitted. References to laws and to terms of use are general warnings only. Consult the
publisher or a qualified lawyer about Your own situation.

## 10. Third-Party Services and components

10.1 Third-Party Services are governed solely by their own terms, which You are responsible for accepting and
following. The Authors are not responsible for Third-Party Services, their availability, their Content or any
change to them. Third-Party Services may block, limit or terminate access at any time, and may detect
automated use.

10.2 Components that the Software bundles, downloads or offers to install are provided by third parties under
their own licences and terms, including: the Noto Sans fonts (see `src/mtnews_dl/sources/mttd/fonts/LICENSE.txt`);
the Python packages listed in `pyproject.toml`; uv; Tesseract OCR, installed through Your own package manager
if You agree, and its English and Maltese language data, downloaded from the Tesseract project if missing; and Google's Chrome for Testing, downloaded from Google only if You have
no Chrome and agree. Your use of each is subject to its own terms.

## 11. Contents of this repository

11.1 The repository contains **no newspaper Content**: no issues, pages, articles or images. Its bundled data
files contain only addresses (links) and dates that the Software uses to locate material at its source:

  (a) `src/mtnews_dl/sources/tom/data/links-seed.json`: links to Times of Malta e-paper PDFs as they appeared in
      the publisher's own public feed; and
  (b) `src/mtnews_dl/sources/mttd/data/gap-catalog.json`: addresses of public MediaToday web articles and of
      Internet Archive captures of them, with dates.

11.2 A link only says where something was published. It does not mean that the material is free to use, that it
is still available, or that downloading it is permitted for You.

## 12. Distribution and modified versions

12.1 The Software is free software licensed under the GNU General Public License, version 3 or (at Your option)
any later version (the "**GPL**"; see `LICENSE`). The disclaimer of warranty and limitation of liability in
sections 5 and 6 restate and supplement sections 15 and 16 of the GPL, and section 8 declines trademark rights,
as permitted by section 7 of the GPL. Nothing in this notice is intended to impose any "further restriction"
within the meaning of sections 7 and 10 of the GPL on the rights the GPL grants to receive, run, study, modify
or convey the Software; to the extent any provision would do so, it is to be read as a notice of Your own legal
responsibilities rather than as a licence condition. If You convey the Software or a modified version, You must
do so under the GPL, and You are asked to keep this notice with it.

12.2 **The Authors are not responsible for any modified version, fork or derivative work**, in particular any
version that removes or weakens safeguards (such as pacing, stopping on refusals or the archive page count),
adds circumvention of any protection measure, or is used or promoted for unauthorised purposes.

## 13. Notices and takedown

If You are a rights holder, publisher, platform or data subject and believe the Software is being used to
infringe Your rights, or that any part of this repository (including its data files) infringes Your rights,
please open an issue in the repository (marked "Legal notice") with enough detail to identify the matter. Do not
include personal data in a public issue; ask for a private contact instead. The Authors will review the notice
in good faith and may change, restrict or remove the Software, its data files or parts of them.

## 14. General

14.1 **Severability.** If any provision of this notice is held invalid or unenforceable, it shall be enforced to
the maximum extent permissible and the remaining provisions remain in full force and effect.

14.2 **No waiver.** No failure or delay in enforcing any provision is a waiver of it.

14.3 **Changes.** This notice may be updated with later versions of the Software. The version that accompanies
the copy of the Software You use applies to that copy.

14.4 **Entire notice.** This notice and the GPL together form the entire statement of terms on which the
Software is made available, and supersede any other statement about it.

14.5 **Survival.** Sections 2 to 9 and 14 continue to apply after You stop using the Software.
