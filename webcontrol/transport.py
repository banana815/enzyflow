"""Exclusive, no-retry MicroPython JSON-RPC transport.

This module never imports serial or opens a device at import time. All serial
access belongs to the controller worker. The only submitted Python program is
the fixed, installed ``enzyflow.motor_web.run`` entry point.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import threading
import uuid


class ConnectionFailure(RuntimeError):
    pass


class DeviceError(RuntimeError):
    """A valid device response rejected an operation; the channel still works."""


def esp32_ports():
    try:
        from serial.tools import list_ports
    except ImportError as exc:
        raise ConnectionFailure("缺少 pyserial，请使用项目启动入口或 Thonny 的 Python。") from exc
    prefixes = ("/dev/cu.",) if sys.platform == "darwin" else ("/dev/ttyACM", "/dev/ttyUSB")
    return sorted({p.device for p in list_ports.comports()
                  if (p.vid == 0x303A or (p.vid, p.pid) == (0x1A86, 0x7523))
                  and p.device.startswith(prefixes)})


def port_holder(port):
    """Return a PID already owning the device, without reading process data."""
    destination = os.path.realpath(port)
    if sys.platform == "darwin":
        dial_in = "/dev/tty." + Path(port).name.removeprefix("cu.")
        devices = [destination]
        if os.path.exists(dial_in):
            devices.append(dial_in)
        try:
            result = subprocess.run(
                ["/usr/sbin/lsof", "-t", "--", *devices],
                capture_output=True, text=True, timeout=2, check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        return next((int(pid) for pid in result.stdout.split()
                     if pid.isdigit() and int(pid) != os.getpid()), None)
    if not sys.platform.startswith("linux"):
        return None
    for process in Path("/proc").iterdir():
        if not process.name.isdigit() or int(process.name) == os.getpid():
            continue
        try:
            for fd in (process / "fd").iterdir():
                try:
                    if os.path.realpath(os.readlink(fd)) == destination:
                        return int(process.name)
                except OSError:
                    pass
        except (PermissionError, FileNotFoundError, ProcessLookupError):
            pass
    return None


class SerialTransport:
    def __init__(self, port=None, timeout=12.0):
        self.port = port
        self.timeout = timeout
        self.serial = None
        self.lock_file = None
        self.session = None
        self.seq = 0
        self.device = "ESP32"
        self.identity = None
        self._buffer = bytearray()
        self._write_lock = threading.Lock()
        self.emergency_requested = threading.Event()
        self.emergency_result = None
        self.supports_emergency = False

    def connect(self):
        if self.serial is not None:
            raise ConnectionFailure("串口已连接，禁止重复打开。")
        available = esp32_ports()
        if self.port:
            if self.port not in available:
                raise ConnectionFailure("未检测到指定的 ESP32 串口，请检查 USB 数据线。")
        elif len(available) == 1:
            self.port = available[0]
        elif not available:
            raise ConnectionFailure("未检测到 ESP32，请连接 USB 数据线。")
        else:
            raise ConnectionFailure("检测到多块 ESP32，请仅连接本套装置或指定串口。")
        try:
            self.lock_file = open("/tmp/enzyflow-board-tool.lock", "a")
            try:
                fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ConnectionFailure("设备正由维护工具使用，请等待当前操作结束。") from exc
            if port_holder(self.port) is not None:
                raise ConnectionFailure("串口已被占用。请在 Thonny 的“运行”菜单选择“断开”，再点击连接。")
            import serial
            from serial.tools import list_ports
            info = next(p for p in list_ports.comports() if p.device == self.port)
            self.identity = {k: getattr(info, k) for k in ('vid','pid','serial_number','location')}
            self.serial = serial.Serial(port=None, baudrate=115200, timeout=0.1,
                                        write_timeout=1.0, exclusive=True)
            self.serial.dtr = False
            self.serial.rts = False
            self.serial.port = self.port
            self.serial.open()
            self._write(b"\x03\x03")
            time.sleep(0.15)
            self.serial.reset_input_buffer()
            self._write(b"\x01")
            prompt = self._read_until(b">", 3.0)
            if b"raw REPL" not in prompt:
                raise ConnectionFailure("ESP32 未进入 MicroPython 控制模式，请检查固件。")
            program = b"from enzyflow.motor_web import run\nrun()\n"
            self._write(program)
            self._write(b"\x04")
            if self._read_until(b"OK", 3.0)[-2:] != b"OK":
                raise ConnectionFailure("ESP32 未接受网页控制程序。")
            ready = self._read_record(5.0, startup=True)
            if ready.get("event") != "ready" or ready.get("protocol") != 1:
                raise ConnectionFailure("网页控制固件版本不匹配，请重新部署。")
            session = ready.get("session")
            if not isinstance(session, str) or not session:
                raise ConnectionFailure("ESP32 未提供有效的新会话。")
            self.session = session
            self.supports_emergency = "emergency_stop" in ready.get("state", {}).get("capabilities", [])
            self.seq = 0
            self.device = "ESP32 · " + self.port
            return ready.get("state", {})
        except BaseException:
            if not self.emergency_requested.is_set():
                self.close()
            raise

    def emergency_stop(self):
        """Only out-of-band byte; never send another JSON or motor command."""
        self.emergency_requested.set()
        with self._write_lock:
            if self.serial is None or self.serial.write(b"\x03") != 1:
                raise ConnectionFailure("急停未送达，请切断电机电源。")
            self.serial.flush()

    def finish_emergency(self):
        end = time.monotonic() + 5.0
        while self.emergency_result is None and time.monotonic() < end:
            self._read_record(max(0.1, end-time.monotonic()), startup=True)
        if self.emergency_result is None:
            raise ConnectionFailure("未收到三轴急停确认，请切断电机电源。")
        return self.emergency_result

    def _write(self, data):
        with self._write_lock:
            if self.emergency_requested.is_set():
                raise ConnectionFailure("急停已触发，禁止继续发送控制指令。")
            if self.serial is None or self.serial.write(data) != len(data):
                raise ConnectionFailure("串口写入不完整，已停止后续操作；不会重发运动指令。")
            self.serial.flush()

    def _read_until(self, marker, timeout):
        end = time.monotonic() + timeout
        output = bytearray()
        while time.monotonic() < end:
            data = self.serial.read(1)
            if data:
                output.extend(data)
                if output.endswith(marker):
                    return bytes(output)
                if len(output) > 8192:
                    raise ConnectionFailure("ESP32 返回了过多启动数据。")
        raise ConnectionFailure("ESP32 连接超时，请检查设备和 MicroPython 固件。")

    def _read_record(self, timeout, startup=False):
        end = time.monotonic() + timeout
        noise = bytearray()
        while time.monotonic() < end:
            data = b"" if b"\n" in self._buffer else self.serial.read(self.serial.in_waiting or 1)
            if not data and b"\n" not in self._buffer:
                continue
            self._buffer.extend(data)
            if len(self._buffer) > 16384:
                raise ConnectionFailure("ESP32 响应超过允许长度，连接已锁定。")
            while b"\n" in self._buffer:
                raw, _, remaining = self._buffer.partition(b"\n")
                self._buffer = bytearray(remaining)
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    value = json.loads(raw.decode("utf-8"))
                except (UnicodeDecodeError, ValueError):
                    if not startup:
                        raise ConnectionFailure("ESP32 响应格式错误，已停止后续操作。")
                    noise.extend(raw + b"\n")
                    if len(noise) > 2048:
                        break
                    continue
                if not isinstance(value, dict):
                    raise ConnectionFailure("ESP32 响应格式错误。")
                if value.get("event") == "emergency_stopped" and value.get("session") == self.session:
                    self.emergency_result = value
                return value
        if startup and (b"ImportError" in noise or b"motor_web" in noise):
            raise ConnectionFailure("ESP32 尚未安装网页控制固件，请先执行项目部署步骤。")
        raise ConnectionFailure("ESP32 响应超时；运动结果可能不明，已停止且不会自动重试。")

    def request(self, op, args=None):
        if not self.session:
            raise ConnectionFailure("设备未连接。")
        self.seq += 1
        request_id = uuid.uuid4().hex
        request = {"id": request_id, "seq": self.seq, "session": self.session,
                   "op": op, "args": args or {}}
        try:
            self._write(json.dumps(request, separators=(",", ":")).encode() + b"\n")
            response = self._read_record(self.timeout)
            if (response.get("id") != request_id or response.get("session") != self.session
                    or response.get("seq") != self.seq):
                raise ConnectionFailure("响应不属于当前命令或设备已复位，已锁定连接。")
            if response.get("ok") is not True:
                error = response.get("error", {})
                message = error.get("message", "设备拒绝了操作。") if isinstance(error, dict) else str(error)
                raise DeviceError(message)
            result = response.get("result")
            if not isinstance(result, dict):
                raise ConnectionFailure("设备未返回有效状态。")
            return result
        except DeviceError:
            raise
        except BaseException:
            if not self.emergency_requested.is_set():
                self.close()
            raise

    def close(self, graceful=False):
        orderly = False
        if graceful and self.serial is not None and self.session and not self.emergency_requested.is_set():
            try:
                orderly = self.request('shutdown', {}).get('shutdown_ready') is True
            except Exception:
                pass
        # Wait for interrupt cleanup before releasing the serial device. This
        # also prevents a subsequent connection from racing board-side braking.
        if not orderly and self.serial is not None and self.session and self.supports_emergency and not self.emergency_requested.is_set():
            try:
                self.emergency_stop()
                self.finish_emergency()
            except Exception:
                pass
        connection, self.serial = self.serial, None
        self.session = None
        self._buffer.clear()
        if connection is not None:
            try:
                if not orderly and not self.emergency_requested.is_set():
                    connection.write(b"\x03\x03")
                    connection.flush()
                    time.sleep(0.1)
                    connection.write(b"\x02")
                    connection.flush()
            except Exception:
                pass
            finally:
                connection.close()
        if self.lock_file is not None:
            self.lock_file.close()
            self.lock_file = None
