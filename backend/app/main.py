"""FastAPI 入口：同一客流下对比多种派梯策略。

设计要点：
- /api/simulate 对所选策略使用**完全相同**的到达序列（同 seed、同案例），
  仅策略不同；保证对照实验有效。
- 每次运行写入 PostgreSQL：参数(scenario)、指标(run)、乘客明细(passengers)、
  事件日志(events)，可复算核对。
- /api/animate/{run_id} 返回前端井道动画所需的逐时刻状态帧
  （由事件日志确定性重放，前端本身不"随机"）。
"""
from __future__ import annotations

import json
from typing import Any

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from . import database as db
from .demand import SCENARIOS, SCENARIO_DEFAULTS
from .policies import POLICIES
from .runner import run_simulation
from .schemas import SimRequest

app = FastAPI(title="电梯派梯策略对比仿真", version="1.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"], allow_methods=["*"], allow_headers=["*"],
)


@app.on_event("startup")
def _startup() -> None:
    try:
        db.init_db()
        app.state.db_ok = True
    except Exception as exc:  # 数据库不可用时仍可演示（不持久化）
        app.state.db_ok = False
        app.state.db_error = str(exc)


@app.get("/api/health")
def health() -> dict:
    ok_engine = getattr(app.state, "db_ok", False)
    pg = None
    if ok_engine:
        try:
            with db.get_engine().connect() as conn:
                pg = conn.execute(text("select version()")).scalar_one()
        except Exception as exc:
            ok_engine = False
            pg = f"error: {exc}"
    return {"status": "ok", "db": ok_engine, "postgres": pg,
            "policies": list(POLICIES), "scenarios": list(SCENARIOS)}


@app.get("/api/scenarios")
def list_scenarios() -> dict:
    return {
        k: {"params": {kk: vv for kk, vv in v.items() if kk != "desc"},
            "desc": v["desc"]}
        for k, v in SCENARIO_DEFAULTS.items()
    }


def _resolve_params(req: SimRequest) -> dict:
    if req.case not in SCENARIOS:
        raise HTTPException(400, f"未知案例 {req.case}")
    d = SCENARIO_DEFAULTS[req.case]
    return {
        "case": req.case,
        "floors": req.floors,
        "car_count": req.car_count,
        "capacity": req.capacity,
        "floor_time": req.floor_time,
        "door_time": req.door_time if req.door_time is not None else d["door_time"],
        "seed": req.seed,
        "rate_per_min": req.rate_per_min if req.rate_per_min is not None else d["rate_per_min"],
        "duration": req.duration if req.duration is not None else d["duration"],
        "until": req.until if req.until is not None else d["until"],
    }


@app.post("/api/simulate")
def simulate(req: SimRequest) -> dict:
    p = _resolve_params(req)
    policy_names = req.policies or list(POLICIES)
    for name in policy_names:
        if name not in POLICIES:
            raise HTTPException(400, f"未知策略 {name}")

    # 同一客流（到达序列）一次性生成，所有策略复用
    from .demand import build_arrivals
    arrivals = build_arrivals(
        p["case"], floors=p["floors"], seed=p["seed"],
        rate_per_min=p["rate_per_min"], duration=p["duration"])

    results: list[dict] = []
    persist_ok = bool(req.persist and getattr(app.state, "db_ok", False))

    # 场景配置落库（相同参数复用同一 scenario_id）
    scenario_id = (f"{p['case']}_F{p['floors']}C{p['car_count']}"
                   f"_cap{p['capacity']}_ft{p['floor_time']}"
                   f"_dt{p['door_time']}_r{p['rate_per_min']}"
                   f"_d{p['duration']}_s{p['seed']}")
    if persist_ok:
        _upsert_scenario(scenario_id, p, len(arrivals))

    for name in policy_names:
        r = run_simulation(
            case=p["case"], policy_name=name,
            floors=p["floors"], car_count=p["car_count"],
            capacity=p["capacity"], floor_time=p["floor_time"],
            door_time=p["door_time"], seed=p["seed"],
            rate_per_min=p["rate_per_min"], duration=p["duration"],
            until=p["until"], arrivals_override=arrivals,
            label=f"{p['case']}/{name}")
        run_id = None
        if persist_ok:
            try:
                with Session(db.get_engine()) as session:
                    run_id = db.save_run(
                        session, scenario_id=scenario_id, policy=name,
                        seed=p["seed"], metrics=r["metrics"],
                        verification=r["verification"],
                        pax_rows=r["passengers"], events=r["events"])
            except Exception as exc:
                r["verification"] = {**r["verification"],
                                     "persist_error": str(exc)}
        results.append({
            "run_id": run_id,
            "policy": name,
            "display_name": POLICIES[name]().display_name,
            "metrics": r["metrics"],
            "verification": r["verification"],
            "event_count": len(r["events"]),
            "events": r["events"],          # 前端直接播放本次结果
            "passengers": r["passengers"],
        })

    return {
        "params": p,
        "scenario_id": scenario_id,
        "arrivals": [{"arrival_time": a[0], "origin": a[1], "dest": a[2]}
                     for a in arrivals],
        "demand_n": len(arrivals),
        "persisted": persist_ok,
        "results": results,
        "comparison": _comparison(results),
    }


