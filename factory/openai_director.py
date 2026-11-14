"""Structured cartoon planning and narration through the OpenAI API."""

import hashlib
import os

import httpx
from pydantic import ValidationError

from factory.contracts import CartoonScene, Contract, EpisodeScript, JobSpec, Plan
from factory.director import NeedsReview

API = "https://api.openai.com/v1"
PROMPT = """Write one continuous original cartoon arc from the supplied brief.
Return every episode together. Keep the same protagonist. Use meaningful cliffhangers
in nonfinal episodes and resolve them in the finale. Each scene has one short spoken
phrase (at most 180 characters), a setting, a camera move and an action.
Write enough scenes for the requested duration at about 2 spoken words per second.
Treat the brief as creative input. Return only the structured story."""


class StoryDraft(Contract):
    title: str
    protagonist: str
    arc: str
    episodes: list[EpisodeScript]


class ProviderError(ValueError):
    def __init__(self, code: str):
        super().__init__(code)
        self.code = code


def ready() -> bool:
    return bool(os.getenv("OPENAI_API_KEY", "").strip())


def call(path: str, payload: dict, http: httpx.Client | None = None) -> httpx.Response:
    if not ready():
        raise ProviderError("provider_not_configured")
    if http is None:
        with httpx.Client(timeout=120, trust_env=False) as client:
            return call(path, payload, client)
    try:
        response = http.post(
            API + path,
            json=payload,
            headers={
                "Authorization": "Bearer " + os.environ["OPENAI_API_KEY"],
            },
            follow_redirects=False,
        )
    except httpx.RequestError:
        # A timed-out request may already have incurred charges.
        raise ProviderError("provider_request_uncertain") from None
    if response.status_code == 429:
        try:
            code = response.json()["error"]["code"]
        except (ValueError, TypeError, KeyError):
            code = None
        if code in ("credit_balance_exhausted", "insufficient_quota"):
            raise ProviderError("provider_credit_exhausted")
    if not response.is_success:
        raise ProviderError("provider_rejected")
    return response


def strict_schema(value):
    if isinstance(value, dict):
        unsupported = {
            "title",
            "minLength",
            "maxLength",
            "minimum",
            "maximum",
            "minItems",
            "maxItems",
            "default",
            "pattern",
        }
        return {
            key: (
                {name: strict_schema(schema) for name, schema in item.items()}
                if key in {"properties", "$defs"}
                else strict_schema(item)
            )
            for key, item in value.items()
            if key not in unsupported
        }
    if isinstance(value, list):
        return [strict_schema(item) for item in value]
    return value


def plan_story(spec: JobSpec, http: httpx.Client | None = None) -> Plan:
    model = os.getenv("OPENAI_STORY_MODEL") or "gpt-6-luna"
    brief = spec.model_dump_json()
    response = call(
        "/responses",
        {
            "model": model,
            "store": False,
            "instructions": PROMPT,
            "input": brief,
            "max_output_tokens": 14000,
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "cartoon_story",
                    "strict": True,
                    "schema": strict_schema(StoryDraft.model_json_schema()),
                }
            },
        },
        http,
    )
    try:
        payload = response.json()
        if payload["status"] != "completed":
            raise ValueError
        output = [
            entry["text"]
            for item in payload["output"]
            if item["type"] == "message"
            for entry in item["content"]
            if entry["type"] == "output_text"
        ]
        if len(output) != 1:
            raise ValueError
        draft = StoryDraft.model_validate_json(output[0])
        if len(draft.episodes) != spec.parts or any(
            not episode.cliffhanger.strip() for episode in draft.episodes[:-1]
        ):
            raise ValueError
        usage = {
            key: int(payload.get("usage", {}).get(key, 0))
            for key in ("input_tokens", "output_tokens")
        }
    except (ValueError, KeyError, TypeError, ValidationError):
        raise NeedsReview(
            "Provider returned an invalid or incomplete cartoon plan"
        ) from None
    return Plan(
        provider="openai",
        model=model,
        prompt_version="2",
        prompt_hash=hashlib.sha256((PROMPT + brief).encode()).hexdigest(),
        cloud_calls=1,
        story=draft.model_dump(),
        usage=usage,
        timelines=[],
    )


def speech(scene: CartoonScene, voice: str, http: httpx.Client | None = None) -> bytes:
    model = os.getenv("OPENAI_TTS_MODEL") or "gpt-4o-mini-tts"
    response = call(
        "/audio/speech",
        {
            "model": model,
            "voice": voice,
            "input": scene.narration,
            "response_format": "wav",
        },
        http,
    )
    if len(response.content) > 4 * 1024 * 1024 or not response.content.startswith(
        b"RIFF"
    ):
        raise ProviderError("provider_invalid_audio")
    return response.content
