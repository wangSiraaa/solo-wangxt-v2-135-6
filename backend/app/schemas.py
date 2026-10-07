"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field


class SimRequest(BaseModel):
    case: str = Field("morning_up",
                      description="morning_up | cross_floor | long_door")
    policies: list[str] | None = Field(
        None, description="要对比的策略；缺省为全部三种")
    floors: int = Field(12, ge=2, le=60)
    car_count: int = Field(4, ge=1, le=12)
    capacity: int = Field(8, ge=1, le=40)
    floor_time: float = Field(2.0, gt=0, le=60)
    door_time: float | None = Field(None, gt=0, le=120)
    seed: int = Field(11, ge=0)
    rate_per_min: float | None = Field(None, gt=0)
    duration: float | None = Field(None, gt=0)
    until: float | None = Field(None, gt=0)
    persist: bool = True
