"""An upgrade must put the new engine image into service, not merely build it.

A running container keeps the image it was started from. Rebuilding the image while leaving
the containers alone ships nothing: every line goes on serving the previous dialplan while
the control plane reports the new version. That mismatch is invisible from the UI — a user
whose engine predated service-code support saw the feature fail and was told "the carrier
does not support this code", when the request had never left the gateway.
"""
import re
import unittest
from pathlib import Path

INSTALL = (Path(__file__).resolve().parent.parent / "install.sh").read_text(encoding="utf-8")


def _body(name: str) -> str:
    """The text of a shell function, up to the next top-level closing brace."""
    start = INSTALL.index(f"{name}() {{")
    return INSTALL[start:INSTALL.index("\n}\n", start)]


class EngineImageRefreshTests(unittest.TestCase):
    def test_rebuilding_the_image_forces_the_containers_to_be_re_created(self):
        # The decision must follow what actually happened, not a flag the operator has to
        # know to pass: nobody upgrading reads release notes for "--engines".
        reload_body = _body("cmd_reload")
        self.assertIn('[ "$ENGINE_IMAGE_CHANGED" = 1 ]', reload_body)
        removal = reload_body.index("docker rm -f")
        condition = reload_body.rindex("if [", 0, removal)
        self.assertIn("ENGINE_IMAGE_CHANGED", reload_body[condition:removal])

    def test_removed_containers_trigger_a_control_plane_rescan(self):
        reload_body = _body("cmd_reload")
        removal = reload_body.index("docker rm -f")
        restart = reload_body.index("restart_control_plane", removal)
        self.assertLess(removal, restart)
        self.assertIn('removed=$((removed + 1))', reload_body[removal:restart])
        self.assertIn('if [ "$removed" -gt 0 ]', reload_body[removal:restart])

    def test_the_rescan_does_not_restart_the_orchestrator(self):
        restart = _body("restart_control_plane")
        self.assertIn("systemctl restart mdd-sim-gateway-control", restart)
        self.assertIn('docker restart "$CONTROL_NAME"', restart)
        self.assertNotIn("orchestrator", restart)

    def test_every_rebuild_path_reports_that_it_replaced_the_image(self):
        ensure = _body("ensure_engine_image")
        # Overlay refresh, the pre-fingerprint adoption path, the offline overlay and the
        # full build all replace the image; each has to say so or the containers are kept.
        # Four local build/overlay paths plus the verified CI-distributed image path.
        self.assertEqual(ensure.count("ENGINE_IMAGE_CHANGED=1"), 5, ensure)

    def test_a_distributed_image_is_identity_checked_before_activation(self):
        ensure = _body("ensure_engine_image")
        distributed = ensure.index('MDD_ENGINE_DISTRIBUTION_IMAGE')
        activated = ensure.index('docker tag "$distributed" "$ENGINE_IMAGE"', distributed)
        identity = ensure.index('identity=$(docker image inspect "$distributed"', distributed)
        self.assertLess(identity, activated)
        self.assertIn('[ "$identity" = "$expected" ]', ensure[identity:activated])
        self.assertIn('"$ENGINE_IMAGE:previous"', ensure[identity:activated])

    def test_full_build_passes_explicit_source_repository_overrides(self):
        ensure = _body("ensure_engine_image")
        self.assertIn('[ -n "${PJPROJECT_REPOSITORY:-}" ]', ensure)
        self.assertIn(
            'set -- "$@" --build-arg "PJPROJECT_REPOSITORY=$PJPROJECT_REPOSITORY"',
            ensure,
        )
        self.assertIn('[ -n "${ASTERISK_REPOSITORY:-}" ]', ensure)
        self.assertIn(
            'set -- "$@" --build-arg "ASTERISK_REPOSITORY=$ASTERISK_REPOSITORY"',
            ensure,
        )

    def test_reusing_an_unchanged_image_leaves_the_lines_alone(self):
        # The flag starts at 0 and the reuse path returns before any assignment, so an
        # ordinary reload does not interrupt calls for nothing.
        ensure = _body("ensure_engine_image")
        self.assertIn("ENGINE_IMAGE_CHANGED=0", ensure)
        reuse = ensure.index("matches this checkout — reusing")
        # The only earlier replacement is explicitly gated by the updater's distributed-image
        # environment variable; the ordinary path itself returns without changing the flag.
        prefix = ensure[:reuse]
        self.assertEqual(prefix.count("ENGINE_IMAGE_CHANGED=1"), 1)
        self.assertIn('if [ -n "${MDD_ENGINE_DISTRIBUTION_IMAGE:-}" ]', prefix)

    def test_no_engines_is_overridden_only_for_the_release_handoff(self):
        reload_body = _body("cmd_reload")
        self.assertIn('PRESERVE_ENGINES=1', reload_body)
        handoff_gate = reload_body.index(
            '[ "$PRESERVE_ENGINES" = 1 ] && [ -f "$ENGINE_HANDOFF_MANIFEST" ]')
        handoff = reload_body.index("handoff_release_engine", handoff_gate)
        preserve = reload_body.index('if [ "$PRESERVE_ENGINES" = 1 ]; then', handoff)
        self.assertIn("PRESERVE_ENGINES=0", reload_body[handoff:preserve])
        self.assertIn('elif [ "$(host_arch)" != arm64 ]; then', reload_body[handoff_gate:preserve])
        self.assertLess(reload_body.index("refreshing the native image locally", handoff_gate),
                        handoff)
        self.assertLess(preserve, reload_body.index('[ "$ENGINE_IMAGE_CHANGED" = 1 ]'))

    def test_release_handoff_reuses_the_old_updaters_private_route_file(self):
        handoff = _body("handoff_release_engine")
        self.assertIn('network_file="$MDD_DATA_DIR/update/network.json"', handoff)
        self.assertIn('--network-config "$network_file" --engine-handoff', handoff)
        self.assertIn("MDD_ENGINE_DISTRIBUTION_IMAGE=$distributed", handoff)


if __name__ == "__main__":
    unittest.main()
