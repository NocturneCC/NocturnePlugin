from __future__ import annotations

import unittest
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "website"))

from legacy_challenge_retirement import (
    REDIRECT_PAGE,
    remove_legacy_member_summary_consumer,
    rewrite_legacy_progress_links,
)


class LegacyChallengeRetirementTests(unittest.TestCase):
    def test_redirect_preserves_rsn_and_defaults_to_current_challenges_page(self):
        self.assertIn("/challenge-member.html?rsn=", REDIRECT_PAGE)
        self.assertIn("encodeURIComponent(rsn)", REDIRECT_PAGE)
        self.assertIn("'/challenges.html'", REDIRECT_PAGE)
        self.assertNotIn("nocturne-challenge-progress.html", REDIRECT_PAGE)
        committed_page = Path(__file__).resolve().parents[2] / "website" / "nocturne-challenge-progress.html"
        page = committed_page.read_text(encoding="utf-8")
        self.assertIn("encodeURIComponent(rsn)", page)
        self.assertIn("'/challenges.html'", page)
        self.assertNotIn("/api/nocturne-challenges/", page)

    def test_internal_member_and_navigation_links_are_retargeted(self):
        source = '''<a href="/nocturne-challenge-progress.html">Progress</a>
location.href=`/nocturne-challenge-progress.html?rsn=${encodeURIComponent(rsn)}`;'''
        result = rewrite_legacy_progress_links(source)
        self.assertIn('href="/challenges.html"', result)
        self.assertIn("/challenge-member.html?rsn=${encodeURIComponent(rsn)}", result)
        self.assertNotIn("nocturne-challenge-progress.html", result)

    def test_legacy_member_viewer_csv_consumer_is_removed(self):
        source = '''async function loadNocturneChallengeCard(rsn) {
  const response = await fetch(`/api/nocturne-challenges/summary?rsn=${encodeURIComponent(rsn)}`);
}

async function loadMember() {
  loadNocturneChallengeCard(member.rsn || rsn);
}'''
        result = remove_legacy_member_summary_consumer(source)
        self.assertNotIn("/api/nocturne-challenges/summary", result)
        self.assertNotIn("loadNocturneChallengeCard", result)
        self.assertIn("async function loadMember()", result)

    def test_ambiguous_member_consumer_fails_closed(self):
        with self.assertRaises(ValueError):
            remove_legacy_member_summary_consumer("no known legacy source structure")


if __name__ == "__main__":
    unittest.main()
