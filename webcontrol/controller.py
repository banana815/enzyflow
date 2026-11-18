"""One worker owns transport; finite continuous moves are supervised on ESP32."""
from __future__ import annotations

from collections import OrderedDict, deque
import copy
from datetime import datetime, timezone
import json
import math
from pathlib import Path
import threading
import time
import uuid

from .transport import ConnectionFailure, DeviceError, SerialTransport


from .errors import APIError

def finite(value, name):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise APIError(name + "必须是有限数值。")
    return float(value)


def axis_id(value):
    if type(value) is not int or value not in (1, 2, 3):
        raise APIError("机号必须是 1、2 或 3。")
    return value


def speed(value):
    if type(value) is not int or not 3 <= value <= 30:
        raise APIError("速度必须是 3–30 rpm 的整数。")
    return value


def blank_axes(long_id=1):
    short = iter(("短轴 A", "短轴 B"))
    return [{"id": i, "name": "长轴" if i == long_id else next(short),
             "min_mm": 30.0, "max_mm": 310.0 if i == long_id else 210.0,
             "physical_min_mm": 20.0, "physical_max_mm": 320.0 if i == long_id else 220.0,
             "position_mm": None, "anchored": False, "lead_mm": None,
             "forward_sign": None, "max_jog_mm": 1.0} for i in (1, 2, 3)]


