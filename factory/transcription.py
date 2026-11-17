"""Local, bounded speech decoding with reusable word timestamps."""

import gc
import os
from pathlib import Path

from factory.media import ffmpeg, run


def transcribe(
    source: Path, duration: float, language: str | None, progress=None
) -> dict:
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        raise ValueError("ASR dependency missing. Rebuild the worker image.") from None
    model_name = os.getenv("WHISPER_MODEL", "small")
    if model_name not in {"tiny", "base", "small", "medium"}:
        raise ValueError("Unsupported Whisper model")
    cache = Path(os.getenv("ASR_MODEL_ROOT", "data/models"))
    cache.mkdir(parents=True, exist_ok=True)
    result: dict = {
        "provider": "faster-whisper",
        "model": model_name,
        "language": language,
        "segments": [],
        "words": [],
        "text": "",
    }
    # CPU int8 is the conservative baseline on shared 4 GB GPUs.
    try:
        model = WhisperModel(
            model_name,
            device="cpu",
            compute_type="int8",
            cpu_threads=2,
            num_workers=1,
            download_root=str(cache),
        )
    except Exception:
        raise ValueError(
            "Whisper model could not be loaded. Check model download/network and available RAM; retry the job."
        ) from None
    chunk_seconds = 900
    try:
        for offset in range(0, int(duration) + 1, chunk_seconds):
            if offset >= duration:
                break
            chunk = source.parent / "speech-chunk.wav"
            try:
                run(
                    [
                        ffmpeg(),
                        "-nostdin",
                        "-v",
                        "error",
                        "-y",
                        "-threads",
                        "2",
                        "-protocol_whitelist",
                        "file,pipe",
                        "-ss",
                        str(offset),
                        "-i",
                        source.name,
                        "-t",
                        str(min(chunk_seconds, duration - offset)),
                        "-vn",
                        "-ac",
                        "1",
                        "-ar",
                        "16000",
                        chunk.name,
                    ],
                    source.parent,
                    timeout=300,
                )
                segments, info = model.transcribe(
                    str(chunk),
                    language=language or result["language"],
                    word_timestamps=True,
                    vad_filter=True,
                    beam_size=3,
                    condition_on_previous_text=False,
                )
                result["language"] = result["language"] or info.language
                for segment in segments:
                    start = max(0, offset + segment.start)
                    end = min(duration, offset + segment.end)
                    if end <= start or not segment.text.strip():
                        continue
                    words = [
                        {
                            "word": word.word.strip(),
                            "start": max(start, offset + word.start),
                            "end": min(end, offset + word.end),
                            "probability": word.probability,
                        }
                        for word in segment.words or []
                        if word.word.strip() and word.end > word.start
                    ]
                    result["segments"].append(
                        {
                            "start": start,
                            "end": end,
                            "text": segment.text.strip(),
                            "confidence": min(
                                1.0, max(0.0, 1 - segment.no_speech_prob)
                            ),
                        }
                    )
                    result["words"].extend(words)
                    if progress:
                        progress(min(duration, end), duration)
            finally:
                chunk.unlink(missing_ok=True)
        result["text"] = " ".join(item["text"] for item in result["segments"])
        return result
    except (RuntimeError, MemoryError):
        raise ValueError(
            "Speech recognition failed. Select tiny/base model or free RAM and retry."
        ) from None
    finally:
        del model
        gc.collect()
