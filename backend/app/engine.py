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

from .blockages import DoorBlock
from .models import (
    UP, DOWN, IDLE,
    Pax, PaxStatus, HallCall,
)


class _LowPriorityTick(simpy.Event):
    """同刻低优先级事件：priority=2（NORMAL=1）之后执行，标记成功无值。

    用于阻挡武装：让同一时间戳的正常停站回调（doors_open 等）先落日志。
    """

    def __init__(self, env: simpy.Environment):
        super().__init__(env)
        self._ok = True
        env.schedule(self, priority=2)


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
        # ---- 门保持打开（阻挡）运行时状态，由事件日志可完全重建 ----
        self.block_pending: DoorBlock | None = None  # 已武装、待进入的阻挡
        self.block_active: DoorBlock | None = None   # 正在执行的阻挡
        self.holding_until: float | None = None      # 当前保持开门到何时


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
                 label: str = "", blocks: list[DoorBlock] | None = None):
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
        self.blocks: list[DoorBlock] = sorted(blocks or [],
                                              key=lambda b: (b.start, b.car))

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
        # 每个阻挡的影响汇总（受影响乘客等），key=(block_id, car)
        self.block_impact: list[dict] = []

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

    # ---------- 门保持打开（阻挡） ----------
    def car_unavailable(self, car: Car, now: float | None = None) -> bool:
        """阻挡武装/执行期间的轿厢不参与新外呼指派（其他轿厢照常运行）。"""
        return car.block_pending is not None or car.block_active is not None

    def _block_due(self, car: Car, now: float) -> DoorBlock | None:
        b = car.block_pending
        if b is not None and b.start <= now:
            return b
        return None

    def block_monitor(self):
        """在每个 start 时刻武装对应轿厢并唤醒其物理过程。

        start 前的仿真与无阻挡完全一致，因此在基准重放时间轴上配置的
        楼层必然与轿厢实际楼层吻合。武装事件以较低调度优先级排在同一
        时刻的正常停站事件（doors_open 等）之后，日志阅读顺序自然。
        """
        env = self.env
        for b in self.blocks:
            yield env.timeout(max(0.0, b.start - env.now))
            # 武装以低优先级（2 > NORMAL=1）排到同一时刻的停站回调
            # （doors_open/pax_board/doors_close）之后，日志顺序自然：
            # 先记录正常停站，再记 door_block_arm/start
            yield _LowPriorityTick(env)
            self._arm_block(b)

    def _arm_block(self, b: DoorBlock) -> None:
        env = self.env
        car = self.cars[b.car]
        # 同一轿厢窗不重叠（normalize_blocks 已校验），故至多一个在途
        if car.block_pending is not None or car.block_active is not None:
            raise RuntimeError(
                f"轿厢 {car.id} 在 t={env.now} 已有未完成阻挡")
        car.block_pending = b
        self.log(env.now, "door_block_arm", car=car.id,
                 block_id=b.block_id, floor=car.floor,
                 requested_floor=b.floor,
                 start=b.start, end=b.end,
                 load=len(car.aboard))
        self.policy.on_tick(self)
        # 唤醒空闲/等任务中的轿厢；正在移动或停站的轿厢自行检查 pending。
        # 用零时刻超时触发，确保本时刻已排定的停站回调先执行完
        proc = getattr(car, "_proc", None)
        if proc is not None and not proc.triggered:
            def _wake():
                yield self.env.timeout(0)
                if not proc.triggered:
                    proc.succeed()
            self.env.process(_wake())

    def _enter_hold(self, car: Car, b: DoorBlock, floor_mismatch: bool,
                    boarded: list[Pax], hold_seconds: float,
                    attached: bool = False):
        """记录 door_block_start 并挂起 hold_seconds 秒（期间轿厢不动、不登乘）。"""
        env = self.env
        car.block_pending = None
        car.block_active = b
        car.holding_until = b.end
        car._hold_enter_t = env.now  # type: ignore[attr-defined]
        aboard_pids = sorted(p.pid for p in car.aboard)
        self.log(env.now, "door_block_start", car=car.id,
                 block_id=b.block_id, floor=car.floor,
                 requested_floor=b.floor, floor_mismatch=floor_mismatch,
                 end=b.end, duration=round(hold_seconds, 4),
                 attached=attached,
                 boarded_pids=sorted(p.pid for p in boarded),
                 aboard_pids=aboard_pids,
                 load=len(car.aboard),
                 note=("正常停站开门期间阻挡武装：门不关闭、续接保持"
                       if attached else
                       "门保持打开：阻挡期间禁动、禁重复登乘"))
        # 其他轿厢不受影响；on_tick 只是让调度器把呼叫从本车视角重新分配
        self.policy.on_tick(self)
        yield env.timeout(hold_seconds)
        # 到达 end：阻挡解除
        self._release_hold(car, b, boarded)

    def _release_hold(self, car: Car, b: DoorBlock, boarded: list[Pax]):
        env = self.env
        f = car.floor
        # 阻挡期间在本层等候、未能登乘本车的乘客（可能已被其他轿厢接走）
        stranded = sorted(
            p.pid for p in self.pax
            if p.status == PaxStatus.WAITING and p.origin == f)
        self.log(env.now, "door_block_end", car=car.id,
                 block_id=b.block_id, floor=f,
                 load=len(car.aboard),
                 boarded_pids=sorted(p.pid for p in boarded),
                 aboard_pids=sorted(p.pid for p in car.aboard),
                 stranded_pids=stranded)
        self.log(env.now, "doors_close", car=car.id, floor=f,
                 load=len(car.aboard))
        car.load_profile.append((env.now, len(car.aboard)))
        car.block_active = None
        car.holding_until = None
        # 释放本车仍持有的队列认领，交给调度器重派
        for call in self.active_calls():
            if call.claimed_by == car.id:
                call.claimed_by = None
                call.assign_time = None
                self.log(env.now, "call_release", floor=call.floor,
                         dir=call.direction, car=car.id, reason="block_cleared",
                         queue_len=len([q for q in call.queue
                                        if q.status == PaxStatus.WAITING]))
        self.block_impact.append({
            "block_id": b.block_id, "car": car.id,
            "requested_floor": b.floor, "actual_floor": f,
            "floor_mismatch": f != b.floor,
            "start": b.start, "end": b.end,
            "enter_t": round(getattr(car, "_hold_enter_t", env.now), 4),
            "release_t": round(env.now, 4),
            "boarded_pids": sorted(p.pid for p in boarded),
            "aboard_pids": sorted(p.pid for p in car.aboard),
            "stranded_pids": stranded,
            "affected_pids": sorted(
                set(sorted(p.pid for p in boarded))
                | set(p.pid for p in car.aboard) | set(stranded)),
            "load_at_release": len(car.aboard),
        })
        self.policy.on_tick(self)

    # ---------- 轿厢物理过程 ----------
    def car_process(self, car: Car):
        env = self.env
        while True:
            # 已武装的阻挡优先处理（IDLE 被 block_monitor 唤醒后也走这里）
            if self._block_due(car, env.now) is not None:
                yield from self._handle_block(car)
                continue

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
                    door_left_open, stop_boarded = \
                        yield from self._serve_floor(car)
                    car.stop_count += 1
                    # 阻挡恰好在本层、且停站门还开着：不关门、不再登乘，
                    # 直接续接为门保持打开（一批登乘来自正常停站）
                    b = self._block_due(car, env.now)
                    if b is not None and door_left_open:
                        hold = max(0.0, b.end - env.now)
                        yield from self._attach_hold(car, b, hold,
                                                     stop_boarded)
                        break

                # 其余武装情况（不同层、或停站未开门）：正常停站已完整结束，
                # 关门逐层就位（位置不跳变）
                if self._block_due(car, env.now) is not None:
                    yield from self._handle_block(car)
                    break

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
                # 阻挡可能在这一层移动中途武装：让当前这层移动走完，
                # 到落点后处理 —— 不回退、不跳层（位置不跳变）
                was_pending_before = car.block_pending
                yield env.timeout(self.floor_time)
                car.floor += nd
                # 这层移动在武装前已启动（门关闭、逐层），允许它走完：
                # verifier 只放行这一段 armed_mid_move，之后必须朝目标层
                armed_mid_move = (was_pending_before is None
                                  and car.block_pending is not None)
                self.log(env.now, "car_move", car=car.id, floor=car.floor,
                         dir=nd, load=len(car.aboard),
                         **({"blocked_relocating": True,
                             "armed_mid_move": True} if armed_mid_move else {}))
                if self._block_due(car, env.now) is not None:
                    yield from self._handle_block(car)
                    break

    def _handle_block(self, car: Car):
        """已武装阻挡的完整处理：必要时逐层就位 → 开门一批 → 保持 → 解除。"""
        pending = car.block_pending
        if pending is None or pending.start > self.env.now:
            return
        b = pending
        env = self.env
        floor_mismatch = car.floor != b.floor
        # 逐层就位：门关闭、途中不停站不登乘（位置不跳变）
        while car.floor != b.floor:
            d = UP if b.floor > car.floor else DOWN
            car.dir = d
            self.log(env.now, "car_dir", car=car.id, floor=car.floor,
                     dir=d, bound=b.floor, load=len(car.aboard),
                     blocked_relocating=True)
            yield env.timeout(self.floor_time)
            car.floor += d
            self.log(env.now, "car_move", car=car.id, floor=car.floor,
                     dir=d, load=len(car.aboard),
                     blocked_relocating=True)
        yield from self._serve_blocked(car, b, floor_mismatch)

    def _serve_blocked(self, car: Car, b: DoorBlock, floor_mismatch: bool):
        env = self.env
        f = car.floor
        # 就位时窗已过（配置楼层过远导致）：记录跳过，按正常策略继续
        if env.now >= b.end:
            car.block_pending = None
            self.log(env.now, "door_block_skip_expired", car=car.id,
                     block_id=b.block_id, floor=f, end=b.end,
                     load=len(car.aboard))
            self.policy.on_tick(self)
            return
        self.log(env.now, "car_stop", car=car.id, floor=f,
                 load=len(car.aboard), blocked=True)
        yield from self._open_door_and_alight(car)
        # 开门+下客后 end 已过（目标层过远）：正常一批登乘后关门离开
        if env.now >= b.end:
            self._board_one_batch(car)
            self.log(env.now, "doors_close", car=car.id, floor=f,
                     load=len(car.aboard))
            car.load_profile.append((env.now, len(car.aboard)))
            car.block_pending = None
            self.log(env.now, "door_block_skip_expired", car=car.id,
                     block_id=b.block_id, floor=f, end=b.end,
                     load=len(car.aboard), after_open=True)
            self.policy.on_tick(self)
            return
        boarded = self._board_one_batch(car)
        # 一批登乘后剩余时间门保持打开；end 已过则保持 0
        hold = max(0.0, b.end - env.now)
        yield from self._enter_hold(car, b, floor_mismatch, boarded, hold)

    def _attach_hold(self, car: Car, b: DoorBlock, hold_seconds: float,
                     boarded: list[Pax]):
        """阻挡在本层正常停站开门期间武装：门不关、不重复登乘，直接续接保持。"""
        # 本批登乘来自刚完成的正常停站（先下后上一批），保持期间不再登乘
        yield from self._enter_hold(
            car, b, floor_mismatch=False, boarded=boarded,
            hold_seconds=hold_seconds, attached=True)

    def _serve_floor(self, car: Car) -> tuple[bool, list[Pax]]:
        """正常停站。返回 (门仍开着(被阻挡续接), 本批登乘乘客)。"""
        env = self.env
        f = car.floor
        self.log(env.now, "car_stop", car=car.id, floor=f,
                 load=len(car.aboard))

        leaving = any(p.dest == f for p in car.aboard)
        board_dir = self.policy.board_dir(car, f)
        if not (leaving or board_dir is not None):
            # 停站但无实际服务对象（呼叫被他车接空等）：不开门即走
            self.log(env.now, "car_skip", car=car.id, floor=f)
            return False, []

        # 确定本次开门的接梯方向：有内呼则维持当前方向，空车可换向
        if board_dir is not None and board_dir != car.dir:
            car.dir = board_dir
            self.log(env.now, "car_reverse_at_floor", car=car.id, floor=f,
                     dir=board_dir, load=len(car.aboard))

        # 开门 → 下客；开门瞬间让出的零时刻使同刻 arm 先落日志
        yield from self._open_door_and_alight(car)
        # 本层阻挡在开门期间武装：允许正常一批登乘（发生在保持开始前），
        # 随后门不关，直接续接为保持
        b = self._block_due(car, env.now)
        if b is not None and car.floor == b.floor:
            boarded = self._board_one_batch(car)
            return True, boarded

        boarded = self._board_one_batch(car)
        self.log(env.now, "doors_close", car=car.id, floor=f,
                 load=len(car.aboard))
        car.load_profile.append((env.now, len(car.aboard)))
        # 本车服务完毕（满载或不服务该方向）后，若队列仍有人，
        # 释放全局指派以便其它轿厢重新认领（避免呼叫被永久绑死一车）
        call = self.calls.get((f, car.dir)) if car.dir != IDLE else None
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
        return False, boarded

    def _open_door_and_alight(self, car: Car) -> None:
        """开门并完成先下后上中的"下客"，门保持打开。"""
        env = self.env
        f = car.floor
        yield env.timeout(self.door_time)
        self.log(env.now, "doors_open", car=car.id, floor=f,
                 load=len(car.aboard), door_time=self.door_time)
        # 让出一个零时刻：阻挡若恰在开门瞬间武装，door_block_arm 先落日志
        yield env.timeout(0)
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

    def _board_one_batch(self, car: Car) -> list[Pax]:
        """门开状态下的一批 FIFO 登乘（仅当前接梯方向，受容量约束）。"""
        env = self.env
        f = car.floor
        boarded: list[Pax] = []
        call = self.calls.get((f, car.dir)) if car.dir != IDLE else None
        if call is not None and call.is_active() \
                and self.policy.claim_hall(car, call):
            while len(car.aboard) < self.capacity:
                p = call.head()
                if p is None or not self._eligible(car, p):
                    break
                call.queue.remove(p)
                car.aboard.append(p)
                boarded.append(p)
                p.status = PaxStatus.ABOARD
                p.car_id = car.id
                p.board_time = env.now
                p.location = ("car", car.id)
                self.log(env.now, "pax_board", pid=p.pid, car=car.id,
                         floor=f, dest=p.dest, wanted=p.wanted,
                         wait_time=round(env.now - p.arrival_time, 4),
                         load=len(car.aboard), location=f"car:{car.id}")
            if len(car.aboard) >= self.capacity and call.is_active():
                # 满载：未上车者保留在原队列（阻挡期间也不得再登乘）
                left = [q for q in call.queue if q.status == PaxStatus.WAITING]
                self.log(env.now, "car_full", car=car.id, floor=f,
                         left_waiting=True, queue_len=len(left))
        return boarded


    def _eligible(self, car: Car, p: Pax) -> bool:
        """乘客能否上本车：轿厢必须朝乘客目的层方向扫描。"""
        return (car.dir != IDLE
                and (p.dest - p.origin) * car.dir > 0)

    # ---------- 运行 ----------
    def run(self) -> dict:
        for car in self.cars:
            self.env.process(self.car_process(car))
        self.env.process(self.passenger_source())
        if self.blocks:
            self.env.process(self.block_monitor())
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

        # 阻挡配置了却未在仿真中执行（理论上不会，end ≤ until 必定武装）
        fired = {imp["block_id"] for imp in self.block_impact}
        for b in self.blocks:
            if b.block_id not in fired:
                self.block_impact.append({
                    "block_id": b.block_id, "car": b.car,
                    "requested_floor": b.floor, "actual_floor": None,
                    "floor_mismatch": True,
                    "start": b.start, "end": b.end,
                    "enter_t": None, "release_t": None,
                    "boarded_pids": [], "aboard_pids": [],
                    "stranded_pids": [], "affected_pids": [],
                    "load_at_release": None, "not_executed": True,
                })

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

        summary = {
            "label": self.label,
            "seed": self.seed,
            "demand": len(self.pax),
            "served": len(served),
            "unserved": len(unserved),
            "completion_rate": round(completion, 4),
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
        if self.blocks:
            summary["blockages"] = self._blockage_summary(pct)
        return summary

    def _blockage_summary(self, pct) -> dict:
        """阻挡事件汇总与"受影响乘客"子口径（与全客流口径并列，不替换）。"""
        affected_pids = sorted({
            pid for imp in self.block_impact for pid in imp["affected_pids"]})
        by_id = {p.pid: p for p in self.pax}
        affected = [by_id[i] for i in affected_pids if i in by_id]
        end = self.until

        def wait_of(p: Pax) -> float:
            if p.board_time is not None:
                return p.board_time - p.arrival_time
            return end - p.arrival_time

        def total_of(p: Pax) -> float:
            if p.alight_time is not None:
                return p.alight_time - p.arrival_time
            return end - p.arrival_time

        waits = [wait_of(p) for p in affected]
        totals = [total_of(p) for p in affected]
        return {
            "configured": [b.to_dict() for b in self.blocks],
            "events": self.block_impact,
            "affected_pids": affected_pids,
            "affected_count": len(affected),
            "affected_served": sum(1 for p in affected
                                   if p.status == PaxStatus.DONE),
            "affected_unserved": sum(1 for p in affected
                                     if p.status == PaxStatus.UNSERVED),
            # 受影响乘客口径：含被挡期间梯内、本批登乘、留在本层候梯的人
            "affected_metrics": {
                "wait_mean": round(sum(waits) / len(waits), 3) if waits else None,
                "wait_p90": pct(waits, 0.9),
                "total_mean": round(sum(totals) / len(totals), 3) if totals else None,
                "total_p90": pct(totals, 0.9),
            },
        }
