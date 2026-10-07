"""门保持打开（阻挡）事件测试：禁动/禁重复登乘、有/无阻挡同客流、可复现、其他轿厢照常。"""
from __future__ import annotations

import pytest

from app.blockages import normalize_blocks
from app.demand import build_arrivals
from app.runner import run_simulation
from app.verifier import verify, VerificationError

ALL_POLICIES = ["collective", "nearest", "zoning"]

BASE = dict(floors=10, car_count=2, capacity=6, floor_time=2.0,
            door_time=4.0, seed=7, rate_per_min=8,
            duration=600, until=1200)


def run(policy, blocks=None, case="cross_floor", arrivals=None, **over):
    kw = dict(BASE)
    kw.update(over)
    return run_simulation(case=case, policy_name=policy,
                          blocks_raw=blocks,
                          arrivals_override=arrivals, **kw)


@pytest.fixture(scope="module")
def arrivals():
    return build_arrivals("cross_floor", floors=10, seed=7,
                          rate_per_min=8, duration=600)


# ---------- 1. 各策略下阻挡事件完整执行且通过全部不变量 ----------
@pytest.mark.parametrize("policy", ALL_POLICIES)
def test_block_runs_clean_all_policies(policy, arrivals):
    r = run(policy, blocks=[{"car": 0, "floor": 5,
                            "start": 200.0, "end": 260.0}],
            arrivals=arrivals)
    types = [e["type"] for e in r["events"]]
    assert types.count("door_block_arm") == 1
    assert types.count("door_block_start") == 1
    assert types.count("door_block_end") == 1
    assert r["verification"]["ok"]
    assert r["verification"]["warnings"] == []
    # 配置楼层与实际进入楼层一致（同客流下 start 前仿真与无阻挡一致）
    imp = r["metrics"]["blockages"]["events"][0]
    assert imp["actual_floor"] == 5
    assert imp["floor_mismatch"] is False
    assert imp["release_t"] == pytest.approx(260.0, abs=1e-6)


# ---------- 2. 阻挡期间：轿厢不动、不关门、不重复登乘；乘客/轿厢位置不跳变 ----------
def test_hold_physical_invariants(arrivals):
    r = run("nearest",
            blocks=[{"car": 0, "floor": 5, "start": 200.0, "end": 260.0}],
            arrivals=arrivals)
    ev = r["events"]
    s = next(i for i, e in enumerate(ev) if e["type"] == "door_block_start")
    en = next(i for i, e in enumerate(ev) if e["type"] == "door_block_end")
    st, ed = ev[s], ev[en]
    for e in ev[s + 1:en]:
        if e.get("car") == 0:
            assert e["type"] not in ("car_move", "pax_board", "doors_close"), e
            # 只有携带轿厢位置语义的事件需要楼层不变；call_release 的
            # floor 是候梯楼层，不是轿厢位置
            if e["type"] in ("door_block_start", "car_stop"):
                assert e["floor"] == st["floor"]
    # 解除时轿厢楼层与开始时相同（位置不跳变）
    assert ed["floor"] == st["floor"]
    # 门在保持开始前已处于打开状态、解除事件之后才关门
    last_open = max(i for i, e in enumerate(ev[:s])
                    if e["type"] == "doors_open" and e["car"] == 0)
    last_close = max([-1] + [i for i, e in enumerate(ev[:s])
                             if e["type"] == "doors_close" and e["car"] == 0])
    assert last_open > last_close
    assert ev[en + 1]["type"] == "doors_close"
    # 阻挡期间车内人数不变（不登乘、不下客，下客发生在开门首批）
    assert st["load"] == ed["load"]


def test_passenger_positions_continuous(arrivals):
    """阻挡期间每名乘客物理位置不跳变（日志逐事件位置唯一）。"""
    r = run("collective",
            blocks=[{"car": 1, "floor": 3, "start": 120.0, "end": 200.0}],
            arrivals=arrivals)
    assert r["verification"]["ok"]
    locs = r["verification"]["final_locations"]
    m = r["metrics"]
    assert locs["outside"] == m["served"]
    assert locs["hall"] + locs["car"] == m["unserved"]


# ---------- 3. 阻挡期间其他轿厢继续运行 ----------
def test_other_cars_keep_running(arrivals):
    r = run("nearest",
            blocks=[{"car": 0, "floor": 5, "start": 200.0, "end": 260.0}],
            arrivals=arrivals)
    ev = r["events"]
    in_hold = False
    other_moves = 0
    for e in ev:
        if e["type"] == "door_block_start":
            in_hold = True
        elif e["type"] == "door_block_end":
            in_hold = False
        elif in_hold and e["type"] == "car_move" and e["car"] == 1:
            other_moves += 1
    assert other_moves > 0


