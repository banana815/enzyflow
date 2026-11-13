"""Temporary ID1-only +10 mm identity test, explicitly authorized by operator.

Exec/import only defines Id1Test10. Construction only reads and anchors from
the fresh measured 51 mm start. batch() sends at most three bounded steps.
No automatic motion, fabricated observation, persistent state, or retries.
"""
from enzyflow.motor_bus import MotorBus
from enzyflow.motor_safety import ThreeAxes, MARGIN_MM, TOLERANCE_PULSES


class Id1Test10:
    IDS = (1, 2, 3)
    MAX_STEPS = 12
    MAX_BATCH = 3
    FINISH_TOLERANCE_MM = 0.1
    OVERSHOOT_TOLERANCE_MM = 640.0 / 12800.0 + 0.00001

    def __init__(self, bus, confirmed=False, clock=None):
        self.controller = ThreeAxes(bus, clock)
        self.bus = bus
        self.state = 'initializing'
        self.long_id = 1
        self.start_mm = 51.0
        self.target_mm = 61.0
        self.steps = 0
        self.feedback_mm = None
        self.baselines = {}
        self.last_counts = {}
        try:
            if confirmed is not True:
                raise ValueError('Current measured 51 mm and authorized ID1 test required')
            if MARGIN_MM != 10.0 or MotorBus.SPEED_RPM != 6 or MotorBus.MAX_PULSES != 12800:
                raise ValueError('Unexpected safety constants; no motion permitted')
            self.controller.configure(self.IDS, 1, confirmed=True)
            axis = self.controller.axes[1]
            axis.check_target(self.start_mm)
            axis.check_target(self.target_mm)
            for ident in self.IDS:
                self.baselines[ident] = self.controller._stable(ident)
            self.controller.anchor(1, self.start_mm, -1, 4, confirmed=True)
            if abs(axis.last - self.baselines[1]) > TOLERANCE_PULSES:
                raise ValueError('ID1 counter changed during initialization')
            self.last_counts = dict(self.baselines)
            self.last_counts[1] = axis.last
            self.feedback_mm = self.start_mm
            self._check_others()
            self.state = 'ready_for_batches'
        except BaseException:
            self._fault()
            raise

    def _fault(self):
        self.state = 'faulted'
        self.controller.lock()

    def _check_others(self):
        for ident in (2, 3):
            value = self.bus.read_pulses(ident)
            self.last_counts[ident] = value
            if abs(value - self.baselines[ident]) > TOLERANCE_PULSES:
                raise ValueError('A nonselected axis changed from its initial baseline')

    def _current(self):
        value = self.controller.position(1)
        self.controller.axes[1].check_target(value)
        if value > self.target_mm + self.OVERSHOOT_TOLERANCE_MM:
            raise ValueError('Approved 10 mm target exceeded beyond feedback tolerance')
        self.feedback_mm = value
        return value

    def _step(self, distance):
        self._check_others()
        current = self._current()
        if self.steps >= self.MAX_STEPS:
            raise ValueError('Finite move budget exhausted')
        if not 0.1 <= distance <= 1.0:
            raise ValueError('Only positive 0.1..1 mm finite steps are permitted')
        if current + distance > self.target_mm + 0.000001:
            raise ValueError('Step exceeds the approved 10 mm target')
        self.steps += 1  # Attempts include ambiguous writes; never retry them.
        self.feedback_mm = self.controller.jog(1, distance)
        self.last_counts[1] = self.controller.axes[1].last
        self.controller.axes[1].check_target(self.feedback_mm)
        if self.feedback_mm > self.target_mm + self.OVERSHOOT_TOLERANCE_MM:
            raise ValueError('Approved 10 mm target exceeded beyond feedback tolerance')
        self._check_others()

    def _finish_if_close(self):
        if self.target_mm - self.feedback_mm < self.FINISH_TOLERANCE_MM:
            if self.feedback_mm > self.target_mm + self.OVERSHOOT_TOLERANCE_MM:
                raise ValueError('Unexpected endpoint overshoot')
            self.state = 'done'
            self.controller.lock()
            return True
        return False

    def batch(self):
        """At most three finite ID1 steps towards the original 61 mm target."""
        try:
            if self.state != 'ready_for_batches':
                raise ValueError('Session completed, cancelled, or faulted; no retry')
            for _ in range(self.MAX_BATCH):
                self._check_others()
                self._current()
                if self._finish_if_close():
                    break
                self._step(min(1.0, self.target_mm - self.feedback_mm))
                if self._finish_if_close():
                    break
            if self.state != 'done':
                self._check_others()
                self._current()
                self._finish_if_close()
            return self.status()
        except BaseException:
            self._fault()
            raise

    def cancel(self):
        """Between batches only; cannot cancel a command already sent."""
        self.controller.lock()
        self.state = 'cancelled'
        return self.status()

    def status(self):
        """Cached status only; never reads hardware or starts motion."""
        return {
            'state': self.state,
            'selected_id': 1,
            'start_mm': self.start_mm,
            'target_mm': self.target_mm,
            'feedback_mm': self.feedback_mm,
            'displacement_mm': None if self.feedback_mm is None else self.feedback_mm - self.start_mm,
            'step_attempts': self.steps,
            'encoders': dict(self.last_counts),
            'all_locked': all(a.reference is None for a in self.controller.axes.values()),
        }
