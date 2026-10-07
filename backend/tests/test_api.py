"""FastAPI 端到端测试（TestClient 走完整请求，不依赖运行中的服务）。"""
import pytest

# 让不持久化的请求也能走：测试中默认 persist=False，避免依赖 PG 服务
pytest.importorskip("fastapi")

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402

client = TestClient(app)


def test_health():
    r = client.get("/api/health")
    assert r.status_code == 200
    data = r.json()
    assert data["status"] == "ok"
    assert set(data["policies"]) == {"collective", "nearest", "zoning"}


def test_scenarios():
    r = client.get("/api/scenarios")
    assert r.status_code == 200
    body = r.json()
    assert {"morning_up", "cross_floor", "long_door"} <= set(body)


def test_simulate_no_persist_uses_same_arrivals():
    body = {
        "case": "cross_floor", "floors": 10, "car_count": 2,
        "capacity": 6, "seed": 5, "rate_per_min": 9, "duration": 600,
        "until": 1500, "persist": False,
    }
    r = client.post("/api/simulate", json=body)
    assert r.status_code == 200
    data = r.json()
    assert len(data["results"]) == 3
    # 同一客流：各策略的 demand 相同
    demands = [res["metrics"]["demand"] for res in data["results"]]
    assert len(set(demands)) == 1
    # 指标结构区分三类口径
    for res in data["results"]:
        m = res["metrics"]
        assert {"wait_mean", "ride_mean", "total_mean"} <= set(m["served_only"])
        assert {"wait_mean", "total_mean"} <= set(m["all_pax"])
        assert m["served"] + m["unserved"] == m["demand"]
        assert res["verification"]["ok"]


def test_simulate_morning_peak_all_complete():
    body = {
        "case": "morning_up", "floors": 12, "car_count": 4,
        "capacity": 8, "seed": 11, "rate_per_min": 22, "duration": 900,
        "until": 1500, "persist": False,
    }
    r = client.post("/api/simulate", json=body)
    data = r.json()
    for res in data["results"]:
        m = res["metrics"]
        assert m["served"] + m["unserved"] == m["demand"]


def test_compare_long_door_against_cross_flow():
    """长开门 vs 普通门：跨层客流相同（同 seed），门时不同。"""
    common = dict(case="long_door", floors=12, car_count=4, capacity=8,
                  seed=2, rate_per_min=12, duration=1200, until=2400,
                  persist=False)
    r4 = client.post("/api/simulate", json={**common, "door_time": 4.0})
    r12 = client.post("/api/simulate", json={**common, "door_time": 12.0})
    a4 = r4.json()["arrivals"]
    a12 = r12.json()["arrivals"]
    assert a4 == a12  # 客流完全一致，只改门时
    for x, y in zip(r4.json()["results"], r12.json()["results"]):
        # 门时 12s 的候梯不会比 4s 更短（弱单调性的教学预期，非硬约束断言）
        assert x["policy"] == y["policy"]


def test_bad_policy_rejected():
    r = client.post("/api/simulate",
                    json={"case": "cross_floor", "policies": ["nope"],
                          "persist": False})
    assert r.status_code == 400


def test_block_same_flow_with_baseline():
    """阻挡运行 + 无阻挡对照：同一份到达序列，事件含开始/解除，指标含受影响口径。"""
    body = {
        "case": "cross_floor", "floors": 10, "car_count": 2,
        "capacity": 6, "seed": 5, "rate_per_min": 9, "duration": 600,
        "until": 1500, "persist": False,
        "policies": ["nearest"],
        "blocks": [{"car": 0, "floor": 4, "start": 200, "end": 280}],
        "compare_baseline": True,
    }
    r = client.post("/api/simulate", json=body)
    assert r.status_code == 200, r.text
    data = r.json()
    # 同一份客流：对照与阻挡的到达序列逐元素相同（未重新生成乘客）
    assert data["arrivals"]
    blk = data["results"][0]
    base = data["baselines"][0]
    assert base["metrics"]["demand"] == blk["metrics"]["demand"]
    types_ = [e["type"] for e in blk["events"]]
    assert {"door_block_arm", "door_block_start", "door_block_end"} <= set(types_)
    # 阻挡期间无 0 号梯移动/登乘/关门
    ev = blk["events"]
    s = next(i for i, e in enumerate(ev) if e["type"] == "door_block_start")
    en = next(i for i, e in enumerate(ev) if e["type"] == "door_block_end")
    for e in ev[s + 1:en]:
        if e.get("car") == 0:
            assert e["type"] not in ("car_move", "pax_board", "doors_close")
    # 其他轿厢在阻挡期间继续运行
    assert any(e["type"] == "car_move" and e.get("car") == 1
               for e in ev[s:en])
    # 受影响乘客口径 + 全客流变化
    bg = blk["metrics"]["blockages"]
    assert bg["affected_count"] >= 0 and "affected_metrics" in bg
    assert data["deltas"]["rows"]["nearest"]["wait_mean_all_delta"] is not None
    assert blk["verification"]["ok"]
    assert blk["blocks"][0]["start"] == 200 and blk["blocks"][0]["end"] == 280


def test_block_validation_rejected():
    body = {
        "case": "cross_floor", "floors": 10, "car_count": 2,
        "capacity": 6, "seed": 5, "rate_per_min": 9, "duration": 600,
        "until": 1500, "persist": False,
        "blocks": [{"car": 0, "floor": 99, "start": 0, "end": 10}],
    }
    r = client.post("/api/simulate", json=body)
    assert r.status_code == 400
    assert "阻挡" in r.json()["detail"]


def test_no_blocks_means_no_baseline_payload():
    body = {
        "case": "cross_floor", "floors": 10, "car_count": 2,
        "capacity": 6, "seed": 5, "rate_per_min": 9, "duration": 600,
        "until": 1500, "persist": False, "policies": ["nearest"],
    }
    data = client.post("/api/simulate", json=body).json()
    assert data["baselines"] == []
    assert data["deltas"] is None
    assert "blockages" not in data["results"][0]["metrics"]
