"""Console workflows with fake input, encoder and clock; never uses hardware."""
import contextlib
import io
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / 'firmware' / 'lib'))
from enzyflow import motor_console
from enzyflow.motor_safety import ThreeAxes
from test_motor_safety import FakeBus, FakeClock


class ConsoleTests(unittest.TestCase):
    def setUp(self):
        self.bus = FakeBus()
        self.clock = FakeClock()
        self.controller = ThreeAxes(self.bus, self.clock)
        self.prompts = []
        self.stdout = io.StringIO()
        self.scans = []
        self.bus.scan = self.scan

    def scan(self, start, end):
        self.scans.append((start, end))
        return [1, 2, 3]

    def run_input(self, values):
        answers = iter(values)
        def fake_input(prompt):
            self.prompts.append(prompt)
            try:
                value = next(answers)
            except StopIteration:
                raise EOFError()
            if isinstance(value, BaseException):
                raise value
            return value
        with patch('builtins.input', fake_input), patch.object(
                motor_console, 'ThreeAxes', return_value=self.controller), contextlib.redirect_stdout(self.stdout):
            motor_console.run(bus=self.bus)
        self.assertTrue(all(a.reference is None for a in self.controller.axes.values()))
        self.assertFalse(self.controller.busy)
        return self.stdout.getvalue()

    def setup_answers(self):
        return ['1 2 3', '3', 'YES',
                '10', '-1', '4', 'YES',
                '10', '1', '4', 'YES',
                '10', '-1', '4', 'YES']

    def test_startup_prompts_long_short_mapping_before_calibration(self):
        output = self.run_input(['1 2 3', '3', 'YES', 'q'])
        self.assertIn('长轴： 3', output)
        self.assertIn('短轴： [1, 2]', output)
        long_prompt = next(i for i, p in enumerate(self.prompts) if '哪一个机号是长丝杆' in p)
        pos_prompt = next(i for i, p in enumerate(self.prompts) if '当前实测位置' in p)
        self.assertLess(long_prompt, pos_prompt)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])
        self.assertEqual(self.controller.axes[3].maximum, 320)

    def test_quit_before_mapping_is_silent(self):
        self.run_input(['q'])
        self.assertEqual(self.bus.reads, [])
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.controller.axes, {})

    def test_scan_is_read_only_and_still_requires_mapping(self):
        self.run_input(['scan', 'all', 'q'])
        self.assertEqual(self.scans, [(1, 10), (1, 255)])
        self.assertEqual(self.controller.axes, {})
        self.assertEqual(self.bus.moves, [])

    def test_mapping_confirmation_cannot_be_skipped(self):
        output = self.run_input(['1 2 3', '3', 'no', 'q'])
        self.assertIn('未解锁', output)
        self.assertEqual(self.bus.reads, [])
        self.assertEqual(self.bus.moves, [])
        self.assertFalse(any('当前实测位置' in p for p in self.prompts))

    def test_duplicate_mapping_never_reaches_calibration(self):
        output = self.run_input(['1 1 3', '3', 'YES', 'q'])
        self.assertIn('机号必须不同', output)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])

    def test_calibration_alone_never_moves(self):
        output = self.run_input(self.setup_answers() + ['p', 'q'])
        self.assertEqual(self.bus.moves, [])
        self.assertIn('ID1 10.0000 cm', output)
        self.assertIn('ID2 10.0000 cm', output)
        self.assertIn('ID3 10.0000 cm', output)

    def test_forward_backward_cancel_independently_without_motion(self):
        output = self.run_input(self.setup_answers() + ['f 1 0.2', 'cancel', 'b 2 0.3', 'MOVE 1', 'q'])
        self.assertIn('前进 0.2000mm', output)
        self.assertIn('后退 0.3000mm', output)
        self.assertTrue(any('MOVE 1' in p for p in self.prompts))
        self.assertTrue(any('MOVE 2' in p for p in self.prompts))
        self.assertEqual(output.count('已取消，未发送运动命令'), 2)
        self.assertEqual(self.bus.moves, [])

    def test_only_exact_id_confirmation_can_move_selected_axis(self):
        self.run_input(self.setup_answers() + ['f 1 0.2', 'MOVE 1', 'b 2 0.3', 'MOVE 2', 'q'])
        self.assertEqual(self.bus.moves, [(1, -2560), (2, -3840)])

    def test_invalid_distances_nan_inf_and_subminimum_never_prompt_motion(self):
        output = self.run_input(self.setup_answers() + ['f 1 nan', 'b 2 inf', 'f 3 -inf',
                              'f 1 0.05', 'b 2 1.01', 'f 1 invalid', 'q'])
        self.assertEqual(self.bus.moves, [])
        self.assertFalse(any('执行请输入 MOVE' in p for p in self.prompts))
        self.assertEqual(output.count('拒绝操作/故障'), 6)

    def test_nan_anchor_never_anchors_or_moves(self):
        output = self.run_input(['1 2 3', '3', 'YES', 'nan', '-1', '4', 'YES', 'q'])
        self.assertIn('未标定', output)
        self.assertEqual(self.bus.reads, [])
        self.assertEqual(self.bus.moves, [])

    def test_out_of_range_short_target_never_prompts_motion(self):
        answers = self.setup_answers()
        answers[3] = '22'
        output = self.run_input(answers + ['f 1 0.1', 'q'])
        self.assertIn('拒绝操作/故障', output)
        self.assertEqual(self.bus.moves, [])
        self.assertFalse(any('执行请输入 MOVE' in p for p in self.prompts))

    def test_console_one_cm_margin_boundaries_and_outward_rejection(self):
        for ident, start_cm, direction, expected_pulses in (
                (1, '3.1', 'b', 12800),
                (2, '20.9', 'f', 12800),
                (3, '30.9', 'f', -12800)):
            with self.subTest(ident=ident, start_cm=start_cm):
                self.setUp()
                answers = self.setup_answers()
                answers[3 + (ident - 1) * 4] = start_cm
                output = self.run_input(answers + [
                    '%s %d 1' % (direction, ident), 'MOVE %d' % ident,
                    '%s %d 0.1' % (direction, ident), 'MOVE %d' % ident, 'q'])
                self.assertEqual(self.bus.moves, [(ident, expected_pulses)])
                self.assertEqual(sum('执行请输入 MOVE' in p for p in self.prompts), 1)
                self.assertIn('拒绝操作/故障', output)

    def test_eof_after_calibration_locks_every_axis(self):
        output = self.run_input(self.setup_answers())
        self.assertIn('已退出并锁定', output)
        self.assertEqual(self.bus.moves, [])

    def test_control_c_after_calibration_locks_every_axis(self):
        output = self.run_input(self.setup_answers() + [KeyboardInterrupt()])
        self.assertIn('已退出并锁定', output)
        self.assertEqual(self.bus.moves, [])

    def test_control_c_after_write_locks_and_does_not_resend(self):
        def stop_after_write(ms):
            if self.bus.moves:
                raise KeyboardInterrupt()
        self.clock.on_sleep = stop_after_write
        output = self.run_input(self.setup_answers() + ['f 1 0.2', 'MOVE 1'])
        self.assertEqual(self.bus.moves, [(1, -2560)])
        self.assertIn('没有发送硬件停止指令', output)

    def test_lock_then_move_is_refused(self):
        output = self.run_input(self.setup_answers() + ['lock', 'f 1 0.2', 'q'])
        self.assertIn('尚未标定', output)
        self.assertEqual(self.bus.moves, [])
        self.assertFalse(any('执行请输入 MOVE' in p for p in self.prompts))

    def test_eof_during_recalibration_leaves_no_old_anchor(self):
        self.run_input(self.setup_answers() + ['cal 1'])
        self.assertEqual(self.bus.moves, [])


    def test_unknown_direction_requires_matching_explicit_probe_confirmation(self):
        for confirm in ('cancel', 'YES', 'PROBE 2'):
            with self.subTest(confirm=confirm):
                self.setUp()
                output = self.run_input(['1 2 3', '3', 'YES', '7', '?', '4', confirm, 'q'])
                self.assertEqual(self.bus.moves, [])
                self.assertEqual(self.bus.reads, [])
                self.assertIn('只正转0.5mm', output)

    def test_unknown_direction_probe_moves_once_then_requires_new_measurement(self):
        output = self.run_input(['1 2 3', '3', 'YES', '7', '?', '4', 'PROBE 1', 'q'])
        self.assertEqual(self.bus.moves, [(1, 6400)])
        self.assertIn('正转编码器等效行程 0.5000mm', output)
        self.assertIn('全部已锁定', output)
        self.assertEqual(sum('当前实测位置' in p for p in self.prompts), 2)

    def test_console_probe_near_boundary_is_refused_without_motion(self):
        output = self.run_input(['1 2 3', '3', 'YES', '3', '?', '4', 'PROBE 1', 'q'])
        self.assertIn('两端至少11mm', output)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])

    def test_console_probe_bad_lead_is_refused_without_motion(self):
        output = self.run_input(['1 2 3', '3', 'YES', '7', '?', '3', 'PROBE 1', 'q'])
        self.assertIn('未标定', output)
        self.assertEqual(self.bus.moves, [])
        self.assertEqual(self.bus.reads, [])

    def test_console_probe_control_c_never_resends_and_locks(self):
        def interrupt(ms):
            if self.bus.moves:
                raise KeyboardInterrupt()
        self.clock.on_sleep = interrupt
        output = self.run_input(['1 2 3', '3', 'YES', '7', '?', '4', 'PROBE 1'])
        self.assertEqual(self.bus.moves, [(1, 6400)])
        self.assertIn('没有发送硬件停止指令', output)


    def test_startup_revisits_earlier_axes_invalidated_by_later_probe(self):
        output = self.run_input([
            '1 2 3', '3', 'YES',
            '10', '-1', '4', 'YES',  # axis1 initial anchor
            '7', '?', '4', 'PROBE 2',  # axis2 probe invalidates axis1
            '10', '-1', '4', 'YES',  # axis3 in first pass
            '10', '-1', '4', 'YES',  # axis1 must be measured again
            '7.05', '1', '4', 'YES',  # axis2 must be measured after probe
            'p', 'q'])
        self.assertEqual(self.bus.moves, [(2, 6400)])
        self.assertEqual(output.count('ID1 本次位置基准已建立'), 2)
        self.assertIn('ID1 10.0000 cm', output)
        self.assertIn('ID2 7.0500 cm', output)
        self.assertIn('ID3 10.0000 cm', output)
        self.assertNotIn('未标定/已锁定', output)


    def test_console_uses_confirmed_two_mm_axis_limit_and_conversion(self):
        answers = self.setup_answers()
        answers[9] = '2'  # axis2 lead; axis1 and axis3 remain4mm
        output = self.run_input(answers + ['f 2 1', 'f 2 0.5', 'MOVE 2', 'f 1 1', 'MOVE 1', 'q'])
        self.assertEqual(self.bus.moves, [(2, 12800), (1, -12800)])
        self.assertIn('拒绝操作/故障', output)
        self.assertEqual(sum('执行请输入 MOVE 2' in p for p in self.prompts), 1)

    def test_console_two_mm_direction_probe_has_correct_half_mm_pulses(self):
        output = self.run_input(['1 2 3', '3', 'YES', '7', '?', '2', 'PROBE 1', 'q'])
        self.assertEqual(self.bus.moves, [(1, 12800)])
        self.assertIn('正转编码器等效行程 0.5000mm', output)


if __name__ == '__main__':
    unittest.main()
