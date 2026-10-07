"""三种派梯策略，统一实现 Policy 接口：

1. collective  传统集选（SCAN/电梯算法）：无全局指派，每台梯沿单一方向扫描
                直到该方向再无内呼/同向外呼，再反向；空车可空载(deadhead)
                去任意方向的呼叫，到层后换向开门。多台梯可重复响应同一呼梯。

2. nearest     最近梯调度（ETA 最小者）：新外呼全局指派给预估到达时间最小的
                轿厢；指派后仅该车在该层停站，避免重复停站。

3. zoning      分区调度：楼分高/低区，各车只接本区呼叫（含内呼进入本区）；
                本区车满载时才越区支援。

公平性：三者物理参数（速度、容量、门时）完全相同；只改决策钩子。
"""
from __future__ import annotations

from .engine import Building, Car
from .models import HallCall, UP, DOWN, IDLE


def _visible_calls(car: Car, claimed_only: bool) -> list[HallCall]:
    calls = car.b.active_calls()
    if claimed_only:
        calls = [c for c in calls
                 if c.claimed_by == car.id
                 or car.id in car.b.multi_claims.get((c.floor, c.direction),
                                                     set())]
    return calls


def _service_targets(car: Car, calls: list[HallCall], d: int) -> list[int]:
    """沿 d 方向扫描需要到达的楼层（内呼 + 同向外呼）。"""
    f = car.floor
    bs = [p.dest for p in car.aboard if (p.dest - f) * d > 0]
    for c in calls:
        head = c.head()
        if head is None:
            continue
        if (c.floor - f) * d > 0 and c.direction == d:
            bs.append(c.floor)
    return bs


def _decide(car: Car, claimed_only: bool) -> tuple[int, int | None]:
    """SCAN 方向/边界决策。

    - 载客车：按当前方向扫描内呼/同向外呼，扫干净再反向；
    - 空车：可去任意呼叫（含反向外呼，到层后换向接人），选最近者；
    - 同层同向外呼：立即在本层开门。
    """
    f = car.floor
    calls = _visible_calls(car, claimed_only)
    empty = len(car.aboard) == 0

    def same_floor_dir() -> int | None:
        for c in calls:
            head = c.head()
            if head is None or c.floor != f:
                continue
            if (head.dest - head.origin) * c.direction > 0:
                return c.direction
        return None

    def bound_after_board(d: int, same_floor: bool) -> int | None:
        bs = _service_targets(car, calls, d)
        if same_floor:
            # 本层接客后，最远同向外呼/队首目的层延伸边界
            for c in calls:
                head = c.head()
                if head is not None and c.floor == f and c.direction == d:
                    bs.append(head.dest)
        return (max(bs) if d == UP else min(bs)) if bs else None

    # 1) 本层就有可接的同方向呼叫：立即开门
    sd = same_floor_dir()
    if sd is not None and (empty or sd == car.dir):
        return sd, bound_after_board(sd, same_floor=True)

    # 2) 载客：延续当前方向，扫完再反（只计同向外呼与内呼）
    if not empty:
        if car.dir != IDLE:
            bs = _service_targets(car, calls, car.dir)
            if bs:
                return car.dir, (max(bs) if car.dir == UP else min(bs))
            opp = -car.dir
            bo = _service_targets(car, calls, opp)
            if bo:
                return opp, (max(bo) if opp == UP else min(bo))
            return IDLE, None

    # 3) 空车：选最近呼叫；同方向优先仅在等距时
    best: tuple[int, int, int, int] | None = None  # 距离, 同向偏好, dir, bound层
    for c in calls:
        head = c.head()
        if head is None:
            continue
        if c.floor == f:
            continue  # 同层但方向已在步骤1处理
        dist = abs(c.floor - f)
        d = UP if c.floor > f else DOWN   # 去接人的行驶方向
        pref = 0 if (car.dir != IDLE and d == car.dir) else 1
        key = (dist, pref)
        if best is None or key < best[:2]:
            best = (dist, pref, d, c.floor)
    if best is not None:
        d, target_floor = best[2], best[3]
        return d, target_floor
    return IDLE, None


class _BasePolicy:
    claimed_only = False

    def plan(self, car: Car) -> tuple[int, int | None]:
        return _decide(car, self.claimed_only)

    def board_dir(self, car: Car, floor: int) -> int | None:
        """开门时按哪个方向组织上车：已有内呼方向优先；空车则可换向接反向客。"""
        b = car.b
        for wanted in (car.dir if car.dir != IDLE else UP,
                       -car.dir if car.dir != IDLE else DOWN):
            call = b.calls.get((floor, wanted))
            if call is not None and call.is_active() and self.claim_hall(car, call):
                head = call.head()
                if head is not None and (head.dest - head.origin) * wanted > 0:
                    return wanted
        return None


