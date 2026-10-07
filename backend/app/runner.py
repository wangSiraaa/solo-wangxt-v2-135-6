"""把"客流 + 建筑参数 + 策略"组装成一次仿真并校验。"""
from __future__ import annotations

from .blockages import normalize_blocks
from .demand import build_arrivals
from .engine import Building
from .models import PaxStatus
from .policies import POLICIES
from .verifier import verify


def run_simulation(*, case: str, policy_name: str,
                   floors: int, car_count: int, capacity: int,
                   floor_time: float, door_time: float,
                   seed: int, rate_per_min: float, duration: float,
                   until: float, label: str = "",
                   arrivals_override: list | None = None,
                   blocks_raw: list | None = None) -> dict:
    if policy_name not in POLICIES:
        raise ValueError(f"未知策略 {policy_name}，可选 {list(POLICIES)}")

    if arrivals_override is not None:
        arrivals = arrivals_override
    else:
        arrivals = build_arrivals(
            case, floors=floors, seed=seed,
            rate_per_min=rate_per_min, duration=duration)

    blocks = normalize_blocks(blocks_raw, floors=floors,
                              car_count=car_count, until=until)

    policy = POLICIES[policy_name]()
    b = Building(
        floors=floors, car_count=car_count, capacity=capacity,
        floor_time=floor_time, door_time=door_time,
        seed=seed, until=until, policy=policy,
        label=label or f"{case}/{policy_name}",
        blocks=blocks,
    )
    b.set_arrivals(arrivals)
    metrics = b.run()

    pax_rows = [_pax_row(p, until) for p in b.pax]
    pax_for_verify = [{"status": p.status} for p in b.pax]
    warnings = verify(b.events, pax_for_verify,
                      capacity=capacity, floors=floors, until=until,
                      blocks=blocks)
    checks = [
        "每名乘客任意时刻唯一物理位置(hall/car/outside)",
        "上下客位置与轿厢楼层一致、方向约束",
        "轿厢容量上限", "轿厢逐层移动、开门不动",
        "状态链 arrive→board→alight 无重复/无消失",
        "日志完成人数与乘客表一致",
    ]
    if blocks:
        checks += [
            "阻挡期间轿厢不移动、不关门、不重复登乘",
            "阻挡开始/解除事件与配置窗一一对应，位置不跳变",
        ]
    verification = {
        "ok": True,
        "warnings": warnings,
        "checks": checks,
        "final_locations": _final_locations(b),
    }

    return {
        "metrics": metrics,
        "events": b.events,
        "passengers": pax_rows,
        "arrivals": [{"arrival_time": a[0], "origin": a[1], "dest": a[2]}
                     for a in arrivals],
        "config": {
            "case": case, "policy": policy_name,
            "floors": floors, "car_count": car_count,
            "capacity": capacity, "floor_time": floor_time,
            "door_time": door_time, "seed": seed,
            "rate_per_min": rate_per_min, "duration": duration,
            "until": until,
            "blocks": [x.to_dict() for x in blocks],
        },
        "verification": verification,
    }


def _pax_row(p, until: float) -> dict:
    wait = (p.board_time - p.arrival_time) if p.board_time is not None \
        else until - p.arrival_time
    ride = ((p.alight_time - p.board_time)
            if p.board_time is not None and p.alight_time is not None
            else ((until - p.board_time) if p.board_time is not None else None))
    total = ((p.alight_time - p.arrival_time)
             if p.alight_time is not None else until - p.arrival_time)
    return {
        "pid": p.pid, "origin": p.origin, "dest": p.dest,
        "arrival_time": p.arrival_time,
        "board_time": p.board_time, "alight_time": p.alight_time,
        "car_id": p.car_id, "status": int(p.status),
        "wait_time": round(wait, 4),
        "ride_time": round(ride, 4) if ride is not None else None,
        "total_time": round(total, 4),
    }


def _final_locations(b: Building) -> dict:
    out = {"hall": 0, "car": 0, "outside": 0}
    for p in b.pax:
        kind = p.location[0]
        out[kind] = out.get(kind, 0) + 1
    return out
