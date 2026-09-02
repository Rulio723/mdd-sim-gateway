"""The push that publishes a subscriber identifier is the one that has to be stopped.

CI has scanned for these since v1.3.15, but it reports after the fact: a push cannot be taken
back, and rewriting the branch afterwards leaves the object reachable by its SHA for a while.
A real MSISDN reached an open pull request that way. Worse, by the time it was noticed the
working tree was already clean — the value only survived in an earlier commit's blob — so a
tree scan would have called the branch safe. These cover the check against the commits being
pushed, and the hook that runs it.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
CHECKER = ROOT / "tools" / "check-subscriber-identifiers.sh"
HOOK = ROOT / "hooks" / "pre-push"

# A value the checker must reject, which rules out every fictional range it recognises --
# and which therefore must not be a number anyone could hold. NANP area codes cannot begin
# with 0 or 1, so 111 can never be assigned. If the allow-list ever grows to cover this, these
# tests fail and want a new fixture rather than a real number.
# Assembled rather than written out, because this file is itself scanned: a literal here
# would make the repository fail its own check. NANP area codes cannot begin with 0 or 1, so
# 111 can never be assigned to anyone -- which is what makes it safe to use as the value the
# checker is supposed to reject. If the allow-list ever grows to cover it, these tests fail
# and want a new fixture rather than a real number.
REJECTED = "+1" + "1" * 10
FICTIONAL = '+15555550100'   # NANP 555, which the allow-list recognises


def git(*args, cwd, **kwargs):
    return subprocess.run(("git",) + args, cwd=cwd, capture_output=True, text=True, **kwargs)


class PrePushPrivacyHookTests(unittest.TestCase):
    def setUp(self):
        self._temp = tempfile.TemporaryDirectory()
        self.addCleanup(self._temp.cleanup)
        self.repo = Path(self._temp.name) / "repo"
        (self.repo / "tools").mkdir(parents=True)
        (self.repo / "hooks").mkdir()
        shutil.copy(CHECKER, self.repo / "tools" / CHECKER.name)
        shutil.copy(HOOK, self.repo / "hooks" / "pre-push")
        # The modes git records, not whatever the checkout filesystem invented. Scripts in
        # tools/ are tracked without the executable bit, so a hook that executes the checker
        # directly fails with "Permission denied" on every ordinary filesystem -- and refuses
        # every push while blaming a subscriber identifier it never got to look for.
        os.chmod(self.repo / "tools" / CHECKER.name, 0o644)
        os.chmod(self.repo / "hooks" / "pre-push", 0o755)

        git("init", "-q", ".", cwd=self.repo)
        git("config", "user.email", "test@example.invalid", cwd=self.repo)
        git("config", "user.name", "test", cwd=self.repo)
        git("config", "core.hooksPath", "hooks", cwd=self.repo)
        self.commit("a.py", "value = 1", "base")
        self.base = git("rev-parse", "HEAD", cwd=self.repo).stdout.strip()

    def commit(self, name, body, message):
        (self.repo / name).write_text(body + "\n", encoding="utf-8")
        git("add", "-A", cwd=self.repo)
        git("commit", "-qm", message, cwd=self.repo)

    def check(self, *args):
        return subprocess.run(["sh", "tools/check-subscriber-identifiers.sh", *args],
                              cwd=self.repo, capture_output=True, text=True)

    def push(self):
        remote = Path(self._temp.name) / "remote.git"
        if not remote.exists():
            git("init", "-q", "--bare", str(remote), cwd=Path(self._temp.name))
            git("remote", "add", "origin", str(remote), cwd=self.repo)
        branch = git("rev-parse", "--abbrev-ref", "HEAD", cwd=self.repo).stdout.strip()
        return git("push", "origin", branch, cwd=self.repo)

    def test_a_value_removed_from_the_tree_is_still_caught_in_the_history(self):
        self.commit("a.py", f'peer = "{REJECTED}"', "introduce")
        self.commit("a.py", f'peer = "{FICTIONAL}"', "fix the working tree")

        # The tree is clean, which is exactly what made this worth a test.
        self.assertEqual(self.check("a.py").returncode, 0, self.check("a.py").stdout)

        result = self.check("--commits", f"{self.base}..HEAD")
        self.assertEqual(result.returncode, 1)
        self.assertIn(REJECTED, result.stdout)
        # The report has to name the file and the blob, or the commit cannot be found.
        self.assertIn("a.py", result.stdout)
        self.assertIn("blob", result.stdout)

    def test_a_fictional_value_is_not_reported(self):
        self.commit("a.py", f'peer = "{FICTIONAL}"', "fictional")
        result = self.check("--commits", f"{self.base}..HEAD")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_the_hook_refuses_the_push_that_would_publish_it(self):
        self.commit("a.py", f'peer = "{REJECTED}"', "introduce")
        self.commit("a.py", f'peer = "{FICTIONAL}"', "fix the working tree")

        result = self.push()
        self.assertNotEqual(result.returncode, 0, "the push must not succeed")
        # Which stream git forwards hook output on is git's business, not the hook's.
        reported = result.stdout + result.stderr
        self.assertIn("push refused", reported)
        self.assertIn(REJECTED, reported)

    def test_the_hook_lets_a_clean_push_through(self):
        self.commit("a.py", f'peer = "{FICTIONAL}"', "fictional")
        result = self.push()
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_a_check_that_cannot_run_refuses_without_inventing_a_finding(self):
        # Failing closed is right; claiming to have found a subscriber identifier when the
        # check never ran is not -- that reading cost an afternoon once already.
        self.commit("a.py", f'peer = "{FICTIONAL}"', "fictional")
        (self.repo / "tools" / CHECKER.name).write_text("#!/bin/sh\nexit 3\n", encoding="utf-8")

        result = self.push()
        reported = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, "an unverified push must not go out")
        self.assertIn("could not run", reported)
        self.assertNotIn("carry a subscriber identifier", reported)

    def test_the_hook_is_executable_and_wired_to_the_checker(self):
        # A hook without the executable bit is silently never run, which would leave the
        # repository believing it is protected.
        self.assertTrue(os.access(HOOK, os.X_OK), "hooks/pre-push must be executable")
        text = HOOK.read_text(encoding="utf-8")
        self.assertIn("sh tools/check-subscriber-identifiers.sh", text)
        self.assertIn("--commits", text)
        self.assertIn("core.hooksPath hooks",
                      (ROOT / "CONTRIBUTING.md").read_text(encoding="utf-8"),
                      "the hook only runs once a clone opts in; that has to be documented")


if __name__ == "__main__":
    unittest.main()
