"""Independent UVC service: python -m webcontrol.spectrometer_server (8766)."""
from __future__ import annotations

import argparse
import mimetypes
import threading
from urllib.parse import quote, unquote
import webbrowser

from .errors import APIError
from .http_common import LocalHTTPServer, LocalHandler
from .spectrometer import Spectrometer


class SpectrometerServer(LocalHTTPServer):
    service = "spectrometer"
    page = "spectrometer.html"

    def __init__(self, spectrometer, port=8766, token=None):
        self.spectrometer = spectrometer
        self.demo = spectrometer.demo
        super().__init__(Handler, port, token)

    def server_close(self):
        try:
            super().server_close()
        finally:
            self.spectrometer.close()


class Handler(LocalHandler):
    def get_api(self, path):
        if path == "/api/spectrometer-state":
            self._respond(200, self.server.spectrometer.snapshot())
            return True
        if path == "/api/spectrometer-devices":
            try:
                self._respond(200, {"devices": self.server.spectrometer.devices()})
            except Exception as exc:
                self._respond(503, {"ok": False, "error": str(exc)})
            return True
        if path == "/api/spectrometer-preview":
            with self.server.spectrometer.lock:
                preview = self.server.spectrometer.preview
            self._respond(200 if preview else 404, preview or b"", "image/jpeg")
            return True
        if path.startswith("/api/spectrometer-file/"):
            name = unquote(path.removeprefix("/api/spectrometer-file/"))
            with self.server.spectrometer.lock:
                file = self.server.spectrometer.exports.get(name)
            if file and file.is_file():
                # Stream only the indexed local export; bound a growing JSONL to
                # its length at download start instead of loading it all in RAM.
                with file.open('rb') as handle:
                    import os
                    remaining = os.fstat(handle.fileno()).st_size
                    self.send_response(200)
                    self.send_header('Content-Type', mimetypes.guess_type(str(file))[0] or 'application/octet-stream')
                    self.send_header('Content-Length',str(remaining))
                    self.send_header('Content-Disposition', "attachment; filename*=UTF-8''"+quote(file.name))
                    self.send_header('Cache-Control','no-store')
                    self.send_header('X-Content-Type-Options','nosniff')
                    self.end_headers()
                    try:
                        while remaining:
                            chunk=handle.read(min(65536,remaining))
                            if not chunk:break
                            self.wfile.write(chunk)
                            remaining-=len(chunk)
                    except (BrokenPipeError,ConnectionResetError):
                        pass
            else:
                self._respond(404, {"error": "采集文件不存在"})
            return True
        return False

    def post_api(self, path, body):
        if path != "/api/spectrometer":
            raise APIError("未知光谱仪接口。", 404)
        try:
            return self.server.spectrometer.command(body)
        except (ValueError, RuntimeError) as exc:
            raise APIError(str(exc)) from exc


def main(argv=None):
    parser = argparse.ArgumentParser(description="Enzyflow 独立光谱仪控制台")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--demo", action="store_true", help="模拟光谱仪，绝不访问硬件")
    parser.add_argument("--no-browser", action="store_true")
    options = parser.parse_args(argv)
    if not 1024 <= options.port <= 65535:
        parser.error("端口必须在 1024–65535。")
    spectrometer = Spectrometer(demo=options.demo)
    server = None
    try:
        server = SpectrometerServer(spectrometer, options.port)
        url = "http://127.0.0.1:%d/" % options.port
        print("ENZYFLOW_SPECTROMETER_READY " + url + (" (模拟模式)" if options.demo else ""), flush=True)
        if not options.no_browser:
            threading.Timer(0.3, lambda: webbrowser.open(url)).start()
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        else:
            spectrometer.close()


if __name__ == "__main__":
    main()
