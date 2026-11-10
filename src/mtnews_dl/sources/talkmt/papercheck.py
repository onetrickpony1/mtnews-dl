"""Which paper is this? The l-orizzont and it-Torca mastheads are images, so the paper's
name is counted in the whole document's text instead."""

import re

from ...common.pdfcheck import document_text, fold

PAPER_NAMES = {"l-orizzont": re.compile(r"orizzont", re.I),
               "it-torca": re.compile(r"tor[cċ]a", re.I)}


def publication_check(path, publication) -> str:
    """confirmed | mismatch | unconfirmed | not checked (supplements)."""
    if publication not in PAPER_NAMES:
        return "not checked"
    try:
        text = fold(document_text(path))
    except (OSError, ValueError):
        return "unconfirmed"
    counts = {pub: len(rx.findall(text)) for pub, rx in PAPER_NAMES.items()}
    own = counts[publication]
    other = max(v for k, v in counts.items() if k != publication)
    if own and own >= other:
        return "confirmed"
    if other >= 3 and other > 2 * own:
        return "mismatch"
    return "unconfirmed"
