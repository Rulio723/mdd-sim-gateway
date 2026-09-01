"""The proactive-rekey driver's decisions, exercised without the engine's card/crypto stack.

swu_ike imports the SIM reader and crypto libraries that only exist inside the engine image,
so the class cannot be constructed here. The methods under test read and write plain
attributes and call send_data/state_ue_rekey_child, which makes them exact-copy testable by
binding the real functions to a stand-in object: the assertions below run the shipped code,
not a description of it.
"""
import ast
import types
import unittest
from pathlib import Path

SOURCE = Path(__file__).resolve().parent.parent / "engine" / "swu_ike.py"
WANTED = {"_rekey_tick", "_rekey_give_up", "_rekey_select_timeout", "_liveness_tick",
          "_begin_create_child_request", "_accept_create_child_response",
          "_ike_rekey_tick", "_ike_rekey_give_up", "_ike_rekey_select_timeout"}


def _load_methods():
    """Compile just the rekey methods out of swu_ike, with time.monotonic available."""
    tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
    picked = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name in WANTED:
            picked.append(node)
    module = ast.Module(body=picked, type_ignores=[])
    namespace = {"time": __import__("time"), "swu_log": lambda *_a, **_k: None}
    exec(compile(module, str(SOURCE), "exec"), namespace)  # noqa: S102
    return {name: namespace[name] for name in WANTED}


METHODS = _load_methods()


class FakeTunnel:
    """Just enough state for the rekey driver, plus a record of what it sent."""

    def __init__(self, **overrides):
        self.child_rekey_period = 3000.0
        self.rekey_response_timeout = 10.0
        self.rekey_retransmits = 3
        self.rekey_retry_interval = 300.0
        self.liveness_period = 20.0
        self._child_sa_time = 0.0
        self._rekey_outstanding = False
        self._rekey_failed = False
        self._rekey_sent_at = None
        self._rekey_packet = None
        self._rekey_tries = 0
        self._rekey_retry_at = None
        self.message_id_request = 41
        self._create_child_request_id = None
        self._create_child_response_handled = False
        self.ike_rekey_period = 36000.0
        self.ike_rekey_retry_interval = 3600.0
        self._ike_sa_time = 0.0
        self._ike_rekey_outstanding = False
        self._ike_rekey_failed = False
        self._ike_rekey_sent_at = None
        self._ike_rekey_packet = None
        self._ike_rekey_tries = 0
        self._ike_rekey_retry_at = None
        self.sent = []
        self.rekeys_started = 0
        self.ike_rekeys_started = 0
        self.teardowns = []
        self.__dict__.update(overrides)
        for name, function in METHODS.items():
            setattr(self, name, types.MethodType(function, self))

    def send_data(self, packet):
        self.sent.append(packet)

    def state_ue_rekey_child(self):
        self.rekeys_started += 1
        self.message_id_request += 1
        self._rekey_packet = b"rekey-request"
        self._rekey_tries = 1
        self.send_data(self._rekey_packet)

    def state_ue_create_sa(self):
        self.ike_rekeys_started += 1
        self.message_id_request += 1
        self._ike_rekey_packet = b"ike-rekey-request"
        self._ike_rekey_tries = 1
        self.send_data(self._ike_rekey_packet)

    def _rekey_teardown(self, reason):
        self.teardowns.append(reason)
        self._rekey_outstanding = False
        self._ike_rekey_outstanding = False


