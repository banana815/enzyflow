import unittest
from test_continuous_reset import ContinuousResetTests

class ContinuousMoveTests(ContinuousResetTests):
    def test_subset_whole_move_and_live_feedback(self):
        self.call('move_continuous',{'moves':[{'axis':1,'target_mm':110,'rpm':12}],'confirmed':True})
        self.clock.sleep_ms(400)
        state=self.call('reset_heartbeat',{})
        self.assertGreater(state['axes'][0]['position_mm'],100)
        self.assertLess(state['axes'][0]['position_mm'],110)
        self.finish()
        self.assertEqual(self.bus.reset_moves,[(1,-128000,12)])
        self.assertEqual([a['position_mm'] for a in self.call('status',{})['axes']],[110,100,100])
    def test_independent_speeds_and_nonmoving_axis_monitor(self):
        self.call('move_continuous',{'moves':[{'axis':1,'target_mm':110,'rpm':12},{'axis':2,'target_mm':105,'rpm':6}],'confirmed':True})
        self.assertEqual([m[2] for m in self.bus.reset_moves],[12,6])
        self.bus.counts[3]+=1000
        self.clock.sleep_ms(50);self.rpc.tick()
        self.assertFalse(self.rpc.reset.active)
        self.assertIn('非运动轴',self.rpc.reset.result)
        self.assertTrue(all(a.reference is None for a in self.rpc.controller.axes.values()))
    def test_stop_reports_measured_checkpoint_then_locks(self):
        self.call('move_continuous',{'moves':[{'axis':1,'target_mm':110,'rpm':12}],'confirmed':True})
        self.clock.sleep_ms(400)
        state=self.call('lock',{})
        self.assertGreater(state['stopped_axes'][0]['position_mm'],100)
        self.assertLess(state['stopped_axes'][0]['position_mm'],110)
        self.assertFalse(any(a['anchored'] for a in state['axes']))
        self.assertFalse(self.bus.rates)
    def test_unanchored_unselected_axes_do_not_block_single_axis(self):
        self.rpc.controller.axes[2].lock();self.rpc.controller.axes[3].lock()
        self.call('move_continuous',{'moves':[{'axis':1,'target_mm':101,'rpm':12}],'confirmed':True})
        self.finish();self.assertEqual(self.rpc.reset.result,'done')
    def test_minimum_distance_continuous_recovery_and_bounds(self):
        self.call('anchor',{'axis':1,'mm':20,'sign':-1,'lead':4,'confirmed':True})
        self.call('move_continuous',{'moves':[{'axis':1,'target_mm':20.5,'rpm':6}],'recovery':True,'confirmed':True})
        self.finish();self.assertAlmostEqual(self.call('status',{})['axes'][0]['position_mm'],20.5)
    def test_invalid_speed_and_recovery_atomic_before_writes(self):
        for moves,recovery in (([{'axis':1,'target_mm':110,'rpm':31}],False),([{'axis':1,'target_mm':110,'rpm':12}],True)):
            with self.assertRaises(ValueError):self.rpc.reset.start(moves,12,True,general=True,recovery=recovery)
        self.assertEqual(self.bus.reset_moves,[])
