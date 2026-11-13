"""Offline controller tests: no serial imports or real hardware."""
import copy
from pathlib import Path
import sys
import unittest

sys.dont_write_bytecode = True
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from webcontrol.controller import APIError, Controller
from webcontrol.demo import DemoTransport
from webcontrol.transport import ConnectionFailure


class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now


class ControlledDemo(DemoTransport):
    def __init__(self):
        super().__init__(delay=0)
        self.before_jog = None
        self.after_jog = None

    def request(self, op, args=None):
        if op == "move_continuous" and self.before_jog:
            self.before_jog()
        result = super().request(op, args)
        if op == "move_continuous" and self.after_jog:
            self.after_jog()
            result = copy.deepcopy(self.state)
        return result


class ControllerTests(unittest.TestCase):
    def setUp(self):
        self.clock = Clock()
        self.transport = ControlledDemo()
        self.transport.clock = self.clock
        self.c = Controller(lambda: self.transport, clock=self.clock, autostart=False)
        self.sequence = 0

    def tearDown(self):
        self.c.close()

    def post(self, op, **values):
        self.sequence += 1
        return self.c.submit(op, {"client_id": "page-a", "request_id": str(self.sequence), **values})

    def ready(self, position=51):
        self.post("claim")
        self.c.step()
        self.post("configure", ids=[1, 2, 3], long_id=1, confirmed=True)
        self.c.step()
        self.post("anchor", axis=1, mm=position, sign=-1, lead=4, confirmed=True)
        self.c.step()

    def jogs(self):
        return [args for op, args in self.transport.calls if op == "move_continuous"]

    def run_job(self):
        for _ in range(4000):
            self.post('heartbeat')
            self.c.step()
            self.clock.now += .1
            if self.c.snapshot()["job"]["state"] not in ("running", "stopping"):
                return
        self.fail("Job did not finish within its finite budget")

    def test_idle_service_does_not_connect_without_owner(self):
        self.c.step()
        self.assertIsNone(self.transport.session)
        self.assertFalse(self.c.snapshot()["connected"])

    def test_connect_starts_locked_and_requires_configuration(self):
        self.post("claim")
        self.c.step()
        state = self.c.snapshot()
        self.assertTrue(state["connected"])
        self.assertFalse(state["configured"])
        self.assertTrue(all(not axis["anchored"] for axis in state["axes"]))
        with self.assertRaises(APIError):
            self.post("move", axis=1, target_mm=100, rpm=12)
        with self.assertRaises(APIError):
            self.post("anchor", axis=1, mm=51, sign=-1, lead=4, confirmed=True)
        self.assertEqual(self.jogs(), [])

    def test_pending_configuration_and_anchor_are_visible_and_exclusive(self):
        self.post("claim"); self.c.step()
        reply = self.post("configure", ids=[1, 2, 3], long_id=1, confirmed=True)
        self.assertEqual(reply["state"]["pending"], "configure")
        with self.assertRaises(APIError):
            self.post("configure", ids=[1, 2, 3], long_id=2, confirmed=True)
        self.c.step()
        self.assertTrue(self.c.snapshot()["configured"])

    def test_minimum_jog_moves_once_in_both_directions(self):
        self.ready()
        self.post("jog", axis=1, delta_mm=0.1, rpm=12)
        self.run_job()
        self.assertEqual(self.jogs()[0]["moves"], [{"axis": 1, "target_mm": 51.1, "rpm": 12}])
        self.assertAlmostEqual(self.c.snapshot()["axes"][0]["position_mm"], 51.1)
        self.post("jog", axis=1, delta_mm=-0.1, rpm=12)
        self.run_job()
        self.assertEqual(len(self.jogs()), 2)
        self.assertAlmostEqual(self.c.snapshot()["axes"][0]["position_mm"], 51.0)

    def test_minimum_jog_away_from_lower_boundary_is_not_suppressed(self):
        self.ready(30)
        self.post("jog", axis=1, delta_mm=0.1, rpm=6)
        self.run_job()
        self.assertEqual(len(self.jogs()), 1)
        self.assertAlmostEqual(self.c.snapshot()["axes"][0]["position_mm"], 30.1)

    def test_absolute_minimum_difference_is_not_rounded_below_minimum(self):
        self.ready(100)
        self.post("move", axis=1, target_mm=100.1, rpm=12)
        self.run_job()
        self.assertEqual(len(self.jogs()), 1)
        self.assertEqual(self.jogs()[0]["moves"][0]["target_mm"], 100.1)

    def test_single_continuous_move_only_selected_axis_and_no_queue(self):
        self.ready()
        self.post("move", axis=1, target_mm=56.5, rpm=20)
        with self.assertRaises(APIError):
            self.post("move", axis=1, target_mm=60, rpm=20)
        self.run_job()
        state = self.c.snapshot()
        self.assertEqual(state["job"]["state"], "done")
        self.assertAlmostEqual(state["job"]["members"][0]["position_mm"], 56.5)
        self.assertEqual(len(self.jogs()), 1)
        self.assertEqual(self.jogs()[0]["moves"], [{"axis":1,"target_mm":56.5,"rpm":20}])
        self.assertEqual(state["counts"]["2"], 0)
        self.assertEqual(state["counts"]["3"], 0)

    def test_move_to_boundary_leaves_feedback_room(self):
        self.ready(33)
        self.post("move", axis=1, target_mm=30, rpm=12)
        self.run_job()
        self.assertAlmostEqual(self.c.snapshot()["job"]["members"][0]["position_mm"], 30.1, places=3)

    def test_invalid_units_bounds_speed_and_positions_cannot_move(self):
        self.ready()
        for values in ({"axis": 1, "target_mm": 29, "rpm": 12},
                       {"axis": 1, "target_mm": 311, "rpm": 12},
                       {"axis": 1, "target_mm": "100", "rpm": 12},
                       {"axis": 1, "target_mm": 100, "rpm": True},
                       {"axis": 1, "target_mm": 100, "rpm": 31},
                       {"axis": 1, "target_mm": 100, "rpm": 2},
                       {"axis": True, "target_mm": 100, "rpm": 12}):
            with self.subTest(values=values), self.assertRaises(APIError):
                self.post("move", **values)
        self.assertEqual(self.jogs(), [])

    def test_outside_soft_margin_requires_explicit_recovery_not_normal_move(self):
        self.ready(20)
        with self.assertRaises(APIError):
            self.post("move", axis=1, target_mm=31, rpm=12)
        self.assertEqual(self.jogs(), [])

    def test_duplicate_move_never_runs_twice_even_after_completion(self):
        self.ready()
        body = {"client_id": "page-a", "request_id": "stable-command", "axis": 1, "target_mm": 52, "rpm": 12}
        self.c.submit("move", body)
        self.assertTrue(self.c.submit("move", body)["duplicate"])
        self.run_job()
        self.assertTrue(self.c.submit("move", body)["duplicate"])
        self.c.step()
        self.assertEqual(len(self.jogs()), 1)
        with self.assertRaises(APIError):
            self.c.submit("move", dict(body, target_mm=53))

    def test_old_jog_cannot_replay_after_response_cache_eviction(self):
        self.ready()
        old = {"client_id": "page-a", "request_id": "old-jog", "axis": 1,
               "delta_mm": 0.1, "rpm": 12}
        self.c.submit("jog", old)
        self.run_job()
        for _ in range(129):
            self.post("claim")
        self.assertNotIn(("page-a", "old-jog"), self.c.responses)
        with self.assertRaises(APIError):
            self.c.submit("jog", old)
        self.c.step()
        self.assertEqual(len(self.jogs()), 1)

    def test_motion_and_reanchor_require_unique_request_ids(self):
        self.ready()
        for op, payload in (("move", {"axis": 1, "target_mm": 52, "rpm": 12}),
                            ("anchor", {"axis": 1, "mm": 51, "sign": -1,
                                        "lead": 4, "confirmed": True})):
            with self.assertRaises(APIError):
                self.c.submit(op, {"client_id": "page-a", **payload})
        self.assertEqual(self.jogs(), [])

    def test_second_tab_cannot_claim_or_start_motion(self):
        self.ready()
        with self.assertRaises(APIError):
            self.c.submit("claim", {"client_id": "page-b"})
        with self.assertRaises(APIError):
            self.c.submit("move", {"client_id": "page-b", "axis": 1, "target_mm": 52, "rpm": 12})
        self.assertEqual(self.jogs(), [])

    def test_viewer_can_cancel_current_segment_without_another_step(self):
        self.ready()
        self.post("move", axis=1, target_mm=60, rpm=12)
        self.transport.before_jog = lambda: self.c.submit("stop", {"client_id": "viewer"})
        self.c.step()
        self.c.step()
        self.assertEqual(len(self.jogs()), 1)
        self.assertEqual(self.c.snapshot()["job"]["state"], "cancelled")
        self.assertTrue(self.c.snapshot()["axes"][0]["anchored"])

    def test_lease_expiry_stops_before_next_segment_and_locks(self):
        self.ready()
        self.post("move", axis=1, target_mm=60, rpm=12)
        self.c.step()
        self.clock.now += 6
        self.c.step()
        state = self.c.snapshot()
        self.assertEqual(len(self.jogs()), 1)
        self.assertIsNone(state["owner"])
        self.assertEqual(state["job"]["state"], "cancelled")
        self.assertTrue(all(a["position_mm"] is None for a in state["axes"]))

    def test_disconnect_cancels_and_requires_new_anchors(self):
        self.ready()
        self.post("move", axis=1, target_mm=60, rpm=12)
        self.c.step()
        self.post("disconnect"); self.c.step()
        self.assertEqual(len(self.jogs()), 1)
        self.assertFalse(self.c.snapshot()["connected"])
        self.assertFalse(self.c.auto_connect)
        self.post("connect"); self.c.step()
        self.assertTrue(self.c.snapshot()["connected"])
        self.assertFalse(self.c.snapshot()["configured"])
        self.assertTrue(all(not a["anchored"] for a in self.c.snapshot()["axes"]))

    def test_ambiguous_failure_never_replays_after_reconnect(self):
        self.ready()
        self.post("move", axis=1, target_mm=60, rpm=12)
        def fail_after_move():
            raise ConnectionFailure("ambiguous movement result")
        self.transport.after_jog = fail_after_move
        self.c.step()
        self.assertEqual(len(self.jogs()), 1)
        self.assertEqual(self.c.snapshot()["job"]["state"], "failed")
        self.transport.after_jog = None
        self.clock.now += 2.1
        self.c.step(); self.c.step()
        self.assertEqual(len(self.jogs()), 1)
        self.assertTrue(all(not a["anchored"] for a in self.c.snapshot()["axes"]))

    def test_continuous_motion_requires_new_firmware(self):
        self.ready()
        self.c.state['capabilities'].remove('continuous_move')
        with self.assertRaises(APIError):
            self.post('move',axis=1,target_mm=60,rpm=12)
        self.assertEqual(self.jogs(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