class Controller:
    LEASE_SECONDS = 5.0

    def __init__(self, transport_factory=None, *, demo=False, auto_connect=True,
                 clock=time.monotonic, log_path=None, autostart=True):
        self.transport_factory = transport_factory or SerialTransport
        self.clock = clock
        self.demo = demo
        self.auto_connect = auto_connect
        self.mutex = threading.RLock()
        self.wakeup = threading.Event()
        self.shutdown = threading.Event()
        self.worker = None
        self.transport = None
        self.pending = None
        self.cancel_requested = False
        self.normal_stop_requested = False
        self.disconnect_requested = False
        self.needs_lock = False
        self.next_connect = 0.0
        self.next_status = 0.0
        self.lease_until = 0.0
        self.responses = OrderedDict()
        # Keep consumed actuator/configuration IDs beyond the response cache.
        # If the bounded response cache evicts a result, its command may never
        # become executable again in this server process.
        self.consumed_ids = set()
        self.events = deque(maxlen=200)
        self.log_path = Path(log_path) if log_path else None
        self.position_path = self.log_path.with_name('positions.json') if self.log_path and not demo else None
        self.saved_positions = None
        if self.position_path and self.position_path.exists():
            try:
                from .position_store import read
                self.saved_positions = read(self.position_path)
            except (ValueError, KeyError, TypeError, OSError):
                pass
        self.emergency_pending = False
        self.latch_path = self.log_path.with_name("emergency-latch.json") if self.log_path and not demo else None
        latched = bool(self.latch_path and self.latch_path.exists())
        if latched:
            self.auto_connect = False
        self.restore_on_startup = not latched and bool(self.saved_positions and self.saved_positions.get('reliable'))
        self.state = {"estop_latched": latched, "estop_confirmed": False if latched else None, "connected": False, "connecting": False, "demo": demo,
                      "port": None, "device": None, "session": None,
                      "axes": blank_axes(), "counts": {}, "configured": False,
                      "long_id": 1, "margin_mm": 10.0, "rpm_min": 3, "rpm_max": 30,
                      "job": None, "owner": None, "operation": None,
                      "last_error": None, "auto_connect": self.auto_connect}
        self._event("info", "演示模式：所有动作均为模拟。" if demo else "控制台已启动，等待连接 ESP32。")
        if autostart:
            self.worker = threading.Thread(target=self._run, name="enzyflow-device", daemon=True)
            self.worker.start()

    def _event(self, level, message):
        with self.mutex:
            if self.events and self.events[-1]["message"] == message:
                return
            entry = {"time": datetime.now(timezone.utc).isoformat(), "level": level, "message": message}
            self.events.append(entry)
            if self.log_path:
                try:
                    self.log_path.parent.mkdir(parents=True, exist_ok=True)
                    with self.log_path.open("a", encoding="utf-8") as handle:
                        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
                except OSError:
                    pass

    def _expire_lease(self):
        if self.state["owner"] and self.clock() >= self.lease_until:
            self.state["owner"] = None
            self.normal_stop_requested = False
            self.cancel_requested = True
            self.needs_lock = self.state["connected"]
            self._event("warning", "控制页面心跳中断，取消后续运动并锁定；重新确认位置后才能操作。")

    def snapshot(self):
        with self.mutex:
            self._expire_lease()
            result = copy.deepcopy(self.state)
            result["saved_positions"] = copy.deepcopy(self.saved_positions)
            result["supports_batch_anchor"] = True
            result["position_file"] = str(self.position_path) if self.position_path else None
            result["events"] = list(self.events)
            result["busy"] = self._busy()
            result["pending"] = self.pending[0] if self.pending else None
            return result

    def _busy(self):
        job = self.state["job"]
        return bool(self.pending or self.state["connecting"] or self.state["operation"]
                    or (job and job["state"] in ("running", "stopping")))

    def _owner(self, client):
        self._expire_lease()
        if not client or self.state["owner"] != client:
            raise APIError("当前页面未取得控制权，请点击“取得控制权”。", 403)

    def _idle_connected(self):
        if not self.state["connected"]:
            raise APIError("ESP32 尚未连接。", 409)
        if self._busy() or self.needs_lock or self.disconnect_requested:
            raise APIError("设备正在处理上一项操作，请等待完成。", 409)

    def _axis(self, ident):
        ident = axis_id(ident)
        return next(a for a in self.state["axes"] if a["id"] == ident)

    def submit(self, op, body):
        if not isinstance(body, dict):
            raise APIError("请求必须是 JSON 对象。")
        client = body.get("client_id")
        if not isinstance(client, str) or not 1 <= len(client) <= 100:
            raise APIError("缺少有效的页面标识。")
        request_id = body.get("request_id")
        if request_id is not None and (not isinstance(request_id, str) or not 1 <= len(request_id) <= 100):
            raise APIError("命令标识无效。")
        replay_sensitive = op in ("move", "jog", "move_group", "recover", "pump_start", "reset_positions", "configure", "anchor", "restore_positions", "clear_estop", "restore_drives")
        if replay_sensitive and not request_id:
            raise APIError("此操作必须提供唯一命令标识，禁止无标识运动或重新标定。")
        fingerprint = json.dumps([op, {k: v for k, v in body.items() if k != "request_id"}],
                                 sort_keys=True, separators=(",", ":"), allow_nan=False)
        with self.mutex:
            self._expire_lease()
            key = (client, request_id) if request_id else None
            if key and key in self.responses:
                old_fingerprint, reply = self.responses[key]
                if old_fingerprint != fingerprint:
                    raise APIError("相同命令标识不能用于不同动作。", 409)
                return dict(reply, state=self.snapshot(), duplicate=True)
            if key and key in self.consumed_ids:
                raise APIError("此命令已处理且记录已归档，禁止再次执行；请创建新的操作。", 409)
            if replay_sensitive and len(self.consumed_ids) >= 100000:
                raise APIError("本次控制会话命令记录已满，请停止并重新启动控制台。", 503)
            reply = self._submit(op, body, client)
            if replay_sensitive:
                self.consumed_ids.add(key)
            if key and op not in ("heartbeat",):
                self.responses[key] = (fingerprint, reply)
                while len(self.responses) > 128:
                    self.responses.popitem(last=False)
            self.wakeup.set()
            return dict(reply, state=self.snapshot())

    def _anchor_settings(self, body):
        axis = self._axis(body.get("axis"))
        mm = finite(body.get("mm"), "当前位置")
        if not axis["physical_min_mm"] <= mm <= axis["physical_max_mm"]:
            raise APIError("尺量位置超出该轴机械有效范围。")
        saved = self.saved_positions or {}
        previous = {}
        if saved.get('binding') == self._position_binding() and saved.get('long_id') == self.state['long_id']:
            previous = saved.get('geometry', {}).get(str(axis['id'])) or next(
                (a for a in saved.get('axes', []) if a['id'] == axis['id']), {})
        sign = body.get('sign', axis['forward_sign'] or previous.get('forward_sign', -1))
        lead = body.get('lead', axis['lead_mm'] or previous.get('lead_mm', 4))
        if type(sign) is not int or sign not in (-1, 1) or lead not in (2, 4) or isinstance(lead, bool):
            raise APIError("请确认前进方向和每圈 2 或 4 mm 的导程。")
        return {"axis": axis["id"], "mm": mm, "sign": sign, "lead": lead, "confirmed": True}

    def _submit(self, op, body, client):
        if op == "claim":
            if self.state["owner"] not in (None, client):
                raise APIError("另一个页面正在控制设备；可查看状态，但不能同时下发运动。", 409)
            self.state["owner"] = client
            self.lease_until = self.clock() + self.LEASE_SECONDS
            return {"ok": True, "accepted": True}
        if op == "heartbeat":
            self._owner(client)
            self.lease_until = self.clock() + self.LEASE_SECONDS
            return {"ok": True}
        if op == "emergency_stop":
            if self.state["estop_latched"]:
                return {"ok": True, "accepted": True}
            self.normal_stop_requested = False
            self.state.update(estop_latched=True, estop_confirmed=None, auto_connect=False)
            self.auto_connect = False
            self.cancel_requested = True
            self.pending = None
            self._invalidate_positions()
            if self.state["job"] and self.state["job"]["state"] == "running":
                self.state["job"]["state"] = "stopping"
            self.emergency_pending = self.transport is not None
            # Brake first; persistence failure must never suppress the signal.
            if self.transport:
                try:
                    self.transport.emergency_stop()
                except Exception as exc:
                    self.state["last_error"] = str(exc)
            else:
                self.state["last_error"] = "设备未连接，无法确认急停；请切断电机电源。"
                self.state["estop_confirmed"] = False
            try:
                self._mark_positions_pending('急停时无停稳读数；此前记录仅供核对', force=True)
            except OSError:
                self.saved_positions = None
            if self.latch_path:
                try:
                    self.latch_path.parent.mkdir(parents=True, exist_ok=True)
                    self.latch_path.write_text('{"latched":true}\n')
                except OSError:
                    self._event("error", "急停锁存无法写入磁盘；重启后仍须检查设备。")
            self._event("warning", "急停已触发，等待三轴刹车反馈；所有运动被锁定。")
            return {"ok": True, "accepted": True}
        if op == "clear_estop":
            self._owner(client)
            if self.emergency_pending or self.state["connected"] or self._busy():
                raise APIError("等待急停处理和串口断开后再恢复。", 409)
            if body.get("confirmed") is not True:
                raise APIError("请先确认设备停止、故障已排除，允许恢复驱动力。")
            if not self.state['estop_latched']:
                raise APIError('当前没有急停锁定；连接后可使用恢复驱动。')
            self.cancel_requested = self.needs_lock = False
            self.pending = ('restore_drives', {'confirmed':True}, client)
            return {"ok": True, "accepted": True}
        if self.state["estop_latched"] and op not in ("stop", "disconnect"):
            raise APIError("急停锁定中；先确认停机并解除，再重新连接标定。", 409)
        if op == "stop":
            self.normal_stop_requested = not self.state['estop_latched']
            # A local viewer may always request cancellation with the CSRF token.
            self.cancel_requested = True
            self.needs_lock = self.state["connected"]
            self.pending = None
            if self.state["job"] and self.state["job"]["state"] == "running":
                self.state["job"]["state"] = "stopping"
            self._event("warning", "已请求停止连续运动，等待刹车及静止反馈。")
            return {"ok": True, "accepted": True}
        self._owner(client)
        if op == "connect":
            self.auto_connect = self.state["auto_connect"] = True
            self.next_connect = 0.0
            self.state["last_error"] = None
            return {"ok": True, "accepted": True}
        if op == "disconnect":
            self.auto_connect = self.state["auto_connect"] = False
            self.cancel_requested = True
            self.disconnect_requested = True
            self.pending = None
            return {"ok": True, "accepted": True}
        if op == "lock":
            self.cancel_requested = True
            self.needs_lock = self.state["connected"]
            self.pending = None
            return {"ok": True, "accepted": True}
        self._idle_connected()
        if op == 'restore_drives':
            if body.get('confirmed') is not True:
                raise APIError('请确认电机停止、故障排除，并允许恢复驱动力。')
            self.pending = (op, {'confirmed':True}, client)
        elif op == "restore_positions":
            saved = self.saved_positions
            if not saved or not saved.get('reliable'):
                raise APIError('没有可恢复的已确认位置；请重新实测建立基准。')
            if saved.get('binding') != self._position_binding():
                raise APIError('位置记录与当前ESP32身份不匹配。')
            self.pending = (op, copy.deepcopy(saved), client)
        elif op == "configure":
            if body.get("ids") != [1, 2, 3] or body.get("confirmed") is not True:
                raise APIError("请确认机号 1、2、3 与实物的对应关系。")
            long_id = axis_id(body.get("long_id"))
            self.pending = (op, {"ids": [1, 2, 3], "long_id": long_id, "confirmed": True}, client)
        elif op == "anchor":
            if not self.state["configured"]:
                raise APIError("请先确认三轴机号与长轴。", 409)
            if body.get("confirmed") is not True:
                raise APIError("必须确认这是当前实测位置。")
            if "positions" in body:
                positions = body["positions"]
                if not isinstance(positions, list) or len(positions) != 3 or not all(isinstance(item, dict) for item in positions):
                    raise APIError("请提供三个轴的位置。")
                settings = [self._anchor_settings(item) for item in positions]
                if {item["axis"] for item in settings} != {1, 2, 3}:
                    raise APIError("每个轴须填写一次。")
                self.pending = (op, {"positions": settings}, client)
            else:
                self.pending = (op, self._anchor_settings(body), client)
        elif op == "pump_start":
            if "pump_rates" not in self.state.get("capabilities", []):
                raise APIError("请更新板端固件以支持两轴独立注射速率。")
            if body.get("confirmed") is not True:
                raise APIError("请确认注射器内径、推注方向及剩余行程。")
            items = body.get("items")
            if not isinstance(items, list) or len(items) != 2:
                raise APIError("请提供两个短轴的注射参数。")
            shorts = {a["id"] for a in self.state["axes"] if a["id"] != self.state["long_id"]}
            selected = set()
            clean = []
            safe_distances = {}
            for item in items:
                if not isinstance(item, dict):
                    raise APIError("注射参数无效。")
                axis = self._axis(item.get("axis"))
                direction = item.get("direction")
                remaining = finite(item.get("remaining_mm"), "注射器剩余行程")
                if axis["id"] not in shorts or axis["id"] in selected:
                    raise APIError("只能选择两个不同的短轴。")
                if type(direction) is not int or direction not in (-1, 1) or not 1.5 <= remaining <= 200:
                    raise APIError("请确认推注方向并填写1.5–200mm的剩余行程。")
                if not axis["anchored"] or axis["position_mm"] is None or not axis["min_mm"] <= axis["position_mm"] <= axis["max_mm"]:
                    raise APIError("两个短轴须先标定并进入安全区。")
                planned = "segments" in item or "target_volume_ml" in item
                flow = 1.0 if planned else finite(item.get("flow_ml_min", 1.0), "注射速率")
                area = math.pi*(19.22/2)**2
                rpm = flow*1000/area/axis["lead_mm"]
                if not 0.1 <= rpm <= 2:
                    low, high = 0.1*axis["lead_mm"]*area/1000, 2*axis["lead_mm"]*area/1000
                    raise APIError(f"ID{axis['id']}速率须在{low:.4f}–{high:.4f}mL/min内（电机0.1–2rpm）。")
                room = axis["max_mm"]-axis["position_mm"] if direction > 0 else axis["position_mm"]-axis["min_mm"]
                safe_distances[axis['id']] = min(remaining-1, room-0.5)
                if safe_distances[axis['id']] < 0.5:
                    raise APIError("剩余安全行程不足0.5mm。")
                entry = {"axis":axis["id"], "direction":direction, "remaining_mm":remaining, "flow_ml_min":flow}
                if planned:
                    if "pump_segments" not in self.state["capabilities"]:
                        raise APIError("请更新板端固件以支持分段注射。")
                    segments = item.get("segments")
                    if not isinstance(segments, list) or not 1 <= len(segments) <= 12:
                        raise APIError("每轴须设置1–12段。")
                    entry["segments"] = []
                    for segment in segments:
                        if not isinstance(segment, dict):
                            raise APIError("注射分段格式无效。")
                        distance = finite(segment.get("distance_mm"), "分段距离")
                        rate = finite(segment.get("flow_ml_min"), "分段速率")
                        if distance < 0.5 or not 0.1 <= rate*1000/area/axis["lead_mm"] <= 2:
                            raise APIError("每段至少0.5mm，且注射速率须对应0.1–2rpm。")
                        entry["segments"].append({"distance_mm":distance,"flow_ml_min":rate})
                    total = sum(s["distance_mm"] for s in entry["segments"])
                    if total > safe_distances[axis['id']]:
                        raise APIError("分段总距离不得超过安全行程。")
                    if "target_volume_ml" in item:
                        volume = finite(item["target_volume_ml"], "注射体积")
                        if abs(total - volume*1000/area) > 0.000001:
                            raise APIError("分段距离之和须等于体积换算距离。")
                    entry["target_volume_ml"] = total*area/1000
                    entry["flow_ml_min"] = entry["segments"][0]["flow_ml_min"]
                selected.add(axis["id"])
                clean.append(entry)
            if any("segments" in entry for entry in clean):
                for entry in clean:
                    if "segments" not in entry:
                        distance = safe_distances[entry['axis']]
                        entry['segments'] = [{'distance_mm': distance, 'flow_ml_min': entry['flow_ml_min']}]
                        entry['target_volume_ml'] = distance*area/1000
            self.cancel_requested = False
            self.pending = (op, {"items": clean, "confirmed": True}, client)
        elif op in ("move_group", "reset_positions"):
            if "jog_group" not in self.state.get("capabilities", []):
                raise APIError("请更新板端固件以支持并行运动。")
            moves = body.get("moves")
            resetting = op == "reset_positions"
            if resetting:
                if "continuous_reset" not in self.state.get("capabilities", []):
                    raise APIError("请更新板端固件以支持连续复位。")
                if body.get("confirmed") is not True or not isinstance(moves, list) or len(moves) != 3:
                    raise APIError("复位需填写三轴目标，并确认回程路径可移动。")
                reset_rpm = speed(body.get("rpm", 30))
            if not isinstance(moves, list) or not 2 <= len(moves) <= 3:
                raise APIError("请选择2–3个轴并分别填写目标与速度。")
            members = []
            for move in moves:
                if not isinstance(move, dict):
                    raise APIError("各轴参数必须是对象。")
                axis = self._axis(move.get("axis"))
                if any(m["axis"] == axis["id"] for m in members):
                    raise APIError("并行运动不能重复选择同一轴。")
                start = axis["position_mm"]
                if not axis["anchored"] or start is None or not axis["min_mm"] <= start <= axis["max_mm"]:
                    raise APIError("所有选择的轴须先标定并进入安全区。")
                target = finite(move.get("target_mm"), "目标位置")
                rpm = reset_rpm if resetting else speed(move.get("rpm", 12))
                if not axis["min_mm"] <= target <= axis["max_mm"]:
                    raise APIError("并行目标须在各轴安全区内。")
                approach = min(axis["max_mm"] - 0.1, max(axis["min_mm"] + 0.1, target))
                if not resetting and (abs(target - start) < 0.099999 or abs(approach - start) < 0.099999):
                    raise APIError("每个选择的轴目标距离须至少0.1mm。")
                members.append({"axis": axis["id"], "target_mm": target, "rpm": rpm})
            self.cancel_requested = False
            if resetting:
                self.pending = (op, {"moves": [{"axis":m["axis"],"target_mm":m["target_mm"]} for m in members],
                                     "rpm":reset_rpm, "confirmed":True}, client)
            else:
                self._queue_continuous(members, client)
        elif op in ("move", "jog", "recover"):
            axis = self._axis(body.get("axis"))
            if not axis["anchored"] or axis["position_mm"] is None:
                raise APIError("此轴当前位置尚未确认，请先尺量标定。", 409)
            rpm = speed(body.get("rpm", 6 if op == "recover" else 12))
            if op == "recover":
                if "recover" not in self.state.get("capabilities", []):
                    raise APIError("请更新板端固件以支持向内恢复。")
                if body.get("confirmed") is not True or rpm > 6:
                    raise APIError("向内恢复需确认，速度仅允许3–6rpm。")
            start = axis["position_mm"]
            if op != "recover" and not axis["min_mm"] <= start <= axis["max_mm"]:
                raise APIError("当前位置未进入保留 1 cm 余量的安全区，请先处理位置基准或受控恢复。", 409)
            if op in ("jog", "recover"):
                delta = finite(body.get("delta_mm"), "步长")
                maximum = axis.get("max_jog_mm") or min(1.0, 12800 / (51200 / axis["lead_mm"]))
                if not 0.1 <= abs(delta) <= maximum:
                    raise APIError("此轴单步距离必须为 0.1–%g mm。" % maximum)
                target = start + delta
            else:
                target = finite(body.get("target_mm"), "目标位置")
            if op == "recover":
                if abs(delta) > 0.5 or not axis["physical_min_mm"] <= target <= axis["physical_max_mm"]:
                    raise APIError("恢复每次最多0.5mm，且不能越过机械边界。")
                if not ((start < axis["min_mm"] and delta > 0 and target <= axis["min_mm"] + 0.1)
                        or (start > axis["max_mm"] and delta < 0 and target >= axis["max_mm"] - 0.1)):
                    raise APIError("仅允许安全区外的轴向安全区内恢复。")
            elif not axis["min_mm"] <= target <= axis["max_mm"]:
                raise APIError("目标必须在 %.1f–%.1f mm，保留两端各 1 cm 余量。" %
                               (axis["min_mm"], axis["max_mm"]))
            self._queue_continuous([{"axis": axis["id"], "target_mm": target, "rpm": rpm}], client, recovery=op=="recover")
        else:
            raise APIError("未知操作。", 404)
        return {"ok": True, "accepted": True}

    def _queue_continuous(self, members, client, recovery=False):
        if "continuous_move" not in self.state.get("capabilities", []):
            raise APIError("请更新板端固件以启用整段连续运动。")
        self.cancel_requested = False
        self.pending = ("move_continuous", {"moves": [{k: m[k] for k in ("axis", "target_mm", "rpm")} for m in members],
                                            "confirmed": True, "recovery": recovery}, client)

    def _position_binding(self):
        return getattr(self.transport, 'identity', None) or {"port": self.state.get('port')}

    def _mark_positions_pending(self, reason, force=False):
        if self.saved_positions and (self.saved_positions.get('reliable') or force):
            data = dict(self.saved_positions, reliable=False, reason=reason)
            if self.position_path:
                from .position_store import write
                write(self.position_path, data)
            self.saved_positions = data

    def _save_positions(self, axes=None, reference_reason=None):
        from .position_store import checkpoint, validate, write
        with self.mutex:
            if self.state['estop_latched'] and not reference_reason:
                return
            axes = axes if axes is not None else self.state['axes']
            if not any(a.get('anchored') for a in axes):
                return
            data = checkpoint(axes, self.state['long_id'], self._position_binding())
            if reference_reason:
                data.update(reliable=False, reason=reference_reason)
            previous = self.saved_positions or {}
            geometry = {}
            if all(previous.get(k) == data[k] for k in ('binding', 'long_id')):
                geometry.update(previous.get('geometry', {}))
                for axis in previous['axes']:
                    geometry[str(axis['id'])] = {k: axis[k] for k in ('forward_sign', 'lead_mm')}
            for axis in data['axes']:
                geometry[str(axis['id'])] = {k: axis[k] for k in ('forward_sign', 'lead_mm')}
            data['geometry'] = geometry
            validate(data)
            if not reference_reason and all(previous.get(k)==data.get(k) for k in ('axes','long_id','binding','reliable','geometry')):
                return
            if self.position_path:
                write(self.position_path, data)
            self.saved_positions = data

    def _invalidate_positions(self):
        for axis in self.state["axes"]:
            axis.update(position_mm=None, anchored=False, lead_mm=None, forward_sign=None)

    def _apply_device_state(self, value):
        axes = value.get("axes")
        if not isinstance(axes, list):
            raise ConnectionFailure("设备状态缺少轴列表。")
        with self.mutex:
            self.state["capabilities"] = value.get("capabilities", [])
            if axes:
                if sorted(a.get("id") for a in axes) != [1, 2, 3]:
                    raise ConnectionFailure("设备返回的三轴机号不完整。")
                # Never accept non-finite coordinates or unlocked-but-unknown axes.
                for axis in axes:
                    position = axis.get("position_mm")
                    if position is not None:
                        finite(position, "设备反馈位置")
                    if axis.get("anchored") and position is None:
                        raise ConnectionFailure("设备返回了不完整的位置基准。")
                self.state["axes"] = copy.deepcopy(axes)
            else:
                self.state["axes"] = blank_axes(value.get("long_id") or 1)
            for key in ("configured", "long_id", "margin_mm", "rpm_min", "rpm_max", "last_feedback_ms", "capabilities", "pump", "reset"):
                if key in value:
                    self.state[key] = value[key]
            self.state["counts"] = {str(k): v for k, v in value.get("counts", {}).items()}

    def _failed(self, exc):
        with self.mutex:
            if self.emergency_pending:
                # Preserve transport and buffered out-of-band stop confirmation.
                self.state.update(operation=None, connecting=False)
                self.pending = None
                return
            try:
                self._mark_positions_pending('通信或运动异常，旧位置仅供核对')
            except OSError:
                self.saved_positions = None
            self.normal_stop_requested = False
            message = str(exc) or type(exc).__name__
            self.state["last_error"] = message
            self.state["operation"] = None
            self.pending = None
            self.cancel_requested = True
            self._invalidate_positions()
            job = self.state["job"]
            if job and job["state"] in ("running", "stopping"):
                job.update(state="failed", error=message)
            self._event("error", message)
            if isinstance(exc, DeviceError):
                self.needs_lock = self.state["connected"]
            else:
                self._drop_connection()

    def _drop_connection(self, graceful=False):
        if self.transport:
            if graceful and 'graceful_shutdown' in self.state.get('capabilities', []):
                self.transport.close(graceful=True)
            else:
                self.transport.close()
        self.transport = None
        self.state.update(connected=False, connecting=False, session=None, configured=False, operation=None)
        self._invalidate_positions()
        self.needs_lock = False
        self.next_connect = self.clock() + 2.0

    def _restore_checkpoint(self, saved):
        if self.emergency_pending or self.cancel_requested:
            raise DeviceError('位置恢复已取消。')
        self._apply_device_state(self.transport.request('configure', {
            'ids': [1, 2, 3], 'long_id': saved['long_id'], 'confirmed': True}))
        for axis in saved['axes']:
            if self.emergency_pending or self.cancel_requested:
                raise DeviceError('位置恢复已取消。')
            self._apply_device_state(self.transport.request('anchor', {
                'axis': axis['id'], 'mm': axis['position_mm'], 'sign': axis['forward_sign'],
                'lead': axis['lead_mm'], 'confirmed': True}))

    def _connect(self):
        with self.mutex:
            self.state["connecting"] = True
            self.state["operation"] = "connect"
        transport = self.transport_factory()
        with self.mutex:
            self.transport = transport
        try:
            value = transport.connect()
            self._apply_device_state(value)
            with self.mutex:
                self.transport = transport
                self.state.update(connected=True, port=transport.port,
                                  session=transport.session, device=transport.device, last_error=None)
                self._invalidate_positions()
                if self.emergency_pending:
                    return
                self.cancel_requested = False
                self.next_status = self.clock() + 1.0
                saved = self.saved_positions
                restore = (self.restore_on_startup and not self.state['estop_latched']
                           and saved and saved.get('reliable') and saved.get('binding') == self._position_binding())
                self.restore_on_startup = False
            if restore:
                try:
                    self._restore_checkpoint(saved)
                except Exception as exc:
                    with self.mutex:
                        self.state['connecting'] = False
                    self._failed(exc)
                    return
            with self.mutex:
                if self.emergency_pending:
                    self._invalidate_positions()
                    return
                self.state.update(connecting=False, operation=None)
                self._event('info', '已采用上次保存的位置；可在标定面板重新设置。' if restore else
                            'ESP32 已连接，请确认机号和当前尺量位置。')
        except Exception as exc:
            if self.emergency_pending:
                self._failed(exc)
                return
            transport.close()
            with self.mutex:
                self.transport = None
                self.state.update(connected=False, connecting=False, operation=None, last_error=str(exc))
                self.next_connect = self.clock() + 2.0
                self._event("warning", str(exc))

    def _lock_device(self, preserve=False):
        try:
            preserve = preserve and "stop_preserve" in self.state.get("capabilities", [])
            if self.transport:
                value = self.transport.request("stop_motion" if preserve else "lock", {})
                self._apply_device_state(value)
                if value.get('checkpoint_error'):
                    self._mark_positions_pending(value['checkpoint_error'])
                    self._event('warning', '未保存停止位置：'+value['checkpoint_error'])
                    raise DeviceError('停止位置未确认：'+value['checkpoint_error'])
                if value.get('stopped_axes') and not self.state.get('last_error'):
                    self._save_positions(value['stopped_axes'])
            with self.mutex:
                if not preserve:
                    self._invalidate_positions()
                if (self.state.get("pump") or {}).get("stop_fault") or (self.state.get("reset") or {}).get("stop_fault"):
                    raise DeviceError("停止未确认，请切断电机电源并检查。")
                job = self.state["job"]
                if job and job["state"] in ("running", "stopping"):
                    if job["kind"] == "reset_continuous":
                        job["members"] = copy.deepcopy((self.state.get("reset") or {}).get("members", job["members"]))
                    if job["kind"] == "pump":
                        job["members"] = copy.deepcopy((self.state.get("pump") or {}).get("members", job["members"]))
                    job["state"] = "cancelled"
                self.pending = None
                self.cancel_requested = False
                self.normal_stop_requested = False
                self.needs_lock = False
                self.state["operation"] = None
                if preserve:
                    self._event('info', '已确认停止并保存位置，可直接提交新的运动；不会续跑旧任务。')
        except Exception as exc:
            self._failed(exc)

    def _finish_emergency(self):
        confirmed = False
        stopped_axes = []
        try:
            result = self.transport.finish_emergency()
            stopped_axes = result.get('stopped_axes') or []
            confirmed = result.get("confirmed") is True
            details = result.get("details", [])
            if details:
                self._event("warning", "急停诊断：" + json.dumps(details, ensure_ascii=False))
            message = "三轴急停已确认并失能；排除原因后可恢复驱动，无需例行断电重启，位置需重新核对。" if confirmed else "三轴急停未全部确认，请切断电机电源。"
        except Exception:
            message = "急停反馈未确认，请切断电机电源。"
        with self.mutex:
            if stopped_axes:
                try:
                    self._save_positions(stopped_axes, '急停后编码器静止读数（仅供重新尺量校准）')
                except (OSError, ValueError, TypeError, KeyError, AttributeError):
                    self._event('error', '无法保存急停位置参考。')
            self._drop_connection()
            self.emergency_pending = False
            self.cancel_requested = self.disconnect_requested = False
            self.state.update(estop_confirmed=confirmed, last_error=None if confirmed else message)
            if self.state["job"] and self.state["job"]["state"] in ("running", "stopping"):
                self.state["job"].update(state="cancelled" if confirmed else "failed", error=message)
            self._event("warning" if confirmed else "error", message)

    def _restore_drives(self):
        if not self.state['connected']:
            self._connect()
        if not self.state['connected'] or not self.transport:
            raise ConnectionFailure('恢复驱动时连接失败')
        if 'drive_restore' not in self.state.get('capabilities', []):
            raise DeviceError('板端固件不支持受控驱动恢复')
        with self.mutex:
            self._expire_lease()
            if not self.state['owner'] or self.cancel_requested or self.emergency_pending:
                raise DeviceError('恢复已取消或控制权已失效')
        self._mark_positions_pending('恢复驱动力后需核对当前位置')
        value = self.transport.request('restore_drives', {'confirmed':True})
        with self.mutex:
            if self.emergency_pending:
                return
            self._expire_lease()
            if not self.state['owner'] or self.cancel_requested:
                raise DeviceError('恢复期间停止请求或控制权失效，保持锁定')
            drivers = value.get('driver_states', [])
            if len(drivers)!=3 or {d.get('id') for d in drivers}!={1,2,3} or any(d.get('state') not in (0,1) or d.get('disabled')!=0 or d.get('stall')!=0 for d in drivers):
                raise DeviceError('驱动器恢复反馈不完整')
            self._apply_device_state(value)
            if self.latch_path:
                self.latch_path.unlink(missing_ok=True)
            self.state.update(estop_latched=False,estop_confirmed=None,last_error=None,operation=None,driver_states=drivers)
            self.cancel_requested = self.needs_lock = self.normal_stop_requested = False
            self._invalidate_positions()
            self._event('info','三轴驱动器已恢复并确认静止；未下发位移指令，请核对位置后提交新任务。')

    def _execute_pump(self):
        with self.mutex:
            self._expire_lease()
            if self.cancel_requested or self.state["owner"] != self.state["job"]["owner"]:
                self.needs_lock = True
                return
            if self.clock() < self.next_status:
                return
        self._apply_device_state(self.transport.request("pump_heartbeat", {}))
        with self.mutex:
            pump = self.state.get("pump") or {}
            job = self.state["job"]
            job["members"] = copy.deepcopy(pump.get("members", []))
            if not pump.get("active"):
                reason = pump.get("state") or "注射已停止"
                job.update(state="done" if "到达设定行程" in reason else "failed", error=reason)
                if job['state'] != 'done':
                    self._mark_positions_pending(reason)
                    self._invalidate_positions()
                else:
                    self._save_positions()
                self._event("info" if job["state"] == "done" else "warning", reason)
            self.next_status = self.clock() + 0.3

    def _execute_reset(self):
        with self.mutex:
            self._expire_lease()
            if self.cancel_requested or self.state["owner"] != self.state["job"]["owner"]:
                self.needs_lock = True
                return
            if self.clock() < self.next_status:
                return
        self._apply_device_state(self.transport.request("reset_heartbeat", {}))
        with self.mutex:
            reset = self.state.get("reset") or {}
            job = self.state["job"]
            job["members"] = copy.deepcopy(reset.get("members", []))
            job["progress"] = sum(m.get("progress",0) for m in job["members"])/max(1,len(job["members"]))
            if not reset.get("active"):
                done = reset.get("state") == "done"
                job.update(state="done" if done else "failed", error=None if done else reset.get("state"))
                if not done:
                    self._mark_positions_pending(reset.get('state') or '运动异常')
                    self._invalidate_positions()
                else:
                    self._save_positions()
                self._event("info" if done else "error", "连续运动到位，位置已保存。" if done else (reset.get("state") or "复位反馈异常"))
            self.next_status = self.clock()+0.3

    def _execute_job(self):
        if self.state['job']['kind'] == 'reset_continuous':
            return self._execute_reset()
        if self.state['job']['kind'] == 'pump':
            return self._execute_pump()
        raise DeviceError('不支持的连续运动任务')

    def step(self):
        """One worker iteration; exposed for deterministic, hardware-free tests."""
        with self.mutex:
            self._expire_lease()
            disconnect = self.disconnect_requested
            needs_lock = self.needs_lock or (self.cancel_requested and self.state["connected"])
            connected = self.state["connected"]
            pending = self.pending
            job = self.state["job"]
            can_connect = (not connected and self.auto_connect and self.state["owner"]
                           and self.clock() >= self.next_connect)
        try:
            if self.emergency_pending:
                self._finish_emergency()
            elif disconnect:
                if connected:
                    self._lock_device()
                with self.mutex:
                    self._drop_connection(graceful=not self.state.get("last_error"))
                    self.disconnect_requested = False
                    self.cancel_requested = False
                    self._event("info", "已断开 ESP32，位置基准已清除。")
            elif self.state["estop_latched"] and not (pending and pending[0]=='restore_drives'):
                return
            elif needs_lock:
                self._lock_device(preserve=self.normal_stop_requested)
            elif can_connect:
                self._connect()
            elif pending:
                op, args, owner = pending
                with self.mutex:
                    self.pending = None
                    if owner != self.state["owner"]:
                        return
                    self.state["operation"] = op
                if op == 'restore_drives':
                    try:
                        self._restore_drives()
                    except Exception:
                        self.state.update(estop_latched=True,estop_confirmed=False,auto_connect=False)
                        self.auto_connect = False
                        if self.latch_path:
                            self.latch_path.parent.mkdir(parents=True,exist_ok=True)
                            self.latch_path.write_text('{"latched":true}\n')
                        self._drop_connection()
                        raise
                    return
                if op == 'restore_positions':
                    self._restore_checkpoint(args)
                elif op == "anchor" and "positions" in args:
                    for settings in args["positions"]:
                        if self.emergency_pending or self.cancel_requested:
                            raise DeviceError("三轴标定已取消。")
                        self._apply_device_state(self.transport.request("anchor", settings))
                else:
                    if op in ('move_continuous','reset_positions','pump_start'):
                        self.state['job'] = {'id':uuid.uuid4().hex, 'kind':'pump' if op=='pump_start' else 'reset_continuous',
                            'state':'running','owner':owner,'members':[],'progress':0.0,'error':None,'reset':op=='reset_positions'}
                        self._mark_positions_pending('运动进行中；未确认停止前不能恢复旧位置')
                    self._apply_device_state(self.transport.request(op, args))
                with self.mutex:
                    self.state["operation"] = None
                    self.state["last_error"] = None
                    if op in ("reset_positions", "move_continuous"):
                        reset = self.state.get("reset") or {}
                        if not reset.get("active") and reset.get("state") != "done":
                            raise DeviceError(reset.get("state") or "连续复位启动未确认")
                        done = reset.get("state") == "done"
                        self.state["job"] = {"id":uuid.uuid4().hex, "kind":"reset_continuous", "reset":op=="reset_positions",
                                             "state":"done" if done else "running", "owner":owner,
                                             "members":copy.deepcopy(reset["members"]), "progress":1.0 if done else 0.0, "error":None}
                        self.next_status = 0.0
                        if done:
                            self._save_positions()
                        self._event("info", "已启动整段连续运动，途中更新编码器位置，到位自动保存。")
                    elif op == "pump_start":
                        pump = self.state.get("pump") or {}
                        if not pump.get("active"):
                            raise DeviceError(pump.get("state") or "注射启动未确认")
                        self.state["job"] = {"id": uuid.uuid4().hex, "kind": "pump", "state": "running",
                                             "owner": owner, "members": copy.deepcopy(pump["members"]), "error": None}
                        self.next_status = 0.0
                        self._event("info", "两支注射器开始恒速推进：" + "；".join(f"ID{m['axis']} {m['flow_ml_min']:g}mL/min" for m in pump["members"]))
                    elif op == "anchor" and "positions" in args:
                        self._save_positions()
                        self._event("info", "已建立并保存三轴位置基准。")
                    elif op == 'restore_positions':
                        self._save_positions()
                        self._event('info', '已恢复保存的位置、方向和导程；未发送运动指令。')
                    else:
                        if op == 'anchor':
                            self._save_positions()
                        elif op == 'configure':
                            self._mark_positions_pending('机号配置已更改，等待重新建立基准')
                        self._event("info", "机号与长短轴对应已确认。" if op == "configure" else
                                    "ID%d 已按当前尺量位置 %.1f mm 建立基准。" % (args["axis"], args["mm"]))
            elif job and job["state"] in ("running", "stopping"):
                self._execute_job()
            elif connected and self.clock() >= self.next_status:
                self._apply_device_state(self.transport.request("status", {}))
                self.next_status = self.clock() + 1.0
        except Exception as exc:
            self._failed(exc)

    def _run(self):
        while not self.shutdown.is_set():
            self.step()
            self.wakeup.wait(0.025)
            self.wakeup.clear()
        if self.transport:
            if self.emergency_pending:
                self._finish_emergency()
            else:
                self._lock_device()
                if 'graceful_shutdown' in self.state.get('capabilities', []) and not self.state.get('last_error'):
                    self.transport.close(graceful=True)
                else:
                    self.transport.close()

    def close(self):
        with self.mutex:
            self.cancel_requested = True
        self.shutdown.set()
        self.wakeup.set()
        if self.worker and self.worker is not threading.current_thread():
            self.worker.join(timeout=20)
        elif self.transport:
            if self.emergency_pending:
                self._finish_emergency()
            else:
                self._lock_device()
                if 'graceful_shutdown' in self.state.get('capabilities', []) and not self.state.get('last_error'):
                    self.transport.close(graceful=True)
                else:
                    self.transport.close()
