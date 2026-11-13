"""Real host/firmware protocol over an in-memory wire, never a serial device."""
import json
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.dont_write_bytecode = True
ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "firmware/lib"), str(ROOT / "tests/firmware")]
from enzyflow.motor_web import MotorRPC
from test_motor_safety import FakeClock
from test_motor_speed import SpeedBus
from webcontrol.transport import ConnectionFailure, DeviceError, SerialTransport, esp32_ports


class PortDiscoveryTests(unittest.TestCase):
    def test_native_and_current_usb_bridge_are_candidates_only(self):
        ports = [SimpleNamespace(device=device, vid=vid, pid=pid) for device, vid, pid in [
            ("/dev/ttyUSB0", 0x1A86, 0x7523),
            ("/dev/ttyACM0", 0x303A, 0x1001),
            ("/dev/ttyUSB1", 0x1A86, 0x9999),
            ("/dev/ttyS0", 0x1A86, 0x7523),
            ("/dev/ttyUSB2", None, None),
        ]]
        with patch("serial.tools.list_ports.comports", return_value=ports):
            self.assertEqual(esp32_ports(), ["/dev/ttyACM0", "/dev/ttyUSB0"])

    def test_multiple_candidates_are_not_opened(self):
        with patch("webcontrol.transport.esp32_ports", return_value=["/dev/ttyACM0", "/dev/ttyUSB1"]):
            with self.assertRaisesRegex(ConnectionFailure, "多块"):
                SerialTransport().connect()


    def test_usb0_does_not_override_ambiguous_usb_identity(self):
        with patch("webcontrol.transport.esp32_ports", return_value=["/dev/ttyACM0", "/dev/ttyUSB0"]):
            with self.assertRaisesRegex(ConnectionFailure, "多块"):
                SerialTransport().connect()

    def test_video_node_cannot_be_used_as_serial(self):
        with patch("webcontrol.transport.esp32_ports", return_value=["/dev/ttyUSB0"]):
            with self.assertRaises(ConnectionFailure):
                SerialTransport('/dev/video2').connect()


class Wire:
    def __init__(self, rpc):
        self.rpc = rpc
        self.output = bytearray()
        self.requests = []
        self.modify_response = None
        self.closed = False

    @property
    def in_waiting(self):
        return len(self.output)

    def write(self, data):
        if data.startswith(b"{"):
            request = json.loads(data)
            self.requests.append(request)
            response = self.rpc.handle(request)
            if self.modify_response:
                response = self.modify_response(response)
            self.output.extend(json.dumps(response).encode() + b"\n")
        return len(data)

    def flush(self):
        pass

    def read(self, amount):
        data = bytes(self.output[:amount])
        del self.output[:amount]
        return data

    def close(self):
        self.closed = True


class TransportTests(unittest.TestCase):
    def setUp(self):
        self.bus = SpeedBus()
        self.rpc = MotorRPC(self.bus, FakeClock(), session="offline-session")
        self.wire = Wire(self.rpc)
        self.transport = SerialTransport()
        self.transport.serial = self.wire
        self.transport.session = self.rpc.session

    def tearDown(self):
        self.transport.close()

    def ready(self):
        self.transport.request("configure", {"ids": [1, 2, 3], "long_id": 1, "confirmed": True})
        self.transport.request("anchor", {"axis": 1, "mm": 51, "sign": -1, "lead": 4, "confirmed": True})

    def test_graceful_close_sends_shutdown_without_interrupt(self):
        self.ready()
        with patch.object(self.transport, 'emergency_stop') as emergency:
            self.transport.close(graceful=True)
        emergency.assert_not_called()
        self.assertTrue(self.wire.closed)
        self.assertEqual(self.wire.requests[-1]['op'], 'shutdown')
        self.assertTrue(self.rpc.shutdown_requested)

    def test_failed_graceful_close_falls_back_to_emergency(self):
        self.transport.supports_emergency=True
        with patch.object(self.transport, 'request', side_effect=DeviceError('stop uncertain')), patch.object(self.transport, 'emergency_stop') as emergency, patch.object(self.transport, 'finish_emergency'):
            self.transport.close(graceful=True)
        emergency.assert_called_once()
        self.assertTrue(self.wire.closed)

    def test_real_protocol_shapes_and_speed_are_compatible(self):
        self.ready()
        result = self.transport.request("jog", {"axis": 1, "delta_mm": 0.1, "rpm": 12})
        self.assertAlmostEqual(result["axes"][0]["position_mm"], 51.1)
        self.assertEqual(self.bus.moves, [(1, -1280)])
        self.assertEqual(self.bus.rpms, [12])
        self.assertEqual([r["seq"] for r in self.wire.requests], [1, 2, 3])
        self.assertEqual(len({r["id"] for r in self.wire.requests}), 3)

    def test_device_rejection_consumes_sequence_and_locks_without_retry(self):
        self.ready()
        with self.assertRaises(DeviceError):
            self.transport.request("jog", {"axis": 2, "delta_mm": 1, "rpm": 12})
        result = self.transport.request("status")
        self.assertTrue(all(not a["anchored"] for a in result["axes"]))
        self.assertEqual(self.bus.moves, [])
        self.assertEqual([r["seq"] for r in self.wire.requests], [1, 2, 3, 4])

    def test_group_protocol_moves_three_axes_and_preserves_sequence(self):
        self.ready()
        for ident in (2, 3):
            self.transport.request("anchor", {"axis": ident, "mm": 100, "sign": -1, "lead": 4, "confirmed": True})
        result = self.transport.request("jog_group", {"moves": [
            {"axis": 1, "delta_mm": .5, "rpm": 12},
            {"axis": 2, "delta_mm": -.5, "rpm": 6},
            {"axis": 3, "delta_mm": .5, "rpm": 12}]})
        self.assertEqual([a["position_mm"] for a in result["axes"]], [51.5, 99.5, 100.5])
        self.assertEqual(self.bus.moves, [(1,-6400),(2,6400),(3,-6400)])
        self.transport.request("status")

    def test_recovery_protocol_does_not_require_target_already_in_safe_zone(self):
        self.ready()
        self.transport.request("anchor", {"axis":1,"mm":20,"sign":-1,"lead":4,"confirmed":True})
        result = self.transport.request("recover", {"axis":1,"delta_mm":.5,"rpm":6,"confirmed":True})
        self.assertEqual(result["axes"][0]["position_mm"],20.5)
        self.assertEqual(self.bus.moves,[(1,-6400)])
        self.transport.request("status")

    def test_wrong_response_identity_closes_and_never_retries(self):
        self.ready()
        self.wire.modify_response = lambda response: dict(response, id="wrong-response")
        with self.assertRaises(ConnectionFailure):
            self.transport.request("jog", {"axis": 1, "delta_mm": 1, "rpm": 12})
        self.assertEqual(self.bus.moves, [(1, -12800)])
        self.assertEqual(len(self.wire.requests), 3)
        self.assertTrue(self.wire.closed)
        self.assertIsNone(self.transport.session)
        with self.assertRaises(ConnectionFailure):
            self.transport.request("jog", {"axis": 1, "delta_mm": 1, "rpm": 12})
        self.assertEqual(len(self.bus.moves), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
