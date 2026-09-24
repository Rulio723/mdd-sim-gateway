import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from control.app import config, main, operations
from host import mdd_container_update


class ContainerComposeRewriteTests(unittest.TestCase):
    def test_only_release_image_references_change(self):
        source = """name: mdd-sim-gateway
services:
  control:
    image: ghcr.io/mddidd/mdd-sim-gateway-control:v1.0.0
    environment:
      MDD_ENGINE_IMAGE: ghcr.io/mddidd/mdd-sim-gateway-engine:v1.0.0
      KEEP_ME: ghcr.io/example/unrelated:v1
  hardware:
    image: 'ghcr.io/mddidd/mdd-sim-gateway-hardware:v1.0.0' # keep this comment
  egress:
    image: ghcr.io/mddidd/mdd-sim-gateway-egress@sha256:""" + "a" * 64 + "\n"
        images = mdd_container_update.canonical_images("MddIdd/mdd-sim-gateway", "2.0.0")

        updated = mdd_container_update.rewrite_compose(source, images)

        for component, image in images.items():
            self.assertIn(image, updated)
        self.assertIn("KEEP_ME: ghcr.io/example/unrelated:v1", updated)
        self.assertIn("' # keep this comment", updated)
        self.assertNotIn("mdd-sim-gateway-control:v1.0.0", updated)

    def test_incomplete_compose_is_rejected_before_mutation(self):
        with self.assertRaises(mdd_container_update.mdd_update.UpdateError):
            mdd_container_update.rewrite_compose(
                "services:\n  control:\n    image: ghcr.io/x/mdd-sim-gateway-control:v1\n",
                mdd_container_update.canonical_images("x/y", "2.0.0"))

    def test_exactly_one_project_compose_file_is_required(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "docker-compose.yml").write_text("services: {}\n")
            self.assertEqual(mdd_container_update.find_compose(root).name, "docker-compose.yml")
            (root / "compose.yaml").write_text("services: {}\n")
            with self.assertRaises(mdd_container_update.mdd_update.UpdateError):
                mdd_container_update.find_compose(root)


