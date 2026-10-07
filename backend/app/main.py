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
    blocks = [b.model_dump() for b in (req.blocks or [])]
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
        "blocks": blocks,
        "compare_baseline": req.compare_baseline,
    }


def _scenario_id(p: dict, blocked_variant: bool) -> str:
    base = (f"{p['case']}_F{p['floors']}C{p['car_count']}"
            f"_cap{p['capacity']}_ft{p['floor_time']}"
            f"_dt{p['door_time']}_r{p['rate_per_min']}"
            f"_d{p['duration']}_s{p['seed']}")
    if blocked_variant and p["blocks"]:
        key = json.dumps(
            sorted(p["blocks"], key=lambda x: (x["car"], x["start"])),
            sort_keys=True, ensure_ascii=False)
        import hashlib
        return f"{base}_blk{hashlib.sha1(key.encode()).hexdigest()[:12]}"
    return base


@app.post("/api/simulate")
def simulate(req: SimRequest) -> dict:
    p = _resolve_params(req)
    policy_names = req.policies or list(POLICIES)
    for name in policy_names:
        if name not in POLICIES:
            raise HTTPException(400, f"未知策略 {name}")

    # 阻挡配置在解析后的建筑参数下校验（楼层/轿厢/时间窗/重叠）
    from .blockages import normalize_blocks
    try:
        norm_blocks = normalize_blocks(
            p["blocks"], floors=p["floors"],
            car_count=p["car_count"], until=p["until"])
    except ValueError as exc:
        raise HTTPException(400, f"阻挡事件配置非法：{exc}")
    p["blocks"] = [b.to_dict() for b in norm_blocks]

    # 同一客流（到达序列）一次性生成，所有策略、有/无阻挡全部复用，
    # 绝不因阻挡对照重新随机生成乘客
    from .demand import build_arrivals
    arrivals = build_arrivals(
        p["case"], floors=p["floors"], seed=p["seed"],
        rate_per_min=p["rate_per_min"], duration=p["duration"])

    results: list[dict] = []
    baselines: list[dict] = []
    persist_ok = bool(req.persist and getattr(app.state, "db_ok", False))
    want_baseline = bool(p["compare_baseline"] and p["blocks"])

    # 场景配置落库（相同参数复用同一 scenario_id）
    scenario_id = _scenario_id(p, blocked_variant=bool(p["blocks"]))
    baseline_scenario_id = _scenario_id(p, blocked_variant=False)
    if persist_ok:
        _upsert_scenario(baseline_scenario_id, p, len(arrivals), [])
        if p["blocks"]:
            _upsert_scenario(scenario_id, p, len(arrivals), p["blocks"])

    # 先跑无阻挡对照（同一份 arrivals）
    baseline_ids: dict[str, int] = {}
    if want_baseline:
        for name in policy_names:
            r = run_simulation(
                case=p["case"], policy_name=name,
                floors=p["floors"], car_count=p["car_count"],
                capacity=p["capacity"], floor_time=p["floor_time"],
                door_time=p["door_time"], seed=p["seed"],
                rate_per_min=p["rate_per_min"], duration=p["duration"],
                until=p["until"], arrivals_override=arrivals,
                label=f"{p['case']}/{name}/baseline")
            run_id = None
            if persist_ok:
                try:
                    with Session(db.get_engine()) as session:
                        run_id = db.save_run(
                            session, scenario_id=baseline_scenario_id,
                            policy=name, seed=p["seed"],
                            metrics=r["metrics"],
                            verification=r["verification"],
                            pax_rows=r["passengers"], events=r["events"])
                except Exception as exc:
                    r["verification"] = {**r["verification"],
                                         "persist_error": str(exc)}
            baseline_ids[name] = run_id
            baselines.append({
                "run_id": run_id,
                "policy": name,
                "display_name": POLICIES[name]().display_name,
                "metrics": r["metrics"],
                "verification": r["verification"],
                "event_count": len(r["events"]),
                "events": r["events"],
                "passengers": r["passengers"],
            })

    for name in policy_names:
        r = run_simulation(
            case=p["case"], policy_name=name,
            floors=p["floors"], car_count=p["car_count"],
            capacity=p["capacity"], floor_time=p["floor_time"],
            door_time=p["door_time"], seed=p["seed"],
            rate_per_min=p["rate_per_min"], duration=p["duration"],
            until=p["until"], arrivals_override=arrivals,
            label=f"{p['case']}/{name}",
            blocks_raw=p["blocks"] if p["blocks"] else None)
        run_id = None
        if persist_ok:
            try:
                with Session(db.get_engine()) as session:
                    run_id = db.save_run(
                        session, scenario_id=scenario_id, policy=name,
                        seed=p["seed"], metrics=r["metrics"],
                        verification=r["verification"],
                        pax_rows=r["passengers"], events=r["events"],
                        blocks=p["blocks"], blocked=bool(p["blocks"]),
                        baseline_run_id=baseline_ids.get(name))
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
            "blocks": r["config"]["blocks"],
            "baseline_run_id": baseline_ids.get(name),
        })

    return {
        "params": p,
        "scenario_id": scenario_id,
        "arrivals": [{"arrival_time": a[0], "origin": a[1], "dest": a[2]}
                     for a in arrivals],
        "demand_n": len(arrivals),
        "persisted": persist_ok,
        "results": results,
        "baselines": baselines,
        "comparison": _comparison(results),
        "baseline_comparison": _comparison(baselines) if baselines else None,
        "deltas": _deltas(baselines, results) if baselines else None,
    }


