"""No-hardware transport used by UI preview and deterministic tests."""
import copy
import math
import time
import uuid

from .controller import blank_axes
from .transport import DeviceError


class DemoTransport:
    def __init__(self, delay=0.12, clock=time.monotonic):
        self.delay = delay
        self.clock = clock
        self.port = "模拟设备"
        self.device = "ESP32-S3 · 演示模式"
        self.session = None
        self.state = None
        self.calls = []

    def connect(self):
        self.session = "demo-" + uuid.uuid4().hex
        self.state = {"axes": blank_axes(), "counts": {"1": 0, "2": 0, "3": 0},
                      "configured": False, "long_id": 1, "margin_mm": 10.0,
                      "rpm_min": 3, "rpm_max": 30, "feedback_cached": True, "capabilities": ["recover", "jog_group", "pump", "pump_rates", "emergency_stop", "continuous_reset", "continuous_move", "stop_preserve", "drive_restore", "graceful_shutdown"], "pump": None, "reset": None}
        return copy.deepcopy(self.state)

    def close(self, graceful=False):
        self.session = None

    def emergency_stop(self):
        self._advance_pump()
        self._advance_reset()
        if self.state.get("reset"):
            self.state["reset"].update(active=False, state="急停已执行")
        if self.state.get("pump"):
            self.state["pump"].update(active=False, state="急停已执行")
        for axis in self.state["axes"]:
            axis.update(position_mm=None, anchored=False, lead_mm=None, forward_sign=None)

    def finish_emergency(self):
        return {"confirmed": True, "failed_axes": []}

    def request(self, op, args=None):
        args = args or {}
        self._advance_pump()
        self._advance_reset()
        self.calls.append((op, dict(args)))
        if not self.session:
            raise OSError("模拟设备已断开。")
        if op in ("reset_positions", "move_continuous"):
            members = []
            for move in args["moves"]:
                axis = self._axis(move["axis"])
                start, target = axis["position_mm"], move["target_mm"]
                if not axis["anchored"] or not (axis["physical_min_mm"] if args.get("recovery") else axis["min_mm"]) <= target <= (axis["physical_max_mm"] if args.get("recovery") else axis["max_mm"]):
                    raise DeviceError("复位基准或目标无效。")
                approach = target if args.get("recovery") else min(axis["max_mm"]-.1,max(axis["min_mm"]+.1,target))
                done = abs(target-start)<.099999 or abs(approach-start)<.099999
                members.append({"axis":axis["id"],"start_mm":start,"position_mm":start,"target_mm":target,
                                "approach_mm":start if done else approach,"rpm":move.get("rpm",args.get("rpm",12)),"done":done,"progress":1.0 if done else 0.0})
            self.reset_started = self.reset_heartbeat = self.clock()
            self.state["reset"] = {"active":not all(m["done"] for m in members),"state":"done" if all(m["done"] for m in members) else "running",
                                   "members":members,"stop_fault":False,"continuous":True}
        elif op == "reset_heartbeat":
            self.reset_heartbeat = self.clock()
        elif op == "pump_start":
            area = math.pi * (19.22/2)**2
            members = []
            if args.get("confirmed") is not True or len(args.get("items", [])) != 2:
                raise DeviceError("请确认注射参数。")
            for item in args["items"]:
                axis = self._axis(item["axis"])
                direction = item["direction"]
                if axis["id"] == self.state["long_id"] or not axis["anchored"]:
                    raise DeviceError("只能用已标定的短轴注射。")
                flow = item.get("flow_ml_min",1.0)
                rpm = flow*1000/area/axis["lead_mm"]
                if isinstance(flow,bool) or not math.isfinite(flow) or not 0.1 <= rpm <= 2:
                    raise DeviceError("注射速率超出范围。")
                start = axis["position_mm"]
                room = axis["max_mm"]-start if direction > 0 else start-axis["min_mm"]
                distance = min(item["remaining_mm"]-1, room-0.5)
                if distance < 0.5:
                    raise DeviceError("注射行程不足。")
                members.append({"axis":axis["id"], "start_mm":start, "position_mm":start,
                                "end_mm":start+direction*distance, "distance_mm":distance,
                                "direction":direction, "rpm":rpm, "flow_ml_min":flow, "mm_min":flow*1000/area,
                                "volume_ml":0.0,"progress":0.0})
            self.pump_started = self.pump_heartbeat = time.monotonic()
            self.state["pump"] = {"active":True,"state":"running","members":members,"stop_fault":False,"total_flow_ml_min":sum(m["flow_ml_min"] for m in members)}
        elif op == "pump_heartbeat":
            self.pump_heartbeat = time.monotonic()
        elif op == "configure":
            if args.get("confirmed") is not True or args.get("ids") != [1, 2, 3] or args.get("long_id") not in (1, 2, 3):
                raise DeviceError("请确认三轴配置。")
            self.state.update(configured=True, long_id=args["long_id"], axes=blank_axes(args["long_id"]))
        elif op == "anchor":
            axis = self._axis(args["axis"])
            if not self.state["configured"] or args.get("confirmed") is not True:
                raise DeviceError("请先确认配置和实测位置。")
            mm = args["mm"]
            if not math.isfinite(mm) or not axis["physical_min_mm"] <= mm <= axis["physical_max_mm"]:
                raise DeviceError("位置超出机械有效范围。")
            if args["sign"] not in (-1, 1) or args["lead"] not in (2, 4):
                raise DeviceError("方向或导程无效。")
            axis.update(position_mm=mm, anchored=True, lead_mm=args["lead"], forward_sign=args["sign"],
                        max_jog_mm=min(1.0, 12800.0 / (51200.0 / args["lead"])))
        elif op == "jog_group":
            original = copy.deepcopy(self.state)
            moves = args.get("moves", [])
            if not 1 <= len(moves) <= 3 or len({m["axis"] for m in moves}) != len(moves):
                raise DeviceError("并行参数无效。")
            try:
                for move in moves:
                    self.request("jog", move)
            except Exception:
                self.state = original
                raise
        elif op in ("jog", "recover"):
            axis = self._axis(args["axis"])
            delta, rpm = args["delta_mm"], args["rpm"]
            if not axis["anchored"] or not 0.1 <= abs(delta) <= axis["max_jog_mm"] or not 3 <= rpm <= 30:
                raise DeviceError("运动参数或基准无效。")
            target = axis["position_mm"] + delta
            start = axis["position_mm"]
            if op == "recover":
                if (args.get("confirmed") is not True or rpm > 6 or abs(delta) > 0.5
                        or not axis["physical_min_mm"] <= target <= axis["physical_max_mm"]
                        or not ((start < axis["min_mm"] and delta > 0 and target <= axis["min_mm"]+0.1)
                                or (start > axis["max_mm"] and delta < 0 and target >= axis["max_mm"]-0.1))):
                    raise DeviceError("恢复只能低速小步向内。")
            elif not axis["min_mm"] <= target <= axis["max_mm"] or not axis["min_mm"] <= start <= axis["max_mm"]:
                raise DeviceError("目标未保留 1 cm 余量。")
            time.sleep(self.delay)
            pulses = int(abs(delta) * 51200.0 / axis["lead_mm"])
            signed = pulses * axis["forward_sign"] * (1 if delta > 0 else -1)
            self.state["counts"][str(axis["id"])] += signed
            axis["position_mm"] += (1 if delta > 0 else -1) * pulses / (51200.0 / axis["lead_mm"])
        elif op == 'restore_drives':
            self.state['driver_states'] = [dict(id=i,state=0,stall=0,disabled=0) for i in (1,2,3)]
            for axis in self.state['axes']:
                axis.update(position_mm=None,anchored=False,lead_mm=None,forward_sign=None)
        elif op == 'shutdown':
            self.request('lock')
            self.state['shutdown_ready'] = True
        elif op in ("lock", "stop_motion"):
            self.state["stopped_axes"] = copy.deepcopy(self.state["axes"]) if all(a["anchored"] for a in self.state["axes"]) else None
            if self.state.get("reset"):
                self.state["reset"].update(active=False, state="已停止连续复位")
            if self.state.get("pump"):
                self.state["pump"].update(active=False, state="已停止注射")
            if op == 'lock':
                for axis in self.state["axes"]:
                    axis.update(position_mm=None, anchored=False, lead_mm=None, forward_sign=None)
        elif op not in ("hello", "status"):
            raise DeviceError("未知操作。")
        return copy.deepcopy(self.state)

    def _advance_reset(self):
        reset = self.state.get("reset") if self.state else None
        if not reset or not reset["active"]:
            return
        now = self.clock()
        if now-self.reset_heartbeat >= 1.5:
            reset.update(active=False,state="复位主机心跳超时")
            for axis in self.state["axes"]:
                axis.update(position_mm=None,anchored=False,lead_mm=None,forward_sign=None)
            return
        for m in reset["members"]:
            if m["done"]:
                continue
            axis = self._axis(m["axis"])
            distance = abs(m["approach_mm"]-m["start_mm"])
            travel = min(distance,(now-self.reset_started)*m["rpm"]*axis["lead_mm"]/60)
            position = m["start_mm"]+math.copysign(travel,m["approach_mm"]-m["start_mm"])
            delta = position-axis["position_mm"]
            self.state["counts"][str(axis["id"])] += round(delta*axis["forward_sign"]*51200/axis["lead_mm"])
            axis["position_mm"] = position
            m.update(position_mm=position,progress=travel/distance,done=travel>=distance)
        if all(m["done"] for m in reset["members"]):
            reset.update(active=False,state="done")

    def _advance_pump(self):
        pump = self.state.get("pump") if self.state else None
        if not pump or not pump["active"]:
            return
        now = time.monotonic()
        if now-self.pump_heartbeat >= 1.5:
            pump.update(active=False, state="主机心跳超时，已停止注射")
        else:
            area = math.pi*(19.22/2)**2
            elapsed = min(now-self.pump_started, min(m["distance_mm"]/m["mm_min"]*60 for m in pump["members"]))
            for member in pump["members"]:
                travel = elapsed*member["mm_min"]/60
                travelled = min(travel, member["distance_mm"])
                member.update(position_mm=member["start_mm"]+member["direction"]*travelled,
                              volume_ml=travelled*area/1000, progress=travelled/member["distance_mm"])
                axis = self._axis(member["axis"])
                axis["position_mm"] = member["position_mm"]
                if travel >= member["distance_mm"]:
                    pump.update(active=False, state="已到达设定行程，停止两支注射器")
        if not pump["active"] and "到达设定行程" not in pump["state"]:
            for axis in self.state["axes"]:
                axis.update(position_mm=None, anchored=False, lead_mm=None, forward_sign=None)

    def _axis(self, ident):
        return next(a for a in self.state["axes"] if a["id"] == ident)
