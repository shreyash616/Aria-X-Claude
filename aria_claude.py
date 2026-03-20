#!/usr/bin/env python3
# Copyright (c) 2026 Shreyash Padhi. All rights reserved.
# Licensed under the Aria Source Available License v1.0 — see LICENSE file.
# Contact: shreyashpadhi101@gmail.com for commercial licensing.
"""
aria_claude.py — Windowed Claude assistant with voice input (PTY edition)

Embeds a proper ConPTY terminal running `claude` inside a tkinter window.
Continuously listens for speech; ships audio to the Aria Voice API for
transcription + validation, then forwards confirmed prompts to the terminal.

Dependencies:
    pip install sounddevice numpy requests pywinpty pyte
"""

import ctypes
import io
import logging
import os
import re
import shutil
import sys
import threading
import time
import tkinter as tk
import wave
import winreg
from tkinter import font as tkfont

import numpy as np
import pyte
import requests
import sounddevice as sd
from winpty import PtyProcess

# ── Session logger ────────────────────────────────────────────────────────────
_LOG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aria_voice_log.txt")
logging.basicConfig(
    filename=_LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    encoding="utf-8",
)
_log = logging.getLogger("aria")

# ── API config ────────────────────────────────────────────────────────────────
API_BASE_URL = os.environ.get("ARIA_API_URL", "https://shreyash616-aria-voice-api.hf.space").rstrip("/")

# ── Constants ─────────────────────────────────────────────────────────────────
SAMPLE_RATE = 16_000
APP_NAME = "aria_claude"
FONT_SIZE = 11
REDRAW_MS = 50  # ~20 fps

SILENCE_RMS = 0.02
SILENCE_S = 1.2

# ── Catppuccin Mocha palette ──────────────────────────────────────────────────
TERM_BG = "#0c0c0c"
TERM_FG = "#cdd6f4"

PYTE_COLOR_MAP: dict[str, str] = {
    "default": TERM_FG,
    "black": "#45475a",
    "red": "#f38ba8",
    "green": "#a6e3a1",
    "yellow": "#f9e2af",
    "blue": "#89b4fa",
    "magenta": "#cba6f7",
    "cyan": "#89dceb",
    "white": "#cdd6f4",
    "brightblack": "#585b70",
    "brightred": "#f38ba8",
    "brightgreen": "#a6e3a1",
    "brightyellow": "#f9e2af",
    "brightblue": "#89b4fa",
    "brightmagenta": "#cba6f7",
    "brightcyan": "#89dceb",
    "brightwhite": "#ffffff",
}

SPECIAL_KEY_MAP: dict[str, str] = {
    "Return": "\r",
    "KP_Enter": "\r",
    "BackSpace": "\x7f",
    "Tab": "\t",
    "Escape": "\x1b",
    "Up": "\x1b[A",
    "Down": "\x1b[B",
    "Right": "\x1b[C",
    "Left": "\x1b[D",
    "Home": "\x1b[H",
    "End": "\x1b[F",
    "Delete": "\x1b[3~",
    "Insert": "\x1b[2~",
    "Prior": "\x1b[5~",
    "Next": "\x1b[6~",
    "F1": "\x1bOP",
    "F2": "\x1bOQ",
    "F3": "\x1bOR",
    "F4": "\x1bOS",
    "F5": "\x1b[15~",
    "F6": "\x1b[17~",
    "F7": "\x1b[18~",
    "F8": "\x1b[19~",
    # F9 intentionally omitted — it is the voice hotkey
    "F10": "\x1b[21~",
    "F11": "\x1b[23~",
    "F12": "\x1b[24~",
}



# ── 256-color helper ──────────────────────────────────────────────────────────


