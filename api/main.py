"""
Aria Voice API — FastAPI service for transcription + validation.

Endpoints:
    GET  /health      — liveness check
    POST /transcribe  — audio (WAV) → transcript text
    POST /validate    — text → {valid, verdict}
    POST /process     — audio (WAV) → {transcript, valid, verdict}

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
from pydantic import BaseModel

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
    "You are a voice input filter for an AI coding assistant named Aria. "
    "You receive speech-to-text transcriptions from a microphone. "
    "Reply with only the word 'true' or 'false'. "
    "Reply 'true' if the transcription contains the name 'Aria' or a likely speech-to-text mishearing of it "
    "(e.g. 'Arya', 'Area', 'Ariel', 'Ariah', 'Aeria', 'Areia', 'Riya', 'Ria', 'Aya', 'Ah yeah', 'Are') — case-insensitive. "
    "Be generous: if the wake word is present, reply 'true' even if the command is short, casual, or incomplete. "
    "Only reply 'false' if the wake word is clearly absent AND the transcription is obviously background noise, "
    "random words, or gibberish with no intent to address an assistant."
)


# ── Helpers ───────────────────────────────────────────────────────────────────

def _wav_to_numpy(data: bytes) -> np.ndarray:
    buf = io.BytesIO(data)
    with wave.open(buf, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio


def _transcribe(audio: np.ndarray) -> str:
    segments, _ = _whisper.transcribe(audio, beam_size=5, language="en")
    return " ".join(s.text.strip() for s in segments).strip()


def _validate(text: str) -> tuple[bool, str]:
    response = _anthropic.messages.create(
        model="claude-haiku-4-5",
        max_tokens=10,
        system=_VALIDATE_SYSTEM,
        messages=[{"role": "user", "content": text}],
    )
    raw = response.content[0].text.strip()
    return raw.lower() == "true", raw


# ── Routes ────────────────────────────────────────────────────────────────────

@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/transcribe")
async def transcribe(file: UploadFile):
    if not file.filename.lower().endswith(".wav"):
        raise HTTPException(status_code=400, detail="Only WAV files are accepted.")
    data = await file.read()
    try:
        audio = _wav_to_numpy(data)
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Could not decode WAV: {exc}")
    text = _transcribe(audio)
    log.info("TRANSCRIBE  %r", text)
    return {"transcript": text}


class ValidateRequest(BaseModel):
    text: str


@app.post("/validate")
def validate(body: ValidateRequest):
    if not body.text.strip():
        return {"valid": False, "verdict": "empty"}
    valid, raw = _validate(body.text)
    log.info("VALIDATE  %r → %s", body.text, raw)
    return {"valid": valid, "verdict": raw}


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
