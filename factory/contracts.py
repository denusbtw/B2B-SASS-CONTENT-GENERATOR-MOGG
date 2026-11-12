from datetime import datetime, timezone
from enum import StrEnum
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class Contract(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class Mode(StrEnum):
    STORY = "STORY"
    CLIPS = "CLIPS"
    CARTOON_2D = "CARTOON_2D"


class JobSpec(Contract):
    mode: Mode
    prompt: str = Field(min_length=1, max_length=6000)
    locale: Literal["uk", "en", "ru"] = "uk"
    rights: Literal["original", "authorized", "unknown", "denied"] = "unknown"
    evidence: str = Field(default="", max_length=500)
    parts: int = Field(default=1, ge=1, le=8)
    stacked: bool = False
    synthetic_clip: bool = False

    @property
    def rights_pass(self) -> bool:
        return self.rights in {"original", "authorized"} and bool(self.evidence)


class BatchInput(Contract):
    jobs: list[dict] = Field(min_length=1, max_length=20)


class Claim(Contract):
    worker: str = Field(pattern=r"^[\w-]{1,64}$")


class Lease(Claim):
    token: UUID


class Failure(Lease):
    code: Literal["transient", "invalid_input", "review_required", "render_error"]


class Cue(Contract):
    start_ms: int = Field(ge=0)
    end_ms: int = Field(gt=0)
    text: str = Field(min_length=1, max_length=220)

    @model_validator(mode="after")
    def ordered(self):
        if self.end_ms <= self.start_ms:
            raise ValueError("Cue end must follow start")
        return self


class Timeline(Contract):
    schema_version: Literal["1.0"] = "1.0"
    mode: Mode
    width: Literal[360] = 360
    height: Literal[640] = 640
    fps: Literal[24] = 24
    duration_ms: int = Field(ge=2000, le=12000)
    cues: list[Cue] = Field(min_length=1, max_length=12)
    part: int = Field(ge=1, le=8)
    source_start_ms: int = Field(default=0, ge=0, le=90000)
    stacked: bool = False

    @model_validator(mode="after")
    def validate_cues(self):
        previous = 0
        for cue in self.cues:
            if cue.start_ms < previous or cue.end_ms > self.duration_ms:
                raise ValueError("Cue overlaps or exceeds timeline")
            previous = cue.end_ms
        if self.mode == Mode.CLIPS and self.source_start_ms + self.duration_ms > 90000:
            raise ValueError("Synthetic source span exceeds fixture")
        return self


class Plan(Contract):
    provider: Literal["MockGPT"] = "MockGPT"
    prompt_version: Literal["1"] = "1"
    prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    timelines: list[Timeline] = Field(min_length=1, max_length=8)
    cloud_calls: Literal[0] = 0


class Review(Contract):
    provider: Literal["MockVisualReview"] = "MockVisualReview"
    decision: Literal["pass", "review_required"]
    media: list[dict]


class TargetSpec(Contract):
    job: UUID
    part: int = Field(default=1, ge=1, le=8)
    account: Literal[
        "mock-youtube-a", "mock-tiktok-a", "mock-instagram-a", "mock-youtube-b"
    ]
    caption: str = Field(min_length=1, max_length=2200)
    visibility: Literal["private", "unlisted", "public"] = "private"
    scheduled_at: datetime

    @field_validator("scheduled_at")
    @classmethod
    def utc(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("Schedule must include timezone offset")
        return value.astimezone(timezone.utc)


class Approval(Contract):
    targets: list[TargetSpec] = Field(min_length=1, max_length=60)
