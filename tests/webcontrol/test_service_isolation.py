"""Device services must remain usable without the other device or dependencies."""
from http.client import HTTPConnection
from io import StringIO
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest.mock import patch

from tools.launch_webcontrol import open_browser, validate_service
from webcontrol.controller import Controller
from webcontrol.demo import DemoTransport
from webcontrol.server import LocalServer
from webcontrol.spectrometer import Spectrometer
from webcontrol.spectrometer_server import SpectrometerServer

ROOT = Path(__file__).resolve().parents[2]


def request(server, method, path, body=None, headers=None):
    connection = HTTPConnection('127.0.0.1', server.server_port, timeout=3)
    data = None if body is None else json.dumps(body)
    actual_headers = {'Content-Type': 'application/json', 'X-Enzyflow-Token': server.token}
    actual_headers.update(headers or {})
    try:
        connection.request(method, path, data, actual_headers)
        response = connection.getresponse()
        return response.status, response.read(), dict(response.getheaders())
    finally:
        connection.close()


class ServiceIsolationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.controller = Controller(DemoTransport, demo=True, autostart=False)
        self.addCleanup(self.controller.close)
        self.motor = LocalServer(self.controller, 0, token='motor-token')
        self.instrument = Spectrometer(demo=True, data_dir=self.temp.name)
        self.spectrum = SpectrometerServer(self.instrument, 0, token='spectrum-token')
        self.threads = []
        for server in (self.motor, self.spectrum):
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            self.threads.append(thread)

    def tearDown(self):
        for server, thread in zip((self.motor, self.spectrum), self.threads):
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

    def test_pages_only_start_their_own_controls(self):
        for server, script, forbidden in ((self.motor, 'app.js', 'spectrometer.js'),
                                          (self.spectrum, 'spectrometer.js', 'app.js')):
            status, page, _ = request(server, 'GET', '/')
            self.assertEqual(status, 200)
            self.assertIn(('/static/' + script).encode(), page)
            self.assertNotIn(('/static/' + forbidden).encode(), page)
            self.assertIn(server.token.encode(), page)
            self.assertNotIn(b'__ENZYFLOW_TOKEN__', page)
            status, body, _ = request(server, 'GET', '/api/health')
            self.assertEqual(json.loads(body)['service'], server.service)
        self.assertIsNone(self.controller.snapshot()['owner'])
        self.assertFalse(self.controller.snapshot()['connected'])

    def test_other_devices_endpoints_are_unavailable(self):
        for method, path in (('GET', '/api/spectrometer-state'),
                             ('GET', '/api/spectrometer-devices'),
                             ('POST', '/api/spectrometer')):
            self.assertEqual(request(self.motor, method, path, {'op': 'connect'})[0], 404)
        for method, path in (('GET', '/api/state'), ('POST', '/api/claim'),
                             ('POST', '/api/connect'), ('POST', '/api/estop')):
            self.assertEqual(request(self.spectrum, method, path, {'client_id': 'test'})[0], 404)
        self.assertIsNone(self.controller.snapshot()['owner'])
        self.assertIsNone(self.instrument.thread)

    def test_spectrum_security_and_invalid_requests(self):
        for headers in ({'Host': 'attacker.test'}, {'Origin': 'https://attacker.test'},
                        {'Origin': 'http://127.0.0.1:%d' % self.motor.server_port},
                        {'X-Enzyflow-Token': self.motor.token}, {'X-Enzyflow-Token': ''}):
            self.assertEqual(request(self.spectrum, 'POST', '/api/spectrometer',
                                     {'op': 'connect'}, headers)[0], 403)
        for body in ([], None, {'op': 'move'}):
            self.assertEqual(request(self.spectrum, 'POST', '/api/spectrometer', body)[0], 400)
        for path in ('/static/%2e%2e/server.py', '/api/spectrometer-file/../../server.py'):
            self.assertEqual(request(self.spectrum, 'GET', path)[0], 404)
        self.assertIsNone(self.instrument.thread)

    def test_spectrum_state_preview_and_indexed_download(self):
        status, body, _ = request(self.spectrum, 'GET', '/api/spectrometer-state')
        self.assertEqual(status, 200)
        self.assertFalse(json.loads(body)['connected'])
        self.assertEqual(request(self.spectrum, 'GET', '/api/spectrometer-preview')[0], 404)
        self.instrument.preview = b'jpeg-preview'
        self.assertEqual(request(self.spectrum, 'GET', '/api/spectrometer-preview')[:2],
                         (200, b'jpeg-preview'))
        export = Path(self.temp.name) / 'test.csv'
        export.write_text('pixel,intensity\n1,2\n')
        self.instrument.exports[export.name] = export
        status, body, headers = request(self.spectrum, 'GET', '/api/spectrometer-file/test.csv')
        self.assertEqual(status, 200)
        self.assertEqual(body, export.read_bytes())
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertEqual(request(self.spectrum, 'POST', '/api/spectrometer', {'op': 'disconnect'})[0], 202)
        self.assertFalse(self.controller.snapshot()['connected'])

    def test_stopping_spectrum_keeps_motor_alive(self):
        self.spectrum.shutdown()
        self.spectrum.server_close()
        self.assertEqual(request(self.motor, 'GET', '/api/state')[0], 200)
        self.assertEqual(request(self.motor, 'POST', '/api/claim', {'client_id': 'test'})[0], 202)

    def test_stopping_motor_keeps_spectrum_alive(self):
        self.motor.shutdown()
        self.motor.server_close()
        self.controller.close()
        self.assertEqual(request(self.spectrum, 'GET', '/api/spectrometer-state')[0], 200)
        self.assertEqual(request(self.spectrum, 'POST', '/api/spectrometer', {'op': 'disconnect'})[0], 202)


