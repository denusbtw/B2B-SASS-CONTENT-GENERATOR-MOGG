import json

import httpx


from factory.captions import clip_captions
from factory.semantic_highlights import rank


def transcript():
    return {
        "language": "uk",
        "text": "Оригінальна історія.",
        "segments": [
            {"start": i, "end": i + 5, "text": f"Цікава подія {i}."}
            for i in range(0, 120, 5)
        ],
        "words": [{"start": i, "end": i + 0.5, "word": "подія"} for i in range(120)],
    }


def test_ollama_validated_disjoint_candidates():
    candidates = [
        {
            "candidate_id": 0,
            "score": 85,
            "title": "Подія",
            "reason": "Завершена думка",
            "confidence": 0.8,
        },
        {
            "candidate_id": 0,
            "score": 75,
            "title": "Повтор",
            "reason": "Той самий момент",
            "confidence": 0.7,
        },
        {
            "candidate_id": 999,
            "score": 99,
            "title": "Немає",
            "reason": "Вигаданий момент",
            "confidence": 0.9,
        },
    ]

    calls = []

    def handler(request):
        calls.append(1)
        body = json.loads(request.content)
        assert body["keep_alive"] == 0 and body["options"]["num_ctx"] == 2048
        return httpx.Response(
            200,
            json={
                "response": json.dumps(
                    {"candidates": candidates if len(calls) == 1 else []}
                )
            },
        )

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        result = rank(transcript(), 120, 5, 30, http=client)
    assert not result["fallback"] and len(result["parts"]) == 1
    assert result["parts"][0]["title"] == "Подія"
    assert result["language"] == "uk"


def test_missing_ollama_is_honest_fallback():
    with httpx.Client(
        transport=httpx.MockTransport(lambda _: httpx.Response(503))
    ) as client:
        result = rank(transcript(), 120, 5, 30, http=client)
    assert result["analyzer"] == "transcript_heuristic"
    assert result["fallback"] and result["parts"]
    assert all(20 <= p["end"] - p["start"] <= 60 for p in result["parts"])


def test_srt_is_clip_relative_unicode(tmp_path):
    path = clip_captions(tmp_path, transcript(), {"part": 1, "start": 10, "end": 40})
    assert path
    content = path.with_suffix(".srt").read_text(encoding="utf-8")
    assert r"\kf" in path.read_text(encoding="utf-8")
    assert "00:00:00,000" in content
    assert "подія" in content
