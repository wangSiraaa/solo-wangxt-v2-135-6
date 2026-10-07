"""核心仿真测试：物理不变量、容量、未服务乘客、同客流可复现。"""
import pytest

from app.demand import build_arrivals
from app.engine import Building
from app.models import PaxStatus
from app.policies import POLICIES
from app.runner import run_simulation
from app.verifier import verify, VerificationError, location_timeline

ALL_POLICIES = list(POLICIES)

BASE = dict(floors=10, car_count=2, capacity=6, floor_time=2.0,
            door_time=4.0, seed=7, rate_per_min=8,
            duration=600, until=1200)


def run(policy, case="cross_floor", **over):
    kw = dict(BASE)
    kw.update(over)
    return run_simulation(case=case, policy_name=policy, **kw)


# ---------- 1. 三种策略均通过位置/容量/状态链校验 ----------
@pytest.mark.parametrize("policy", ALL_POLICIES)
@pytest.mark.parametrize("case", ["morning_up", "cross_floor"])
def test_invariants(policy, case):
    r = run(policy, case)
    assert r["verification"]["ok"]
    assert r["verification"]["warnings"] == []
    locs = r["verification"]["final_locations"]
    assert locs["outside"] == r["metrics"]["served"]
    assert locs["hall"] + locs["car"] == r["metrics"]["unserved"]


# ---------- 2. 同一 seed + 同一案例 → 完全相同的到达序列与结果 ----------
def test_same_arrivals_same_results():
    a1 = build_arrivals("morning_up", floors=10, seed=42,
                        rate_per_min=10, duration=300)
    a2 = build_arrivals("morning_up", floors=10, seed=42,
                        rate_per_min=10, duration=300)
    assert a1 == a2
    r1 = run("nearest", "morning_up", seed=42, rate_per_min=10, duration=300)
    r2 = run("nearest", "morning_up", seed=42, rate_per_min=10, duration=300)
    assert r1["metrics"] == r2["metrics"]
    assert r1["events"] == r2["events"]


def test_different_seed_different_arrivals():
    a1 = build_arrivals("cross_floor", floors=10, seed=1)
    a2 = build_arrivals("cross_floor", floors=10, seed=2)
    assert a1 != a2


# ---------- 3. 满载：未上车乘客留在原队列，终态仍在候梯厅 ----------
def test_full_capacity_leaves_passengers_behind():
    # 1 梯、容量 3、同一时刻大堂爆发 6 名上行乘客
    arrivals = [(10.0, 1, 9)] * 6
    for pol in ALL_POLICIES:
        r = run_simulation(case="cross_floor", policy_name=pol,
                           floors=10, car_count=1, capacity=3,
                           floor_time=2.0, door_time=4.0, seed=1,
                           rate_per_min=1, duration=1, until=400,
                           arrivals_override=arrivals)
        m = r["metrics"]
        # 一次只能载 3 人，需要两趟；仿真时窗够则全部送达，事件日志必须出现 car_full
        assert m["demand"] == 6
        full_events = [e for e in r["events"] if e["type"] == "car_full"]
        assert full_events, f"{pol} 未记录满载事件"
        # 任意 board 事件后 load ≤ 3 由 verifier 保证
        assert r["verification"]["ok"]


def test_capacity_hard_limit_events():
    arrivals = [(5.0, 1, 8)] * 4
    r = run_simulation(case="cross_floor", policy_name="collective",
                       floors=8, car_count=1, capacity=2,
                       floor_time=1.0, door_time=2.0, seed=1,
                       rate_per_min=1, duration=1, until=300,
                       arrivals_override=arrivals)
    loads = [e["load"] for e in r["events"] if e["type"] == "pax_board"]
    assert loads and max(loads) <= 2


# ---------- 4. 未服务乘客：仿真窗口不足时必须计入指标，不得剔除 ----------
def test_unserved_passengers_counted():
    # 乘客在截止前刚上车，未来得及到达 → 终态位置仍在轿厢内，同样计入 UNSERVED
    arrivals = [(0.0, 1, 12), (196.0, 12, 1)]
    r = run_simulation(case="cross_floor", policy_name="collective",
                       floors=12, car_count=1, capacity=6,
                       floor_time=2.0, door_time=4.0, seed=1,
                       rate_per_min=1, duration=1, until=205,
                       arrivals_override=arrivals)
    m = r["metrics"]
    assert m["served"] + m["unserved"] == m["demand"] == 2
    assert m["unserved"] >= 1
    locs = r["verification"]["final_locations"]
    assert locs["outside"] == m["served"]
    assert locs["hall"] + locs["car"] == m["unserved"]
    # 全客流口径仍有值（不是只统计完成者）
    assert m["all_pax"]["total_mean"] is not None
    assert r["verification"]["ok"]