class LaunchIsolationTests(unittest.TestCase):
    def test_browser_opener_is_selected_by_platform(self):
        url = 'http://127.0.0.1:8766/'
        for platform, opener in (('darwin', 'open'), ('linux', 'xdg-open')):
            with self.subTest(platform=platform), \
                    patch('tools.launch_webcontrol.sys.platform', platform), \
                    patch('tools.launch_webcontrol.subprocess.Popen') as launch:
                open_browser(url)
                self.assertEqual(launch.call_args.args[0], [opener, url])

    def test_missing_browser_opener_prints_manual_url(self):
        url = 'http://127.0.0.1:8766/'
        with patch('tools.launch_webcontrol.subprocess.Popen', side_effect=FileNotFoundError), \
                patch('sys.stderr', new_callable=StringIO) as stderr:
            open_browser(url)
        self.assertIn(url, stderr.getvalue())

    def test_launcher_rejects_legacy_or_wrong_device_or_demo(self):
        for current in ({'demo': False}, {'service': 'spectrometer', 'demo': False},
                        {'service': 'motor', 'demo': True}):
            with self.assertRaises(RuntimeError):
                validate_service(current, 'motor', False)
        with self.assertRaises(RuntimeError):
            validate_service({'service': 'motor', 'demo': False, 'version': 10}, 'motor', False)
        validate_service({'service': 'motor', 'demo': False, 'version': 11}, 'motor', False)
        validate_service(None, 'spectrometer', False)

    def test_imports_do_not_require_the_other_device_stack(self):
        for module, blocked in (
            ('webcontrol.server', ['webcontrol.spectrometer', 'spectrometer', 'numpy', 'cv2']),
            ('webcontrol.spectrometer_server', ['webcontrol.controller', 'webcontrol.transport',
                                               'webcontrol.demo', 'serial']),
        ):
            with self.subTest(module=module):
                code = '''import importlib, importlib.abc, sys
blocked = %r
class BlockOtherDevice(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if any(fullname == x or fullname.startswith(x + '.') for x in blocked):
            raise AssertionError('unexpected dependency: ' + fullname)
sys.meta_path.insert(0, BlockOtherDevice())
importlib.import_module(%r)
''' % (blocked, module)
                result = subprocess.run([sys.executable, '-B', '-c', code], cwd=ROOT,
                                        capture_output=True, text=True, timeout=10)
                self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == '__main__':
    unittest.main()
