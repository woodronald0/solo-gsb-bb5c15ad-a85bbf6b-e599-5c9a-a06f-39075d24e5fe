"""Pydantic 请求/响应模型。"""
from datetime import datetime, timezone

from pydantic import BaseModel, ConfigDict, Field, field_validator


class EventIn(BaseModel):
    box_code: str = Field(..., min_length=1, max_length=64, description="周转箱箱号")
    seq: int = Field(..., ge=0, description="该箱递增的事件序号，从 0 开始")
    temperature: float = Field(..., ge=-100, le=100, description="摄氏温度")
    event_time: datetime = Field(..., description="ISO 8601，建议带时区，如 2026-09-23T10:00:00+08:00")

    @field_validator("box_code")
    @classmethod
    def _strip_box_code(cls, v: str) -> str:
        v = v.strip()
        if not v:
            raise ValueError("box_code 不能为空")
        return v

    @field_validator("event_time")
    @classmethod
    def _to_utc(cls, v: datetime) -> datetime:
        # 无时区信息按 UTC 处理；有时区则换算为 UTC 后去掉 tzinfo 落库
        if v.tzinfo is None:
            return v.replace(tzinfo=timezone.utc).replace(tzinfo=None)
        return v.astimezone(timezone.utc).replace(tzinfo=None)


class EventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    box_code: str
    seq: int
    temperature: float
    event_time: datetime
    created_at: datetime


class EventAck(BaseModel):
    status: str = Field(..., description="accepted=首次入库，duplicate=重复上报（幂等忽略）")
    event: EventOut


class BoxState(BaseModel):
    box_code: str
    seq: int
    temperature: float
    event_time: datetime


class BoxListItem(BaseModel):
    box_code: str


class ClockAuditItem(BaseModel):
    seq: int = Field(..., description="事件序号")
    original_time: datetime = Field(..., description="原始 UTC 事件时间（不改动原始记录）")
    suggested_time: datetime = Field(..., description="保序回归后的建议 UTC 时间")
    offset_micros: int = Field(
        ..., description="建议时间 - 原时间（微秒，正为向前修正，负为回拨修正）"
    )
    block_seq_from: int = Field(..., description="该事件所属合并块的起始序号")
    block_seq_to: int = Field(..., description="该事件所属合并块的结束序号")


class HeatExposureSegment(BaseModel):
    seq_from: int = Field(..., description="该段起点事件序号")
    seq_to: int = Field(..., description="该段终点事件序号（缺号不补点，二者不一定相邻）")
    corrected_time_from: datetime = Field(..., description="起点事件经等权保序校正后的 UTC 时间")
    corrected_time_to: datetime = Field(..., description="终点事件经等权保序校正后的 UTC 时间")
    interval_seconds: float = Field(
        ..., description="校正后相邻事件的时间间隔（秒），保序回归保证其非负"
    )
    temperature_from: float = Field(..., description="起点事件温度（°C）")
    temperature_to: float = Field(..., description="终点事件温度（°C）")
    excess_degree_seconds: float = Field(
        ..., description="该段温度线性变化时超过 upper_limit 部分的精确积分（摄氏度·秒）；未计入段为 0"
    )
    counted: bool = Field(
        ..., description="是否计入总计：校正后间隔为零或超过 max_gap_seconds 时为 false"
    )
    excluded_reason: str | None = Field(
        None, description="未计入原因：zero_interval（校正后间隔为零）/ gap_too_large（超过 max_gap_seconds）"
    )


class HeatExposureReport(BaseModel):
    box_code: str
    seq_from: int = Field(..., description="查询起始序号（含）")
    seq_to: int = Field(..., description="查询结束序号（含）")
    upper_limit: float = Field(..., description="温度上限（°C），仅积分超出该值的部分")
    max_gap_seconds: float = Field(..., description="允许计入积分的最大相邻事件间隔（秒）")
    event_count: int = Field(..., description="区间内实际取到的事件条数（不补缺号）")
    total_degree_seconds: float = Field(
        ..., description="所有计入段 excess_degree_seconds 之和（摄氏度·秒）"
    )
    segments: list[HeatExposureSegment] = Field(
        ..., description="逐段积分明细（按序号升序的相邻事件两两成段）"
    )
