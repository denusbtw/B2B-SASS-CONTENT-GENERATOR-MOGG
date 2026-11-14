import io
import json
import math
import struct
import wave

import httpx
import pytest

from factory import cartoon, media
from factory.contracts import EpisodeScript, JobSpec
from factory.openai_director import ProviderError, plan_story, speech


def draft():
    scenes = [
        {
            "narration": f"The message appeared. Scene {i}.",
            "setting": setting,
            "camera": "push",
            "action": "observe",
            "overlay": "",
        }
        for i, setting in enumerate(("room", "phone", "forest", "tower"))
    ]
    return {
        "title": "The message",
        "protagonist": "Mara",
        "arc": "Find the sender",
        "episodes": [
            {"title": "Part one", "cliffhanger": "A stranger calls", "scenes": scenes},
            {"title": "Part two", "cliffhanger": "", "scenes": scenes},
        ],
    }


def test_structured_story(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "mock-test-key")

    def handler(request):
        body = json.loads(request.content)
        assert request.url.path == "/v1/responses"
        assert body["text"]["format"]["strict"]
        schema = body["text"]["format"]["schema"]
        for obj in (
            schema,
            schema["$defs"]["EpisodeScript"],
            schema["$defs"]["CartoonScene"],
        ):
            assert set(obj["required"]) == set(obj["properties"])
        assert schema["properties"]["title"]["type"] == "string"
        assert (
            schema["$defs"]["EpisodeScript"]["properties"]["title"]["type"] == "string"
        )
        assert not body["store"]
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output": [
                    {
                        "type": "message",
                        "content": [
                            {"type": "output_text", "text": json.dumps(draft())}
                        ],
                    }
                ],
                "usage": {"input_tokens": 90, "output_tokens": 200},
            },
        )

    spec = JobSpec(mode="CARTOON_2D", provider="openai", prompt="Mystery", parts=2)
    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        plan = plan_story(spec, client)
    assert plan.provider == "openai"
    assert plan.usage["output_tokens"] == 200
    assert plan.story and plan.story["episodes"][0]["cliffhanger"]


def test_missing_key_is_explicit(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    with pytest.raises(ProviderError, match="provider_not_configured"):
        plan_story(JobSpec(mode="CARTOON_2D", provider="openai", prompt="Mystery"))


def test_speech_error_does_not_echo_provider_body(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "mock-test-key")
    episode = EpisodeScript.model_validate(draft()["episodes"][0])
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(401, text="secret"))
    ) as client:
        with pytest.raises(ProviderError, match="^provider_rejected$"):
            speech(episode.scenes[0], "onyx", client)


def fixture_sound():
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as audio:
        audio.setparams((1, 2, 24000, 0, "NONE", "not compressed"))
        audio.writeframes(
            b"".join(
                struct.pack("<h", int(2000 * math.sin(i / 12))) for i in range(24000)
            )
        )
    return buffer.getvalue()


def test_mp4_uses_audio_duration(monkeypatch, tmp_path):
    sound = fixture_sound()
    monkeypatch.setattr(cartoon, "speech", lambda *_: sound)
    episode = EpisodeScript.model_validate(draft()["episodes"][0])
    output, captions, timeline = cartoon.render_episode(episode, 1, "onyx", tmp_path, 4)
    assert output.stat().st_size > 1000
    assert timeline.duration_ms == 4000
    assert timeline.fps == 30
    assert "00:00:03,000 --> 00:00:04,000" in captions
    assert media.inspect_video(output, timeline)["passed"]


def test_exhausted_credits_are_identified(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "mock-test-key")
    spec = JobSpec(mode="CARTOON_2D", provider="openai", prompt="Mystery")
    transport = httpx.MockTransport(
        lambda _: httpx.Response(
            429, json={"error": {"code": "credit_balance_exhausted"}}
        )
    )
    with httpx.Client(transport=transport) as client:
        with pytest.raises(ProviderError, match="^provider_credit_exhausted$"):
            plan_story(spec, client)