def _upsert_scenario(scenario_id: str, p: dict, demand: int,
                     blocks: list | None = None) -> None:
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
                until=p["until"], demand=demand,
                blocks_json=json.dumps(blocks or [], ensure_ascii=False)))
            session.commit()


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


def _deltas(baselines: list[dict], blocked: list[dict]) -> dict:
    """同一客流、同种子：有阻挡 − 无阻挡 的全客流指标变化（不重新生成乘客）。"""
    base = {r["policy"]: r["metrics"] for r in baselines}
    rows = {}
    for r in blocked:
        m = r["metrics"]
        bm = base.get(r["policy"])
        if bm is None:
            continue

        def diff(blk, base_v):
            return round(blk - base_v, 3) if blk is not None and base_v is not None else None

        rows[r["policy"]] = {
            "wait_mean_all_delta": diff(m["all_pax"]["wait_mean"],
                                        bm["all_pax"]["wait_mean"]),
            "wait_p90_all_delta": diff(m["all_pax"]["wait_p90"],
                                       bm["all_pax"]["wait_p90"]),
            "total_mean_all_delta": diff(m["all_pax"]["total_mean"],
                                         bm["all_pax"]["total_mean"]),
            "total_p90_all_delta": diff(m["all_pax"]["total_p90"],
                                        bm["all_pax"]["total_p90"]),
            "ride_mean_served_delta": diff(m["served_only"]["ride_mean"],
                                           bm["served_only"]["ride_mean"]),
            "served_delta": m["served"] - bm["served"],
            "unserved_delta": m["unserved"] - bm["unserved"],
            # 阻挡事件受影响乘客子口径（来自引擎 blockages 汇总）
            "blockages": m.get("blockages"),
        }
    return {"rows": rows}


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
            "blocked": r.blocked,
            "baseline_run_id": r.baseline_run_id,
            "blocks": json.loads(r.blocks_json or "[]"),
        } for r in runs]}


@app.get("/api/runs/{run_id}")
def get_run(run_id: int) -> dict:
    if not getattr(app.state, "db_ok", False):
        raise HTTPException(503, "数据库不可用")
    with Session(db.get_engine()) as session:
        run = session.get(db.Run, run_id)
        if run is None:
            raise HTTPException(404, f"run {run_id} 不存在")
        scenario = session.get(db.Scenario, run.scenario_id)
        pax = session.execute(
            select(db.Passenger).where(db.Passenger.run_id == run_id)
            .order_by(db.Passenger.pid)).scalars().all()
        blocks = json.loads(run.blocks_json or "[]")
        payload = {
            "run": {
                "id": run.id, "scenario_id": run.scenario_id,
                "policy": run.policy, "seed": run.seed,
                "blocked": run.blocked,
                "baseline_run_id": run.baseline_run_id,
                "blocks": blocks,
                "metrics": json.loads(run.metrics_json),
                "verification": json.loads(run.verification_json or "{}"),
            },
            "scenario": None if scenario is None else {
                "id": scenario.id, "case": scenario.case,
                "floors": scenario.floors, "car_count": scenario.car_count,
                "capacity": scenario.capacity,
                "floor_time": scenario.floor_time,
                "door_time": scenario.door_time,
                "rate_per_min": scenario.rate_per_min,
                "duration": scenario.duration, "until": scenario.until,
                "demand": scenario.demand,
            },
            "passengers": [{
                "pid": p.pid, "origin": p.origin, "dest": p.dest,
                "arrival_time": p.arrival_time, "board_time": p.board_time,
                "alight_time": p.alight_time, "car_id": p.car_id,
                "status": p.status, "wait_time": p.wait_time,
                "ride_time": p.ride_time, "total_time": p.total_time,
            } for p in pax],
        }
        # 阻挡运行：附带无阻挡对照的指标与事件，支持直接重放对照
        if run.baseline_run_id is not None:
            base = session.get(db.Run, run.baseline_run_id)
            if base is not None:
                payload["baseline"] = {
                    "run_id": base.id, "policy": base.policy,
                    "metrics": json.loads(base.metrics_json),
                }
        return payload


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
