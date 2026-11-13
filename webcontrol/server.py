"""Independent motor service: python -m webcontrol.server (port 8765)."""
from __future__ import annotations

import argparse
import threading
import webbrowser

from .controller import Controller
from .errors import APIError
from .demo import DemoTransport
from .transport import SerialTransport
from .http_common import LocalHTTPServer, LocalHandler, ROOT


class LocalServer(LocalHTTPServer):
    service = "motor"
    page = "index.html"

    def __init__(self, controller, port=8765, token=None):
        self.controller = controller
        self.demo = controller.demo
        super().__init__(Handler, port, token)


class Handler(LocalHandler):
    def get_api(self, path):
        if path == "/api/state":
            self._respond(200, self.server.controller.snapshot())
            return True
        return False

    def post_api(self, path, body):
        if path.startswith("/api/spectrometer"):
            raise APIError("光谱仪使用独立服务。", 404)
        return self.server.controller.submit(path[len("/api/"):], body)

    def api_error(self, exc):
        return {"ok": False, "error": str(exc), "state": self.server.controller.snapshot()}


def main(argv=None):
    parser = argparse.ArgumentParser(description="Enzyflow 本机三轴控制台")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--demo", action="store_true", help="模拟设备，绝不访问硬件")
    parser.add_argument("--no-auto-connect", action="store_true")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--serial-port", default=None)
    options = parser.parse_args(argv)
    if not 1024 <= options.port <= 65535:
        parser.error("端口必须在 1024–65535。")
    factory = DemoTransport if options.demo else lambda: SerialTransport(options.serial_port)
    controller = Controller(factory, demo=options.demo, auto_connect=not options.no_auto_connect,
                            log_path=ROOT / "runtime" / "events.jsonl")
    server = None
    try:
        server = LocalServer(controller, options.port)
        url = "http://127.0.0.1:%d/" % options.port
        print("ENZYFLOW_READY " + url + (" (模拟模式)" if options.demo else ""), flush=True)
        if not options.no_browser:
            threading.Timer(0.3, lambda: webbrowser.open(url)).start()
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        if server:
            server.server_close()
        controller.close()


if __name__ == "__main__":
    main()