class UnansweredRekeyTests(unittest.TestCase):
    """One lost packet must not cost a working tunnel: it took down a line that had been
    carrying IMS for fifty minutes, and the rebuild cost five more minutes of registration."""

    def _fire_due_rekey(self, **overrides):
        tunnel = FakeTunnel(**overrides)
        tunnel._child_sa_time = __import__("time").monotonic() - tunnel.child_rekey_period - 1
        tunnel._rekey_tick()
        return tunnel

    @staticmethod
    def _let_it_time_out(tunnel, rounds=10):
        """Age the in-flight request past its timeout until the driver stops waiting."""
        for _ in range(rounds):
            if not tunnel._rekey_outstanding:
                return
            tunnel._rekey_sent_at -= tunnel.rekey_response_timeout
            tunnel._rekey_tick()

    def test_an_unanswered_request_is_retransmitted_verbatim(self):
        tunnel = self._fire_due_rekey()
        self.assertEqual(tunnel.sent, [b"rekey-request"])
        for expected in (2, 3):
            tunnel._rekey_sent_at -= tunnel.rekey_response_timeout
            tunnel._rekey_tick()
            self.assertEqual(tunnel._rekey_tries, expected)
        # Same bytes every time: a retransmission carries the original message id and nonce.
        self.assertEqual(tunnel.sent, [b"rekey-request"] * 3)
        self.assertEqual(tunnel.teardowns, [])

    def test_all_retransmissions_unanswered_reestablishes_the_ambiguous_ike_sa(self):
        tunnel = self._fire_due_rekey()
        self._let_it_time_out(tunnel)
        self.assertEqual(tunnel.sent, [b"rekey-request"] * 3)
        self.assertEqual(tunnel.teardowns, ["rekey_timeout"])

    def test_an_explicit_rejection_consumes_the_message_id(self):
        tunnel = self._fire_due_rekey()
        self.assertEqual(tunnel.message_id_request, 42)
        # Match the real response handler: it clears outstanding and records the rejection.
        tunnel._rekey_outstanding = False
        tunnel._rekey_failed = True
        tunnel._rekey_tick()
        self.assertEqual(tunnel.message_id_request, 42)
        self.assertEqual(tunnel.teardowns, [])
        self.assertIsNotNone(tunnel._rekey_retry_at)

    def test_an_explicit_rejection_also_keeps_the_tunnel(self):
        # TEMPORARY_FAILURE means "ask again later"; even NO_PROPOSAL_CHOSEN leaves the
        # current SA usable, so neither is a reason to tear one down.
        tunnel = self._fire_due_rekey()
        tunnel._rekey_outstanding = False
        tunnel._rekey_failed = True
        tunnel._rekey_tick()
        self.assertEqual(tunnel.teardowns, [])
        self.assertIsNotNone(tunnel._rekey_retry_at)

    def test_the_retry_is_honoured_before_sa_age(self):
        tunnel = self._fire_due_rekey()
        tunnel._rekey_outstanding = False
        tunnel._rekey_failed = True
        tunnel._rekey_tick()
        started = tunnel.rekeys_started
        tunnel._rekey_tick()                    # still inside the retry interval
        self.assertEqual(tunnel.rekeys_started, started)
        tunnel._rekey_retry_at = __import__("time").monotonic() - 1
        tunnel._rekey_tick()
        self.assertEqual(tunnel.rekeys_started, started + 1)

    def test_select_wakes_for_the_scheduled_retry(self):
        tunnel = FakeTunnel()
        tunnel._rekey_retry_at = __import__("time").monotonic() + 120
        self.assertAlmostEqual(tunnel._rekey_select_timeout(), 120, delta=2)

    def test_dpd_does_not_overtake_an_inflight_rekey_request(self):
        tunnel = FakeTunnel(_rekey_outstanding=True)
        tunnel._liveness_tick()
        self.assertEqual(tunnel.sent, [])

    def test_a_create_child_response_is_consumed_exactly_once(self):
        tunnel = FakeTunnel()
        tunnel.message_id_request = 42
        tunnel._begin_create_child_request()
        self.assertFalse(tunnel._accept_create_child_response(41))
        self.assertTrue(tunnel._accept_create_child_response(42))
        self.assertFalse(tunnel._accept_create_child_response(42))

    def test_rekey_disabled_stays_a_no_op(self):
        tunnel = FakeTunnel(child_rekey_period=0.0, _child_sa_time=None)
        tunnel._rekey_tick()
        self.assertEqual(tunnel.rekeys_started, 0)
        self.assertIsNone(tunnel._rekey_select_timeout())


