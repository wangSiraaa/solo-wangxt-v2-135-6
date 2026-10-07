"""SimPy 电梯物理引擎。

策略无关：引擎只负责时间推进与物理动作（移动、开关门、上下客、容量约束）。
派梯策略实现 Policy 接口的三个钩子：

- plan(car)             决定下一段扫描方向 dir 与转向边界 bound
- should_stop(car, f)   轿厢经过/到达 f 时是否停站
- claim_hall(car, call) 停站开门后是否把该候梯队列视为本车的任务

物理规则（所有策略共用，保证对照公平）：
- 轿厢逐层移动，每层耗时 floor_time（匀速简化模型）；
- 每次停站一个完整开关门周期 door_time；
- 先下后上；同层候梯队列 FIFO；
- 满载后剩余乘客继续留在原队列（事件日志中位置仍是候梯厅）；
- 仿真结束仍在系统内的乘客标记 UNSERVED，不允许从指标里剔除。
"""
from __future__ import annotations

from typing import Protocol

import simpy

from .models import (
    UP, DOWN, IDLE,
    Pax, PaxStatus, HallCall, DoorHold,
)


class Car:
    def __init__(self, car_id: int, building: "Building",
                 home_floor: int | None = None):
        self.id = car_id
        self.b = building
        self.floor = building.parking_floor if home_floor is None else home_floor
        self.initial_floor = self.floor
        self.dir = IDLE
        self.aboard: list[Pax] = []
        self.stop_count = 0
        self.load_profile: list[tuple[float, int]] = []  # 时间, 车内人数


class Policy(Protocol):
    def plan(self, car: Car) -> tuple[int, int | None]:
        """决定下一段 SCAN：返回 (dir, bound)。

        dir == IDLE 表示无任务可做，bound 忽略；
        bound 为该方向扫描的转向楼层（到达后反向重新规划）。
        """

    def should_stop(self, car: Car, floor: int) -> bool:
        ...

    def claim_hall(self, car: Car, call: HallCall) -> bool:
        """开门时是否接纳该方向队列（决定可否上车，不改变队列成员顺序）。"""

    def board_dir(self, car: Car, floor: int) -> int | None:
        """在该层开门时按哪个方向组织上车；None 表示该层无客可上。"""

    def on_tick(self, b: "Building") -> None:
        """每个物理事件点触发一次，供全局调度器做指派（默认空实现）。"""


