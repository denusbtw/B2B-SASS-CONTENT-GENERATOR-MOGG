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
    provider: Literal["mock", "openai"] = "mock"
    duration_seconds: int = Field(default=45, ge=30, le=180)
    duration_scope: Literal["per_part", "series"] = "per_part"
    voice: Literal["alloy", "coral", "onyx", "nova"] = "onyx"

    @model_validator(mode="after")
    def validate_live(self):
        if self.provider == "openai":
            if self.mode != Mode.CARTOON_2D:
                raise ValueError("OpenAI generation currently supports CARTOON_2D")
            if (
                self.duration_scope == "series"
                and self.duration_seconds < 30 * self.parts
            ):
                raise ValueError("Allow at least 30 seconds per episode")
        return self

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
    code: Literal[
        "transient",
        "invalid_input",
        "review_required",
        "render_error",
        "provider_not_configured",
        "provider_request_uncertain",
        "provider_rejected",
        "provider_credit_exhausted",
        "provider_invalid_audio",
    ]


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
    width: Literal[360, 1080] = 360
    height: Literal[640, 1920] = 640
    fps: Literal[24, 30] = 24
    duration_ms: int = Field(ge=2000, le=210000)
    cues: list[Cue] = Field(min_length=1, max_length=48)
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


class CartoonScene(Contract):
    narration: str = Field(min_length=1, max_length=180)
    setting: Literal["room", "forest", "tower", "road", "phone"]
    camera: Literal["push", "pull", "pan", "still"]
    action: Literal["observe", "walk", "shake", "fall", "signal"]
    overlay: str = Field(max_length=70)


class EpisodeScript(Contract):
    title: str = Field(min_length=1, max_length=100)
    cliffhanger: str = Field(max_length=300)
    scenes: list[CartoonScene] = Field(min_length=4, max_length=48)


class Plan(Contract):
    provider: str = "MockGPT"
    model: str | None = None
    story: dict | None = None
    usage: dict[str, int] = Field(default_factory=dict)
    prompt_version: str = "1"
    prompt_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    timelines: list[Timeline] = Field(max_length=8)
    cloud_calls: int = Field(default=0, ge=0)


class Review(Contract):
    provider: str = "MockVisualReview"
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
