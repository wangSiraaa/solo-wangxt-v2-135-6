"""核心仿真数据结构（不依赖 SimPy 之外的框架）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import IntEnum

UP = 1
DOWN = -1
IDLE = 0


class PaxStatus(IntEnum):
    WAITING = 1     # 在候梯厅队列中，物理位置 = 起始层
    ABOARD = 2      # 在某台轿厢内
    DONE = 3        # 已到达目的层离开系统
    UNSERVED = 4    # 仿真结束仍在系统内（候梯或被困在梯内未到达）


@dataclass
class Pax:
    pid: int
    origin: int
    dest: int
    arrival_time: float
    wanted: int = field(init=False)           # UP / DOWN
    status: PaxStatus = PaxStatus.WAITING
    car_id: int | None = None
    board_time: float | None = None
    alight_time: float | None = None
    # 每一时刻物理位置由事件日志重建，这里保留当前快照（引擎校验用）
    location: tuple[str, int] = field(init=False)  # ("hall", floor) / ("car", car_id) / ("outside", dest)

    def __post_init__(self) -> None:
        assert self.origin != self.dest, f"乘客 {self.pid} 起终层相同"
        self.wanted = UP if self.dest > self.origin else DOWN
        self.location = ("hall", self.origin)


@dataclass
class DoorHold:
    """门阻挡事件：乘客在 [start, start+duration) 窗口内挡住指定楼层的门。

    窗口内第一台在该楼层完成正常开关门周期、正欲关门的轿厢被"挡住"：
    关门推迟到窗口结束。阻挡期间该轿厢不能移动、不能再次登乘；
    事件只触发一次（一台轿厢），其它轿厢按各自策略照常运行。
    """
    floor: int
    start: float
    duration: float
    consumed: bool = False          # 是否已命中某台轿厢（只触发一次）
    car_id: int | None = None       # 被挡轿厢
    hold_start: float | None = None  # 实际生效时刻（= 本应关门的时刻）
    released: bool = False          # 是否已记录解除（仿真截止前解除才算）

    @property
    def end(self) -> float:
        return self.start + self.duration


@dataclass
class HallCall:
    """一层、一方向上的候梯队列（FIFO）。

    claimed_by: nearest/zoning 策略下被调度器整队指派给的轿厢；
                collective 策略恒为 None，轿厢到场时自行认领。
    assign_time: 被指派的时刻（用于事件日志）。
    """
    floor: int
    direction: int
    queue: list[Pax] = field(default_factory=list)
    claimed_by: int | None = None
    assign_time: float | None = None

    def is_active(self) -> bool:
        return any(p.status == PaxStatus.WAITING for p in self.queue)

    def head(self) -> Pax | None:
        """FIFO 队首仍在等候的乘客；已被他车独占的人不可被本车接走。"""
        for p in self.queue:
            if p.status == PaxStatus.WAITING:
                return p
        return None