class Building:
    def __init__(self, *, floors: int, car_count: int, capacity: int,
                 floor_time: float, door_time: float,
                 parking_floor: int = 1, seed: int = 0,
                 until: float = 3600.0, policy: Policy | None = None,
                 label: str = "", door_hold: dict | None = None):
        self.floors = floors
        self.car_count = car_count
        self.capacity = capacity
        self.floor_time = floor_time
        self.door_time = door_time
        self.parking_floor = parking_floor
        self.seed = seed
        self.until = until
        self.policy: Policy = policy  # type: ignore[assignment]
        self.label = label
        # 门阻挡事件（可选）：{"floor": f, "start": t, "duration": d}
        if door_hold is not None:
            if not (1 <= door_hold["floor"] <= floors):
                raise ValueError(
                    f"门阻挡楼层 {door_hold['floor']} 超出井道 1..{floors}")
            if door_hold["start"] < 0 or door_hold["duration"] <= 0:
                raise ValueError("门阻挡 start 必须 >= 0 且 duration > 0")
            self.door_hold: DoorHold | None = DoorHold(**door_hold)
        else:
            self.door_hold = None

        self.env = simpy.Environment()
        self.cars: list[Car] = []
        for i in range(car_count):
            self.cars.append(Car(i, self))
        # calls[(floor, dir)] -> HallCall
        self.calls: dict[tuple[int, int], HallCall] = {}
        # (floor, dir) -> {car_id,...} 多车并发响应长队列时的补位车集合
        self.multi_claims: dict[tuple[int, int], set[int]] = {}
        self.pax: list[Pax] = []
        self.events: list[dict] = []
        self._arrivals: list[Pax] = []

    # ---------- 候梯队列工具 ----------
    def hall(self, floor: int, direction: int) -> HallCall:
        key = (floor, direction)
        call = self.calls.get(key)
        if call is None:
            call = HallCall(floor, direction)
            self.calls[key] = call
        return call

    def active_calls(self) -> list[HallCall]:
        return [c for c in self.calls.values() if c.is_active()]

    # ---------- 事件日志 ----------
    MAX_EVENTS = 200_000

    def log(self, t: float, etype: str, **kw) -> None:
        if len(self.events) >= self.MAX_EVENTS:
            raise RuntimeError(
                f"事件数超过 {self.MAX_EVENTS}，疑似零时间忙循环（{etype} @ t={t}）")
        rec = {"t": round(t, 4), "type": etype}
        rec.update(kw)
        self.events.append(rec)

    # ---------- 客流注入 ----------
    def set_arrivals(self, arrivals: list[tuple[float, int, int]]) -> None:
        """arrivals: (到达时刻, 起始层, 目的层)，按时间排序。"""
        for pid, (at, o, d) in enumerate(sorted(arrivals, key=lambda x: x[0])):
            p = Pax(pid=pid, origin=o, dest=d, arrival_time=at)
            self.pax.append(p)
            self._arrivals.append(p)

    def passenger_source(self):
        for p in self._arrivals:
            yield self.env.timeout(max(0.0, p.arrival_time - self.env.now))
            call = self.hall(p.origin, p.wanted)
            call.queue.append(p)
            p.status = PaxStatus.WAITING
            p.location = ("hall", p.origin)
            self.log(self.env.now, "pax_arrive",
                     pid=p.pid, origin=p.origin, dest=p.dest,
                     wanted=p.wanted, queue_len=len(call.queue),
                     location=f"hall:{p.origin}")
            # 新需求可能改变调度，唤醒所有轿厢重新规划
            for car in self.cars:
                self.policy.on_tick(self)
                if car.dir == IDLE and len(car.aboard) == 0:
                    self.env.process(self._wake(car))

    def _wake(self, car: Car):
        # 用零超时让当前事件先落日志，再让空闲梯重新决策
        yield self.env.timeout(0)
        car_proc = getattr(car, "_proc", None)
        if car_proc is not None and not car_proc.triggered:
            car_proc.succeed()

    # ---------- 轿厢物理过程 ----------
    def car_process(self, car: Car):
        env = self.env
        while True:
            d, bound = self.policy.plan(car)
            if d == IDLE:
                event = env.event()
                car._proc = event  # type: ignore[attr-defined]
                self.log(env.now, "car_idle", car=car.id, floor=car.floor,
                         load=len(car.aboard))
                yield event
                continue

            car.dir = d
            self.log(env.now, "car_dir", car=car.id, floor=car.floor,
                     dir=d, bound=bound, load=len(car.aboard))

            stall = 0
            while True:
                # 1) 先看本层是否停站（含端层换向后同层反向外呼）
                if self.policy.should_stop(car, car.floor):
                    yield from self._serve_floor(car)
                    car.stop_count += 1

                # 2) 停站后重新规划
                nd, nb = self.policy.plan(car)
                if nd == IDLE:
                    car.dir = IDLE
                    break
                if nd != car.dir:
                    # 换向：不移动，下一轮在同层服务反向外呼
                    car.dir = nd
                    self.log(env.now, "car_dir", car=car.id,
                             floor=car.floor, dir=nd, bound=nb,
                             load=len(car.aboard), reverse=True)
                    stall += 1
                    if stall > 4:
                        raise RuntimeError(
                            f"{car.id} 号梯在 {car.floor} 层零时间换向死循环")
                    continue
                stall = 0
                # 3) 同方向：若本层接客后 bound 仍在前方则移动一层；
                #    否则任务到此为止（新呼叫会唤醒）
                if nb is None or (nd == UP and car.floor >= nb) \
                        or (nd == DOWN and car.floor <= nb):
                    car.dir = IDLE
                    break
                yield env.timeout(self.floor_time)
                car.floor += nd
                self.log(env.now, "car_move", car=car.id, floor=car.floor,
                         dir=nd, load=len(car.aboard))

    def _serve_floor(self, car: Car):
        env = self.env
        f = car.floor
        self.log(env.now, "car_stop", car=car.id, floor=f,
                 load=len(car.aboard))

        leaving = any(p.dest == f for p in car.aboard)
        board_dir = self.policy.board_dir(car, f)
        if not (leaving or board_dir is not None):
            # 停站但无实际服务对象（呼叫被他车接空等）：不开门即走
            self.log(env.now, "car_skip", car=car.id, floor=f)
            return

        # 确定本次开门的接梯方向：有内呼则维持当前方向，空车可换向
        if board_dir is not None and board_dir != car.dir:
            car.dir = board_dir
            self.log(env.now, "car_reverse_at_floor", car=car.id, floor=f,
                     dir=board_dir, load=len(car.aboard))

        yield env.timeout(self.door_time)
        self.log(env.now, "doors_open", car=car.id, floor=f,
                 load=len(car.aboard), door_time=self.door_time)

        # ---- 下客（先下后上）----
        leaving_pax = [p for p in car.aboard if p.dest == f]
        for p in leaving_pax:
            car.aboard.remove(p)
            p.status = PaxStatus.DONE
            p.location = ("outside", f)
            p.alight_time = env.now
            self.log(env.now, "pax_alight", pid=p.pid, car=car.id, floor=f,
                     ride_time=round(env.now - p.board_time, 4),
                     total_time=round(env.now - p.arrival_time, 4),
                     load=len(car.aboard), location=f"outside:{f}")

        # ---- 上客：仅接当前接梯方向（= 轿厢扫描方向）的 FIFO 队列 ----
        call = self.calls.get((f, car.dir)) if car.dir != IDLE else None
        if call is not None and call.is_active() \
                and self.policy.claim_hall(car, call):
            while len(car.aboard) < self.capacity:
                p = call.head()
                if p is None or not self._eligible(car, p):
                    break
                call.queue.remove(p)
                car.aboard.append(p)
                p.status = PaxStatus.ABOARD
                p.car_id = car.id
                p.board_time = env.now
                p.location = ("car", car.id)
                self.log(env.now, "pax_board", pid=p.pid, car=car.id,
                         floor=f, dest=p.dest, wanted=p.wanted,
                         wait_time=round(env.now - p.arrival_time, 4),
                         load=len(car.aboard), location=f"car:{car.id}")
            if len(car.aboard) >= self.capacity and call.is_active():
                # 满载：未上车者保留在原队列
                left = [q for q in call.queue if q.status == PaxStatus.WAITING]
                self.log(env.now, "car_full", car=car.id, floor=f,
                         left_waiting=True, queue_len=len(left))

        # ---- 门阻挡事件：窗口内第一台在本层正欲关门的轿厢被乘客挡住 ----
        # 正常下客/上客已完成；阻挡只推迟关门，等待期间不得移动、不得再次登乘。
        hold = self.door_hold
        if (hold is not None and not hold.consumed and f == hold.floor
                and hold.start <= env.now < hold.end):
            hold.consumed = True
            hold.car_id = car.id
            hold.hold_start = env.now
            self.log(env.now, "door_hold_start", car=car.id, floor=f,
                     planned_close=round(env.now, 4), until=hold.end,
                     load=len(car.aboard))
            yield env.timeout(hold.end - env.now)
            hold.released = True
            self.log(env.now, "door_hold_end", car=car.id, floor=f,
                     held_for=round(env.now - hold.hold_start, 4),
                     load=len(car.aboard))

        self.log(env.now, "doors_close", car=car.id, floor=f,
                 load=len(car.aboard))
        car.load_profile.append((env.now, len(car.aboard)))
        # 本车服务完毕（满载或不服务该方向）后，若队列仍有人，
        # 释放全局指派以便其它轿厢重新认领（避免呼叫被永久绑死一车）
        if call is not None and call.is_active():
            if call.claimed_by == car.id and len(car.aboard) >= self.capacity:
                pass  # 满载离开：稍后由 on_tick 重派
            elif call.claimed_by == car.id:
                call.claimed_by = None
                call.assign_time = None
                self.log(env.now, "call_release", floor=f,
                         dir=car.dir, car=car.id,
                         queue_len=len([q for q in call.queue
                                        if q.status == PaxStatus.WAITING]))
        self.policy.on_tick(self)

    def _eligible(self, car: Car, p: Pax) -> bool:
        """乘客能否上本车：轿厢必须朝乘客目的层方向扫描。"""
        return (car.dir != IDLE
                and (p.dest - p.origin) * car.dir > 0)

    # ---------- 运行 ----------
    def run(self) -> dict:
        for car in self.cars:
            self.env.process(self.car_process(car))
        self.env.process(self.passenger_source())
        self.env.run(until=self.until)

        # 未服务乘客：候梯中或仍在梯内未到达目的层 —— 全部保留，不得剔除
        end = self.until
        for p in self.pax:
            if p.status in (PaxStatus.WAITING,):
                p.status = PaxStatus.UNSERVED
                p.location = ("hall", p.origin)
                self.log(end, "pax_unserved", pid=p.pid, where="hall",
                         floor=p.origin, dest=p.dest,
                         waited=round(end - p.arrival_time, 4),
                         location=f"hall:{p.origin}")
            elif p.status == PaxStatus.ABOARD:
                p.status = PaxStatus.UNSERVED
                car_floor = next((c.floor for c in self.cars
                                  if c.id == p.car_id), p.origin)
                p.location = ("car", p.car_id if p.car_id is not None else -1)
                self.log(end, "pax_unserved", pid=p.pid, where="car",
                         car=p.car_id, floor=car_floor, dest=p.dest,
                         waited=round((p.board_time or end) - p.arrival_time, 4),
                         location=f"car:{p.car_id}")

        return self.summarize()

    # ---------- 指标 ----------
    def summarize(self) -> dict:
        served = [p for p in self.pax if p.status == PaxStatus.DONE]
        unserved = [p for p in self.pax if p.status == PaxStatus.UNSERVED]
        end = self.until

        def wait_of(p: Pax) -> float:
            if p.board_time is not None:
                return p.board_time - p.arrival_time
            return end - p.arrival_time  # 未上车：等到仿真结束

        def ride_of(p: Pax) -> float:
            if p.board_time is not None and p.alight_time is not None:
                return p.alight_time - p.board_time
            # 上过车但未到达：已乘时间计入乘梯耗时
            if p.board_time is not None:
                return end - p.board_time
            return 0.0

        def total_of(p: Pax) -> float:
            if p.alight_time is not None:
                return p.alight_time - p.arrival_time
            return end - p.arrival_time

        def pct(values, q):
            if not values:
                return 0.0
            s = sorted(values)
            i = min(len(s) - 1, int(round(q * (len(s) - 1))))
            return round(s[i], 3)

        served_wait = [wait_of(p) for p in served]
        served_ride = [ride_of(p) for p in served]
        served_total = [total_of(p) for p in served]
        all_wait = [wait_of(p) for p in self.pax]
        all_total = [total_of(p) for p in self.pax]

        # 策略的有效性（全部客流，未服务不剔除）：
        # 未完成者的总行程按仿真结束时刻计，并以惩罚形式体现"没送到"
        completion = len(served) / len(self.pax) if self.pax else 1.0

        car_stats = []
        for c in self.cars:
            car_stats.append({
                "car_id": c.id,
                "stops": c.stop_count,
                "final_floor": c.floor,
                "max_load": max((n for _, n in c.load_profile), default=0),
            })

        return {
            "label": self.label,
            "seed": self.seed,
            "demand": len(self.pax),
            "served": len(served),
            "unserved": len(unserved),
            "completion_rate": round(completion, 4),
            "door_hold": self._door_hold_info(),
            # 只对已完成乘客有"物理意义"的条件均值（明确标注口径）
            "served_only": {
                "wait_mean": round(sum(served_wait) / len(served_wait), 3) if served_wait else None,
                "wait_p90": pct(served_wait, 0.9),
                "ride_mean": round(sum(served_ride) / len(served_ride), 3) if served_ride else None,
                "ride_p90": pct(served_ride, 0.9),
                "total_mean": round(sum(served_total) / len(served_total), 3) if served_total else None,
                "total_p90": pct(served_total, 0.9),
            },
            # 全客流口径（未服务按仿真结束截断，不允许丢弃后夸大）
            "all_pax": {
                "wait_mean": round(sum(all_wait) / len(all_wait), 3) if all_wait else None,
                "wait_p90": pct(all_wait, 0.9),
                "total_mean": round(sum(all_total) / len(all_total), 3) if all_total else None,
                "total_p90": pct(all_total, 0.9),
            },
            "cars": car_stats,
        }

    def _door_hold_info(self) -> dict | None:
        """门阻挡事件配置与触发结果（供指标/前端时间轴/持久化复放）。"""
        h = self.door_hold
        if h is None:
            return None
        info: dict = {
            "configured": {"floor": h.floor, "start": h.start,
                           "duration": h.duration},
            "end": h.end,
            "triggered": h.consumed,
        }
        if h.consumed:
            info.update({
                "car": h.car_id,
                "floor": h.floor,
                "hold_start": h.hold_start,
                "hold_end": h.end if h.released else None,
                "released": h.released,
                # 额外延误 = 解除时刻 - 本应关门时刻（未解除按截止时刻截断）
                "delay": round(min(h.end, self.until) - h.hold_start, 4),
            })
        return info