def _upsert_scenario(scenario_id: str, p: dict, demand: int) -> None:
    try:
        with Session(db.get_engine()) as session:
            existing = session.get(db.Scenario, scenario_id)
            if existing is None:
                session.add(db.Scenario(
                    id=scenario_id, case=p["case"],
                    label=SCENARIO_DEFAULTS[p["case"]]["desc"],
                    floors=p["floors"], car_count=p["car_count"],
                    capacity=p["capacity"], floor_time=p["floor_time"],
                    door_time=p["door_time"], seed=p["seed"],
                    rate_per_min=p["rate_per_min"], duration=p["duration"],
                    until=p["until"], demand=demand))
                session.commit()
    except Exception:
        raise


def _comparison(results: list[dict]) -> dict:
    rows = {}
    for r in results:
        m = r["metrics"]
        rows[r["policy"]] = {
            "wait_mean_all": m["all_pax"]["wait_mean"],
            "wait_p90_all": m["all_pax"]["wait_p90"],
            "ride_mean_served": m["served_only"]["ride_mean"],
            "total_mean_all": m["all_pax"]["total_mean"],
            "total_p90_all": m["all_pax"]["total_p90"],
            "served": m["served"], "unserved": m["unserved"],
            "completion_rate": m["completion_rate"],
        }
    best = {}
    if rows:
        for key in ("wait_mean_all", "total_mean_all", "wait_p90_all"):
            valid = [(k, v[key]) for k, v in rows.items() if v[key] is not None]
            best[key] = min(valid, key=lambda x: x[1])[0] if valid else None
        best["completion_rate"] = max(rows, key=lambda k: rows[k]["completion_rate"])
    return {"rows": rows, "best": best}


@app.get("/api/runs")
def list_runs(limit: int = Query(50, le=200)) -> dict:
    if not getattr(app.state, "db_ok", False):
        return {"runs": [], "note": "数据库不可用"}
    with Session(db.get_engine()) as session:
        stmt = select(db.Run).order_by(db.Run.id.desc()).limit(limit)
        runs = session.execute(stmt).scalars().all()
        return {"runs": [{
            "id": r.id, "scenario_id": r.scenario_id, "policy": r.policy,
            "seed": r.seed, "served": r.served, "unserved": r.unserved,
            "completion_rate": r.completion_rate,
            "wait_mean_all": r.wait_mean_all,
            "total_mean_all": r.total_mean_all,
        } for r in runs]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: int) -> dict:
    if not getattr(app.state, "db_ok", False):
        raise HTTPException(503, "数据库不可用")
    with Session(db.get_engine()) as session:
        run = session.get(db.Run, run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} 不存在")
        pax = session.execute(
            select(db.Passenger).where(db.Passenger.run_id == run_id)
            .order_by(db.Passenger.pid)).scalars().all()
        return {
            "run": {
                "id": run.id, "scenario_id": run.scenario_id,
                "policy": run.policy, "seed": run.seed,
                "metrics": json.loads(run.metrics_json),
                "verification": json.loads(run.verification_json or "{}"),
            },
            "passengers": [{
                "pid": p.pid, "origin": p.origin, "dest": p.dest,
                "arrival_time": p.arrival_time, "board_time": p.board_time,
                "alight_time": p.alight_time, "car_id": p.car_id,
                "status": p.status, "wait_time": p.wait_time,
                "ride_time": p.ride_time, "total_time": p.total_time,
            } for p in pax],
        }


@app.get("/api/runs/{run_id}/events")
def get_events(run_id: int, limit: int = Query(20000, le=200000)) -> dict:
    if not getattr(app.state, "db_ok", False):
        raise HTTPException(503, "数据库不可用")
    with Session(db.get_engine()) as session:
        rows = session.execute(
            select(db.EventLog).where(db.EventLog.run_id == run_id)
            .order_by(db.EventLog.seq).limit(limit)).scalars().all()
        return {"events": [
            {"seq": e.seq, "t": e.t, "type": e.etype,
             **json.loads(e.payload_json)} for e in rows]}
