"""
Aria Voice API — FastAPI service for transcription + validation.

Endpoints:
    GET  /health  — liveness check
    POST /process — audio (WAV) → {transcript, valid, verdict}

Deploy to HuggingFace Spaces (Docker SDK, port 7860).
Set ANTHROPIC_API_KEY as a Space secret.
"""

import io
import logging
import os
import wave

import anthropic
import numpy as np
from fastapi import FastAPI, HTTPException, UploadFile
from faster_whisper import WhisperModel

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("aria-api")

app = FastAPI(title="Aria Voice API", version="1.0.0")

# ── Whisper model (loaded once at startup) ────────────────────────────────────
log.info("Loading Whisper model …")
_whisper = WhisperModel("small.en", device="cpu", compute_type="int8")
log.info("Whisper ready.")

# ── Anthropic client ──────────────────────────────────────────────────────────
_anthropic = anthropic.Anthropic()

_VALIDATE_SYSTEM = (
    "Reply only 'true' or 'false'. "
    "Reply 'true' if the text contains 'Aria' or a likely mishearing: Arya, Area, Ariel, Ariah, Aeria, Areia, Riya, Ria, Aya, Ah yeah, Are (case-insensitive). "
    "Reply 'false' only if the wake word is clearly absent and the text is noise or gibberish."
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _wav_to_numpy(data: bytes) -> np.ndarray:
    buf = io.BytesIO(data)
    with wave.open(buf, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio


def _transcribe(audio: np.ndarray) -> str:
    segments, _ = _whisper.transcribe(audio, beam_size=1, language="en")
    parts = []
    for s in segments:
        # Drop segments Whisper itself is unsure about:
        #   no_speech_prob > 0.6  → likely silence / noise
        #   avg_logprob   < -1.0  → low token-level confidence (gibberish)
        if s.no_speech_prob > 0.6 or s.avg_logprob < -1.0:
            continue
        parts.append(s.text.strip())
    return " ".join(parts).strip()


def _validate(text: str) -> tuple[bool, str]:
    response = _anthropic.messages.create(
        model="claude-haiku-4-5-20251001",
        max_tokens=1,
        system=_VALIDATE_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip()
    return raw.lower() == "true", raw


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/process")
async def process(file: UploadFile):
    if not file.filename.lower().endswith(".wav"):
        raise HTTPException(status_code=400, detail="Only WAV files are accepted.")
    data = await file.read()
    try:
        audio = _wav_to_numpy(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode WAV: {exc}")

    text = _transcribe(audio)
    if not text:
        log.info("PROCESS  (empty transcript)")
        return {"transcript": "", "valid": False, "verdict": "empty"}

    valid, raw = _validate(text)
    log.info("PROCESS  %r → %s (%s)", text, "SENT" if valid else "BLOCKED", raw)
    return {"transcript": text, "valid": valid, "verdict": raw}
