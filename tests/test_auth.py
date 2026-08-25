import json
import os
import tempfile
import time
import unittest
from unittest.mock import patch

from control.app import auth


class AuthTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path_patch = patch.object(auth, "AUTH_PATH", os.path.join(self.temp.name, "auth.json"))
        self.path_patch.start()
        # Sessions are persisted now, so the suite has to own that file too; without this the
        # tests write into the installation's real data directory.
        self.sessions_patch = patch.object(
            auth, "SESSIONS_PATH", os.path.join(self.temp.name, "sessions.json"))
        self.sessions_patch.start()
        self.data_dir_patch = patch.object(auth.cfg, "DATA_DIR", self.temp.name)
        self.data_dir_patch.start()
        auth._sessions.clear()
        auth._failures.clear()

    def tearDown(self):
        self.data_dir_patch.stop()
        self.sessions_patch.stop()
        self.path_patch.stop()
        self.temp.cleanup()

    def _restart(self):
        """Drop every in-memory session the way a control-plane restart does."""
        auth._sessions.clear()
        auth._load_sessions()

    def test_setup_hashes_password_and_creates_session(self):
        auth.setup("correct horse battery", "admin")
        self.assertEqual(auth.username(), "admin")
        with open(auth.AUTH_PATH, encoding="utf-8") as handle:
            stored = json.load(handle)
        self.assertNotIn("correct horse battery", json.dumps(stored))
        token, csrf = auth.login("admin", "correct horse battery", "127.0.0.1")
        self.assertEqual(auth.session(token)["csrf"], csrf)

    def test_wrong_password_is_rate_limited(self):
        auth.setup("correct horse battery")
        for _ in range(5):
            self.assertIsNone(auth.login("admin", "wrong password", "192.0.2.4"))
        self.assertGreater(auth.throttled("192.0.2.4"), 0)

    def test_short_password_and_second_setup_are_rejected(self):
        with self.assertRaises(ValueError):
            auth.setup("short")
        auth.setup("ten-characters")
        with self.assertRaises(ValueError):
            auth.setup("another-password")

    def test_password_change_revokes_sessions(self):
        auth.setup("correct horse battery")
        token, _ = auth.login("admin", "correct horse battery", "127.0.0.1")
        auth.change_password("correct horse battery", "different safe password")
        self.assertIsNone(auth.session(token))
        self.assertIsNone(auth.login("admin", "correct horse battery", "127.0.0.1"))
        self.assertIsNotNone(auth.login("admin", "different safe password", "127.0.0.1"))


if __name__ == "__main__":
    unittest.main()


class SessionPersistenceTests(AuthTests):
    """A restart used to log the administrator out; these pin down that it no longer does."""

    def test_session_survives_a_restart(self):
        auth.setup("correct horse battery", "admin")
        token, csrf = auth.login("admin", "correct horse battery", "127.0.0.1")
        self._restart()
        restored = auth.session(token)
        self.assertIsNotNone(restored)
        self.assertEqual(restored["csrf"], csrf)

    def test_stored_file_cannot_be_replayed_as_a_cookie(self):
        auth.setup("correct horse battery", "admin")
        token, csrf = auth.login("admin", "correct horse battery", "127.0.0.1")
        with open(auth.SESSIONS_PATH, encoding="utf-8") as handle:
            raw = handle.read()
        # The CSRF token is useless without the cookie, but the cookie itself must never be
        # readable from disk: anyone who can read the file could otherwise become the admin.
        self.assertNotIn(token, raw)
        stored_keys = list(json.loads(raw)["sessions"])
        self.assertEqual(len(stored_keys), 1)
        self.assertIsNone(auth.session(stored_keys[0]))

    def test_remember_me_outlives_the_default_window(self):
        auth.setup("correct horse battery", "admin")
        plain, _ = auth.login("admin", "correct horse battery", "127.0.0.1")
        remembered, _ = auth.login("admin", "correct horse battery", "127.0.0.1",
                                   remember=True)
        sessions = auth._sessions
        short = sessions[auth._token_key(plain)]["expires"]
        long = sessions[auth._token_key(remembered)]["expires"]
        self.assertGreater(long - short, auth.SESSION_TTL)
        self.assertAlmostEqual(sessions[auth._token_key(remembered)]["ttl"],
                               auth.SESSION_TTL_REMEMBER, delta=1)

    def test_expired_session_is_not_restored(self):
        auth.setup("correct horse battery", "admin")
        token, _ = auth.login("admin", "correct horse battery", "127.0.0.1")
        auth._sessions[auth._token_key(token)]["expires"] = time.time() - 1
        auth._save_sessions()
        self._restart()
        self.assertIsNone(auth.session(token))

    def test_logout_and_password_change_clear_the_stored_sessions(self):
        auth.setup("correct horse battery", "admin")
        token, _ = auth.login("admin", "correct horse battery", "127.0.0.1")
        auth.logout(token)
        self._restart()
        self.assertIsNone(auth.session(token))

        other, _ = auth.login("admin", "correct horse battery", "127.0.0.1")
        auth.change_password("correct horse battery", "different safe password")
        self._restart()
        self.assertIsNone(auth.session(other))
