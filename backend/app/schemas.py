"""Pydantic 请求/响应模型。"""
from __future__ import annotations

from pydantic import BaseModel, Field, model_validator


class DoorBlockEvent(BaseModel):
    car: int = Field(..., ge=0, description="被阻挡的轿厢编号（0 起）")
    floor: int = Field(..., ge=1, description="门保持打开的指定楼层")
    start: float = Field(..., ge=0, description="阻挡武装时刻（仿真秒）")
    end: float = Field(..., gt=0, description="阻挡解除时刻（仿真秒）")

    @model_validator(mode="after")
    def _check_window(self):
        if self.end <= self.start:
            raise ValueError("阻挡事件需要 start < end")
        return self


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
    blocks: list[DoorBlockEvent] | None = Field(
        None, description="门保持打开事件：指定轿厢/楼层/时间窗，"
                          "所有策略使用同一份配置，且与无阻挡对照同客流同种子")
    compare_baseline: bool = Field(
        True, description="配置了 blocks 时，是否同时运行无阻挡对照"
                          "（同一份到达序列，不重新随机生成乘客）")
    persist: bool = True
