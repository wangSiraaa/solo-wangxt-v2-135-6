"""可配置的"门保持打开（阻挡）"事件。

事件语义（确定性，所有策略一致）：
- 每个事件指定一台轿厢 car、一个楼层 floor 与一个半开时间窗 [start, end]；
- start 时刻事件"武装（armed）"：该轿厢不再接受新的外呼指派；
  若轿厢正在两层之间移动，允许当前这一层移动走完（位置不跳变），
  随后在到达的楼层进入阻挡；若轿厢正处在一次正常停站中，则该停站
  （先下后上，仅一批登乘）完整结束后再进入阻挡；
- 进入阻挡时若门未开，执行一次与普通停站完全相同的开门—下客—一批上客，
  随后记录 door_block_start：门持续保持打开，直到 end 才允许关门；
- **阻挡期间（door_block_start → door_block_end）**：
  轿厢不能移动、不能重复登乘（新到乘客留在候梯队列）、已在车上的乘客
  位置不变；其他轿厢完全不受影响，继续按各自策略运行；
- end 时刻记录 door_block_end 并关门，轿厢立即按原策略继续服务。

配置楼层与实际进入阻挡时的楼层不一致（例如 start 落在移动中途）时，
阻挡在轿厢实际所在楼层执行，door_block_start 事件带 floor_mismatch=true，
verifier 产生告警但不算物理错误；推荐做法是在基准重放时间轴上取点配置，
由于有/无阻挡使用同一份到达序列，start 之前仿真完全一致，配置楼层必然吻合。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class DoorBlock:
    block_id: int
    car: int
    floor: int
    start: float
    end: float

    def to_dict(self) -> dict:
        return {"block_id": self.block_id, "car": self.car,
                "floor": self.floor, "start": self.start, "end": self.end}


def normalize_blocks(blocks, *, floors: int, car_count: int,
                     until: float) -> list[DoorBlock]:
    """校验并归一化阻挡配置。blocks: dict 列表或 DoorBlock 列表。"""
    out: list[DoorBlock] = []
    seen: set[tuple[int, int]] = set()
    for i, raw in enumerate(blocks or []):
        b = raw if isinstance(raw, DoorBlock) else DoorBlock(
            block_id=i, car=int(raw["car"]), floor=int(raw["floor"]),
            start=float(raw["start"]), end=float(raw["end"]))
        if not (0 <= b.car < car_count):
            raise ValueError(f"阻挡事件 {i}: 轿厢编号 {b.car} 越界"
                             f"（共 {car_count} 台，编号 0..{car_count - 1}）")
        if not (1 <= b.floor <= floors):
            raise ValueError(f"阻挡事件 {i}: 楼层 {b.floor} 越界（1..{floors}）")
        if not (0.0 <= b.start < b.end):
            raise ValueError(f"阻挡事件 {i}: 需要 0 ≤ start < end"
                             f"（得到 start={b.start}, end={b.end}）")
        if b.end > until + 1e-9:
            raise ValueError(f"阻挡事件 {i}: end={b.end} 晚于仿真截止 {until}")
        key = (b.car, i)
        # 同一轿厢的时间窗不允许重叠
        for other in out:
            if other.car == b.car and b.start < other.end and other.start < b.end:
                raise ValueError(
                    f"阻挡事件 {i} 与轿厢 {b.car} 的另一阻挡时间窗重叠")
        seen.add(key)
        out.append(b)
    out.sort(key=lambda b: (b.start, b.car))
    return out
