import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import announcement_admin_ui_support as support


class AnnouncementAdminUiSupportTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.site = self.root / "website"
        self.site.mkdir()
        self.admin = self.site / "admin.html"
        self.original = ('<!doctype html>\n<section>Event tools</section>\n'
                         + support.ANCHOR + '\n  <div>existing card</div>\n')
        self.admin.write_text(self.original)
        self.admin.chmod(0o644)
        self.backups = self.root / "backups"
        self.backups.mkdir(mode=0o700)
        self.page_source = Path(__file__).with_name(support.PAGE_NAME)

    def test_dry_run_apply_idempotency_and_exact_rollback(self):
        before = self.admin.read_bytes()
        plan = support.install(self.site, self.page_source, self.backups)
        self.assertEqual("not_applied", plan["state"])
        self.assertEqual(before, self.admin.read_bytes())
        self.assertFalse((self.site / support.PAGE_NAME).exists())
        with patch.object(support.os, "geteuid", return_value=0):
            applied = support.install(self.site, self.page_source, self.backups, apply=True)
        self.assertEqual("already_applied", applied["state"])
        self.assertTrue((self.site / support.PAGE_NAME).is_file())
        self.assertIn('href="/plugin-announcements-admin.html"', self.admin.read_text())
        self.assertEqual("already_applied", support.inspect(self.site, source_page=self.page_source)["state"])
        with patch.object(support.os, "geteuid", return_value=0):
            rolled = support.rollback(applied["backup"], self.site)
        self.assertEqual("rolled_back", rolled["state"])
        self.assertEqual(before, self.admin.read_bytes())
        self.assertFalse((self.site / support.PAGE_NAME).exists())

    def test_partial_navigation_and_symlink_targets_fail_closed(self):
        page = self.site / support.PAGE_NAME
        page.write_text("unexpected")
        with self.assertRaisesRegex(ValueError, "unexpected content"):
            support.inspect(self.site, source_page=self.page_source)
        page.unlink()
        page.symlink_to(self.page_source)
        with self.assertRaisesRegex(ValueError, "single-link regular"):
            support.inspect(self.site, source_page=self.page_source)

    def test_navigation_anchor_must_be_unique(self):
        with self.assertRaisesRegex(ValueError, "missing or ambiguous"):
            support.candidate_admin("<html></html>")

    def test_admin_page_uses_existing_api_and_text_only_preview(self):
        source = self.page_source.read_text(encoding="utf-8")
        self.assertIn("/admin/api/nocturne/plugin-announcements", source)
        self.assertIn("[Nocturne Announcement]", source)
        self.assertIn("textContent", source)
        self.assertNotIn("innerHTML", source)
        self.assertIn("window.confirm", source)
        self.assertIn("expected_revision", source)


if __name__ == "__main__":
    unittest.main()
