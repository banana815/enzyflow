"""Group launch, failure containment and inward-only recovery, without hardware."""
import unittest
from enzyflow.motor_safety import ThreeAxes
from test_motor_speed import SpeedBus
from test_motor_safety import FakeClock

class ParallelTests(unittest.TestCase):
    def setUp(self):
        self.bus = SpeedBus()
        self.clock = FakeClock()
        self.c = ThreeAxes(self.bus, self.clock)
        self.c.configure([1,2,3], 1, confirmed=True)
        for i in (1,2,3): self.c.anchor(i, 100, -1, 4, confirmed=True)

    def moves(self):
        return [{'axis':i, 'delta_mm': .5 if i != 2 else -.5, 'rpm':12} for i in (1,2,3)]

    def test_all_launch_before_waiting_and_track_different_directions(self):
        times=[]
        self.bus.on_move=lambda i,p: times.append(self.clock.now)
        result=self.c.jog_group(self.moves())
        self.assertEqual(result,{1:100.5,2:99.5,3:100.5})
        self.assertEqual(len(times),3)
        self.assertEqual(len(set(times)),1)

    def test_invalid_last_axis_prevents_every_launch(self):
        moves=self.moves();moves[-1]['delta_mm']=2
        with self.assertRaises(ValueError): self.c.jog_group(moves)
        self.assertEqual(self.bus.moves,[])

    def test_partial_launch_failure_never_launches_remaining_axis(self):
        original=self.bus._move_pulses
        def move(i,p,rpm=6):
            original(i,p,rpm)
            if i==2: raise OSError('ambiguous write')
        self.bus._move_pulses=move
        with self.assertRaises(OSError): self.c.jog_group(self.moves())
        self.assertEqual([i for i,p in self.bus.moves],[1,2])
        self.assertTrue(all(a.reference is None for a in self.c.axes.values()))

    def test_stationary_axis_drift_aborts_group(self):
        self.bus.on_move=lambda i,p:self.bus.counts.__setitem__(3,self.bus.counts[3]+1000)
        with self.assertRaises(ValueError): self.c.jog_group(self.moves()[:2])
        self.assertTrue(all(a.reference is None for a in self.c.axes.values()))

    def test_recovery_both_physical_ends_and_safe_entry(self):
        for i,start,delta in ((1,20,.5),(2,220,-.5),(3,29.8,.3),(1,310.2,-.3)):
            self.setUp();self.c.anchor(i,start,-1,4,confirmed=True)
            result=self.c.jog(i,delta,rpm=6,recovery=True)
            self.assertAlmostEqual(result,start+delta)

    def test_recovery_rejects_outward_fast_large_and_inside(self):
        for start,delta,rpm in ((20,-.1,6),(320,.1,6),(20,.5,12),(20,1,6),(100,.5,6)):
            self.setUp();self.c.anchor(1,start,-1,4,confirmed=True)
            with self.assertRaises(ValueError): self.c.jog(1,delta,rpm=rpm,recovery=True)
            self.assertEqual(self.bus.moves,[])

    def test_group_timeout_locks_every_axis(self):
        self.bus.respond_to_motion=False
        with self.assertRaises(ValueError): self.c.jog_group(self.moves())
        self.assertEqual(len(self.bus.moves),3)
        self.assertTrue(all(a.reference is None for a in self.c.axes.values()))
