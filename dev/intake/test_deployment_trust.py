import subprocess
from pathlib import Path
import tempfile
import unittest

from deployment_trust import verify_checkout


class DeploymentTrustTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name) / "repo"
        self.repo.mkdir()
        subprocess.run(["git", "init", "-q", str(self.repo)], check=True)
        (self.repo / "tracked").write_text("committed\n")
        subprocess.run(["git", "-C", str(self.repo), "add", "tracked"], check=True)
        subprocess.run(["git", "-C", str(self.repo), "-c", "user.name=Test",
                        "-c", "user.email=test@example.invalid", "commit", "-qm", "fixture"],
                       check=True)
        self.commit = subprocess.run(
            ["git", "-C", str(self.repo), "rev-parse", "HEAD"], check=True,
            stdout=subprocess.PIPE, text=True).stdout.strip()

    def test_exact_clean_head_is_required(self):
        self.assertTrue(verify_checkout(self.repo, self.commit)["clean"])
        for value in (self.commit[:12], self.commit.upper(), "0" * 40):
            with self.subTest(value=value), self.assertRaises((ValueError, subprocess.CalledProcessError)):
                verify_checkout(self.repo, value)

    def test_tracked_and_untracked_changes_are_rejected(self):
        (self.repo / "tracked").write_text("dirty\n")
        with self.assertRaisesRegex(ValueError, "dirty"):
            verify_checkout(self.repo, self.commit)
        subprocess.run(["git", "-C", str(self.repo), "restore", "tracked"], check=True)
        (self.repo / "untracked").write_text("dirty\n")
        with self.assertRaisesRegex(ValueError, "dirty"):
            verify_checkout(self.repo, self.commit)

    def test_subdirectory_is_not_accepted_as_the_repository_root(self):
        child = self.repo / "child"
        child.mkdir()
        with self.assertRaisesRegex(ValueError, "worktree root"):
            verify_checkout(child, self.commit)


if __name__ == "__main__":
    unittest.main()