# ---------- 4. 有/无阻挡同一份客流，绝不重新生成乘客 ----------
def test_same_arrivals_with_without_block(arrivals):
    r0 = run("nearest", arrivals=arrivals)
    rb = run("nearest",
             blocks=[{"car": 0, "floor": 5, "start": 200.0, "end": 260.0}],
             arrivals=arrivals)
    assert r0["arrivals"] == rb["arrivals"]
    assert r0["metrics"]["demand"] == rb["metrics"]["demand"]
    # 阻挡武装之前，两个世界的事件完全一致（确定性分叉点）
    t0 = [e for e in r0["events"]]
    tb = [e for e in rb["events"]]
    arm = next(i for i, e in enumerate(tb) if e["type"] == "door_block_arm")
    # 无阻挡对照在该时刻前的事件是阻挡日志的前缀（去掉阻挡事件本身）
    base_prefix = [e for e in tb[:arm]]
    assert base_prefix == t0[:len(base_prefix)]


# ---------- 5. 阻挡整体上不改善全客流候梯（弱单调性教学预期），且产生影响指标 ----------
def test_blockage_impact_metrics(arrivals):
    r0 = run("nearest", arrivals=arrivals)
    rb = run("nearest",
             blocks=[{"car": 0, "floor": 1, "start": 100.0, "end": 220.0}],
             arrivals=arrivals)
    bg = rb["metrics"]["blockages"]
    assert bg["configured"][0]["start"] == 100.0
    assert bg["affected_count"] >= 1
    assert "affected_metrics" in bg and "wait_mean" in bg["affected_metrics"]
    # 受影响乘客口径与全客流口径同时存在
    assert rb["metrics"]["all_pax"]["wait_mean"] is not None
    # 长阻挡在跨层客流下通常增加候梯（此处只校验指标结构与可复现）
    assert r0["metrics"]["demand"] == rb["metrics"]["demand"]


# ---------- 6. 解除后服务继续：阻挡期间到客在解除后才登乘并送达 ----------
def test_service_resumes_after_release():
    # 单梯 0# 在 1 层被挡 10–60s；第 3 名乘客 60s 才到达 1 层
    arrivals = [(0.0, 1, 8), (50.0, 3, 9), (60.0, 1, 7)]
    r = run("collective", case="cross_floor", car_count=1,
            floor_time=2.0, door_time=4.0, rate_per_min=1, duration=1,
            until=400, arrivals=arrivals,
            blocks=[{"car": 0, "floor": 1, "start": 10.0, "end": 60.0}])
    m = r["metrics"]
    assert m["served"] == 3
    board_p2 = next(e for e in r["events"]
                    if e.get("pid") == 2 and e["type"] == "pax_board")
    assert board_p2["t"] >= 60.0
    rel = next(e["t"] for e in r["events"] if e["type"] == "door_block_end")
    assert any(e["type"] == "car_move" and e["t"] > rel
               for e in r["events"])
    assert r["verification"]["ok"]


# ---------- 7. 同层停站期间武装：门不重开、只有一批登乘，随后续接保持 ----------
def test_attach_to_open_door_single_batch():
    base = run("nearest", arrivals=build_arrivals(
        "cross_floor", floors=10, seed=7, rate_per_min=8, duration=600))
    op = next(e for e in base["events"]
              if e["type"] == "doors_open" and e["car"] == 0)
    f, t0 = op["floor"], op["t"]
    r = run("nearest",
            blocks=[{"car": 0, "floor": f, "start": t0, "end": t0 + 40.0}],
            arrivals=base["arrivals"] and [
                (a["arrival_time"], a["origin"], a["dest"])
                for a in base["arrivals"]])
    ev = r["events"]
    st = next(e for e in ev if e["type"] == "door_block_start")
    en = next(e for e in ev if e["type"] == "door_block_end")
    assert st.get("attached") is True
    # 保持期间 0# 无登乘/移动/关门
    for e in ev[ev.index(st) + 1:ev.index(en)]:
        if e.get("car") == 0:
            assert e["type"] not in ("pax_board", "car_move", "doors_close")
    # 从开门到 start 只有一次 doors_open（没有关门再开门）
    arm_i = next(i for i, e in enumerate(ev) if e["type"] == "door_block_arm")
    opens = [e for e in ev[arm_i:ev.index(st) + 1]
             if e["type"] == "doors_open" and e["car"] == 0]
    assert len(opens) == 1
    assert r["verification"]["ok"]


