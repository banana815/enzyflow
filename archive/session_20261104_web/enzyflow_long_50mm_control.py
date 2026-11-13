"""Temporary supervised 50 mm move; importing/exec only defines the class.

No machine/UART initialization, motion, saved-position restore, or retries.
The caller must supply the sole bus owner and freshly confirmed physical
identity/start position. probe() is the FIRST 1 mm of the total 50 mm.
After observing that step, verify(confirmed=True) permits the remaining path.
"""
from enzyflow.motor_bus import MotorBus
from enzyflow.motor_safety import ThreeAxes, MARGIN_MM, TOLERANCE_PULSES, number


class LongMove50:
    IDS = (1, 2, 3)
    DISTANCE_MM = 50.0
    MAX_STEPS = 55
    MAX_BATCH = 3
    FINISH_TOLERANCE_MM = 0.1
    OVERSHOOT_TOLERANCE_MM = 640.0 / 12800.0 + 0.00001

    def __init__(self, bus, long_id, start_mm=51.0, confirmed=False, clock=None):
        self.controller = ThreeAxes(bus, clock)
        self.bus = bus
        self.state = 'initializing'
        self.long_id = long_id
        self.steps = 0
        self.feedback_mm = None
        self.baselines = {}
        self.last_counts = {}
        try:
            if confirmed is not True:
                raise ValueError('Fresh identity and measured start confirmation required')
            if MARGIN_MM != 10.0 or MotorBus.SPEED_RPM != 6 or MotorBus.MAX_PULSES != 12800:
                raise ValueError('Unexpected safety constants; no motion permitted')
            self.start_mm = number(start_mm)
            self.target_mm = self.start_mm + self.DISTANCE_MM
            self.controller.configure(self.IDS, long_id, confirmed=True)
            axis = self.controller.axes[long_id]
            axis.check_target(self.start_mm)
            axis.check_target(self.target_mm)
            # Establish all three stationary counters before anchoring one axis.
            for ident in self.IDS:
                self.baselines[ident] = self.controller._stable(ident)
            self.controller.anchor(long_id, self.start_mm, -1, 4, confirmed=True)
            if abs(axis.last - self.baselines[long_id]) > TOLERANCE_PULSES:
                raise ValueError('Long-axis counter changed during initialization')
            self.last_counts = dict(self.baselines)
            self.last_counts[long_id] = axis.last
            self.feedback_mm = self.start_mm
            self._check_others()
            self.state = 'ready_for_first_step'
        except BaseException:
            self._fault()
            raise

    def _fault(self):
        self.state = 'faulted'
        self.controller.lock()

    def _check_others(self):
        for ident in self.IDS:
            if ident != self.long_id:
                value = self.bus.read_pulses(ident)
                self.last_counts[ident] = value
                if abs(value - self.baselines[ident]) > TOLERANCE_PULSES:
                    raise ValueError('A nonselected axis changed from its initial baseline')

    def _current(self):
        value = self.controller.position(self.long_id)
        self.controller.axes[self.long_id].check_target(value)
        if value > self.target_mm + self.OVERSHOOT_TOLERANCE_MM:
            raise ValueError('Approved total target exceeded beyond feedback tolerance')
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
            raise ValueError('Step exceeds the original 50 mm target')
        self.steps += 1  # Count attempts too; ambiguous writes can never be retried.
        self.feedback_mm = self.controller.jog(self.long_id, distance)
        self.last_counts[self.long_id] = self.controller.axes[self.long_id].last
        self.controller.axes[self.long_id].check_target(self.feedback_mm)
        if self.feedback_mm > self.target_mm + self.OVERSHOOT_TOLERANCE_MM:
            raise ValueError('Approved total target exceeded beyond feedback tolerance')
        self._check_others()

    def _finish_if_close(self):
        if self.target_mm - self.feedback_mm < self.FINISH_TOLERANCE_MM:
            if self.feedback_mm > self.target_mm + self.OVERSHOOT_TOLERANCE_MM:
                raise ValueError('Unexpected endpoint overshoot')
            self.state = 'done'
            self.controller.lock()
            return True
        return False

    def probe(self):
        """Move the first 1 mm once, then require explicit physical observation."""
        try:
            if self.state != 'ready_for_first_step':
                raise ValueError('First step already consumed or session not ready')
            self._step(1.0)
            self.state = 'awaiting_identity_direction_confirmation'
            return self.status()
        except BaseException:
            self._fault()
            raise

    def verify(self, confirmed=False):
        """No motion: accept explicit observation of correct axis and direction."""
        try:
            if self.state != 'awaiting_identity_direction_confirmation' or confirmed is not True:
                raise ValueError('Explicit first-step identity/direction confirmation required')
            self._check_others()
            self._current()
            self.state = 'ready_for_batches'
            return self.status()
        except BaseException:
            self._fault()
            raise

    def batch(self):
        """At most three finite steps, based on feedback to the ORIGINAL target."""
        try:
            if self.state != 'ready_for_batches':
                raise ValueError('Session unverified, completed, or faulted; no retry')
            for _ in range(self.MAX_BATCH):
                self._check_others()
                self._current()
                if self._finish_if_close():
                    break
                distance = min(1.0, self.target_mm - self.feedback_mm)
                self._step(distance)
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
        """Called between batches only; cannot cancel a command already sent."""
        self.controller.lock()
        self.state = 'cancelled'
        return self.status()

    def status(self):
        """Cached status only; never reads hardware or starts motion."""
        return {
            'state': self.state,
            'long_id': self.long_id,
            'start_mm': self.start_mm,
            'target_mm': self.target_mm,
            'feedback_mm': self.feedback_mm,
            'displacement_mm': None if self.feedback_mm is None else self.feedback_mm - self.start_mm,
            'step_attempts': self.steps,
            'encoders': dict(self.last_counts),
            'all_locked': all(a.reference is None for a in self.controller.axes.values()),
        }
