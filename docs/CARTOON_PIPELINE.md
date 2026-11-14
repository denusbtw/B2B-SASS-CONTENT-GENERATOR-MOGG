# CARTOON_2D with OpenAI

This increment adds an opt-in cartoon generation path to the existing stage ledger.
The planner sends one structured Responses API request for all parts. The validated
title, protagonist, arc, episodes, scene directions, prompt fingerprint and token
usage are recorded with the existing plan stage. This is an initial continuity
mechanism. It does not validate narrative continuity independently.

The worker calls OpenAI Speech for each scene. Captions follow measured narration
segments. FFmpeg assembles H.264/AAC at 1080×1920 and 30 fps. Five original procedural
settings and bounded motion/camera controls provide the visual treatment. The
model returns data only. Rendering runs trusted code and never executes generated
JavaScript or shell commands. The existing QA is technical and mock review remains
labeled as such.

## Local trial

Set OPENAI_API_KEY in the ignored project .env. The Compose worker receives that
key. OPENAI_STORY_MODEL and OPENAI_TTS_MODEL override the configured defaults.
Build and start with docker compose up --build -d --wait. In the operator dashboard
select **2D-мультфільм → OpenAI + озвучка**. Start with one part and 30 seconds.
Use original text. For example, ask for an original Ukrainian suspense story
about a phone notification in an empty tower, with four or more distinct scenes.

Submitting a live job sends the brief to OpenAI, then sends a speech request for
each scene. It incurs API usage. A key is required for explicit live requests.
An out-of-tolerance narration length requires review. The provider response is
not automatically retried when its billing outcome is uncertain. The mock path
is still the default. A live request and speech quality review are pending.

## Remaining work

The full mandate also requires durable creative projects and revisions, reference
analysis and style profiles, Remotion, generated animation components and a
verified sandbox, more complete timing and visual review, durable paid-call
accounting and budgets, large artifact resumability, authorized CLIPS input and
real publication adapters. These are outside this increment. The current
renderer composes a finite declarative vocabulary with trusted local code.
