"""Deterministic source transforms for retiring the CSV-backed progress page.

The website tree is deployed separately. These functions are deliberately
offline-only inputs to a later reviewed website deployment; they do not write
to the website or production from this repository.
"""

from __future__ import annotations

import re


LEGACY_PAGE = "/nocturne-challenge-progress.html"
REDIRECT_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Nocturne Challenges</title><script>
(() => {
  const rsn = new URLSearchParams(location.search).get('rsn');
  location.replace(rsn
    ? `/challenge-member.html?rsn=${encodeURIComponent(rsn)}`
    : '/challenges.html');
})();
</script></head><body><p>Opening the current Nocturne Challenges page…</p></body></html>
"""

_CARD_BLOCK = re.compile(
    r"async function loadNocturneChallengeCard\(rsn\) \{.*?\n\}\n\n(?=async function loadMember\(\))",
    re.DOTALL,
)


def rewrite_legacy_progress_links(source: str) -> str:
    """Point legacy page links at current Midgard-backed public pages."""
    updated = source.replace(
        "/nocturne-challenge-progress.html?rsn=",
        "/challenge-member.html?rsn=",
    ).replace(LEGACY_PAGE, "/challenges.html")
    if LEGACY_PAGE in updated:
        raise ValueError("legacy progress link remains")
    return updated


def remove_legacy_member_summary_consumer(source: str) -> str:
    """Remove the member-viewer card that fetched the retired CSV summary."""
    updated, count = _CARD_BLOCK.subn(
        "// Challenge rank/progress is provided by the Midgard-backed profile.\n\n",
        source,
    )
    if count != 1:
        raise ValueError("legacy member summary block is missing or ambiguous")
    updated, calls = re.subn(
        r"^\s*loadNocturneChallengeCard\(member\.rsn \|\| rsn\);\s*\n",
        "",
        updated,
        flags=re.MULTILINE,
    )
    if calls != 1 or "loadNocturneChallengeCard" in updated:
        raise ValueError("legacy member summary call is missing or ambiguous")
    if "/api/nocturne-challenges/summary" in updated:
        raise ValueError("legacy summary API consumer remains")
    return updated
