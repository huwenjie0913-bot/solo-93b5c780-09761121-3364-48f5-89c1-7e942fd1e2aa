"""Pydantic 模型：入参字段校验与规则配置。

时间戳同时接受 Unix 秒（int/float/数字字符串）与 ISO 8601 字符串，
统一解析为 Unix 秒（float）。
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Annotated, Literal, Optional

from pydantic import BaseModel, BeforeValidator, Field, model_validator

# 合理时间范围：2000-01-01 ~ 2100-01-01
_TS_MIN = 946_684_800.0
_TS_MAX = 4_102_444_800.0


def parse_ts(v) -> float:
    if isinstance(v, bool):
        raise ValueError("时间戳类型非法")
    if isinstance(v, (int, float)):
        ts = float(v)
    elif isinstance(v, str):
        s = v.strip()
        try:
            ts = float(s)
        except ValueError:
            try:
                dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
            except ValueError:
                raise ValueError(f"无法解析的时间戳: {v!r}")
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            ts = dt.timestamp()
    else:
        raise ValueError(f"无法解析的时间戳类型: {type(v).__name__}")
    if not (_TS_MIN <= ts <= _TS_MAX):
        raise ValueError(f"时间戳超出合理范围(2000~2100年): {v!r}")
    return ts


Ts = Annotated[float, BeforeValidator(parse_ts)]


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()


# ---------------------------------------------------------------- 上报数据

class TempSampleIn(BaseModel):
    probe_id: str = Field(min_length=1, max_length=64)
    ts: Ts
    value: float = Field(ge=-80.0, le=80.0, description="温度 ℃")


class DoorEventIn(BaseModel):
    door_id: str = Field(min_length=1, max_length=64)
    ts: Ts
    state: Literal["open", "closed"]


class CompressorStatusIn(BaseModel):
    compressor_id: str = Field(min_length=1, max_length=64)
    ts: Ts
    state: Literal["on", "off"]


class DefrostRecordIn(BaseModel):
    zone_id: str = Field(default="default", max_length=64)
    start_ts: Ts
    end_ts: Ts

    @model_validator(mode="after")
    def _check_order(self):
        if self.end_ts <= self.start_ts:
            raise ValueError("化霜结束时间必须大于开始时间")
        return self


# ---------------------------------------------------------------- 规则配置

class RuleConfig(BaseModel):
    """越界识别与归因的可配置规则。"""

    temp_upper: float = Field(default=-18.0, description="温度上限 ℃")
    min_duration_s: int = Field(default=300, gt=0, description="越界持续时长阈值(秒)")
    max_gap_s: int = Field(default=600, gt=0, description="采样缺口阈值(秒)，超过则切断区段")
    door_lead_s: int = Field(default=900, gt=0, description="开门事件与区段起点的关联窗口(秒)")
    recovery_window_s: int = Field(default=1800, gt=0, description="关门后恢复时间窗口(秒)")
    slope_window_s: int = Field(default=600, gt=0, description="开门后升温斜率计算窗口(秒)")
    peer_deviation_c: float = Field(default=2.0, gt=0, description="相邻探头偏差阈值 ℃")
    compressor_duty_high: float = Field(default=0.8, gt=0, le=1, description="压缩机高占空阈值")
    compressor_duty_low: float = Field(default=0.3, ge=0, lt=1, description="压缩机低占空阈值")

    @model_validator(mode="after")
    def _check_duty(self):
        if self.compressor_duty_low >= self.compressor_duty_high:
            raise ValueError("compressor_duty_low 必须小于 compressor_duty_high")
        return self


class RuleSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    config: RuleConfig = Field(default_factory=RuleConfig)


class RunRequest(BaseModel):
    rule_set_id: Optional[int] = Field(default=None, description="缺省使用最新规则集")
    range_start: Ts
    range_end: Ts
    zone: Optional[str] = Field(
        default=None, description="库区编码（或数字 ID）；指定后只分析该库区绑定设备"
    )
    topology_version_id: Optional[int] = Field(
        default=None, description="拓扑版本 ID；缺省使用该库区最新版本"
    )

    @model_validator(mode="after")
    def _check_range(self):
        if self.range_end <= self.range_start:
            raise ValueError("range_end 必须大于 range_start")
        if self.topology_version_id is not None and not self.zone:
            raise ValueError("指定 topology_version_id 时必须同时指定 zone")
        return self


# ---------------------------------------------------------------- 库区拓扑

class ZoneCreate(BaseModel):
    code: str = Field(min_length=1, max_length=64, description="库区编码，唯一")
    name: str = Field(min_length=1, max_length=128)


class TopologyVersionCreate(BaseModel):
    """为库区生成不可变拓扑版本；绑定设备全量替换（新版本快照）。"""

    probes: list[str] = Field(default_factory=list)
    doors: list[str] = Field(default_factory=list)
    compressors: list[str] = Field(default_factory=list)
    effective_from: Optional[Ts] = Field(
        default=None, description="生效时间；缺省为当前时间"
    )
    note: Optional[str] = Field(default=None, max_length=256)

    @model_validator(mode="after")
    def _check_nonempty(self):
        if not (self.probes or self.doors or self.compressors):
            raise ValueError("拓扑版本至少需要绑定一个设备（探头/库门/压缩机）")
        for label, ids in (
            ("probes", self.probes),
            ("doors", self.doors),
            ("compressors", self.compressors),
        ):
            if any(not isinstance(x, str) or not (1 <= len(x) <= 64) for x in ids):
                raise ValueError(f"{label} 中的设备 ID 长度需在 1~64 之间")
        return self
