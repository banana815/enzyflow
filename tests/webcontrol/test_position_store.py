import tempfile
from pathlib import Path
import unittest
from unittest.mock import patch
import test_controller as fixtures
from webcontrol.controller import Controller, APIError
from webcontrol.position_store import read

class PositionTests(fixtures.ControllerTests):
    # Use the existing motion/session regressions with disk persistence as well.
    def setUp(self):
        super().setUp()
        self.tmp = tempfile.TemporaryDirectory()
        self.c.position_path = Path(self.tmp.name)/'positions.json'
    def tearDown(self):
        super().tearDown()
        self.tmp.cleanup()
    def test_completed_position_survives_reconnect_without_motion(self):
        self.ready()
        self.post('move',axis=1,target_mm=55,rpm=12);self.run_job()
        saved=read(self.c.position_path)
        self.assertTrue(saved['reliable']);self.assertEqual(saved['axes'][0]['position_mm'],55)
        calls=len(self.jogs())
        self.post('disconnect');self.c.step();self.post('connect');self.c.step()
        self.post('restore_positions');self.c.step()
        self.assertEqual(self.c.snapshot()['axes'][0]['position_mm'],55)
        self.assertEqual(len(self.jogs()),calls)
    def test_crash_marker_blocks_stale_position_restore(self):
        self.ready()
        self.post('move',axis=1,target_mm=60,rpm=12);self.c.step()
        self.assertFalse(read(self.c.position_path)['reliable'])
        with self.assertRaises(APIError):
            self.post('restore_positions')

    def test_one_confirmation_sets_and_saves_all_three_positions(self):
        self.ready()
        positions = [dict(axis=axis, mm=position) for axis, position in enumerate((130, 80, 90), 1)]
        with self.assertRaises(APIError):
            self.post('anchor', positions=positions[:2] + [dict(axis=3, mm=900)], confirmed=True)
        self.assertEqual(self.c.snapshot()['axes'][0]['position_mm'], 51)
        self.post('anchor', positions=positions, confirmed=True)
        self.c.step()
        saved = read(self.c.position_path)
        self.assertEqual([axis['position_mm'] for axis in saved['axes']], [130, 80, 90])
        self.assertEqual(self.jogs(), [])
    def test_position_write_failure_prevents_motion(self):
        self.ready()
        self.post('move',axis=1,target_mm=60,rpm=12)
        with patch('webcontrol.position_store.write',side_effect=OSError('disk full')):
            self.c.step()
        self.assertEqual(self.jogs(),[])
    def test_completed_positions_loaded_after_server_restart(self):
        self.ready()
        restarted_transport = fixtures.ControlledDemo()
        c=Controller(lambda:restarted_transport,log_path=self.c.position_path.with_name('events.jsonl'),autostart=False)
        self.assertEqual(c.snapshot()['saved_positions']['axes'][0]['position_mm'],51)
        try:
            c.submit('claim', {'client_id': 'restart'})
            c.step()
            axis = c.snapshot()['axes'][0]
            self.assertTrue(axis['anchored'])
            self.assertEqual(axis['position_mm'], 51)
            self.assertNotIn('move_continuous', [op for op, _ in restarted_transport.calls])
        finally:
            c.close()

    def test_emergency_stop_saves_reference_and_never_restores_it(self):
        self.ready()
        axis = self.c.snapshot()['axes'][0]
        axis['position_mm'] += 0.5
        self.post('emergency_stop')
        with patch.object(self.transport, 'finish_emergency', return_value={
                'confirmed': False, 'stopped_axes': [axis]}):
            self.c.step()
        saved = read(self.c.position_path)
        self.assertFalse(saved['reliable'])
        self.assertIn('急停后编码器静止读数', saved['reason'])
        self.assertEqual(saved['axes'][0]['position_mm'], axis['position_mm'])
        self.assertTrue(self.c.snapshot()['estop_latched'])

    def test_anchor_defaults_reuse_mechanical_settings(self):
        self.ready()
        self.post('anchor', axis=1, mm=52, sign=1, lead=2, confirmed=True)
        self.c.step()
        self.post('anchor', axis=1, mm=53, confirmed=True)
        self.c.step()
        saved = read(self.c.position_path)['axes'][0]
        self.assertEqual((saved['position_mm'], saved['forward_sign'], saved['lead_mm']), (53, 1, 2))
