import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import requests

from control.app import config, update_check


class _Response:
    def __init__(self, payload, status=200):
        self.payload, self.status_code = payload, status

    def json(self):
        return self.payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(response=self)


class UpdateCheckTests(unittest.TestCase):
    def setUp(self):
        update_check._cache = None
        update_check._releases_cache = None
        update_check._stars_cache = None
        update_check._stars_checked_at = 0
        direct = patch.object(update_check, "_network_selection", return_value={
            "proxy_mode": "direct", "proxy_profile_id": ""})
        direct.start()
        self.addCleanup(direct.stop)

    def test_newer_release_is_reported_without_applying_it(self):
        newer = list(update_check._version_tuple(update_check.VERSION))
        newer[-1] += 1
        payload = {"tag_name": "v" + ".".join(map(str, newer)),
                   "html_url": "https://example.invalid/release",
                   "published_at": "2026-08-01T00:00:00Z", "body": "notes",
                   "assets": [{"name": "mdd-sim-gateway-v9.9.9.tar.gz", "size": 1234}]}
        with patch("control.app.update_check.requests.Session.get",
                   return_value=_Response(payload)):
            result = update_check.check(True)
        self.assertTrue(result["update_available"])
        self.assertEqual(result["current"], update_check.VERSION)
        self.assertEqual(result["asset_sizes"]["mdd-sim-gateway-v9.9.9.tar.gz"], 1234)
        self.assertNotIn("apply", result)

    def test_bilingual_release_notes_are_not_truncated_at_the_old_limit(self):
        newer = list(update_check._version_tuple(update_check.VERSION))
        newer[-1] += 1
        notes = "中" * 4500 + "\n---\n" + "English release notes"
        payload = {"tag_name": "v" + ".".join(map(str, newer)), "body": notes}
        with patch("control.app.update_check.requests.Session.get",
                   return_value=_Response(payload)):
            result = update_check.check(True)
        self.assertEqual(result["notes"], notes)

    def test_release_notes_still_have_a_bounded_size(self):
        newer = list(update_check._version_tuple(update_check.VERSION))
        newer[-1] += 1
        notes = "x" * (update_check._MAX_RELEASE_NOTES_CHARS + 10)
        payload = {"tag_name": "v" + ".".join(map(str, newer)), "body": notes}
        with patch("control.app.update_check.requests.Session.get",
                   return_value=_Response(payload)):
            result = update_check.check(True)
        self.assertEqual(len(result["notes"]), update_check._MAX_RELEASE_NOTES_CHARS)

    def test_semantic_comparison(self):
        self.assertGreater(update_check._version_tuple("v1.10.0"), update_check._version_tuple("1.9.9"))

    def test_final_release_is_newer_than_rc_with_the_same_core_version(self):
        self.assertGreater(update_check._version_key("1.6.1"),
                           update_check._version_key("1.6.1-rc2"))
        self.assertGreater(update_check._version_key("1.6.1-rc10"),
                           update_check._version_key("1.6.1-rc2"))
        payload = {"tag_name": "v1.6.1", "prerelease": False}
        with patch.object(update_check, "VERSION", "1.6.1-rc2"), \
                patch("control.app.update_check.requests.Session.get",
                      return_value=_Response(payload)):
            result = update_check.check(True)
        self.assertTrue(result["update_available"])

    def test_update_network_accepts_library_and_country_selections(self):
        self.assertEqual(update_check.validate_network_settings(None)["proxy_mode"], "auto")
        with self.assertRaises(update_check.UpdateNetworkError):
            update_check.validate_network_settings({"proxy_mode": "library",
                                                     "proxy_profile_id": ""})
        self.assertEqual(update_check.validate_network_settings({
            "proxy_mode": "country", "proxy_country": "US"}), {
                "proxy_mode": "country", "proxy_profile_id": "", "proxy_country": "us"})
        with self.assertRaises(update_check.UpdateNetworkError):
            update_check.validate_network_settings({"proxy_mode": "country",
                                                     "proxy_country": ""})

    def test_complete_update_settings_defaults_to_automatic_main_releases(self):
        self.assertEqual(update_check.validate_update_settings(None), {
            "proxy_mode": "auto", "proxy_profile_id": "",
            "update_mode": "automatic", "version_scope": "main",
        })
        with self.assertRaises(update_check.UpdateNetworkError):
            update_check.validate_update_settings({"update_mode": "sometimes"})
        with self.assertRaises(update_check.UpdateNetworkError):
            update_check.validate_update_settings({"version_scope": "sometimes"})
        self.assertEqual(update_check.validate_update_settings({
            "update_mode": "automatic", "version_scope": "feature",
        })["version_scope"], "main")

    def test_auto_update_requires_separate_matching_promotion(self):
        info = {"update_available": True, "latest": "1.5.0",
                "network": {"proxy_mode": "direct", "proxy_profile_id": ""}}
        session = MagicMock()
        session.get.return_value = _Response({
            "schema": 1,
            "release": {"version": "1.5.0", "kind": "main"},
            "auto_update": {"version": "1.5.0", "not_before": "2026-09-01T00:00:00Z"},
        })
        with patch.object(update_check, "_session", return_value=session):
            early = update_check.auto_update_authorization(
                info, datetime(2026, 8, 31, tzinfo=timezone.utc))
            ready = update_check.auto_update_authorization(
                info, datetime(2026, 9, 2, tzinfo=timezone.utc))
        self.assertFalse(early["authorized"])
        self.assertEqual(early["reason"], "waiting")
        self.assertEqual(early["release_kind"], "main")
        self.assertTrue(ready["authorized"])

    def test_channel_policy_authorizes_main_independently_of_latest_patch(self):
        info = {"update_available": True, "latest": "1.6.0",
                "network": {"proxy_mode": "direct", "proxy_profile_id": ""}}
        policy = {
            "schema": 1,
            "release": {"version": "1.6.1", "kind": "patch"},
            "auto_update": {"version": "1.6.1", "not_before": "2026-09-01T00:00:00Z"},
            "channels": {
                "all": {"version": "1.6.1", "not_before": "2026-09-01T00:00:00Z"},
                "main": {"version": "1.6.0", "not_before": "2026-08-01T00:00:00Z"},
            },
        }
        result = update_check.auto_update_authorization(
            info, datetime(2026, 8, 27, tzinfo=timezone.utc), scope="main", policy=policy)
        self.assertTrue(result["authorized"])
        self.assertEqual(result["release_kind"], "main")
        self.assertEqual(result["target_version"], "1.6.0")

    def test_tagged_release_lookup_can_fetch_an_older_configured_main(self):
        payload = {"tag_name": "v1.6.0", "html_url": "https://example.invalid/v1.6.0",
                   "assets": [{"name": "mdd-sim-gateway-v1.6.0.tar.gz", "size": 1024}]}
        session = MagicMock()
        session.get.return_value = _Response(payload)
        # This exercises an older installed gateway fetching the independently configured
        # feature release. Keep the installed version explicit so bumping the repository's
        # VERSION to that target does not silently turn the fixture into a no-update case.
        with patch.object(update_check, "VERSION", "1.5.4"), \
                patch.object(update_check, "_network_candidates", return_value=[{
                "proxy_mode": "direct", "proxy_profile_id": ""}]), \
                patch.object(update_check, "_session", return_value=session):
            result = update_check.check_release("1.6.0")
        self.assertTrue(result["ok"])
        self.assertEqual(result["latest"], "1.6.0")
        self.assertTrue(result["update_available"])
        self.assertIn("/releases/tags/v1.6.0", session.get.call_args.args[0])

    def test_tagged_main_target_must_be_a_stable_release(self):
        session = MagicMock()
        session.get.return_value = _Response({"tag_name": "v1.6.0", "prerelease": True})
        with patch.object(update_check, "_network_candidates", return_value=[{
                "proxy_mode": "direct", "proxy_profile_id": ""}]), \
                patch.object(update_check, "_session", return_value=session):
            result = update_check.check_release("1.6.0")
        self.assertFalse(result["ok"])
        self.assertFalse(result["update_available"])

    def test_manual_release_list_exposes_latest_stable_and_published_tests(self):
        payload = [
            {"tag_name": "v1.7.0-rc2", "prerelease": True, "body": "test two"},
            {"tag_name": "v1.7.0-rc1", "prerelease": True, "body": "test one"},
            {"tag_name": "v1.6.0", "prerelease": False, "body": "stable"},
            {"tag_name": "v1.5.0", "prerelease": False, "body": "old stable"},
            {"tag_name": "v1.8.0-dev", "prerelease": True, "draft": True},
        ]
        with patch("control.app.update_check.requests.Session.get",
                   return_value=_Response(payload)):
            result = update_check.releases(True)
        self.assertTrue(result["ok"])
        self.assertEqual([item["latest"] for item in result["releases"]],
                         ["1.7.0-rc2", "1.7.0-rc1", "1.6.0"])
        self.assertTrue(result["releases"][0]["prerelease"])
        self.assertFalse(result["releases"][-1]["prerelease"])

    def test_explicit_manual_lookup_allows_a_test_release_and_older_stable(self):
        session = MagicMock()
        session.get.side_effect = [
            _Response({"tag_name": "v1.7.0-rc1", "prerelease": True}),
            _Response({"tag_name": "v1.5.0", "prerelease": False}),
        ]
        with patch.object(update_check, "_network_candidates", return_value=[{
                "proxy_mode": "direct", "proxy_profile_id": ""}]), \
                patch.object(update_check, "_session", return_value=session), \
                patch.object(update_check, "VERSION", "1.6.1-rc1"):
            test = update_check.check_release(
                "1.7.0-rc1", allow_prerelease=True, allow_older=True)
            stable = update_check.check_release(
                "1.5.0", allow_prerelease=True, allow_older=True)
        self.assertTrue(test["update_available"])
        self.assertTrue(test["prerelease"])
        self.assertTrue(stable["update_available"])
        self.assertFalse(stable["prerelease"])

    def test_unpromoted_release_cannot_auto_update(self):
        info = {"update_available": True, "latest": "1.5.0",
                "network": {"proxy_mode": "direct", "proxy_profile_id": ""}}
        session = MagicMock()
        session.get.return_value = _Response({
            "schema": 1,
            "release": {"version": "1.5.0", "kind": "main"},
            "auto_update": {"version": "1.4.9", "not_before": "2026-01-01T00:00:00Z"},
        })
        with patch.object(update_check, "_session", return_value=session):
            result = update_check.auto_update_authorization(info)
        self.assertFalse(result["authorized"])
        self.assertEqual(result["reason"], "not_promoted")

    def test_repository_policy_keeps_legacy_and_all_channel_in_sync(self):
        policy = __import__("json").loads(
            (Path(__file__).resolve().parents[1] / "update-policy.json").read_text())
        self.assertEqual(policy["auto_update"], policy["channels"]["all"])
        all_version = policy["channels"]["all"]["version"]
        self.assertEqual(
            update_check._policy_target(
                policy, "all", all_version),
            all_version,
        )
        if policy["release"]["version"] != all_version:
            self.assertEqual(
                update_check._policy_target(
                    policy, "all", policy["release"]["version"]),
                "",
            )
        self.assertEqual(
            update_check._policy_target(
                policy, "main", policy["release"]["version"]),
            policy["channels"]["main"]["version"],
        )

    def test_repository_can_be_overridden_without_changing_the_ui(self):
        self.assertEqual(update_check.repository(), "MddIdd/mdd-sim-gateway")
        with patch.dict("os.environ", {"MDD_UPDATE_REPOSITORY": "example/private"}):
            self.assertEqual(update_check.repository(), "example/private")

    def test_release_request_never_sends_a_github_token(self):
        payload = {"tag_name": "v1.0.0"}
        captured = {}

        def get(url, headers, timeout):
            captured["authorization"] = headers.get("Authorization")
            return _Response(payload)

        with patch.dict("os.environ", {"MDD_GITHUB_TOKEN": "must-not-be-used"}), patch(
                "control.app.update_check.requests.Session.get", side_effect=get):
            update_check.check(True)
        self.assertIsNone(captured["authorization"])

    def test_private_repository_does_not_prompt_for_authentication(self):
        with patch("control.app.update_check.requests.Session.get",
                   return_value=_Response({}, 401)):
            result = update_check.check(True)
        self.assertEqual(result["error_code"], "update.error.no_release")
        self.assertNotIn("auth", result["error"].lower())

    def test_library_entry_is_used_as_socks_proxy(self):
        session = MagicMock()
        session.proxies = {}
        session.get.return_value = _Response({"tag_name": "v1.0.0"})
        with patch.object(update_check, "_network_selection", return_value={
                "proxy_mode": "library", "proxy_profile_id": "primary"}), \
                patch.object(update_check, "_proxy_url",
                             return_value="socks5h://172.17.0.1:22538"), \
                patch("control.app.update_check.requests.Session", return_value=session):
            update_check.check(True)
        self.assertFalse(session.trust_env)
        self.assertEqual(session.proxies["https"], "socks5h://172.17.0.1:22538")

    def test_socks5_library_credentials_are_url_encoded(self):
        self.assertEqual(update_check._socks5_profile_url({
            "server": "proxy.example", "port": 1080,
            "username": "a@b", "password": "p:/w",
        }), "socks5h://a%40b:p%3A%2Fw@proxy.example:1080")

    def test_selected_country_exit_is_used_directly(self):
        settings = {"proxy": {
            "profiles": {"primary": {"name": "Primary", "type": "node"}},
            "exits": {"us": {"enabled": True, "profile_id": "primary"}},
        }}
        live = {"exits": {"us": {"ready": True, "proxy_host": "172.17.0.1",
                                     "proxy_port": 22538}}}
        with patch.object(config, "get_settings", return_value=settings), \
                patch("control.app.egress.status", return_value=live):
            self.assertEqual(update_check._proxy_url({
                "proxy_mode": "country", "proxy_profile_id": "", "proxy_country": "us",
            }), "socks5h://172.17.0.1:22538")

    def test_auto_falls_back_to_library_and_records_the_working_route(self):
        direct = MagicMock()
        direct.get.side_effect = requests.ConnectionError("blocked")
        proxied = MagicMock()
        proxied.get.return_value = _Response({"tag_name": "v9.9.9"})
        candidates = [
            {"proxy_mode": "direct", "proxy_profile_id": ""},
            {"proxy_mode": "library", "proxy_profile_id": "primary"},
        ]
        with patch.object(update_check, "_network_candidates", return_value=candidates), \
                patch.object(update_check, "_session", side_effect=[direct, proxied]):
            result = update_check.check(True)
        self.assertTrue(result["ok"])
        self.assertEqual(result["network"], candidates[1])

    def test_repository_stars_retries_independently_and_caches_success(self):
        direct = MagicMock()
        direct.get.side_effect = requests.ConnectionError("blocked")
        proxied = MagicMock()
        proxied.get.return_value = _Response({"stargazers_count": 178})
        candidates = [
            {"proxy_mode": "direct", "proxy_profile_id": ""},
            {"proxy_mode": "library", "proxy_profile_id": "primary"},
        ]
        with patch.object(update_check, "_network_candidates", return_value=candidates), \
                patch.object(update_check, "_session", side_effect=[direct, proxied]):
            result = update_check.repository_stars()
        self.assertEqual(result["stars"], 178)
        self.assertTrue(result["ok"])
        self.assertFalse(result["cached"])

        with patch.object(update_check, "_session") as session:
            cached = update_check.repository_stars()
        self.assertEqual(cached["stars"], 178)
        self.assertTrue(cached["cached"])
        session.assert_not_called()