def _256_to_hex(n: int) -> str:
    if n < 16:
        palette = [
            "#000000",
            "#800000",
            "#008000",
            "#808000",
            "#000080",
            "#800080",
            "#008080",
            "#c0c0c0",
            "#808080",
            "#ff0000",
            "#00ff00",
            "#ffff00",
            "#0000ff",
            "#ff00ff",
            "#00ffff",
            "#ffffff",
        ]
        return palette[n] if n < len(palette) else "#ffffff"
    if n < 232:
        n -= 16
        b = n % 6
        g = (n // 6) % 6
        r = n // 36

        def _c(x: int) -> int:
            return 0 if x == 0 else 55 + x * 40

        return f"#{_c(r):02x}{_c(g):02x}{_c(b):02x}"
    v = 8 + (n - 232) * 10
    return f"#{v:02x}{v:02x}{v:02x}"


def _resolve_color(color, is_bg: bool = False) -> str:
    if color == "default":
        return TERM_BG if is_bg else TERM_FG
    if isinstance(color, int):
        return _256_to_hex(color)
    return PYTE_COLOR_MAP.get(str(color).lower(), TERM_BG if is_bg else TERM_FG)


# ── Audio feedback tones ──────────────────────────────────────────────────────


def _play_tone(freqs: list[float], durations: list[float], vol: float = 0.35, gap_s: float = 0.04):
    """Synthesise and play a sequence of sine-wave tones asynchronously."""

    def _run():
        chunks = []
        fade_n = int(0.008 * SAMPLE_RATE)
        for i, (freq, dur) in enumerate(zip(freqs, durations, strict=False)):
            n = int(SAMPLE_RATE * dur)
            t = np.linspace(0, dur, n, endpoint=False)
            wave = vol * np.sin(2 * np.pi * freq * t).astype(np.float32)
            fade = min(fade_n, n // 4)
            wave[:fade] *= np.linspace(0, 1, fade)
            wave[-fade:] *= np.linspace(1, 0, fade)
            chunks.append(wave)
            if i < len(freqs) - 1:
                chunks.append(np.zeros(int(SAMPLE_RATE * gap_s), dtype=np.float32))
        try:
            sd.play(np.concatenate(chunks), SAMPLE_RATE)
            sd.wait()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()


def _tone_wake():
    _play_tone([660, 880], [0.10, 0.14])


def _tone_rec_start():
    _play_tone([330], [0.07], vol=0.25)


def _tone_rec_stop():
    _play_tone([880, 660], [0.08, 0.10])


def _tone_sent():
    _play_tone([1047, 1319], [0.10, 0.16])


def _tone_active():
    _play_tone([528], [0.08], vol=0.20)


def _tone_error():
    _play_tone([220], [0.20], vol=0.30)


# ── TerminalWidget ────────────────────────────────────────────────────────────


class TerminalWidget(tk.Frame):
    """Tkinter widget that wraps a ConPTY session via pywinpty + pyte."""

    def __init__(self, parent, command: list[str], **kwargs):
        super().__init__(parent, bg=TERM_BG, **kwargs)

        self._font = self._detect_font()

        self._cols = 120
        self._rows = 36
        self._screen = pyte.Screen(self._cols, self._rows)
        self._stream = pyte.ByteStream(self._screen)
        self._screen_lock = threading.Lock()
        self._dirty = False
        self._first_render = True
        self._need_full_redraw = False

        self._pty: PtyProcess | None = None
        self._pty_lock = threading.Lock()

        self._tag_cache: dict[tuple, str] = {}
        self._tag_counter = 0

        self._text = tk.Text(
            self,
            font=self._font,
            bg=TERM_BG,
            fg=TERM_FG,
            insertbackground=TERM_FG,
            relief=tk.FLAT,
            borderwidth=0,
            wrap=tk.NONE,
            cursor="xterm",
            state=tk.DISABLED,
        )
        vsb = tk.Scrollbar(self, orient=tk.VERTICAL, command=self._text.yview)
        hsb = tk.Scrollbar(self, orient=tk.HORIZONTAL, command=self._text.xview)
        self._text.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        vsb.pack(side=tk.RIGHT, fill=tk.Y)
        hsb.pack(side=tk.BOTTOM, fill=tk.X)
        self._text.pack(fill=tk.BOTH, expand=True)

        self._text.bind("<Key>", self._on_key)
        self._text.focus_set()
        self.bind("<Configure>", self._on_resize)

        threading.Thread(target=self._run_pty, args=(command,), daemon=True).start()
        self.after(REDRAW_MS, self._redraw_loop)

    # ── Font detection ─────────────────────────────────────────────────────

    @staticmethod
    def _detect_font() -> tuple[str, int]:
        available = set(tkfont.families())
        for name in ("Cascadia Code", "Cascadia Mono", "Consolas", "Courier New", "Courier"):
            if name in available:
                return (name, FONT_SIZE)
        return ("Courier New", FONT_SIZE)

    # ── PTY lifecycle ──────────────────────────────────────────────────────

    def _run_pty(self, command: list[str]):
        try:
            env = os.environ.copy()
            env.setdefault("TERM", "xterm-256color")
            env.setdefault("COLORTERM", "truecolor")
            extra_paths = [
                os.path.expanduser(r"~\.local\bin"),
                os.path.join(os.environ.get("APPDATA", ""), "npm"),
                os.path.join(os.environ.get("LOCALAPPDATA", ""), "npm"),
            ]
            env["PATH"] = (
                os.pathsep.join(p for p in extra_paths if os.path.isdir(p))
                + os.pathsep
                + env.get("PATH", "")
            )
            pty = PtyProcess.spawn(command, dimensions=(self._rows, self._cols), env=env)
        except Exception as exc:
            msg = f"Failed to start terminal: {exc}"
            self.after(0, lambda: self._show_error(msg))
            return

        with self._pty_lock:
            self._pty = pty

        while pty.isalive():
            try:
                data = pty.read(4096)
                if not data:
                    continue
                raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else data
                with self._screen_lock:
                    self._stream.feed(raw)
                self._dirty = True

            except EOFError:
                break
            except Exception:
                break

    def _show_error(self, msg: str):
        self._text.config(state=tk.NORMAL)
        self._text.insert(tk.END, f"\n[{msg}]\n")
        self._text.config(state=tk.DISABLED)

    # ── Rendering ──────────────────────────────────────────────────────────

    def _redraw_loop(self):
        if self._dirty:
            self._dirty = False
            self._redraw()
        self.after(REDRAW_MS, self._redraw_loop)

    def _get_tag(self, fg: str, bg: str, bold: bool, italic: bool, reverse: bool) -> str:
        if reverse:
            fg, bg = bg, fg
        key = (fg, bg, bold, italic)
        if key in self._tag_cache:
            return self._tag_cache[key]
        name = f"t{self._tag_counter}"
        self._tag_counter += 1
        style = " ".join(s for s in ("bold" if bold else "", "italic" if italic else "") if s)
        self._text.tag_configure(
            name,
            foreground=fg,
            background=bg,
            font=(*self._font, style) if style else self._font,
        )
        self._tag_cache[key] = name
        return name

    def _build_segments(self, line: dict, cols: int) -> list[tuple[str, str]]:
        segments: list[tuple[str, str]] = []
        cur_tag: str | None = None
        cur_chars: list[str] = []

        for x in range(cols):
            ch = line.get(x)
            data = ch.data if ch else " "
            fg = _resolve_color(ch.fg if ch else "default", is_bg=False)
            bg = _resolve_color(ch.bg if ch else "default", is_bg=True)
            bold = ch.bold if ch else False
            italic = getattr(ch, "italics", getattr(ch, "italic", False)) if ch else False
            reverse = ch.reverse if ch else False
            tag = self._get_tag(fg, bg, bold, italic, reverse)
            if tag != cur_tag:
                if cur_chars and cur_tag is not None:
                    segments.append(("".join(cur_chars), cur_tag))
                cur_tag = tag
                cur_chars = [data]
            else:
                cur_chars.append(data)

        if cur_chars and cur_tag is not None:
            segments.append(("".join(cur_chars), cur_tag))
        return segments

    def _redraw(self):
        with self._screen_lock:
            full = self._first_render or self._need_full_redraw
            if full:
                dirty_lines = set(range(self._screen.lines))
            else:
                dirty_lines = set(self._screen.dirty)
            self._screen.dirty.clear()
            buf = {y: dict(self._screen.buffer.get(y, {})) for y in dirty_lines}
            cur_x = self._screen.cursor.x
            cur_y = self._screen.cursor.y
            cols = self._screen.columns
            rows = self._screen.lines

        if not dirty_lines:
            return

        self._text.config(state=tk.NORMAL)

        if full:
            self._text.delete("1.0", tk.END)
            for y in range(rows):
                for text, tag in self._build_segments(buf.get(y, {}), cols):
                    self._text.insert(tk.END, text, tag)
                if y < rows - 1:
                    self._text.insert(tk.END, "\n")
            self._first_render = False
            self._need_full_redraw = False
        else:
            for y in sorted(dirty_lines):
                if y >= rows:
                    continue
                self._text.delete(f"{y + 1}.0", f"{y + 1}.end")
                for text, tag in self._build_segments(buf.get(y, {}), cols):
                    self._text.insert(f"{y + 1}.end", text, tag)

        try:
            idx = f"{cur_y + 1}.{cur_x}"
            self._text.mark_set(tk.INSERT, idx)
            self._text.see(idx)
        except Exception:
            pass

        self._text.config(state=tk.DISABLED)

    # ── Input ──────────────────────────────────────────────────────────────

    def _on_key(self, event: tk.Event):
        with self._pty_lock:
            pty = self._pty
        if pty is None or not pty.isalive():
            return "break"

        ctrl = bool(event.state & 0x4)

        if ctrl and len(event.keysym) == 1 and event.keysym.isalpha():
            code = ord(event.keysym.upper()) - 64
            if 1 <= code <= 26:
                pty.write(chr(code))
            return "break"

        if event.keysym in SPECIAL_KEY_MAP:
            pty.write(SPECIAL_KEY_MAP[event.keysym])
            return "break"

        if event.char:
            pty.write(event.char)
        return "break"

    # ── Resize ─────────────────────────────────────────────────────────────

    def _on_resize(self, event: tk.Event):
        try:
            f = tkfont.Font(family=self._font[0], size=self._font[1])
            cw = f.measure("M")
            ch = f.metrics("linespace")
        except Exception:
            return
        if cw <= 0 or ch <= 0:
            return
        new_cols = max(20, event.width // cw)
        new_rows = max(5, event.height // ch)
        if new_cols == self._cols and new_rows == self._rows:
            return
        self._cols, self._rows = new_cols, new_rows
        with self._screen_lock:
            self._screen.resize(new_rows, new_cols)
        with self._pty_lock:
            if self._pty and self._pty.isalive():
                self._pty.setwinsize(new_rows, new_cols)
        self._need_full_redraw = True
        self._dirty = True

    # ── Public API ─────────────────────────────────────────────────────────

    def send_text(self, text: str):
        with self._pty_lock:
            pty = self._pty
        if pty and pty.isalive():
            pty.write(text + "\r")

    def send_raw(self, seq: str):
        with self._pty_lock:
            pty = self._pty
        if pty and pty.isalive():
            pty.write(seq)

    def terminate(self):
        with self._pty_lock:
            if self._pty:
                try:
                    self._pty.terminate(force=True)
                except Exception:
                    pass


# ── AriaApp ──────────────────────────────────────────────────────────────────


class AriaApp:
    """
    State machine for voice input:

      idle        — waiting for model to load / mic to connect
      listening   — continuously recording; waits for speech then silence
      processing  — transcribing audio + validating with Claude sub-agent

      Transitions:
        startup               → idle
        idle + model ready    → listening
        listening + silence   → processing
        processing → done     → listening
    """

    def __init__(self):
        self._ensure_single_instance()
        self.root = tk.Tk()
        self.root.title("Aria — Claude")
        self.root.geometry("1280x820")
        self.root.configure(bg="#1e1e2e")
        self.root.minsize(800, 500)

        self._state = "idle"
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()

        # Mic health tracking
        self._mic_connected: bool = False

        self._api_ready: bool = False
        self._terminal: TerminalWidget | None = None

        self._build_ui()
        self._start_voice_thread()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.mainloop()

    # ── UI ────────────────────────────────────────────────────────────────

    def _build_ui(self):
        bar = tk.Frame(self.root, bg="#313244", height=44)
        bar.pack(fill=tk.X, side=tk.TOP)
        bar.pack_propagate(False)

        tk.Label(
            bar,
            text="⚡  Aria  ×  Claude",
            bg="#313244",
            fg="#cdd6f4",
            font=("Segoe UI", 12, "bold"),
        ).pack(side=tk.LEFT, padx=14, pady=10)

        tk.Label(
            bar,
            text="speech is transcribed + validated via Aria API",
            bg="#313244",
            fg="#7f849c",
            font=("Segoe UI", 9),
        ).pack(side=tk.RIGHT, padx=14, pady=10)

        self._status_var = tk.StringVar(value="● Warming up …")
        self._status_lbl = tk.Label(
            bar,
            textvariable=self._status_var,
            bg="#313244",
            fg="#45475a",
            font=("Consolas", 10),
        )
        self._status_lbl.pack(side=tk.RIGHT, padx=8, pady=10)

        _extra_path = os.pathsep.join(
            filter(
                os.path.isdir,
                [
                    os.path.expanduser(r"~\.local\bin"),
                    os.path.join(os.environ.get("APPDATA", ""), "npm"),
                    os.path.join(os.environ.get("LOCALAPPDATA", ""), "npm"),
                ],
            )
        )
        claude_exe = (
            shutil.which("claude")
            or shutil.which("claude", path=_extra_path + os.pathsep + os.environ.get("PATH", ""))
            or os.path.expanduser(r"~\.local\bin\claude.exe")
        )
        if not claude_exe or not os.path.exists(claude_exe):
            self._terminal = None
            tk.Label(
                self.root,
                text=(
                    "Claude CLI not found.\n\n"
                    "Install it with:  npm install -g @anthropic-ai/claude-code\n"
                    "Then restart Aria."
                ),
                bg=TERM_BG,
                fg="#f38ba8",
                font=("Consolas", 12),
                justify=tk.CENTER,
            ).pack(expand=True)
            return
        self._terminal = TerminalWidget(
            self.root,
            command=["cmd.exe", "/k", claude_exe],
        )
        self._terminal.pack(fill=tk.BOTH, expand=True)

        foot = tk.Frame(self.root, bg="#181825", height=22)
        foot.pack(fill=tk.X, side=tk.BOTTOM)
        foot.pack_propagate(False)

        self._model_lbl = tk.Label(
            foot,
            text="Warming up …",
            bg="#181825",
            fg="#fab387",
            font=("Segoe UI", 8),
        )
        self._model_lbl.pack(side=tk.LEFT, padx=8, pady=3)

        tk.Label(foot, text=APP_NAME, bg="#181825", fg="#313244", font=("Segoe UI", 8)).pack(
            side=tk.RIGHT, padx=8, pady=3
        )

    def _set_status(self, text: str, color: str = "#45475a"):
        self._status_var.set(text)
        self._status_lbl.config(fg=color)

    def _start_label_anim(self, text: str, color: str) -> None:
        self._anim_running = True
        self._anim_idx = 0
        frames = ["   ", ".  ", ".. ", "..."]

        def _tick():
            if not getattr(self, "_anim_running", False):
                return
            self._model_lbl.config(
                text=f"{text}{frames[self._anim_idx % len(frames)]}", fg=color
            )
            self._anim_idx += 1
            self._anim_id = self.root.after(400, _tick)

        _tick()

    def _stop_label_anim(self) -> None:
        self._anim_running = False
        if hasattr(self, "_anim_id"):
            self.root.after_cancel(self._anim_id)

    # ── API bootstrap ──────────────────────────────────────────────────────

    def _start_voice_thread(self):
        threading.Thread(target=self._connect_and_start, daemon=True).start()

    def _connect_and_start(self):
        """Poll /health until the API is reachable, then start the record loop."""
        self.root.after(0, lambda: self._start_label_anim("Connecting to API", "#fab387"))
        url = f"{API_BASE_URL}/health"
        MAX_WAIT_S = 120
        waited = 0
        while waited < MAX_WAIT_S:
            try:
                r = requests.get(url, timeout=10)
                if r.status_code == 200:
                    break
            except Exception:
                pass
            time.sleep(3)
            waited += 3
        else:
            self.root.after(0, self._stop_label_anim)
            self.root.after(0, lambda: self._model_lbl.config(
                text="⚠ API unreachable — check your internet connection and restart Aria.",
                fg="#f38ba8",
            ))
            self.root.after(0, lambda: self._set_status("⚠ API offline", "#f38ba8"))
            return

        self._api_ready = True
        self.root.after(0, self._stop_label_anim)
        self.root.after(
            0,
            lambda: self._model_lbl.config(text="API ready  |  listening for speech", fg="#a6e3a1"),
        )

        threading.Thread(target=self._record_loop, daemon=True).start()

    # ── Shared audio stream ────────────────────────────────────────────────

    def _record_loop(self):
        """Continuously read from the mic. Retries on disconnect."""
        RETRY_S = 2.0

        while True:
            try:
                stream_ctx = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32")
                stream_ctx.__enter__()
            except Exception:
                if self._mic_connected:
                    self._mic_connected = False
                    self.root.after(0, self._on_mic_disconnected)
                time.sleep(RETRY_S)
                continue

            was_connected = self._mic_connected
            self._mic_connected = True
            if not was_connected:
                self.root.after(0, self._on_mic_reconnected)

            try:
                while True:
                    chunk, _ = stream_ctx.read(512)
                    chunk = chunk.copy().flatten()
                    with self._lock:
                        if self._state == "listening":
                            self._frames.append(chunk)
            except Exception as exc:
                print(f"[audio] stream error: {exc}")
            finally:
                self._mic_connected = False
                self.root.after(0, self._on_mic_disconnected)
                try:
                    stream_ctx.__exit__(None, None, None)
                except Exception:
                    pass

            time.sleep(RETRY_S)

    def _on_mic_disconnected(self):
        with self._lock:
            if self._state == "listening":
                self._state = "idle"
                self._frames.clear()
        self._set_status("⚠ Microphone disconnected", "#f38ba8")
        self._model_lbl.config(
            text="Reconnect your microphone — Aria will detect it automatically", fg="#f38ba8"
        )

    def _on_mic_reconnected(self):
        self._set_status("✓ Microphone connected", "#a6e3a1")
        self._model_lbl.config(text="Mic ready  |  listening for speech", fg="#a6e3a1")
        self._enter_listening()

    # ── Listening ─────────────────────────────────────────────────────────

    def _enter_listening(self):
        """Switch to listening state and start the listening loop."""
        if not self._api_ready:
            return  # API not reachable yet; _connect_and_start will call this
        with self._lock:
            self._state = "listening"
            self._frames.clear()
        self.root.after(0, lambda: self._set_status('Listening…  (say "Aria" anywhere in your command)', "#cba6f7"))
        threading.Thread(target=self._listening_loop, daemon=True).start()

    def _listening_loop(self):
        """
        Phase 1: wait for voice activity, clearing silence frames.
        Phase 2: record until silence after speech, then hand off.
        """
        # Phase 1 — wait for speech onset
        while True:
            time.sleep(0.1)
            with self._lock:
                if self._state != "listening":
                    return
                frames = list(self._frames)

            if sum(len(f) for f in frames) / SAMPLE_RATE < 0.2:
                continue

            recent = np.concatenate(frames)[-int(0.3 * SAMPLE_RATE):]
            rms = float(np.sqrt(np.mean(recent**2)))

            if rms >= SILENCE_RMS:
                break  # speech detected

            # Still silence — clear accumulated noise frames
            with self._lock:
                if self._state == "listening":
                    self._frames.clear()

        self.root.after(0, lambda: self._set_status('Listening…  (say "Aria" anywhere in your command)', "#cba6f7"))

        # Phase 2 — record until silence
        silence_since: float | None = None
        started = time.time()

        while True:
            time.sleep(0.1)
            with self._lock:
                if self._state != "listening":
                    return
                frames = list(self._frames)

            if not frames or sum(len(f) for f in frames) / SAMPLE_RATE < 0.5:
                continue

            recent = np.concatenate(frames)[-int(0.3 * SAMPLE_RATE):]
            rms = float(np.sqrt(np.mean(recent**2)))

            if rms < SILENCE_RMS:
                if silence_since is None:
                    silence_since = time.time()
                elif time.time() - silence_since >= SILENCE_S:
                    break
            else:
                silence_since = None

            if time.time() - started > 30:
                break

        with self._lock:
            if self._state != "listening":
                return
            self._state = "processing"
            frames = list(self._frames)
            self._frames.clear()

        self.root.after(0, lambda: self._set_status("Transcribing...", "#fab387"))
        threading.Thread(target=self._transcribe_and_send, args=(frames,), daemon=True).start()

    # ── API call ───────────────────────────────────────────────────────────

    @staticmethod
    def _frames_to_wav(frames: list[np.ndarray]) -> bytes:
        audio = np.concatenate(frames).flatten()
        pcm = (audio * 32767).clip(-32768, 32767).astype(np.int16)
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm.tobytes())
        return buf.getvalue()

    # ── Transcription + send ───────────────────────────────────────────────

    def _transcribe_and_send(self, frames: list[np.ndarray]):
        """Ship audio to the API, validate, send to terminal if valid."""
        try:
            if not frames:
                return

            wav = self._frames_to_wav(frames)
            resp = requests.post(
                f"{API_BASE_URL}/process",
                files={"file": ("audio.wav", wav, "audio/wav")},
                timeout=30,
            )
            resp.raise_for_status()
            data = resp.json()

            text = data.get("transcript", "")
            valid = data.get("valid", False)
            verdict = data.get("verdict", "")
            verdict_label = "SENT" if valid else "BLOCKED"

            if not text:
                _log.info("TRANSCRIBED  (empty — skipped)")
                return

            _log.info("TRANSCRIBED  %r", text)
            _log.info("VERDICT      %s  (subagent raw: %r)", verdict_label, verdict)

            if valid and self._terminal:
                cleaned = re.sub(r'\b(aria|arya|area|ariel|ariah|aeria|areia)\b[,\s]*', '', text, flags=re.IGNORECASE).strip()
                self._terminal.send_text(cleaned or text)
            elif not valid:
                preview = text if len(text) <= 42 else text[:42] + "…"
                self.root.after(0, lambda p=preview: self._set_status(f"✗ Blocked: {p}", "#f38ba8"))
                self.root.after(3000, lambda: self._set_status('Listening…  (say "Aria" anywhere in your command)', "#cba6f7"))

        except requests.exceptions.ConnectionError:
            _log.error("TRANSCRIBE ERROR  API unreachable")
            self.root.after(0, lambda: self._set_status("⚠ API unreachable", "#f38ba8"))
            self.root.after(3000, lambda: self._set_status('Listening…  (say "Aria" anywhere in your command)', "#cba6f7"))
        except requests.exceptions.Timeout:
            _log.error("TRANSCRIBE ERROR  API timed out")
            self.root.after(0, lambda: self._set_status("⚠ API timed out", "#f38ba8"))
            self.root.after(3000, lambda: self._set_status('Listening…  (say "Aria" anywhere in your command)', "#cba6f7"))
        except Exception as exc:
            _log.exception("TRANSCRIBE ERROR")
            self.root.after(0, lambda: self._set_status("⚠ Error — see log", "#f38ba8"))
            self.root.after(3000, lambda: self._set_status('Listening…  (say "Aria" anywhere in your command)', "#cba6f7"))
            print(f"[transcribe] error: {exc}")
        finally:
            with self._lock:
                if self._state == "processing":
                    self._state = "idle"
            self._enter_listening()

    # ── Startup registration ───────────────────────────────────────────────

    def _register_startup(self):
        script = os.path.abspath(__file__)
        pythonw = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(pythonw):
            pythonw = sys.executable
        cmd = f'"{pythonw}" "{script}"'
        try:
            key = winreg.OpenKey(
                winreg.HKEY_CURRENT_USER,
                r"Software\Microsoft\Windows\CurrentVersion\Run",
                0,
                winreg.KEY_SET_VALUE,
            )
            winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, cmd)
            winreg.CloseKey(key)
        except Exception as exc:
            print(f"[startup] Could not register: {exc}")

    # ── Single-instance guard ──────────────────────────────────────────────

    def _ensure_single_instance(self):
        """Exit immediately if another instance of Aria is already running."""
        self._mutex = ctypes.windll.kernel32.CreateMutexW(None, True, f"Global\\{APP_NAME}Mutex")
        if ctypes.windll.kernel32.GetLastError() == 183:  # ERROR_ALREADY_EXISTS
            sys.exit(0)

    # ── Cleanup ────────────────────────────────────────────────────────────

    def _on_close(self):
        if self._terminal:
            self._terminal.terminate()
        self.root.destroy()


if __name__ == "__main__":
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
    except Exception:
        pass
    AriaApp()
