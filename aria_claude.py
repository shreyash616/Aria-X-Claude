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

import collections
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
SILENCE_S = 2.0

# ── Catppuccin Mocha palette ──────────────────────────────────────────────────
TERM_BG = "#0c0c0c"
TERM_FG = "#cdd6f4"

PYTE_COLOR_MAP: dict[str, str] = {
    "default": TERM_FG,
    "black": "#45475a",
    "red": "#f38ba8",
    "green": "#a6e3a1",
    "yellow": "#f9e2af",
    "brown": "#f9e2af",       # pyte uses "brown" for ANSI color 3 (ESC[33m)
    "blue": "#89b4fa",
    "magenta": "#cba6f7",
    "cyan": "#89dceb",
    "white": "#cdd6f4",
    "brightblack": "#585b70",
    "brightred": "#f38ba8",
    "brightgreen": "#a6e3a1",
    "brightyellow": "#f9e2af",
    "brightbrown": "#f9e2af",  # pyte uses "brightbrown" for ESC[93m
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
        # Catppuccin Mocha — must match PYTE_COLOR_MAP
        palette = [
            "#45475a",  # 0  black
            "#f38ba8",  # 1  red
            "#a6e3a1",  # 2  green
            "#f9e2af",  # 3  yellow
            "#89b4fa",  # 4  blue
            "#cba6f7",  # 5  magenta
            "#89dceb",  # 6  cyan
            "#cdd6f4",  # 7  white
            "#585b70",  # 8  bright black
            "#f38ba8",  # 9  bright red
            "#a6e3a1",  # 10 bright green
            "#f9e2af",  # 11 bright yellow
            "#89b4fa",  # 12 bright blue
            "#cba6f7",  # 13 bright magenta
            "#89dceb",  # 14 bright cyan
            "#ffffff",  # 15 bright white
        ]
        return palette[n]
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
    # pyte returns 256-color and truecolor as bare 6-char hex strings (e.g. "00cd00")
    s = str(color).lower()
    if len(s) == 6 and all(c in "0123456789abcdef" for c in s):
        return f"#{s}"
    return PYTE_COLOR_MAP.get(s, TERM_BG if is_bg else TERM_FG)


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

    def __init__(self, parent, command: list[str], on_first_output=None, on_f9=None, **kwargs):
        super().__init__(parent, bg=TERM_BG, **kwargs)

        self._font = self._detect_font()
        self._on_first_output = on_first_output
        self._on_f9 = on_f9
        self._first_output_fired = False

        self._cols = 120
        self._rows = 36
        self._screen = pyte.HistoryScreen(self._cols, self._rows, history=500, ratio=3/self._rows)
        self._stream = pyte.ByteStream(self._screen)
        self._screen_lock = threading.Lock()
        self._dirty = False
        self._scroll_dirty = False
        self._in_history = False
        self._first_render = True
        self._need_full_redraw = False

        self._pty: PtyProcess | None = None
        self._pty_lock = threading.Lock()

        self._tag_cache: dict[tuple, str] = {}
        self._tag_counter = 0
        self._resize_job = None
        self._pty_buf: collections.deque[bytes] = collections.deque(maxlen=256)  # ~1 MB cap
        self._font_obj = tkfont.Font(family=self._font[0], size=self._font[1])
        self._cw: int = 0
        self._ch: int = 0

        self._text = tk.Text(
            self,
            font=self._font,
            bg=TERM_BG,
            fg=TERM_FG,
            insertbackground=TERM_FG,
            relief=tk.FLAT,
            borderwidth=0,
            wrap=tk.CHAR,
            cursor="xterm",
            state=tk.NORMAL,
            undo=False,  # prevent unbounded undo stack growth from continuous redraws
        )
        self._text.pack(fill=tk.BOTH, expand=True)
        self._cw = self._font_obj.measure("M")
        self._ch = self._font_obj.metrics("linespace")

        self._text.bind("<Key>", self._on_key)
        self._text.bind("<MouseWheel>", self._on_mousewheel)
        self.bind("<MouseWheel>", self._on_mousewheel)
        self._text.bind("<Button-3>", self._on_right_click)
        self._text.bind("<<Paste>>", lambda e: "break")
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
            if not self._first_output_fired and self._on_first_output:
                self._first_output_fired = True
                self.after(0, self._on_first_output)
            return

        with self._pty_lock:
            self._pty = pty

        while pty.isalive():
            try:
                data = pty.read(4096)
                if not data:
                    continue
                raw = data.encode("utf-8", errors="replace") if isinstance(data, str) else data
                self._pty_buf.append(raw)
                self._dirty = True
                if not self._first_output_fired and self._on_first_output:
                    self._first_output_fired = True
                    self.after(0, self._on_first_output)

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
        # Drain PTY output buffer into pyte — cap per frame to avoid blocking the main thread
        if self._pty_buf:
            with self._screen_lock:
                for _ in range(64):  # max 64 × 4 KB = 256 KB per frame
                    if not self._pty_buf:
                        break
                    self._stream.feed(self._pty_buf.popleft())
        if self._dirty and self._in_history:
            # New PTY output arrived while scrolled — jump back to bottom
            with self._screen_lock:
                while self._screen.history.position > 0:
                    self._screen.next_page()
            self._in_history = False
            self._need_full_redraw = True
        if self._dirty or self._scroll_dirty:
            self._dirty = False
            self._scroll_dirty = False
            self._redraw()
        self.after(REDRAW_MS, self._redraw_loop)

    def _get_tag(self, fg: str, bg: str, bold: bool, italic: bool, reverse: bool) -> str:
        if reverse:
            fg, bg = bg, fg
        key = (fg, bg, bold, italic)
        if key in self._tag_cache:
            return self._tag_cache[key]
        # Purge cache when it grows too large to prevent tkinter slowdown
        if len(self._tag_cache) >= 256:
            for tag_name in self._tag_cache.values():
                try:
                    self._text.tag_delete(tag_name)
                except Exception:
                    pass
            self._tag_cache.clear()
            self._tag_counter = 0
            self._need_full_redraw = True
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

        # Preserve any active mouse selection across the redraw
        try:
            sel_first = self._text.index(tk.SEL_FIRST)
            sel_last = self._text.index(tk.SEL_LAST)
        except tk.TclError:
            sel_first = sel_last = None

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

        # Restore selection — indices are still valid since line content is unchanged
        if sel_first and sel_last:
            try:
                self._text.tag_add(tk.SEL, sel_first, sel_last)
            except tk.TclError:
                pass

        try:
            idx = f"{cur_y + 1}.{cur_x}"
            self._text.mark_set(tk.INSERT, idx)
            if not self._in_history:
                self._text.see(idx)
        except Exception:
            pass

    # ── Input ──────────────────────────────────────────────────────────────

    def _copy_selection(self):
        try:
            text = self._text.get(tk.SEL_FIRST, tk.SEL_LAST)
            self.clipboard_clear()
            self.clipboard_append(text)
        except tk.TclError:
            pass

    def _on_right_click(self, event: tk.Event):
        menu = tk.Menu(self, tearoff=0, bg="#313244", fg="#cdd6f4",
                       activebackground="#45475a", activeforeground="#cdd6f4",
                       relief=tk.FLAT, borderwidth=0)
        menu.add_command(label="Copy", command=self._copy_selection)
        menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _on_key(self, event: tk.Event):
        with self._pty_lock:
            pty = self._pty
        if pty is None or not pty.isalive():
            return "break"

        ctrl = bool(event.state & 0x4)

        # Ctrl+C: copy selection if present, otherwise send interrupt to PTY
        if ctrl and event.keysym.lower() == "c":
            try:
                self._text.get(tk.SEL_FIRST, tk.SEL_LAST)
                self._copy_selection()
            except tk.TclError:
                pty.write("\x03")
            return "break"

        if ctrl and len(event.keysym) == 1 and event.keysym.isalpha():
            code = ord(event.keysym.upper()) - 64
            if 1 <= code <= 26:
                pty.write(chr(code))
            return "break"

        if event.keysym == "F9":
            if self._on_f9:
                self.after(0, self._on_f9)
            return "break"

        if event.keysym in SPECIAL_KEY_MAP:
            pty.write(SPECIAL_KEY_MAP[event.keysym])
            return "break"

        if event.char:
            pty.write(event.char)
        return "break"

    def _on_mousewheel(self, event: tk.Event):
        steps = max(1, abs(event.delta) // 120)
        with self._screen_lock:
            for _ in range(steps):
                if event.delta > 0:
                    self._screen.prev_page()
                else:
                    self._screen.next_page()
            self._in_history = self._screen.history.position > 0
        self._need_full_redraw = True
        self._scroll_dirty = True
        return "break"

    # ── Resize ─────────────────────────────────────────────────────────────

    def _on_resize(self, event: tk.Event):
        if self._resize_job is not None:
            self.after_cancel(self._resize_job)
        self._resize_job = self.after(200, self._apply_resize, event.width, event.height)

    def _apply_resize(self, width: int, height: int):
        self._resize_job = None
        cw, ch = self._cw, self._ch
        if cw <= 0 or ch <= 0:
            return
        new_cols = max(20, width // cw)
        new_rows = max(5, height // ch)
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

        # Must be set before the window is created for Windows to use it for taskbar grouping
        try:
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("aria.claude.app")
        except Exception:
            pass

        self.root = tk.Tk()
        self.root.title("Aria — Claude")
        self.root.configure(bg="#1e1e2e")
        self.root.minsize(800, 500)
        self.root.state("zoomed")

        _icon = os.path.join(os.path.dirname(os.path.abspath(__file__)), "aria.ico")
        if os.path.exists(_icon):
            self.root.iconbitmap(_icon)
            try:
                # Set large icon so the taskbar button shows the app icon, not python.exe's
                WM_SETICON = 0x0080
                LR_LOADFROMFILE = 0x10
                hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())
                for size, which in ((16, 0), (32, 1)):  # ICON_SMALL=0, ICON_BIG=1
                    hicon = ctypes.windll.user32.LoadImageW(None, _icon, 1, size, size, LR_LOADFROMFILE)
                    if hicon:
                        ctypes.windll.user32.SendMessageW(hwnd, WM_SETICON, which, hicon)
            except Exception:
                pass

        self._state = "idle"
        self._paused = False
        self._frames: list[np.ndarray] = []
        self._lock = threading.Lock()
        self._listening_active = False
        self._listening_guard = threading.Lock()
        self._status_restore_id: str | None = None

        # Mic health tracking
        self._mic_connected: bool = False
        self._mic_active = threading.Event()
        self._mic_active.set()  # active by default; cleared while paused

        self._terminal_ready: bool = False
        self._api_ready: bool = False
        self._mic_ready: bool = False
        self._terminal: TerminalWidget | None = None

        self._build_ui()
        self.root.bind("<F9>", lambda e: self._toggle_pause())
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
            fg="#94e2d5",
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

        foot = tk.Frame(self.root, bg="#181825", height=22)
        foot.pack(fill=tk.X, side=tk.BOTTOM)
        foot.pack_propagate(False)

        self._model_lbl = tk.Label(
            foot,
            text="",
            bg="#181825",
            fg="#94e2d5",
            font=("Segoe UI", 8),
        )
        self._model_lbl.pack(side=tk.LEFT, padx=8, pady=3)

        tk.Label(foot, text=APP_NAME, bg="#181825", fg="#313244", font=("Segoe UI", 8)).pack(
            side=tk.RIGHT, padx=8, pady=3
        )

        self._content = tk.Frame(self.root, bg=TERM_BG)
        self._content.pack(fill=tk.BOTH, expand=True)

        if not claude_exe or not os.path.exists(claude_exe):
            self._terminal = None
            tk.Label(
                self._content,
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

        self._loading_frame = self._build_loading_view(self._content)
        self._loading_frame.pack(fill=tk.BOTH, expand=True)

        self._terminal = TerminalWidget(
            self._content,
            command=["cmd.exe", "/k", claude_exe, "--remote-control"],
            on_first_output=self._on_claude_ready,
            on_f9=self._toggle_pause,
        )
        # Terminal is created but not packed — shown after loading
        # Start API + mic checks in parallel with terminal startup
        self._start_voice_thread()

    def _build_loading_view(self, parent: tk.Frame) -> tk.Frame:
        frame = tk.Frame(parent, bg=TERM_BG)

        # Vertically centred inner block
        inner = tk.Frame(frame, bg=TERM_BG)
        inner.place(relx=0.5, rely=0.5, anchor=tk.CENTER)

        tk.Label(
            inner,
            text="⚡  Aria  ×  Claude",
            bg=TERM_BG,
            fg="#cdd6f4",
            font=("Segoe UI", 22, "bold"),
        ).pack(pady=(0, 10))

        self._loading_lbl = tk.Label(
            inner,
            text="Loading Claude...",
            bg=TERM_BG,
            fg="#7f849c",
            font=("Segoe UI", 11),
        )
        self._loading_lbl.pack()

        self._loading_anim_idx = 0
        self._loading_anim_running = True
        self._loading_anim_id: str | None = None
        self._loading_start = time.time()
        self._loading_status = "Starting Claude"
        self._animate_loading()

        return frame

    def _animate_loading(self):
        if not self._loading_anim_running:
            return
        dots = ["   ", ".  ", ".. ", "..."]
        self._loading_lbl.config(text=f"{self._loading_status}{dots[self._loading_anim_idx % 4]}")
        self._loading_anim_idx += 1
        self._loading_anim_id = self.root.after(420, self._animate_loading)

    _MIN_LOADING_S = 2.0

    def _on_claude_ready(self):
        self._terminal_ready = True
        self._loading_status = "Connecting to API"
        self._check_all_ready()

    def _check_all_ready(self):
        if not (self._terminal_ready and self._api_ready and self._mic_ready):
            return
        elapsed = time.time() - self._loading_start
        delay_ms = max(0, int((self._MIN_LOADING_S - elapsed) * 1000))
        self.root.after(delay_ms, self._show_terminal)

    def _show_terminal(self):
        if getattr(self, "_terminal_shown", False):
            return
        self._terminal_shown = True
        self._loading_anim_running = False
        if self._loading_anim_id:
            self.root.after_cancel(self._loading_anim_id)
        self._loading_frame.pack_forget()
        self._terminal.pack(fill=tk.BOTH, expand=True)
        self._terminal.focus_set()
        self._enter_listening()

    def _tick_transcribe_status(self):
        with self._lock:
            if self._state != "processing":
                return
        elapsed = int(time.time() - self._transcribe_start)
        self._set_status(f"Transcribing… {elapsed}s  ·  F9 to cancel and pause listening", "#fab387")
        self._transcribe_timer_id = self.root.after(1000, self._tick_transcribe_status)

    def _stop_transcribe_timer(self):
        if hasattr(self, "_transcribe_timer_id"):
            self.root.after_cancel(self._transcribe_timer_id)

    def _schedule_status_restore(self):
        """Schedule (or re-schedule) the 3-second status restore, cancelling any pending one."""
        if self._status_restore_id is not None:
            try:
                self.root.after_cancel(self._status_restore_id)
            except Exception:
                pass
        self._status_restore_id = self.root.after(
            3000,
            lambda: (
                setattr(self, "_status_restore_id", None) or
                (None if self._paused else self._set_status('Listening…  (end your command with "Ok Aria"  ·  F9 to pause)', "#cba6f7"))
            ),
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
        self.root.after(0, lambda: setattr(self, "_loading_status", "Waiting for microphone"))
        self.root.after(0, self._check_all_ready)

        threading.Thread(target=self._record_loop, daemon=True).start()

    # ── Shared audio stream ────────────────────────────────────────────────

    def _record_loop(self):
        """Continuously read from the mic. Retries on disconnect. Closes mic while paused."""
        RETRY_S = 2.0

        while True:
            self._mic_active.wait()  # block here while paused

            try:
                stream_ctx = sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="float32")
                stream_ctx.__enter__()
            except Exception:
                if self._mic_connected:
                    self._mic_connected = False
                    self.root.after(0, self._on_mic_disconnected)
                time.sleep(RETRY_S)
                continue

            if not self._mic_connected:
                self._mic_connected = True
                self.root.after(0, self._on_mic_reconnected)

            disconnected = False
            try:
                while self._mic_active.is_set():
                    chunk, _ = stream_ctx.read(512)
                    chunk = chunk.copy().flatten()
                    with self._lock:
                        if self._state == "listening":
                            self._frames.append(chunk)
            except Exception as exc:
                print(f"[audio] stream error: {exc}")
                disconnected = True
            finally:
                try:
                    stream_ctx.__exit__(None, None, None)
                except Exception:
                    pass

            if disconnected:
                self._mic_connected = False
                self.root.after(0, self._on_mic_disconnected)
                time.sleep(RETRY_S)
            # else: paused intentionally — _mic_connected stays True, no notification

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
        if not self._mic_ready:
            self._mic_ready = True
            self._check_all_ready()
        else:
            self._enter_listening()

    # ── Listening ─────────────────────────────────────────────────────────

    def _toggle_pause(self):
        if not self.root.focus_displayof():
            return
        self._paused = not self._paused
        if self._paused:
            self._mic_active.clear()  # signals _record_loop to close the stream
            with self._lock:
                if self._state in ("listening", "processing"):
                    self._state = "idle"
                self._frames.clear()
            self._stop_transcribe_timer()
            self._set_status("⏸ Listening paused  (press F9 to resume)", "#f9e2af")
            self._model_lbl.config(text="")
        else:
            self._mic_active.set()  # signals _record_loop to reopen the stream
            self._set_status('Listening…  (end your command with "Ok Aria"  ·  F9 to pause)', "#cba6f7")
            self._model_lbl.config(text="Mic ready  |  listening for speech", fg="#a6e3a1")
            self._enter_listening(skip_status=True)

    def _enter_listening(self, skip_status=False):
        """Switch to listening state and start the listening loop."""
        if self._paused:
            return
        if not (self._terminal_ready and self._api_ready and self._mic_ready):
            return
        with self._listening_guard:
            if self._listening_active:
                return
            self._listening_active = True
        with self._lock:
            self._state = "listening"
            self._frames.clear()
        if not skip_status:
            self.root.after(0, lambda: self._set_status('Listening…  (end your command with "Ok Aria"  ·  F9 to pause)', "#cba6f7"))
        threading.Thread(target=self._listening_loop, daemon=True).start()

    def _listening_loop(self):
        """
        Phase 1: wait for voice activity, clearing silence frames.
        Phase 2: record until silence after speech, then hand off.
        """
        try:
            self._listening_loop_body()
        finally:
            with self._listening_guard:
                self._listening_active = False

    def _listening_loop_body(self):
        # Each chunk from stream_ctx.read(512) is exactly 512 samples.
        # We only need the last ~0.3 s for the RMS check → last 10 chunks.
        _TAIL = 10  # 10 × 512 / 16000 ≈ 0.32 s
        _MIN_P1 = int(0.2 * SAMPLE_RATE / 512)  # ≥ 0.2 s of frames before checking
        _MIN_P2 = int(0.5 * SAMPLE_RATE / 512)  # ≥ 0.5 s of frames before checking

        # Phase 1 — wait for speech onset
        while True:
            time.sleep(0.1)
            if self._paused:
                return
            with self._lock:
                if self._state != "listening":
                    return
                n_frames = len(self._frames)
                tail = list(self._frames[-_TAIL:])

            if n_frames < _MIN_P1:
                continue

            recent = np.concatenate(tail) if tail else np.array([], dtype=np.float32)
            rms = float(np.sqrt(np.mean(recent**2))) if len(recent) else 0.0

            if rms >= SILENCE_RMS:
                break  # speech detected

            # Still silence — clear accumulated noise frames
            with self._lock:
                if self._state == "listening":
                    self._frames.clear()

        self.root.after(0, lambda: self._set_status('Listening…  (end your command with "Ok Aria"  ·  F9 to pause)', "#cba6f7"))

        # Phase 2 — record until silence
        silence_since: float | None = None
        started = time.time()

        while True:
            time.sleep(0.1)
            if self._paused:
                return
            with self._lock:
                if self._state != "listening":
                    return
                n_frames = len(self._frames)
                tail = list(self._frames[-_TAIL:])

            if n_frames < _MIN_P2:
                continue

            recent = np.concatenate(tail) if tail else np.array([], dtype=np.float32)
            rms = float(np.sqrt(np.mean(recent**2))) if len(recent) else 0.0

            if rms < SILENCE_RMS:
                if silence_since is None:
                    silence_since = time.time()
                elif time.time() - silence_since >= SILENCE_S:
                    break
            else:
                silence_since = None

            if time.time() - started > 60:
                break

        with self._lock:
            if self._state != "listening":
                return
            self._state = "processing"
            frames = list(self._frames)
            self._frames.clear()

        self._transcribe_start = time.time()
        self.root.after(0, self._tick_transcribe_status)
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
        skip_status = False
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

            # If F9 was pressed while the API was in-flight, discard the result entirely.
            if self._paused:
                return

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

            if valid and self._terminal and not self._paused:
                cleaned = re.sub(r'\b(aria|arya|area|ariel|ariah|aeria|areia|riya|ria|aya|ah\s+yeah|are)\b[,\s]*', '', text, flags=re.IGNORECASE).strip()
                self._terminal.send_text(cleaned or text)
            elif not valid and not self._paused:
                skip_status = True
                self.root.after(0, lambda: self._set_status("✗ Ignored: guess it was not meant for me", "#f38ba8"))
                self._schedule_status_restore()

        except requests.exceptions.ConnectionError:
            _log.error("TRANSCRIBE ERROR  API unreachable")
            if not self._paused:
                self.root.after(0, lambda: self._set_status("⚠ API unreachable", "#f38ba8"))
                self._schedule_status_restore()
        except requests.exceptions.Timeout:
            _log.error("TRANSCRIBE ERROR  API timed out")
            if not self._paused:
                self.root.after(0, lambda: self._set_status("⚠ API timed out", "#f38ba8"))
                self._schedule_status_restore()
        except Exception as exc:
            _log.exception("TRANSCRIBE ERROR")
            if not self._paused:
                self.root.after(0, lambda: self._set_status("⚠ Error — see log", "#f38ba8"))
                self._schedule_status_restore()
            print(f"[transcribe] error: {exc}")
        finally:
            with self._lock:
                if self._state == "processing":
                    self._state = "idle"
            _skip = skip_status
            self.root.after(0, self._stop_transcribe_timer)
            self.root.after(0, lambda: self._enter_listening(skip_status=_skip))

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
