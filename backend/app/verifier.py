"""从事务事件日志重建物理世界，逐条校验不变量。

核心不变量：
1. 位置唯一：任意时刻，每名乘客恰好处于一个物理位置
   （候梯厅某层 / 某台轿厢内 / 已离开系统），事件驱动的位置迁移合法；
2. 位置合法：pax_board 之前必须在同层候梯；pax_alight 之前必须在该梯内；
   目的层必须与行程方向一致（轿厢接客方向约束）；
3. 容量不超限：任何 pax_board 后车内人数 ≤ capacity；
4. 轿厢移动合法：每次 car_move 只跨一层；开关门期间不移动；
5. 每名乘客状态链唯一：arrive → board* → alight 至多各一次
   （未服务者只能停在 arrive 或 board 之后），无重复服务、无凭空消失；
6. 指标与日志一致：served 数 = pax_alight 去重 pid 数。
"""
from __future__ import annotations

from collections import defaultdict

from .models import PaxStatus


class VerificationError(Exception):
    pass


def verify(events: list[dict], pax_records: list[dict],
           capacity: int, floors: int, until: float) -> list[str]:
    """返回告警列表；发现违反硬不变量时抛 VerificationError。"""
    warnings: list[str] = []

    # pid -> 当前 ("hall", floor) / ("car", car_id) / ("outside", floor) / None
    loc: dict[int, tuple[str, int]] = {}
    seen_arrive: set[int] = set()
    seen_board: set[int] = set()
    seen_alight: set[int] = set()
    car_load: dict[int, int] = defaultdict(int)
    car_floor: dict[int, int] = {}
    car_door_open: dict[int, bool] = defaultdict(bool)
    last_t = -1.0

    for e in events:
        t = e["t"]
        if t < last_t - 1e-9:
            raise VerificationError(f"事件时间倒流: {e}")
        last_t = t
        typ = e["type"]
        pid = e.get("pid")

        if typ == "pax_arrive":
            if pid in seen_arrive:
                raise VerificationError(f"乘客 {pid} 重复到达")
            seen_arrive.add(pid)
            if pid in loc and loc[pid] is not None:
                raise VerificationError(f"乘客 {pid} 到达时已有物理位置 {loc[pid]}")
            loc[pid] = ("hall", e["origin"])
            _check_loc_field(e, f"hall:{e['origin']}", pid, loc)

        elif typ == "pax_board":
            if pid not in seen_arrive:
                raise VerificationError(f"乘客 {pid} 未到达即上车")
            if pid in seen_alight:
                raise VerificationError(f"乘客 {pid} 已离开又上车")
            cur = loc.get(pid)
            if cur is None or cur[0] != "hall" or cur[1] != e["floor"]:
                raise VerificationError(
                    f"乘客 {pid} 上车位置非法: 当前 {cur}, 事件楼层 {e['floor']}")
            car = e["car"]
            if car_floor.get(car) != e["floor"]:
                raise VerificationError(
                    f"乘客 {pid} 上 {car} 号梯时轿厢不在 {e['floor']} 层")
            # 方向约束：轿厢扫描方向必须把乘客送往目的层
            if not ((e["dest"] - e["floor"]) * e.get("wanted", 0) > 0):
                raise VerificationError(f"乘客 {pid} 外呼方向与目的层矛盾")
            if pid in seen_board:
                raise VerificationError(f"乘客 {pid} 重复上车")
            seen_board.add(pid)
            loc[pid] = ("car", car)
            car_load[car] += 1
            if car_load[car] > capacity:
                raise VerificationError(
                    f"{car} 号梯超载: {car_load[car]} > {capacity} (乘客 {pid})")
            _check_loc_field(e, f"car:{car}", pid, loc)

        elif typ == "pax_alight":
            cur = loc.get(pid)
            if cur is None or cur[0] != "car" or cur[1] != e["car"]:
                raise VerificationError(
                    f"乘客 {pid} 下客位置非法: 当前 {cur}")
            if e["floor"] != e.get("floor"):
                pass
            if car_floor.get(e["car"]) != e["floor"]:
                raise VerificationError(
                    f"乘客 {pid} 下 {e['car']} 号梯时轿厢不在 {e['floor']} 层")
            seen_alight.add(pid)
            loc[pid] = ("outside", e["floor"])
            car_load[e["car"]] -= 1
            if car_load[e["car"]] < 0:
                raise VerificationError(f"{e['car']} 号梯车内人数为负")
            _check_loc_field(e, f"outside:{e['floor']}", pid, loc)

        elif typ == "car_move":
            car = e["car"]
            prev = car_floor.get(car, e["floor"] - e.get("dir", 0))
            if abs(e["floor"] - prev) != 1:
                raise VerificationError(
                    f"{car} 号梯从 {prev} 跳到 {e['floor']}（必须逐层）")
            if not (1 <= e["floor"] <= floors):
                raise VerificationError(f"{car} 号梯越出井道: {e['floor']}")
            if car_door_open[car]:
                raise VerificationError(f"{car} 号梯开门状态下移动")
            car_floor[car] = e["floor"]

        elif typ == "doors_open":
            car = e["car"]
            car_floor.setdefault(car, e["floor"])
            if car_floor[car] != e["floor"]:
                raise VerificationError(f"开门事件楼层与轿厢位置不符: {e}")
            car_door_open[car] = True

        elif typ == "doors_close":
            car_door_open[e["car"]] = False

        elif typ == "pax_unserved":
            # 仿真结束快照：位置必须仍是候梯厅或轿厢
            cur = loc.get(pid)
            if cur is None:
                raise VerificationError(f"未服务乘客 {pid} 无位置记录")
            if e["where"] == "hall" and cur[0] != "hall":
                raise VerificationError(
                    f"未服务乘客 {pid} 标注候梯但实际位置 {cur}")
            if e["where"] == "car" and cur[0] != "car":
                raise VerificationError(
                    f"未服务乘客 {pid} 标注梯内但实际位置 {cur}")
            _check_loc_field(e, e.get("location"), pid, loc)

    # 终态：每名有到达记录的乘客恰好有一个终态位置
    for pid in seen_arrive:
        if pid not in loc or loc[pid] is None:
            raise VerificationError(f"乘客 {pid} 终态无位置（凭空消失）")

    # 与持久化乘客记录对账
    served_log = len(seen_alight)
    served_db = sum(1 for p in pax_records
                    if p.get("status") == PaxStatus.DONE)
    if served_log != served_db:
        raise VerificationError(
            f"完成服务人数不一致: 日志 {served_log} vs 乘客表 {served_db}")
    total = len(pax_records)
    if total != len(seen_arrive):
        raise VerificationError(
            f"客流人数不一致: 日志到达 {len(seen_arrive)} vs 乘客表 {total}")

    # 容量事件后载荷与重算一致（抽查 car_full 附近已在上面逐事件计数）
    for cid, n in car_load.items():
        if n < 0:
            raise VerificationError(f"{cid} 号梯终态车内人数为负")
    if events and events[-1]["t"] > until + 1e-6:
        warnings.append("存在晚于仿真截止时刻的事件")
    return warnings


def _check_loc_field(e: dict, expected: str | None, pid: int,
                     loc: dict) -> None:
    """日志里记录的 location 字段必须与重建位置一致（供前端直接核对）。"""
    if expected is None:
        return
    cur = loc[pid]
    text = f"{cur[0]}:{cur[1]}"
    if e.get("location") is not None and e["location"] != text:
        raise VerificationError(
            f"乘客 {pid} 日志位置字段 {e['location']} 与重建位置 {text} 不符")


def location_timeline(events: list[dict], pid: int) -> list[dict]:
    """抽取单个乘客的位置迁移链，供 UI 展示与人工核对。"""
    out = []
    for e in events:
        if e.get("pid") != pid:
            continue
        out.append({"t": e["t"], "type": e["type"],
                    "location": e.get("location"),
                    "floor": e.get("floor"),
                    "car": e.get("car")})
    return out
