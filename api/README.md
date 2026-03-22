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

A FastAPI service that acts as the voice-processing backend for the [Aria desktop app](https://github.com/shreyashpadhi/Aria-X-Claude). It receives raw audio from the desktop app, transcribes it with Whisper, and uses Claude Haiku to confirm whether a variant of *"Aria"* was present in the transcript before forwarding the result.

---

## How it works

```
Desktop app (aria_claude.py)
        │
        │  POST /process  (WAV file)
        ▼
┌──────────────────────────────────────────┐
│  1. Whisper small.en  →  transcript      │
│  2. Gibberish filter  →  drop bad audio  │
│  3a. High confidence + wake word present │
│      → short-circuit, skip Claude        │
│  3b. Ambiguous → Claude Haiku validates  │
│      whether wake word was said          │
│  4. Return {transcript, valid, verdict}  │
└──────────────────────────────────────────┘
        │
        │  JSON response
        ▼
Desktop app forwards prompt to claude terminal
```

The two-stage pipeline keeps latency low: obvious cases bypass Claude entirely, while uncertain transcriptions get a second opinion from Claude Haiku.

The wake word (*"Ok Aria"*) is expected at the **end** of the utterance — e.g. *"list my files, Ok Aria"*.

---

## Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Liveness check — returns `{"status": "ok"}` |
| `POST` | `/process` | WAV file → `{transcript, valid, verdict}` |

### `POST /process`

Accepts a `.wav` file upload. Returns a JSON object:

```json
{
  "transcript": "what time is it ok aria",
  "valid": true,
  "verdict": "true"
}
```

| Field | Type | Description |
|---|---|---|
| `transcript` | `string` | Whisper transcription of the audio |
| `valid` | `bool` | `true` if the wake word was detected |
| `verdict` | `string` | Raw validation result (`"true"`, `"false"`, or `"empty"`) |

**Error responses:**
- `400` — file is not a `.wav`, or the WAV could not be decoded

---

## Confidence thresholds

Whisper assigns each audio segment a `no_speech_prob` (probability it is silence/noise) and an `avg_logprob` (confidence in the tokens). The API uses these to filter garbage and skip unnecessary Claude calls:

| Threshold | Value | Effect |
|---|---|---|
| Segment `no_speech_prob` max | `0.60` | Segments above this are dropped as noise |
| Segment `avg_logprob` min | `-1.00` | Segments below this are dropped as gibberish |
| High-confidence `no_speech_prob` max | `0.30` | Combined with wake-word match → skip Claude |
| High-confidence `avg_logprob` min | `-0.60` | Combined with wake-word match → skip Claude |

If all segments are filtered out the endpoint returns `{"transcript": "", "valid": false, "verdict": "empty"}`.

---

## Logging

Each request is logged with a consistent column layout:

```
09:00:01  seg  ✓ kept     │ 'list my files ok aria'                 │ ns=0.04  lp=-0.25
09:00:01  seg  ✗ dropped  │ 'um'                                    │ ns=0.81  lp=-1.40
09:00:01  chk  wake=YES  conf=YES  │ ns=0.04  lp=-0.25
09:00:01  out  ✓ SENT     │ 'list my files ok aria'                 │ via=short-circuit
```

| Tag | Meaning |
|---|---|
| `seg` | Per-segment result from Whisper (kept or dropped by gibberish filter) |
| `chk` | Wake-word and confidence check result |
| `via` | Which path was taken (`short-circuit` skipped Claude; `claude` called Haiku) |
| `out` | Final outcome — `SENT` or `BLOCKED` |

Fields: `ns` = `no_speech_prob`, `lp` = `avg_logprob`.

---

## Deployment (HuggingFace Spaces)

This service is designed to run as a Docker-based HuggingFace Space.

1. Fork or push the `api/` directory to a HuggingFace Space with **Docker** SDK.
2. In the Space settings, add a secret:
   - **Name:** `ANTHROPIC_API_KEY`
   - **Value:** your Anthropic API key
3. The Space will build and start automatically. The public URL will be something like `https://<username>-aria-voice-api.hf.space`.
4. Set `ARIA_API_URL` on your desktop to point at it (see the main [README](../README.md#configuration)).

---

## Local development

```bash
cd api
pip install -r requirements.txt
```

Set your API key:

```bash
export ANTHROPIC_API_KEY=sk-ant-...
```

Run the server:

```bash
uvicorn main:app --host 0.0.0.0 --port 7860
```

The API will be available at `http://localhost:7860`. Point the desktop app at it with:

```bash
set ARIA_API_URL=http://localhost:7860
```

### Docker

```bash
cd api
docker build -t aria-voice-api .
docker run -p 7860:7860 -e ANTHROPIC_API_KEY=sk-ant-... aria-voice-api
```

---

## Dependencies

| Package | Purpose |
|---|---|
| `fastapi` | HTTP framework |
| `uvicorn` | ASGI server |
| `faster-whisper` | Local speech-to-text (Whisper `small.en`, CPU int8) |
| `anthropic` | Claude Haiku wake-word validation |
| `numpy` | Audio array processing |