class IkeRekeyTests(unittest.TestCase):
    """The proactive IKE-SA rekey preempts the ePDG's own rekey clock (EE fires at ~12 h and we
    refuse the responder role, so being second costs a ~1 min teardown). Same driver contract
    as the CHILD rekey: verbatim retransmission, rejection keeps the SA, silence re-establishes."""

    @staticmethod
    def _now():
        return __import__("time").monotonic()

    def _fire_due_ike_rekey(self, **overrides):
        tunnel = FakeTunnel(**overrides)
        tunnel._ike_sa_time = self._now() - tunnel.ike_rekey_period - 1
        tunnel._ike_rekey_tick()
        return tunnel

    def test_a_due_ike_sa_starts_a_ue_initiated_rekey(self):
        tunnel = self._fire_due_ike_rekey()
        self.assertEqual(tunnel.ike_rekeys_started, 1)
        self.assertTrue(tunnel._ike_rekey_outstanding)
        self.assertEqual(tunnel.sent, [b"ike-rekey-request"])

    def test_a_young_ike_sa_is_left_alone(self):
        tunnel = FakeTunnel()
        tunnel._ike_sa_time = self._now()
        tunnel._ike_rekey_tick()
        self.assertEqual(tunnel.ike_rekeys_started, 0)

    def test_an_unanswered_ike_rekey_is_retransmitted_verbatim_then_reestablishes(self):
        tunnel = self._fire_due_ike_rekey()
        for _ in range(10):
            if not tunnel._ike_rekey_outstanding:
                break
            tunnel._ike_rekey_sent_at -= tunnel.rekey_response_timeout
            tunnel._ike_rekey_tick()
        self.assertEqual(tunnel.sent, [b"ike-rekey-request"] * 3)
        self.assertEqual(tunnel.teardowns, ["ike_rekey_timeout"])

    def test_an_explicit_rejection_keeps_the_established_ike_sa(self):
        tunnel = self._fire_due_ike_rekey()
        # Match the real response handler: it clears outstanding and records the rejection.
        tunnel._ike_rekey_outstanding = False
        tunnel._ike_rekey_failed = True
        tunnel._ike_rekey_tick()
        self.assertEqual(tunnel.teardowns, [])
        self.assertIsNotNone(tunnel._ike_rekey_retry_at)
        # And the retry honours ike_rekey_retry_interval, not the (shorter) child interval.
        self.assertGreater(tunnel._ike_rekey_retry_at, self._now() + tunnel.ike_rekey_retry_interval - 5)

    def test_ike_rekey_disabled_stays_a_no_op(self):
        tunnel = FakeTunnel(ike_rekey_period=0.0, _ike_sa_time=None)
        tunnel._ike_rekey_tick()
        self.assertEqual(tunnel.ike_rekeys_started, 0)
        self.assertIsNone(tunnel._ike_rekey_select_timeout())

    def test_the_two_rekey_exchanges_never_overlap(self):
        # An in-flight CHILD rekey defers a due IKE rekey…
        tunnel = FakeTunnel(_rekey_outstanding=True)
        tunnel._ike_sa_time = self._now() - tunnel.ike_rekey_period - 1
        tunnel._ike_rekey_tick()
        self.assertEqual(tunnel.ike_rekeys_started, 0)
        # …and an in-flight IKE rekey defers a due CHILD rekey.
        tunnel = FakeTunnel(_ike_rekey_outstanding=True)
        tunnel._child_sa_time = self._now() - tunnel.child_rekey_period - 1
        tunnel._rekey_tick()
        self.assertEqual(tunnel.rekeys_started, 0)

    def test_dpd_does_not_overtake_an_inflight_ike_rekey_request(self):
        tunnel = FakeTunnel(_ike_rekey_outstanding=True)
        tunnel._liveness_tick()
        self.assertEqual(tunnel.sent, [])

    def test_select_wakes_for_the_inflight_response_timeout(self):
        tunnel = FakeTunnel(_ike_rekey_outstanding=True)
        tunnel._ike_rekey_sent_at = self._now()
        timeout = tunnel._ike_rekey_select_timeout()
        self.assertAlmostEqual(timeout, tunnel.rekey_response_timeout, delta=2)