class UpdateAutomationTests(unittest.TestCase):
    INFO = {"ok": True, "update_available": True, "latest": "1.5.0",
            "release_url": "https://example.invalid/v1.5.0",
            "network": {"proxy_mode": "direct", "proxy_profile_id": ""}}

    def _settings(self, **updates):
        return {
            "updates": {"proxy_mode": "direct", "proxy_profile_id": "",
                        "update_mode": "automatic", "version_scope": "main", **updates},
            "telegram": {"enabled": True, "events": {"software_update": True}},
            "webhook": {"enabled": False}, "pushplus": {"enabled": False},
        }

    def _policy(self, *, all_version="1.5.0", main_version="1.5.0",
                all_not_before="2026-01-01T00:00:00Z",
                main_not_before="2026-01-01T00:00:00Z"):
        return {
            "schema": 1,
            "release": {"version": all_version, "kind": "patch"},
            # Old clients continue to follow the latest/all rollout.
            "auto_update": {"version": all_version, "not_before": all_not_before},
            "channels": {
                "all": {"version": all_version, "not_before": all_not_before},
                "main": {"version": main_version, "not_before": main_not_before},
            },
        }

    def test_release_is_not_applied_without_promotion(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(update_check, "check", return_value=dict(self.INFO)), \
                patch.object(config, "get_settings",
                             return_value=self._settings()), \
                patch.object(update_check, "_load_update_policy", return_value=self._policy(
                    main_not_before="2099-01-01T00:00:00Z")), \
                patch.object(update_check, "request_apply") as apply, \
                patch("control.app.notify_push.dispatch"):
            result = update_check.automation_cycle()
        self.assertFalse(result["auto_update_requested"])
        apply.assert_not_called()

    def test_background_and_manual_checks_share_one_last_check_time(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(update_check, "check", return_value={
                    "ok": False, "checked_at": 900, "update_available": False}), \
                patch.object(update_check.time, "time", return_value=1000):
            update_check.automation_cycle()
            background = update_check._with_automation_check({"checked_at": 900})
            manual = update_check._with_automation_check({"checked_at": 1100})
        self.assertEqual(background["last_check_at"], 1000)
        self.assertEqual(manual["last_check_at"], 1100)

    def test_promoted_release_is_requested_silently_only_once(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(update_check, "check", return_value=dict(self.INFO)), \
                patch.object(config, "get_settings",
                             return_value=self._settings()), \
                patch.object(update_check, "_load_update_policy",
                             return_value=self._policy()), \
                patch.object(update_check, "request_apply", return_value={"ok": True}) as apply, \
                patch("control.app.notify_push.dispatch") as dispatch:
            first = update_check.automation_cycle()
            second = update_check.automation_cycle()
        self.assertFalse(first["notified"])
        self.assertTrue(first["auto_update_requested"])
        self.assertFalse(second["notified"])
        self.assertFalse(second["auto_update_requested"])
        dispatch.assert_not_called()
        self.assertEqual(apply.call_count, 1)

    def test_notify_main_scope_suppresses_policy_classified_patch(self):
        patch_info = {**self.INFO, "latest": update_check.VERSION.rsplit(".", 1)[0] + ".99"}
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(update_check, "check", return_value=patch_info), \
                patch.object(config, "get_settings",
                             return_value=self._settings(update_mode="notify",
                                                         version_scope="main")), \
                patch.object(update_check, "_load_update_policy", return_value=self._policy(
                    all_version=patch_info["latest"], main_version=update_check.VERSION)), \
                patch.object(update_check, "check_release", return_value={
                    "ok": True, "latest": update_check.VERSION, "update_available": False}), \
                patch("control.app.notify_push.dispatch") as dispatch:
            result = update_check.automation_cycle()
        self.assertFalse(result["notified"])
        dispatch.assert_not_called()

    def test_notify_main_scope_announces_policy_classified_main_release(self):
        latest = {**self.INFO, "latest": "1.6.1"}
        main_info = {**self.INFO, "latest": "1.6.0"}
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(update_check, "check", return_value=latest), \
                patch.object(config, "get_settings", return_value=self._settings(
                    update_mode="notify", version_scope="main")), \
                patch.object(update_check, "_load_update_policy", return_value=self._policy(
                    all_version="1.6.1", main_version="1.6.0")), \
                patch.object(update_check, "check_release", return_value=main_info) as tagged, \
                patch("control.app.notify_push.dispatch") as dispatch:
            result = update_check.automation_cycle()
        self.assertTrue(result["notified"])
        dispatch.assert_called_once()
        tagged.assert_called_once()
        self.assertEqual(dispatch.call_args.args[3], "1.6.0")

    def test_automatic_all_scope_installs_a_stable_patch_without_notice(self):
        patch_info = {**self.INFO, "latest": update_check.VERSION.rsplit(".", 1)[0] + ".99"}
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(update_check, "check", return_value=patch_info), \
                patch.object(config, "get_settings", return_value=self._settings(
                    update_mode="automatic", version_scope="all")), \
                patch.object(update_check, "_load_update_policy", return_value=self._policy(
                    all_version=patch_info["latest"])), \
                patch.object(update_check, "request_apply", return_value={"ok": True}) as apply, \
                patch("control.app.notify_push.dispatch") as dispatch:
            result = update_check.automation_cycle()
        self.assertFalse(result["notified"])
        self.assertTrue(result["auto_update_requested"])
        dispatch.assert_not_called()
        apply.assert_called_once()

    def test_automatic_main_scope_installs_configured_main_behind_latest_patch(self):
        patch_info = {**self.INFO, "latest": "1.6.1"}
        main_info = {**self.INFO, "latest": "1.6.0"}
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(update_check, "check", return_value=patch_info), \
                patch.object(config, "get_settings", return_value=self._settings()), \
                patch.object(update_check, "_load_update_policy", return_value=self._policy(
                    all_version="1.6.1", main_version="1.6.0")), \
                patch.object(update_check, "check_release", return_value=main_info) as tagged, \
                patch.object(update_check, "request_apply", return_value={"ok": True}) as apply, \
                patch("control.app.notify_push.dispatch") as dispatch:
            result = update_check.automation_cycle()
        self.assertFalse(result["notified"])
        self.assertTrue(result["auto_update_requested"])
        dispatch.assert_not_called()
        tagged.assert_called_once_with("1.6.0", patch_info["network"])
        apply.assert_called_once_with(info=main_info)

    def test_notify_all_scope_notifies_without_applying(self):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(update_check, "check", return_value=dict(self.INFO)), \
                patch.object(config, "get_settings", return_value=self._settings(
                    update_mode="notify", version_scope="all")), \
                patch.object(update_check, "auto_update_authorization") as authorization, \
                patch.object(update_check, "request_apply") as apply, \
                patch("control.app.notify_push.dispatch") as dispatch:
            result = update_check.automation_cycle()
        self.assertTrue(result["notified"])
        self.assertFalse(result["auto_update_requested"])
        dispatch.assert_called_once()
        authorization.assert_not_called()
        apply.assert_not_called()


class UpdateProxyMigrationTests(unittest.TestCase):
    def _load(self, settings):
        with tempfile.TemporaryDirectory() as temp, \
                patch.object(config, "DATA_DIR", temp), \
                patch.object(config, "CONFIG_PATH", str(Path(temp, "config.yaml"))):
            Path(temp, "config.yaml").write_text(
                "settings:\n" + "\n".join(f"  {line}" for line in settings.splitlines())
                + "\ninstances: {}\n", encoding="utf-8")
            return config.load()["settings"]

    def test_old_country_selection_remains_pinned_to_that_country(self):
        settings = self._load("""proxy:
  profiles:
    primary: {name: Primary, type: node, value: 'vless://example'}
  exits:
    us: {enabled: true, profile_id: primary}
updates: {proxy_mode: country, proxy_country: us}""")
        self.assertEqual(settings["updates"], {
            "proxy_mode": "country", "proxy_profile_id": "", "proxy_country": "us",
            "update_mode": "automatic", "version_scope": "main"})
        self.assertIn("primary", settings["proxy"]["profiles"])

    def test_old_socks_update_proxy_moves_into_the_library(self):
        settings = self._load("""proxy: {}
updates:
  proxy_mode: manual
  proxy_url: 'socks5h://alice:secret@proxy.example:1081'""")
        self.assertEqual(settings["updates"], {
            "proxy_mode": "library", "proxy_profile_id": "legacy-update-proxy",
            "proxy_country": "",
            "update_mode": "automatic", "version_scope": "main"})
        profile = settings["proxy"]["profiles"]["legacy-update-proxy"]
        self.assertEqual((profile["server"], profile["port"], profile["username"]),
                         ("proxy.example", 1081, "alice"))

    def test_subscription_update_proxy_becomes_its_first_enabled_country_exit(self):
        settings = self._load("""proxy:
  profiles:
    shared: {name: Shared, type: subscription, url: 'https://example.test/sub'}
  exits:
    gb: {enabled: true, profile_id: shared}
    us: {enabled: true, profile_id: shared}
updates: {proxy_mode: library, proxy_profile_id: shared}""")
        self.assertEqual(settings["updates"]["proxy_mode"], "country")
        self.assertEqual(settings["updates"]["proxy_country"], "gb")
        self.assertEqual(settings["updates"]["proxy_profile_id"], "")

    def test_subscription_telegram_proxy_becomes_its_first_enabled_country_exit(self):
        settings = self._load("""proxy:
  profiles:
    shared: {name: Shared, type: subscription, url: 'https://example.test/sub'}
  exits:
    gb: {enabled: false, profile_id: shared}
    us: {enabled: true, profile_id: shared}
telegram: {proxy_mode: library, proxy_profile_id: shared}""")
        self.assertEqual(settings["telegram"]["proxy_mode"], "country")
        self.assertEqual(settings["telegram"]["proxy_country"], "us")
        self.assertEqual(settings["telegram"]["proxy_profile_id"], "")

    def test_automatic_update_candidates_skip_subscriptions(self):
        settings = {"proxy": {"profiles": {
            "shared": {"name": "Shared", "type": "subscription"},
            "node": {"name": "Node", "type": "node"},
            "socks": {"name": "SOCKS", "type": "socks5"},
        }}}
        with patch.object(config, "get_settings", return_value=settings):
            self.assertEqual(update_check._network_candidates(), [
                {"proxy_mode": "direct", "proxy_profile_id": ""},
                {"proxy_mode": "library", "proxy_profile_id": "node"},
                {"proxy_mode": "library", "proxy_profile_id": "socks"},
            ])

    def test_previous_auto_update_opt_out_becomes_notify_all(self):
        settings = self._load("""proxy: {}
updates: {proxy_mode: auto, notification_mode: all, auto_update: false}""")
        self.assertEqual(settings["updates"], {
            "proxy_mode": "auto", "proxy_profile_id": "", "proxy_country": "",
            "update_mode": "notify", "version_scope": "all"})

if __name__ == "__main__":
    unittest.main()