class ContainerUpdateLaunchTests(unittest.TestCase):
    def test_control_launches_a_detached_owned_helper_on_both_project_networks(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            orchestrator = root / "orchestrator"
            orchestrator.mkdir()
            (orchestrator / "update-request.json").write_text(json.dumps({
                "version": "2.0.0", "repository": "MddIdd/mdd-sim-gateway",
                "network": {"route": "direct", "proxy_url": ""},
                "networks": [{"route": "direct", "proxy_url": ""}],
                "asset_sizes": {"SHA256SUMS": 123},
            }))
            control = Mock()
            control.image.id = "sha256:control"
            control.attrs = {"Config": {"Labels": {
                "io.mdd-sim-gateway.managed": "true",
                "io.mdd-sim-gateway.component": "control"}}}
            helper = Mock()
            client = Mock()
            client.containers.get.return_value = control
            client.containers.create.return_value = helper
            network = Mock()
            client.networks.get.return_value = network
            env = {"MDD_CONTAINER_STACK": "1", "MDD_HOST_DATA": "/volume1/docker/mdd",
                   "MDD_ENGINE_NETWORK": "mdd-engine", "MDD_ENGINE_DIRECT_NETWORK": "mdd-uplink"}
            with patch.object(config, "DATA_DIR", str(root)), patch.dict(os.environ, env), \
                    patch.object(operations.docker, "from_env", return_value=client):
                result = operations.launch_container_update()

            self.assertTrue(result["ok"])
            create = client.containers.create.call_args
            self.assertEqual(create.args[0], "sha256:control")
            self.assertEqual(create.kwargs["network"], "mdd-uplink")
            self.assertEqual(create.kwargs["volumes"]["/volume1/docker/mdd"]["bind"], "/data")
            self.assertIn("/app/host/mdd_container_update.py", create.kwargs["command"])
            client.networks.get.assert_called_once_with("mdd-engine")
            network.connect.assert_called_once_with(helper)
            helper.start.assert_called_once_with()
            self.assertFalse((orchestrator / "update-request.json").exists())
            saved = json.loads((root / "update/network.json").read_text())
            self.assertEqual(saved["asset_sizes"], {"SHA256SUMS": 123})

    def test_invalid_host_data_fails_without_starting_docker(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "orchestrator").mkdir()
            (root / "orchestrator/update-request.json").write_text(json.dumps({
                "version": "2.0.0", "repository": "MddIdd/mdd-sim-gateway"}))
            with patch.object(config, "DATA_DIR", str(root)), patch.dict(
                    os.environ, {"MDD_HOST_DATA": "relative/path"}, clear=False), \
                    patch.object(operations.docker, "from_env") as docker_client:
                result = operations.launch_container_update()
            self.assertFalse(result["ok"])
            docker_client.assert_not_called()


class ContainerUpdateRollbackTests(unittest.TestCase):
    def test_failed_base_recreation_restores_the_original_compose(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "update").mkdir()
            original = """services:
  control:
    image: ghcr.io/mddidd/mdd-sim-gateway-control:v1.0.0
    environment:
      MDD_ENGINE_IMAGE: ghcr.io/mddidd/mdd-sim-gateway-engine:v1.0.0
  hardware:
    image: ghcr.io/mddidd/mdd-sim-gateway-hardware:v1.0.0
  egress:
    image: ghcr.io/mddidd/mdd-sim-gateway-egress:v1.0.0
"""
            (root / "docker-compose.yml").write_text(original)
            network = root / "update/network.json"
            network.write_text(json.dumps({"route": "direct", "routes": [
                {"route": "direct", "proxy_url": ""}]}))
            base = {name: SimpleNamespace(image=SimpleNamespace(id=f"sha256:old-{name}"))
                    for name in mdd_container_update.BASE_COMPONENTS}
            client = Mock()
            client.containers.get.side_effect = lambda name: base[name.removeprefix(
                "mdd-sim-gateway-")]
            client.containers.list.return_value = []
            status = mdd_container_update.mdd_update.Status(
                root / "orchestrator/update-status.json", "2.0.0")

            def fetch(_url, destination, *_args, **_kwargs):
                destination.write_bytes(b"verified")
                return 0

            with patch.object(mdd_container_update.docker, "from_env", return_value=client), \
                    patch.object(mdd_container_update.mdd_update, "fetch_release_asset",
                                 side_effect=fetch), \
                    patch.object(mdd_container_update.mdd_update, "verify_release_file"), \
                    patch.object(mdd_container_update, "docker_root_free_bytes",
                                 return_value=10 * 1024 ** 3), \
                    patch.object(mdd_container_update, "run"), \
                    patch.object(mdd_container_update, "verify_and_tag_image",
                                 side_effect=lambda _c, component, *_a: f"sha256:new-{component}"), \
                    patch("control.app.operations.create_local_backup",
                          return_value={"name": "backup.tar.gz"}), \
                    patch.object(mdd_container_update, "compose_up",
                                 side_effect=[mdd_container_update.mdd_update.UpdateError(
                                     "new Control unhealthy"), None]) as compose_up, \
                    patch.object(mdd_container_update, "wait_container") as wait:
                with self.assertRaises(mdd_container_update.mdd_update.UpdateError):
                    mdd_container_update.perform(
                        root, "2.0.0", "MddIdd/mdd-sim-gateway", network, status)

            self.assertEqual((root / "docker-compose.yml").read_text(), original)
            self.assertEqual(compose_up.call_count, 2)
            self.assertEqual(wait.call_count, len(mdd_container_update.BASE_COMPONENTS))
            failed = json.loads((root / "orchestrator/update-status.json").read_text())
            self.assertEqual(failed["state"], "failed")
            self.assertTrue(failed["rollback_succeeded"])

    def test_rollout_recreates_only_running_engines_explicitly(self):
        running = Mock(name="running")
        running.name = "mdd-sim-gateway-engine-line-1"
        stopped = Mock(name="stopped")
        stopped.name = "mdd-sim-gateway-engine-line-2"
        client = Mock()
        client.containers.list.return_value = [running]
        status = Mock()

        with patch.object(mdd_container_update, "recreate_engine") as recreate, \
                patch.object(mdd_container_update, "wait_container") as wait:
            mdd_container_update.roll_engines(client, "sha256:new", status)

        client.containers.list.assert_called_once_with(filters={"label": [
            "io.mdd-sim-gateway.managed=true",
            "io.mdd-sim-gateway.component=engine"]})
        running.remove.assert_called_once_with(force=True)
        stopped.remove.assert_not_called()
        recreate.assert_called_once_with(client, running.name)
        wait.assert_called_once_with(client, running.name, "sha256:new", timeout=240)

    def test_recreate_engine_runs_control_lifecycle_code(self):
        control = Mock()
        control.exec_run.return_value = SimpleNamespace(exit_code=0, output=b"")
        client = Mock()
        client.containers.get.return_value = control

        mdd_container_update.recreate_engine(
            client, "mdd-sim-gateway-engine-line_1")

        command = control.exec_run.call_args.args[0]
        self.assertEqual(command[:2], ["python", "-c"])
        self.assertEqual(command[-1], "line_1")
        self.assertIn("engine.start", command[2])

    def test_recreate_engine_accepts_docker_valid_dotted_instance_id(self):
        control = Mock()
        control.exec_run.return_value = SimpleNamespace(exit_code=0, output=b"")
        client = Mock()
        client.containers.get.return_value = control

        mdd_container_update.recreate_engine(
            client, "mdd-sim-gateway-engine-line.1")

        self.assertEqual(control.exec_run.call_args.args[0][-1], "line.1")


class ContainerUpdateApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_container_apply_launches_the_detached_executor(self):
        with patch.object(main.update_check, "request_apply",
                          return_value={"ok": True, "version": "2.0.0"}), \
                patch.object(main.operations, "container_stack_enabled", return_value=True), \
                patch.object(main.operations, "launch_container_update",
                             return_value={"ok": True, "executor": "container"}) as launch:
            result = await main.api_system_update_apply({"version": "2.0.0"})
        self.assertTrue(result["ok"])
        launch.assert_called_once_with()

    async def test_container_launch_failure_is_returned_to_the_dialog(self):
        with patch.object(main.update_check, "request_apply", return_value={"ok": True}), \
                patch.object(main.operations, "container_stack_enabled", return_value=True), \
                patch.object(main.operations, "launch_container_update",
                             return_value={"ok": False, "phase": "launch",
                                           "error": "docker unavailable"}):
            result = await main.api_system_update_apply({"version": "2.0.0"})
        self.assertFalse(result["ok"])
        self.assertEqual(result["error"], "docker unavailable")


if __name__ == "__main__":
    unittest.main()