# ---------------- 1. 集选（无全局指派） ----------------
class CollectivePolicy(_BasePolicy):
    name = "collective"
    display_name = "集选 SCAN（无协调）"
    claimed_only = False

    def should_stop(self, car: Car, f: int) -> bool:
        b = car.b
        if any(p.dest == f for p in car.aboard):
            return True
        empty = len(car.aboard) == 0
        # 空车 deadhead 到层时要能为反向外呼开门，故空车双向检查
        dirs = (car.dir, -car.dir) if (car.dir != IDLE and empty) \
            else ((car.dir,) if car.dir != IDLE else (UP, DOWN))
        for d in dirs:
            call = b.calls.get((f, d))
            if call is None or not call.is_active():
                continue
            head = call.head()
            if head is None:
                continue
            if (head.dest - head.origin) * d > 0 and (d == car.dir or empty):
                return True
        return False

    def claim_hall(self, car: Car, call: HallCall) -> bool:
        # 任何车都可现场认领；不阻止他车后续再停（无协调对照组）
        return True

    def on_tick(self, b: Building) -> None:
        pass


class _AssignedPolicy(_BasePolicy):
    """nearest / zoning 共用：只停/接全局指派给本车的呼叫。"""
    claimed_only = True

    def _owns(self, car: Car, call: HallCall) -> bool:
        if call.claimed_by == car.id:
            return True
        return car.id in car.b.multi_claims.get(
            (call.floor, call.direction), set())

    def should_stop(self, car: Car, f: int) -> bool:
        b = car.b
        if any(p.dest == f for p in car.aboard):
            return True
        empty = len(car.aboard) == 0
        dirs = (car.dir, -car.dir) if (car.dir != IDLE and empty) \
            else ((car.dir,) if car.dir != IDLE else (UP, DOWN))
        for d in dirs:
            call = b.calls.get((f, d))
            if call is None or not call.is_active():
                continue
            if not self._owns(car, call):
                continue
            head = call.head()
            if head is None:
                continue
            if (head.dest - head.origin) * d > 0 and (d == car.dir or empty):
                return True
        return False

    def claim_hall(self, car: Car, call: HallCall) -> bool:
        return self._owns(car, call)


# ---------------- 2. 最近梯（ETA 指派） ----------------
class NearestPolicy(_AssignedPolicy):
    name = "nearest"
    display_name = "最近梯 ETA 指派"

    def on_tick(self, b: Building) -> None:
        # 先释放失效认领
        for call in b.active_calls():
            if call.claimed_by is not None:
                owner = b.cars[call.claimed_by]
                if len(owner.aboard) >= b.capacity:
                    ahead = (call.floor - owner.floor) * (owner.dir or 0) > 0
                    if not ahead:
                        b.log(b.env.now, "call_release", floor=call.floor,
                               dir=call.direction, car=owner.id, reason="full",
                               queue_len=len(call.queue))
                        call.claimed_by = None
                        call.assign_time = None

        # 空闲（或空载）轿厢主动补位：把剩余队列最长的呼叫指给最近的空车，
        # 使大堂式长队列可由多梯并发响应（标准 ETA 调度的并发语义）
        idle_cars = [c for c in b.cars
                     if len(c.aboard) == 0 and c.dir == IDLE]
        for car in sorted(idle_cars, key=lambda c: c.id):
            options = []
            for call in b.active_calls():
                waiting = sum(1 for q in call.queue
                              if q.status.name == "WAITING")
                if waiting <= 0:
                    continue
                eta = self._eta(b, car, call.floor)
                # 已被其它车认领的队列，只有剩余人数仍多（>容量一半）才补第二辆
                claimed = call.claimed_by is not None and call.claimed_by != car.id
                if claimed and waiting <= b.capacity // 2:
                    continue
                options.append((eta, -waiting, call))
            if options:
                options.sort(key=lambda x: (x[0], x[1]))
                target = options[0][2]
                if target.claimed_by is None:
                    target.claimed_by = car.id
                    target.assign_time = b.env.now
                    b.log(b.env.now, "call_assign", floor=target.floor,
                           dir=target.direction, car=car.id,
                           queue_len=len(target.queue), policy=self.name)
                else:
                    # 长队列追加补位车（多车并发响应同一呼梯）
                    b.multi_claims.setdefault(
                        (target.floor, target.direction), set()
                    ).add(car.id)
                    b.log(b.env.now, "call_support", floor=target.floor,
                           dir=target.direction, car=car.id,
                           queue_len=len(target.queue), policy=self.name)

        # 常规：未认领呼叫指派给 ETA 最小者
        for call in b.active_calls():
            if call.claimed_by is not None:
                continue
            best = self._best_car(b, call)
            if best is not None:
                call.claimed_by = best.id
                call.assign_time = b.env.now
                b.log(b.env.now, "call_assign", floor=call.floor,
                       dir=call.direction, car=best.id,
                       queue_len=len(call.queue), policy=self.name)

    @staticmethod
    def _eta(b: Building, car: Car, floor: int) -> float:
        """ETA = 行驶时间 + 沿途停站门时 + 排队前瞻惩罚。

        排队前瞻：大堂式长队列需要多辆车，只按"未满员"派一辆会饿死队列，
        故加入 (车内人数 + 该车已认领的候梯人数)/容量 的负载项，
        让空闲车也愿意共同响应长队列。
        """
        cf, cd = car.floor, car.dir
        queued = 0
        for c in b.active_calls():
            if c.claimed_by == car.id:
                queued += sum(1 for q in c.queue
                              if q.status.name == "WAITING")
        load_penalty = 6.0 * (len(car.aboard) + queued) / b.capacity

        if cd == IDLE or not car.aboard:
            return abs(floor - cf) * b.floor_time + b.door_time + load_penalty
        if (floor - cf) * cd >= 0:
            lo, hi = sorted((cf, floor))
            stops = sum(1 for p in car.aboard if lo <= p.dest <= hi)
            stops += sum(1 for c in b.active_calls()
                         if c.claimed_by == car.id and lo <= c.floor <= hi)
            return abs(floor - cf) * b.floor_time + stops * b.door_time + load_penalty
        turn = cf
        for p in car.aboard:
            turn = max(turn, p.dest) if cd == UP else min(turn, p.dest)
        for c in b.active_calls():
            if c.claimed_by == car.id:
                turn = max(turn, c.floor) if cd == UP else min(turn, c.floor)
        return ((abs(turn - cf) + abs(floor - turn)) * b.floor_time
                + 3 * b.door_time + load_penalty)

    def _best_car(self, b: Building, call: HallCall) -> Car | None:
        candidates = list(b.cars)  # 满载车也可参与（前瞻），但 ETA 惩罚更高
        if not candidates:
            return None
        candidates.sort(key=lambda c: (self._eta(b, c, call.floor), c.id))
        return candidates[0]


