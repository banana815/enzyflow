"""Local HTTP security and static assets shared by independent device services."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import mimetypes
from pathlib import Path
import secrets
from urllib.parse import unquote, urlsplit

from .errors import APIError

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


class LocalHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, handler, port, token=None):
        self.token = token or secrets.token_urlsafe(32)
        super().__init__(("127.0.0.1", port), handler)


class LocalHandler(BaseHTTPRequestHandler):
    server_version = "EnzyflowControl/1"

    def log_message(self, format, *args):
        # Request URLs can contain browser state. Operational logs are explicit.
        pass

    def _host_allowed(self):
        port = self.server.server_port
        return self.headers.get("Host", "").lower() in ("127.0.0.1:%d" % port, "localhost:%d" % port)

    def _origin_allowed(self):
        origin = self.headers.get("Origin")
        port = self.server.server_port
        return origin is None or origin in ("http://127.0.0.1:%d" % port, "http://localhost:%d" % port)

    def _respond(self, status, body, content_type="application/json; charset=utf-8"):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False, allow_nan=False).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def get_api(self, path):
        return False

    def post_api(self, path, body):
        raise APIError("未知接口。", 404)

    def api_error(self, exc):
        return {"ok": False, "error": str(exc)}

    def do_GET(self):
        if not self._host_allowed() or not self._origin_allowed():
            self._respond(403, {"ok": False, "error": "仅允许本机页面访问。"})
            return
        path = urlsplit(self.path).path
        if path == "/api/health":
            self._respond(200, {"app": "enzyflow-control", "version": 10,
                                "service": self.server.service, "demo": self.server.demo})
            return
        if self.get_api(path):
            return
        if path in ("/", "/index.html"):
            file = STATIC / self.server.page
            if not file.is_file():
                self._respond(503, "控制台页面尚未安装。", "text/plain; charset=utf-8")
                return
            self._respond(200, file.read_text(encoding="utf-8").replace("__ENZYFLOW_TOKEN__", self.server.token),
                          "text/html; charset=utf-8")
            return
        if not path.startswith("/static/"):
            self._respond(404, {"ok": False, "error": "页面不存在。"})
            return
        relative = unquote(path[len("/static/"):])
        file = (STATIC / relative).resolve()
        if not file.is_relative_to(STATIC.resolve()) or not file.is_file():
            self._respond(404, {"ok": False, "error": "文件不存在。"})
            return
        kind = mimetypes.guess_type(str(file))[0] or "application/octet-stream"
        if kind.startswith("text/") or kind == "application/javascript":
            kind += "; charset=utf-8"
        self._respond(200, file.read_bytes(), kind)

    def do_POST(self):
        if (not self._host_allowed() or not self._origin_allowed()
                or not secrets.compare_digest(self.headers.get("X-Enzyflow-Token", ""), self.server.token)):
            self._respond(403, {"ok": False, "error": "页面验证失效，请重新打开本机控制台。"})
            return
        path = urlsplit(self.path).path
        if not path.startswith("/api/") or "/" in path[len("/api/"):]:
            self._respond(404, {"ok": False, "error": "未知接口。"})
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 8192:
                raise APIError("请求长度无效。")
            if self.headers.get_content_type() != "application/json":
                raise APIError("请求必须使用 application/json。")
            body = json.loads(self.rfile.read(length).decode("utf-8"),
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError("非有限数值")))
            if type(body) is not dict:
                raise APIError("请求必须是 JSON 对象。")
            result = self.post_api(path, body)
            self._respond(202 if result.get("accepted") else 200, result)
        except APIError as exc:
            self._respond(exc.status, self.api_error(exc))
        except (UnicodeDecodeError, ValueError, TypeError) as exc:
            self._respond(400, {"ok": False, "error": "请求格式或数值无效。"})
        except Exception:
            self._respond(500, {"ok": False, "error": "服务处理失败，请停止任务后查看运行日志。"})