# ---------- 5. 候梯/乘梯/总行程时间口径区分 ----------
def test_wait_ride_total_decomposition():
    arrivals = [(0.0, 1, 5), (2.0, 2, 8)]
    r = run_simulation(case="cross_floor", policy_name="collective",
                       floors=8, car_count=1, capacity=6,
                       floor_time=2.0, door_time=4.0, seed=1,
                       rate_per_min=1, duration=1, until=200,
                       arrivals_override=arrivals)
    for p in r["passengers"]:
        if p["status"] == PaxStatus.DONE:
            assert p["total_time"] == pytest.approx(
                p["wait_time"] + p["ride_time"], abs=1e-6)
            assert p["wait_time"] >= 0 and p["ride_time"] >= 0


# ---------- 6. 手工核对每名乘客的唯一位置链 ----------
def test_single_location_timeline():
    arrivals = [(0.0, 3, 9)]
    r = run_simulation(case="cross_floor", policy_name="collective",
                       floors=10, car_count=1, capacity=6,
                       floor_time=2.0, door_time=4.0, seed=1,
                       rate_per_min=1, duration=1, until=200,
                       arrivals_override=arrivals)
    chain = location_timeline(r["events"], 0)
    types = [e["type"] for e in chain]
    assert types[0] == "pax_arrive"
    assert types.count("pax_board") == 1
    assert types.count("pax_alight") == 1
    assert types.index("pax_board") < types.index("pax_alight")
    # 位置迁移 hall:3 → car:0 → outside:9
    locs = [e["location"] for e in chain if e["location"]]
    assert locs[0] == "hall:3"
    assert "car:0" in locs
    assert locs[-1] == "outside:9"


# ---------- 7. verifier 能抓出伪造日志（重复上车、超载、瞬移） ----------
def test_verifier_detects_fraud():
    good = run("collective")
    ev = [dict(e) for e in good["events"]]
    # 复制一个 board 事件造成重复上车
    boards = [i for i, e in enumerate(ev) if e["type"] == "pax_board"]
    if boards:
        ev.insert(boards[0] + 1, dict(ev[boards[0]]))
        with pytest.raises(VerificationError):
            verify(ev, [{"status": PaxStatus.DONE}] * len(good["passengers"]),
                   capacity=BASE["capacity"], floors=10, until=1200)


def test_verifier_detects_overload():
    # verifier 不信任日志的 load 字段，按上车事件自行计数，故构造 9 人登乘
    ev = []
    for pid in range(9):
        ev.append({"t": 0, "type": "pax_arrive", "pid": pid, "origin": 1,
                   "dest": 5, "wanted": 1, "location": "hall:1"})
    ev.append({"t": 0, "type": "car_move", "car": 0, "floor": 1, "dir": 1})
    for pid in range(9):
        ev.append({"t": 1, "type": "pax_board", "pid": pid, "car": 0,
                   "floor": 1, "dest": 5, "wanted": 1,
                   "load": pid + 1, "location": "car:0"})
    with pytest.raises(VerificationError, match="超载"):
        verify(ev, [{"status": PaxStatus.ABOARD}] * 9, capacity=8,
               floors=10, until=100)


# ---------- 8. 长开门案例参数确实生效 ----------
def test_long_door_scenario():
    r = run("nearest", "long_door", door_time=12.0, until=2400)
    opens = [e for e in r["events"] if e["type"] == "doors_open"]
    assert all(e["door_time"] == 12.0 for e in opens)


# ---------- 9. 轿厢只逐层移动、端层不越界 ----------
def test_car_moves_floor_by_floor():
    r = run("zoning", "cross_floor")
    prev = {}
    for e in r["events"]:
        if e["type"] != "car_move":
            continue
        cid = e["car"]
        if cid in prev:
            assert abs(e["floor"] - prev[cid]) == 1
        assert 1 <= e["floor"] <= 10
        prev[cid] = e["floor"]
