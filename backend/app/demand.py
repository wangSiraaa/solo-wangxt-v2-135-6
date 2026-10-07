"""客流案例：同一批客流在不同策略下复用，保证对照公平。

三类案例（题目要求）：
- morning_up   早高峰上行：大堂/低层 → 办公层，泊松到达
- cross_floor  跨层需求：上、下行混流，含中间层互访
- long_door    长开门时间：沿用跨层客流，仅放大 door_time

随机种子决定泊松到达间隔与楼层抽样；同 seed + 同案例 → 完全相同的到达序列。
"""
from __future__ import annotations

import random
from typing import Callable

Arrival = tuple[float, int, int]


def _poisson_series(rng: random.Random, rate_per_min: float,
                    duration: float, t0: float = 0.0) -> list[float]:
    """生成 [t0, t0+duration) 内泊松到达时刻。rate_per_min: 人/分钟。"""
    out: list[float] = []
    t = t0
    beta = 60.0 / rate_per_min
    while True:
        t += rng.expovariate(1.0 / beta)
        if t >= t0 + duration:
            break
        out.append(round(t, 3))
    return out


def morning_peak(*, floors: int, seed: int = 1, duration: float = 900.0,
                 rate_per_min: float = 18.0,
                 lobby_ratio: float = 0.75) -> list[Arrival]:
    """早高峰：约 75% 从 1 层上行，其余从低区(2..mid)去更高层，全部上行。"""
    rng = random.Random(seed)
    times = _poisson_series(rng, rate_per_min, duration)
    mid = max(3, floors // 3)
    arr: list[Arrival] = []
    for t in times:
        if rng.random() < lobby_ratio:
            o = 1
        else:
            o = rng.randint(2, mid)
        hi = max(o + 1, floors)
        d = rng.randint(max(o + 1, mid + 1), floors) if o <= mid else rng.randint(o + 1, floors)
        d = min(floors, max(o + 1, d))
        arr.append((t, o, d))
    return arr


def cross_floor(*, floors: int, seed: int = 2, duration: float = 1200.0,
                rate_per_min: float = 10.0) -> list[Arrival]:
    """跨层混流：上行下行约各半，楼层均匀抽样，含长距离与相邻层行程。"""
    rng = random.Random(seed)
    times = _poisson_series(rng, rate_per_min, duration)
    arr: list[Arrival] = []
    for t in times:
        o, d = rng.sample(range(1, floors + 1), 2)
        arr.append((t, o, d))
    return arr


def long_door_flow(*, floors: int, seed: int = 2, **kw) -> list[Arrival]:
    """长开门案例：与跨层案例同一客流（默认同 seed=2），只改门时。"""
    return cross_floor(floors=floors, seed=seed, **kw)


SCENARIOS: dict[str, Callable[..., list[Arrival]]] = {
    "morning_up": morning_peak,
    "cross_floor": cross_floor,
    "long_door": long_door_flow,
}

# 案例默认建筑参数（可在 API 中覆盖）
SCENARIO_DEFAULTS: dict[str, dict] = {
    "morning_up": {
        "floors": 12, "car_count": 2, "capacity": 8,
        "floor_time": 2.0, "door_time": 4.0,
        "until": 1500.0,
        "rate_per_min": 20.0, "duration": 900.0,
        "desc": "早高峰上行：大堂→办公层，泊松到达",
    },
    "cross_floor": {
        "floors": 12, "car_count": 2, "capacity": 8,
        "floor_time": 2.0, "door_time": 4.0,
        "until": 1800.0,
        "rate_per_min": 12.0, "duration": 1200.0,
        "desc": "跨层需求：上下行混流、中间层互访",
    },
    "long_door": {
        "floors": 12, "car_count": 2, "capacity": 8,
        "floor_time": 2.0, "door_time": 12.0,   # 3 倍长开门
        "until": 2400.0,
        "rate_per_min": 12.0, "duration": 1200.0,
        "desc": "长开门时间：同跨层客流，门时 4s→12s",
    },
}


def build_arrivals(case: str, *, floors: int, seed: int,
                   rate_per_min: float | None = None,
                   duration: float | None = None) -> list[Arrival]:
    fn = SCENARIOS[case]
    kw = {"floors": floors, "seed": seed}
    if rate_per_min is not None:
        kw["rate_per_min"] = rate_per_min
    if duration is not None:
        kw["duration"] = duration
    return fn(**kw)
