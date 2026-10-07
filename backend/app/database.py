"""PostgreSQL 持久化：楼层需求、容量、门时、随机种子、事件日志、指标。"""
from __future__ import annotations

import json
import os

from sqlalchemy import (
    create_engine, String, Integer, Float, Text, DateTime, func, text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, Session


def database_url() -> str:
    return os.getenv(
        "DATABASE_URL",
        "postgresql+psycopg2://elevator@/elevator_lab?host=/tmp",
    )


_engine = None


def get_engine():
    global _engine
    if _engine is None:
        _engine = create_engine(database_url(), future=True)
    return _engine


class Base(DeclarativeBase):
    pass


class Scenario(Base):
    __tablename__ = "scenarios"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    case: Mapped[str] = mapped_column(String(32))
    label: Mapped[str] = mapped_column(String(128))
    floors: Mapped[int] = mapped_column(Integer)
    car_count: Mapped[int] = mapped_column(Integer)
    capacity: Mapped[int] = mapped_column(Integer)
    floor_time: Mapped[float] = mapped_column(Float)
    door_time: Mapped[float] = mapped_column(Float)
    seed: Mapped[int] = mapped_column(Integer)
    rate_per_min: Mapped[float] = mapped_column(Float)
    duration: Mapped[float] = mapped_column(Float)
    until: Mapped[float] = mapped_column(Float)
    demand: Mapped[int] = mapped_column(Integer, default=0)
    blocks_json: Mapped[str] = mapped_column(Text, default="[]")


class Run(Base):
    __tablename__ = "runs"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    scenario_id: Mapped[str] = mapped_column(String(128), index=True)
    policy: Mapped[str] = mapped_column(String(32), index=True)
    seed: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[str] = mapped_column(DateTime(timezone=True),
                                           server_default=func.now())
    metrics_json: Mapped[str] = mapped_column(Text)
    verification_json: Mapped[str] = mapped_column(Text, default="{}")
    blocks_json: Mapped[str] = mapped_column(Text, default="[]")
    blocked: Mapped[int] = mapped_column(Integer, default=0)
    baseline_run_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    served: Mapped[int] = mapped_column(Integer)
    unserved: Mapped[int] = mapped_column(Integer)
    completion_rate: Mapped[float] = mapped_column(Float)
    wait_mean_all: Mapped[float] = mapped_column(Float, nullable=True)
    total_mean_all: Mapped[float] = mapped_column(Float, nullable=True)
    wait_mean_served: Mapped[float] = mapped_column(Float, nullable=True)
    total_mean_served: Mapped[float] = mapped_column(Float, nullable=True)


class Passenger(Base):
    __tablename__ = "passengers"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    pid: Mapped[int] = mapped_column(Integer)
    origin: Mapped[int] = mapped_column(Integer)
    dest: Mapped[int] = mapped_column(Integer)
    arrival_time: Mapped[float] = mapped_column(Float)
    board_time: Mapped[float] = mapped_column(Float, nullable=True)
    alight_time: Mapped[float] = mapped_column(Float, nullable=True)
    car_id: Mapped[int] = mapped_column(Integer, nullable=True)
    status: Mapped[int] = mapped_column(Integer)  # PaxStatus
    wait_time: Mapped[float] = mapped_column(Float, nullable=True)
    ride_time: Mapped[float] = mapped_column(Float, nullable=True)
    total_time: Mapped[float] = mapped_column(Float, nullable=True)


class EventLog(Base):
    __tablename__ = "events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    run_id: Mapped[int] = mapped_column(Integer, index=True)
    t: Mapped[float] = mapped_column(Float)
    seq: Mapped[int] = mapped_column(Integer)
    etype: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[str] = mapped_column(Text)


def init_db() -> None:
    engine = get_engine()
    Base.metadata.create_all(engine)
    # 旧库平滑升级：幂等补齐阻挡相关列（刷新已保存运行需要它们）
    with engine.begin() as conn:
        for stmt in (
            "ALTER TABLE scenarios ADD COLUMN IF NOT EXISTS blocks_json TEXT DEFAULT '[]'",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS blocks_json TEXT DEFAULT '[]'",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS blocked INTEGER DEFAULT 0",
            "ALTER TABLE runs ADD COLUMN IF NOT EXISTS baseline_run_id INTEGER",
        ):
            conn.execute(text(stmt))


def save_run(session: Session, *, scenario_id: str, policy: str, seed: int,
             metrics: dict, verification: dict,
             pax_rows: list[dict], events: list[dict],
             blocks: list[dict] | None = None, blocked: bool = False,
             baseline_run_id: int | None = None) -> int:
    run = Run(
        scenario_id=scenario_id, policy=policy, seed=seed,
        metrics_json=json.dumps(metrics, ensure_ascii=False),
        verification_json=json.dumps(verification, ensure_ascii=False),
        blocks_json=json.dumps(blocks or [], ensure_ascii=False),
        blocked=1 if blocked else 0,
        baseline_run_id=baseline_run_id,
        served=metrics["served"], unserved=metrics["unserved"],
        completion_rate=metrics["completion_rate"],
        wait_mean_all=metrics["all_pax"]["wait_mean"],
        total_mean_all=metrics["all_pax"]["total_mean"],
        wait_mean_served=metrics["served_only"]["wait_mean"],
        total_mean_served=metrics["served_only"]["total_mean"],
    )
    session.add(run)
    session.flush()
    run_id = run.id

    session.add_all([Passenger(run_id=run_id, **r) for r in pax_rows])
    BATCH = 2000
    batch: list[EventLog] = []
    for seq, e in enumerate(events):
        payload = {k: v for k, v in e.items() if k not in ("type", "t")}
        batch.append(EventLog(run_id=run_id, t=e["t"], seq=seq,
                              etype=e["type"],
                              payload_json=json.dumps(payload, ensure_ascii=False)))
        if len(batch) >= BATCH:
            session.add_all(batch)
            session.flush()
            batch = []
    if batch:
        session.add_all(batch)
    session.commit()
    return run_id
