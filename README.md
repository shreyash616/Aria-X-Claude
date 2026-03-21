# Aria — Claude

A windowed voice assistant for Windows that wraps the [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) in a proper terminal emulator with hands-free voice control.

Speak to Claude without touching the keyboard. Aria listens for a wake word, transcribes your speech locally or via its cloud API, and forwards your prompt directly into a live Claude session — all inside a clean, coloured terminal window.

---

## How it works

Aria is two components that work together:

```
┌─────────────────────────────────┐        ┌──────────────────────────────┐
│        aria_claude.py           │        │      Aria Voice API          │
│  (Windows desktop app)          │        │  (HuggingFace Space / Docker)│
│                                 │        │                              │
│  tkinter window                 │  WAV   │  faster-whisper (small.en)   │
│  ├─ ConPTY terminal ──► claude  │───────►│  transcription               │
│  ├─ sounddevice mic capture     │        │         │                    │
│  └─ F9 / wake-word trigger      │◄───────│  Claude Haiku validation      │
│                                 │  JSON  │  (wake-word confirmation)     │
└─────────────────────────────────┘        └──────────────────────────────┘
```

1. **Capture** — mic audio is captured locally via `sounddevice`.
2. **Transcribe** — audio is sent to the Aria Voice API, which runs Whisper `small.en` to produce a transcript.
3. **Validate** — if the wake word is ambiguous, Claude Haiku double-checks whether *"Hey Aria"* was said.
4. **Forward** — confirmed prompts are typed into the embedded ConPTY terminal running `claude`.

---

## Features

- **Embedded terminal** — full ConPTY session running `claude` inside a tkinter window, with colour (256-color + truecolor), scrollback, and full keyboard input
- **Push-to-talk** — hold `F9` to record, release to send; a short tone confirms start/stop
- **Wake word** — say *"Hey Aria"* to go hands-free; Aria keeps listening automatically after each response
- **Stop listening** — press `Escape` or say *"stop listening"* to exit hands-free mode at any time
- **Smart validation** — high-confidence transcriptions bypass Claude to save latency; ambiguous audio is verified by Claude Haiku
- **Audio feedback** — distinct tones for wake, record-start, record-stop, and error states
- **Catppuccin Mocha theme** — dark terminal with full ANSI colour support
- **Runs at startup** — optional Windows startup registration via a first-run setup wizard
- **Session logging** — all recognised prompts and API decisions are written to `aria_voice_log.txt`

---

## Requirements

| Requirement | Notes |
|---|---|
| Windows 10 / 11 | ConPTY and winreg are Windows-only |
| Python 3.11+ | Required for type-hint syntax used throughout |
| [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) | Must be installed and authenticated (`claude` on PATH) |
| Microphone | Any input device recognised by Windows |
| Internet connection | For the Aria Voice API (transcription + validation) |
| NVIDIA GPU + CUDA | Optional — only needed if you self-host the API with GPU |

---

## Installation

### Option 1 — Pre-built executable (recommended)

Download `Aria.exe` from the [Releases](../../releases) page. Double-click to run — no Python required.

On first launch a setup wizard asks whether to register Aria in the Windows startup registry.

### Option 2 — Run from source

```bash
git clone https://github.com/shreyashpadhi/Aria-X-Claude
cd Aria-X-Claude
pip install -r requirements.txt
```

Then launch with:

```bash
pythonw aria_setup.py
```

> Use `pythonw` (not `python`) to suppress the console window after setup completes.

---

## Usage

### Starting Aria

```bash
# From source
pythonw aria_setup.py

# Or directly (skips setup wizard)
pythonw aria_claude.py
```

The window opens with a live `claude` session already running inside it. You can type directly into the terminal or use voice input.

### Voice controls

| Action | How |
|---|---|
| Start recording (push-to-talk) | Hold `F9` |
| Send command | Release `F9` |
| Enter hands-free mode | Say *"Hey Aria"* |
| Stop hands-free mode | Press `Escape` or say *"stop listening"* |

**Push-to-talk flow:**
1. Hold `F9` — you hear a short beep, Aria starts recording.
2. Speak your prompt.
3. Release `F9` — audio is sent for transcription.
4. If a valid prompt is returned it is typed into the terminal automatically.

**Hands-free flow:**
1. Say *"Hey Aria"* — Aria beeps twice to confirm it heard you.
2. Speak your prompt (Aria listens until silence is detected).
3. Aria forwards the prompt and immediately listens for the next one.
4. Say *"stop listening"* or press `Escape` to exit.

### Keyboard shortcuts (in terminal)

All standard terminal shortcuts work — `Ctrl+C`, `Ctrl+D`, arrow keys, `Tab`, `Enter`, etc.
`F9` is reserved for voice; all other F-keys pass through to the terminal.

---

## Configuration

| Environment variable | Default | Description |
|---|---|---|
| `ARIA_API_URL` | `https://shreyash616-aria-voice-api.hf.space` | Base URL of the Aria Voice API |

To point Aria at a self-hosted API instance:

```bash
set ARIA_API_URL=http://localhost:7860
pythonw aria_claude.py
```

---

## Building from source (PyInstaller)

The repo includes `Aria.spec` for building a standalone `.exe`:

```bash
pip install pyinstaller
pyinstaller Aria.spec
```

The output is placed in `dist/Aria.exe`.

---

## Project structure

```
Aria-X-Claude/
├── aria_claude.py      # Main app — terminal window + voice input
├── aria_setup.py       # Entry point — first-run wizard, then launches main app
├── aria_voice_log.txt  # Runtime log of recognised prompts and API decisions
├── Aria.spec           # PyInstaller build spec
├── requirements.txt    # Python dependencies for the desktop app
├── pyproject.toml      # Black + Ruff config
└── api/                # Aria Voice API (deployed to HuggingFace Spaces)
    ├── main.py         # FastAPI service — transcription + validation
    ├── requirements.txt
    ├── Dockerfile
    └── README.md
```

---

## Aria Voice API

The transcription and validation backend lives in `api/`. It is deployed as a Docker-based HuggingFace Space and is what the desktop app calls at runtime.

See [`api/README.md`](api/README.md) for full details on endpoints, deployment, and local development.

---

## Logging

Aria writes a log of every session to `aria_voice_log.txt` in the project directory. Each entry records the timestamp, the transcribed text, and whether it was sent to Claude or blocked (with the reason).

---

## License

Source-available, non-commercial. See [LICENSE](LICENSE) for full terms.
For commercial licensing: shreyashpadhi101@gmail.com