# ---------- 8. 确定性：同配置重复运行事件/指标完全一致 ----------
def test_block_run_reproducible(arrivals):
    blk = [{"car": 0, "floor": 5, "start": 200.0, "end": 260.0}]
    r1 = run("nearest", blocks=blk, arrivals=arrivals)
    r2 = run("nearest", blocks=blk, arrivals=arrivals)
    assert r1["events"] == r2["events"]
    assert r1["metrics"] == r2["metrics"]


# ---------- 9. 配置校验 ----------
def test_block_config_validation():
    with pytest.raises(ValueError):
        normalize_blocks([{"car": 9, "floor": 1, "start": 0, "end": 10}],
                         floors=10, car_count=2, until=100)
    with pytest.raises(ValueError):
        normalize_blocks([{"car": 0, "floor": 11, "start": 0, "end": 10}],
                         floors=10, car_count=2, until=100)
    with pytest.raises(ValueError):
        normalize_blocks([{"car": 0, "floor": 1, "start": 50, "end": 10}],
                         floors=10, car_count=2, until=100)
    with pytest.raises(ValueError):
        normalize_blocks([{"car": 0, "floor": 1, "start": 0, "end": 200}],
                         floors=10, car_count=2, until=100)
    # 同一轿厢时间窗重叠不允许
    with pytest.raises(ValueError):
        normalize_blocks([
            {"car": 0, "floor": 1, "start": 0, "end": 20},
            {"car": 0, "floor": 2, "start": 10, "end": 30},
        ], floors=10, car_count=2, until=100)
    # 不同轿厢可以同时阻挡
    bs = normalize_blocks([
        {"car": 0, "floor": 1, "start": 0, "end": 20},
        {"car": 1, "floor": 2, "start": 10, "end": 30},
    ], floors=10, car_count=2, until=100)
    assert len(bs) == 2


# ---------- 10. verifier 能抓出阻挡期间移动/登乘的伪造日志 ----------
def test_verifier_rejects_move_during_hold(arrivals):
    r = run("nearest",
            blocks=[{"car": 0, "floor": 5, "start": 200.0, "end": 260.0}],
            arrivals=arrivals)
    ev = [dict(e) for e in r["events"]]
    s = next(i for i, e in enumerate(ev)
             if e["type"] == "door_block_start")
    ev.insert(s + 1, {"t": ev[s]["t"] + 1, "type": "car_move", "car": 0,
                      "floor": 6, "dir": 1, "load": 0})
    with pytest.raises(VerificationError, match="移动"):
        verify(ev, [{"status": 1} for _ in arrivals],
               capacity=6, floors=10, until=1200)


def test_verifier_rejects_board_during_hold(arrivals):
    r = run("nearest",
            blocks=[{"car": 0, "floor": 5, "start": 200.0, "end": 260.0}],
            arrivals=arrivals)
    ev = [dict(e) for e in r["events"]]
    st = next(e for e in ev if e["type"] == "door_block_start")
    # 选一名在阻挡保持开始时仍在该层候梯的真实乘客（未登乘过）
    boarded_pids = {e["pid"] for e in ev
                    if e["type"] in ("pax_board", "pax_alight")
                    and e["t"] <= st["t"]}
    waiting = next(
        (pid for pid, (t, o, d) in enumerate(arrivals)
         if t <= st["t"] and pid not in boarded_pids and o == st["floor"]),
        None)
    assert waiting is not None, "阻挡楼层应有一名候梯乘客用于构造伪造日志"
    _, origin, dest = arrivals[waiting]
    wanted = 1 if dest > origin else -1
    fake = {"t": st["t"] + 1, "type": "pax_board", "pid": waiting, "car": 0,
            "floor": st["floor"], "dest": dest, "wanted": wanted,
            "load": st["load"] + 1, "location": "car:0"}
    ev.insert(ev.index(st) + 1, fake)
    with pytest.raises(VerificationError, match="重复登乘"):
        verify(ev, [{"status": 1} for _ in arrivals],
               capacity=6, floors=10, until=1200)


def test_verifier_rejects_missing_configured_block(arrivals):
    r = run("nearest", arrivals=arrivals)  # 无阻挡日志
    blocks = normalize_blocks(
        [{"car": 0, "floor": 5, "start": 200.0, "end": 260.0}],
        floors=10, car_count=2, until=1200)
    with pytest.raises(VerificationError, match="未在事件日志中执行"):
        verify(r["events"],
               [{"status": p["status"]} for p in r["passengers"]],
               capacity=6, floors=10, until=1200, blocks=blocks)
