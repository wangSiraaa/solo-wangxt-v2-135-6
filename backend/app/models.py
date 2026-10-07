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
