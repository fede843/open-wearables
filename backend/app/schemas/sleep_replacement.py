from datetime import datetime
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StrictBool, model_validator

from app.schemas.model_crud.activities.sleep import SleepStage

StrictMinute = Annotated[int, Field(strict=True, ge=0)]


class ExactSleepAggregates(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sleep_duration_minutes: StrictMinute
    time_in_bed_minutes: StrictMinute
    deep_minutes: StrictMinute
    light_minutes: StrictMinute
    rem_minutes: StrictMinute
    awake_minutes: StrictMinute

    @model_validator(mode="after")
    def validate_totals(self) -> "ExactSleepAggregates":
        sleeping = self.deep_minutes + self.light_minutes + self.rem_minutes
        if self.sleep_duration_minutes != sleeping:
            raise ValueError("sleep duration must equal the specific sleeping stages")
        if sleeping + self.awake_minutes > self.time_in_bed_minutes:
            raise ValueError("sleep and awake stages cannot exceed time in bed")
        return self


class ExactSleepStage(SleepStage):
    model_config = ConfigDict(extra="forbid")


class ExactSleepReplacement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_id: str = Field(min_length=1, max_length=100)
    provider: str = Field(min_length=1, max_length=32)
    source: str = Field(min_length=1, max_length=64)
    device_model: str | None = Field(default=None, max_length=100)
    source_name: str = Field(min_length=1, max_length=64)
    start_time: datetime
    end_time: datetime
    zone_offset: str | None = Field(default=None, max_length=10)
    is_nap: StrictBool = False
    aggregates: ExactSleepAggregates | None = None
    intervals: list[ExactSleepStage] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_complete_timeline(self) -> "ExactSleepReplacement":
        if self.start_time.tzinfo is None or self.end_time.tzinfo is None or self.end_time <= self.start_time:
            raise ValueError("sleep bounds must be timezone-aware and increasing")
        if not self.intervals:
            if self.aggregates is None:
                raise ValueError("aggregate-only replacement requires canonical aggregates")
            return self
        cursor = self.start_time
        for interval in self.intervals:
            if interval.start_time.tzinfo is None or interval.end_time.tzinfo is None:
                raise ValueError("sleep intervals must be timezone-aware")
            if interval.start_time != cursor or interval.end_time <= interval.start_time:
                raise ValueError("sleep intervals must be ordered, complete, and non-overlapping")
            if (interval.end_time - interval.start_time).total_seconds() % 60:
                raise ValueError("sleep intervals must be minute-aligned")
            cursor = interval.end_time
        if cursor != self.end_time:
            raise ValueError("sleep intervals must cover the full event bounds")
        if self.aggregates is not None and int((self.end_time - self.start_time).total_seconds()) != (
            self.aggregates.time_in_bed_minutes * 60
        ):
            raise ValueError("time in bed must equal the exact event bounds")
        return self


class ExactSleepReplacementResponse(BaseModel):
    created: bool
    external_id: str
    start_time: datetime
    end_time: datetime
    duration_seconds: int
    sleep_duration_seconds: int
    aggregates: ExactSleepAggregates | None
    sleep_stage_intervals: list[SleepStage]
    is_nap: bool