class WorkerSupervisionTests(unittest.TestCase):
    def test_both_esp_workers_detach_the_inherited_log_pipe(self):
        tree = ast.parse(SOURCE.read_text(encoding="utf-8"))
        functions = {node.name: node for node in ast.walk(tree)
                     if isinstance(node, ast.FunctionDef)}
        self.assertIn('prepare_ipsec_worker(parent_pid, \'encoder\')',
                      ast.unparse(functions["encapsulate_ipsec"]))
        self.assertIn('prepare_ipsec_worker(parent_pid, \'decoder\')',
                      ast.unparse(functions["decapsulate_ipsec"]))

    def test_worker_processes_receive_their_parent_pid(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertGreaterEqual(source.count("worker_parent_pid"), 3)
        self.assertIn("PR_SET_PDEATHSIG", source)

    def test_worker_restores_default_signals_and_keeps_bounded_diagnostics(self):
        source = SOURCE.read_text(encoding="utf-8")
        self.assertIn("signal.signal(signal.SIGTERM, signal.SIG_DFL)", source)
        self.assertIn("signal.signal(signal.SIGINT, signal.SIG_DFL)", source)
        self.assertIn('"esp-%s.log" % role', source)
        self.assertIn("WORKER_LOG_MAX_BYTES", source)


class IkeRekeyConfigTests(unittest.TestCase):
    """The IKE-SA rekey clock must beat the carrier's session clock (giffgaff/O2: ~2h50m,
    issue #33), so the period has to be settable end-to-end: per line, globally, and with a
    default short enough for the shortest observed carrier."""

    @staticmethod
    def _render(inst_extra=None, settings=None):
        from control.app import config
        inst = {"id": "3", "index": 0, "imsi": "234100000000000",
                "mcc": "234", "mnc": "10", "iccid": "test-card",
                "imei": "490154203237518", "ami_secret": "test-secret",
                "sip": {"webrtc": {"enable": True, "password": "test-password"}},
                **(inst_extra or {})}
        return config.render_instance_json(inst, settings or {})

    def test_default_preempts_a_three_hour_carrier_clock(self):
        rendered = self._render()
        self.assertEqual(rendered["ike_rekey_minutes"], 150)
        self.assertLess(rendered["ike_rekey_minutes"], 170)  # observed giffgaff clock

    def test_settings_default_applies_and_line_override_wins(self):
        settings = {"rekey": {"minutes": 30, "ike_minutes": 600}}
        self.assertEqual(self._render(settings=settings)["ike_rekey_minutes"], 600)
        self.assertEqual(
            self._render({"ike_rekey_minutes": 120}, settings)["ike_rekey_minutes"], 120)

    def test_zero_disables_and_garbage_falls_back(self):
        self.assertEqual(self._render({"ike_rekey_minutes": 0})["ike_rekey_minutes"], 0)
        self.assertEqual(self._render({"ike_rekey_minutes": -5})["ike_rekey_minutes"], 0)
        self.assertEqual(
            self._render({"ike_rekey_minutes": "junk"})["ike_rekey_minutes"], 150)
        self.assertEqual(self._render({"ike_rekey_minutes": 99999})["ike_rekey_minutes"], 1440)

    def test_render_writes_the_ike_rekey_env(self):
        root = Path(__file__).resolve().parent.parent
        source = (root / "engine" / "render.py").read_text(encoding="utf-8")
        self.assertIn('put("SWU_IKE_REKEY_MINUTES", cfg.get("ike_rekey_minutes", 150))',
                      source)


if __name__ == "__main__":
    unittest.main()
