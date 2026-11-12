import hashlib
import re
import textwrap
from pathlib import Path
from typing import Literal, Protocol

from factory.contracts import Cue, JobSpec, Mode, Plan, Review, Timeline

TEMPLATES = Path(__file__).resolve().parents[1] / "prompts"


class NeedsReview(ValueError):
    pass


class ModelGateway(Protocol):
    def plan(self, spec: JobSpec) -> Plan: ...
    def review(self, media: list[dict]) -> Review: ...


class MockGPT:
    def plan(self, spec: JobSpec) -> Plan:
        if spec.mode == Mode.CLIPS and not spec.synthetic_clip:
            raise NeedsReview(
                "No verified local source; select the synthetic fixture for demo"
            )
        lines = textwrap.wrap(re.sub(r"\s+", " ", spec.prompt), width=60)[:16]
        if spec.mode == Mode.CLIPS:
            lines = [lines[0]] + [
                f"Synthetic source · {index * 10}s" for index in range(1, 16)
            ]
        if spec.parts > len(lines):
            raise NeedsReview("Insufficient narrative material for the requested parts")
        timelines = []
        for index in range(spec.parts):
            segment = lines[index :: spec.parts][:2]
            if spec.mode == Mode.CARTOON_2D and len(segment) == 1:
                segment.append(
                    {
                        "en": "Let's see what happens.",
                        "uk": "Побачимо, що далі.",
                        "ru": "Посмотрим, что дальше.",
                    }[spec.locale]
                )
            cues = [
                Cue(start_ms=n * 2000, end_ms=(n + 1) * 2000, text=line)
                for n, line in enumerate(segment)
            ]
            timelines.append(
                Timeline(
                    mode=spec.mode,
                    part=index + 1,
                    cues=cues,
                    duration_ms=len(cues) * 2000,
                    source_start_ms=index * 10000 if spec.mode == Mode.CLIPS else 0,
                    stacked=spec.stacked,
                )
            )
        template = (TEMPLATES / spec.mode.value / "v1.txt").read_bytes()
        return Plan(
            prompt_hash=hashlib.sha256(template + spec.prompt.encode()).hexdigest(),
            timelines=timelines,
        )

    def review(self, media: list[dict]) -> Review:
        decision: Literal["pass", "review_required"] = (
            "pass"
            if media and all(item["passed"] for item in media)
            else "review_required"
        )
        return Review(decision=decision, media=media)