# ---------------- 3. 分区调度 ----------------
class ZoningPolicy(_AssignedPolicy):
    name = "zoning"
    display_name = "高低区分区"

    def _zones(self, b: Building) -> dict[int, tuple[int, int]]:
        """car_id -> (低界, 高界)，含层。n 梯时按层均分。"""
        n, F = b.car_count, b.floors
        if n == 1:
            return {0: (1, F)}
        per = max(1, F // n)
        zones: dict[int, tuple[int, int]] = {}
        for i in range(n):
            lo = 1 + i * per
            hi = F if i == n - 1 else min(F, lo + per - 1)
            zones[i] = (lo, hi)
        return zones

    def _zone_of(self, b: Building, floor: int) -> int | None:
        for cid, (lo, hi) in self._zones(b).items():
            if lo <= floor <= hi:
                return cid
        return None

    def on_tick(self, b: Building) -> None:
        for call in b.active_calls():
            if call.claimed_by is not None:
                owner = b.cars[call.claimed_by]
                if len(owner.aboard) >= b.capacity:
                    ahead = (call.floor - owner.floor) * owner.dir > 0
                    helpers = [c for c in b.cars
                               if c.id != owner.id and len(c.aboard) < b.capacity]
                    if not ahead and helpers:
                        b.log(b.env.now, "call_release", floor=call.floor,
                               dir=call.direction, car=owner.id, reason="full",
                               queue_len=len(call.queue))
                        call.claimed_by = None
                        call.assign_time = None
            if call.claimed_by is not None:
                continue
            owner = self._zone_of(b, call.floor)
            chosen: Car | None = None
            if owner is not None and len(b.cars[owner].aboard) < b.capacity:
                chosen = b.cars[owner]
            if chosen is None:
                # 本区车满载：未满员车越区支援，近者优先
                free = [c for c in b.cars if len(c.aboard) < b.capacity]
                free.sort(key=lambda c: (abs(c.floor - call.floor), c.id))
                chosen = free[0] if free else None
            if chosen is not None:
                call.claimed_by = chosen.id
                call.assign_time = b.env.now
                b.log(b.env.now, "call_assign", floor=call.floor,
                       dir=call.direction, car=chosen.id,
                       queue_len=len(call.queue),
                       zone_owner=owner, policy=self.name)


POLICIES = {
    CollectivePolicy.name: CollectivePolicy,
    NearestPolicy.name: NearestPolicy,
    ZoningPolicy.name: ZoningPolicy,
}
