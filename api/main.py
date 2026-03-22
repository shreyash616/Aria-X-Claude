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
import re
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

# ── Wake-word pattern (used for high-confidence short-circuit) ────────────────
_WAKE_PATTERN = re.compile(
    r"\b(aria|arya|area|ariel|ariah|aeria|areia|riya|ria|aya|are)\b|\bah\s+yeah\b",
    re.IGNORECASE,
)

# ── Confidence thresholds ─────────────────────────────────────────────────────
# Segments outside these bounds are dropped entirely (hard gibberish filter)
_SEG_NO_SPEECH_MAX = 0.6
_SEG_LOGPROB_MIN   = -1.0

# Above these aggregate thresholds → high-confidence transcription → skip Claude
_HIGH_CONF_NO_SPEECH_MAX = 0.3   # Whisper is sure it's speech
_HIGH_CONF_LOGPROB_MIN   = -0.6  # Whisper is sure the tokens are correct


# ── Helpers ───────────────────────────────────────────────────────────────────

def _wav_to_numpy(data: bytes) -> np.ndarray:
    buf = io.BytesIO(data)
    with wave.open(buf, "rb") as wf:
        raw = wf.readframes(wf.getnframes())
        audio = np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0
    return audio


def _transcribe(audio: np.ndarray) -> tuple[str, float, float]:
    """
    Returns (text, avg_no_speech_prob, avg_logprob).
    Segments that are clearly noise/gibberish are dropped.
    If all segments are dropped, returns ("", 1.0, -2.0).
    """
    segments, _ = _whisper.transcribe(audio, beam_size=1, language="en")
    parts: list[str] = []
    no_speech_probs: list[float] = []
    log_probs: list[float] = []

    for s in segments:
        if s.no_speech_prob > _SEG_NO_SPEECH_MAX or s.avg_logprob < _SEG_LOGPROB_MIN:
            log.info("SEGMENT  dropped %r  (no_speech=%.2f > %.1f or logprob=%.2f < %.1f)",
                     s.text.strip(), s.no_speech_prob, _SEG_NO_SPEECH_MAX,
                     s.avg_logprob, _SEG_LOGPROB_MIN)
            continue
        log.info("SEGMENT  kept    %r  (no_speech=%.2f, logprob=%.2f)", s.text.strip(), s.no_speech_prob, s.avg_logprob)
        parts.append(s.text.strip())
        no_speech_probs.append(s.no_speech_prob)
        log_probs.append(s.avg_logprob)

    text = " ".join(parts).strip()
    avg_no_speech = sum(no_speech_probs) / len(no_speech_probs) if no_speech_probs else 1.0
    avg_logprob   = sum(log_probs)       / len(log_probs)       if log_probs       else -2.0
    return text, avg_no_speech, avg_logprob


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

    text, avg_no_speech, avg_logprob = _transcribe(audio)
    if not text:
        log.info("PROCESS  (empty — dropped by gibberish filter)")
        return {"transcript": "", "valid": False, "verdict": "empty"}

    has_wake_word = bool(_WAKE_PATTERN.search(text))
    high_confidence = avg_no_speech < _HIGH_CONF_NO_SPEECH_MAX and avg_logprob > _HIGH_CONF_LOGPROB_MIN

    log.info("CHECK  wake_word=%s  high_confidence=%s  (no_speech=%.2f, logprob=%.2f)",
             has_wake_word, high_confidence, avg_no_speech, avg_logprob)

    if has_wake_word and high_confidence:
        log.info("PROCESS  %r → SENT (short-circuit — skipped Claude)", text)
        return {"transcript": text, "valid": True, "verdict": "true"}

    if not has_wake_word:
        log.info("FALLBACK  no wake word detected — sending to Claude")
    else:
        log.info("FALLBACK  low confidence (no_speech=%.2f >= %.1f or logprob=%.2f <= %.1f) — sending to Claude",
                 avg_no_speech, _HIGH_CONF_NO_SPEECH_MAX, avg_logprob, _HIGH_CONF_LOGPROB_MIN)

    valid, raw = _validate(text)
    log.info("PROCESS  %r → %s (Claude said %r, no_speech=%.2f, logprob=%.2f)",
             text, "SENT" if valid else "BLOCKED", raw, avg_no_speech, avg_logprob)
    return {"transcript": text, "valid": valid, "verdict": raw}
