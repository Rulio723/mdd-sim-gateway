"""Bounded Docker-image retention after an install or one-click update."""

import subprocess
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import call, patch

from host import mdd_image_cleanup


class ImageCleanupTests(unittest.TestCase):
    def test_only_old_mdd_release_tags_are_removed_before_dangling_prune(self):
        listing = "\n".join([
            "mdd-sim-gateway/control:latest",
            "mdd-sim-gateway/control:previous",
            "mdd-sim-gateway/control:v1.5.1",
            "mdd-sim-gateway/control:v1.5.2",
            "mdd-sim-gateway/control:v1.5.3",
            "mdd-sim-gateway/engine:latest",
            "mdd-sim-gateway/engine:previous",
            "mdd-sim-gateway/engine-base:trusted",
            "ghcr.io/mddidd/mdd-sim-gateway-engine:v1.5.2",
            "ghcr.io/mddidd/mdd-sim-gateway-engine:v1.5.3",
            "example/control:v1.5.2",
            "node:<none>",
        ])
        completed = [SimpleNamespace(returncode=0, stdout=listing)] + [
            SimpleNamespace(returncode=0, stdout="") for _ in range(4)
        ]
        with patch.object(mdd_image_cleanup.subprocess, "run",
                          side_effect=completed) as run:
            self.assertTrue(mdd_image_cleanup.prune_superseded_images("1.5.3"))

        self.assertEqual(run.call_args_list, [
            call(["docker", "image", "ls", "--format", "{{.Repository}}:{{.Tag}}"],
                 text=True, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL),
            call(["docker", "image", "rm", "ghcr.io/mddidd/mdd-sim-gateway-engine:v1.5.2"],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
            call(["docker", "image", "rm", "mdd-sim-gateway/control:v1.5.1"],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
            call(["docker", "image", "rm", "mdd-sim-gateway/control:v1.5.2"],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
            call(["docker", "image", "prune", "--force", "--filter",
                  "label=io.mdd-sim-gateway.managed=true"],
                 stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL),
        ])

    def test_cleanup_is_best_effort_but_reports_any_failed_step(self):
        completed = [
            SimpleNamespace(returncode=0, stdout="mdd-sim-gateway/control:v1.5.2\n"),
            SimpleNamespace(returncode=1, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
        with patch.object(mdd_image_cleanup.subprocess, "run",
                          side_effect=completed) as run:
            self.assertFalse(mdd_image_cleanup.prune_superseded_images("1.5.3"))
        self.assertEqual(run.call_args_list[-1].args[0],
                         ["docker", "image", "prune", "--force", "--filter",
                          "label=io.mdd-sim-gateway.managed=true"])

    def test_release_transition_prunes_only_default_dangling_build_cache(self):
        completed = [
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
            SimpleNamespace(returncode=0, stdout=""),
        ]
        with patch.object(mdd_image_cleanup.subprocess, "run",
                          side_effect=completed) as run:
            self.assertTrue(mdd_image_cleanup.prune_superseded_images(
                "1.5.3", prune_build_cache=True))
        self.assertEqual(run.call_args_list[-1], call(
            ["docker", "builder", "prune", "--force"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        self.assertNotIn("--all", run.call_args_list[-1].args[0])

    def test_listing_failure_aborts_without_removing_anything(self):
        with patch.object(mdd_image_cleanup.subprocess, "run",
                          return_value=SimpleNamespace(returncode=1, stdout="")) as run:
            self.assertFalse(mdd_image_cleanup.prune_superseded_images("1.5.3"))
        run.assert_called_once()

    def test_invalid_version_never_invokes_docker(self):
        with patch.object(mdd_image_cleanup.subprocess, "run") as run:
            self.assertFalse(mdd_image_cleanup.prune_superseded_images("../latest"))
        run.assert_not_called()

    def test_new_installer_performs_cleanup_before_reporting_reload_complete(self):
        installer = (Path(__file__).resolve().parent.parent / "install.sh").read_text(
            encoding="utf-8")
        start = installer.index("cmd_reload() {")
        end = installer.index("\n}\n", start)
        reload_body = installer[start:end]
        cleanup = reload_body.index("cleanup_release_artifacts")
        self.assertLess(cleanup, reload_body.index('info "reload complete (data preserved)"'))
        helper_start = installer.index("cleanup_release_artifacts() {")
        helper_end = installer.index("\n}\n", helper_start)
        helper = installer[helper_start:helper_end]
        self.assertIn("--prune-build-cache", helper)


if __name__ == "__main__":
    unittest.main()
