---
title: Aria Voice API
emoji: 🎤
colorFrom: purple
colorTo: blue
sdk: docker
app_port: 7860
pinned: false
---

# Aria Voice API

FastAPI service that transcribes audio (Whisper `small.en`) and validates the
transcript via a Claude sub-agent before forwarding it to the Aria desktop app.

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Liveness check |
| POST | `/transcribe` | WAV file → transcript text |
| POST | `/validate` | Text → `{valid, verdict}` |
| POST | `/process` | WAV file → `{transcript, valid, verdict}` |

## Setup

Set `ANTHROPIC_API_KEY` as a Space secret in the HuggingFace Space settings.
