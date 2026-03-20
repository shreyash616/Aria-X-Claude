# Aria — Claude

A windowed voice assistant that wraps the [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) in a proper terminal emulator with hands-free voice control.

## Features

- **Embedded terminal** — full ConPTY session running `claude` inside a tkinter window, with colour, scrollback, and keyboard input
- **Push-to-talk** — hold `F9` to speak, release to send
- **Wake word** — say *"Hey Aria"* to go hands-free; Aria keeps listening for follow-up commands automatically
- **Stop listening** — press `Escape` or say *"stop listening"* to cancel at any time
- **Local transcription** — powered by [faster-whisper](https://github.com/SYSTRAN/faster-whisper); runs entirely on your machine (GPU or CPU)
- **GPU acceleration** — automatically uses CUDA if available (`base.en float16`), falls back to CPU (`small.en int8`)
- **Runs at startup** — registers itself to the Windows startup registry automatically

## Requirements

- Windows 10 / 11
- Python 3.11+
- [Claude CLI](https://docs.anthropic.com/en/docs/claude-cli) installed and authenticated
- A microphone
- (Optional) NVIDIA GPU with CUDA for faster transcription

## Installation

```bash
pip install -r requirements.txt
```

For GPU support, also install the CUDA-enabled build of `ctranslate2`:

```bash
pip install ctranslate2 nvidia-cublas-cu12 nvidia-cudnn-cu12
```

## Usage

```bash
pythonw aria_claude.py
```

> Use `pythonw` instead of `python` to suppress the console window.

### Voice controls

| Action | How |
|---|---|
| Start recording | Hold `F9` |
| Send command | Release `F9` |
| Hands-free mode | Say *"Hey Aria"* |
| Stop listening | Press `Escape` or say *"stop listening"* |

### Keyboard shortcuts (in terminal)

All standard terminal shortcuts work — `Ctrl+C`, arrow keys, `Tab`, `Enter`, etc.

## License

Source-available, non-commercial. See [LICENSE](LICENSE) for full terms.
For commercial licensing: shreyashpadhi101@gmail.com
