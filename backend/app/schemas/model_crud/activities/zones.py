from pydantic import BaseModel, ConfigDict, Field


class HRZone(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    zone: int
    seconds: float
    max_bpm: int | None = Field(default=None, alias="maxBpm")


class HRZones(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    zones: list[HRZone]
    max_hr: int | None = Field(default=None, alias="maxHr")
    threshold_hr: int | None = Field(default=None, alias="thresholdHr")


class PowerZone(BaseModel):
    zone: int
    seconds: float
    max_watts: int | None = None


class PowerZones(BaseModel):
    zones: list[PowerZone]
    ftp_watts: int | None = None
