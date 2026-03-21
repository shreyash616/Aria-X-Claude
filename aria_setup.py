#!/usr/bin/env python3
# Copyright (c) 2026 Shreyash Padhi. All rights reserved.
# Licensed under the Aria Source Available License v1.0 — see LICENSE file.
"""
aria_setup.py — Entry point and first-time setup wizard for Aria.

Opens a terminal, asks whether to register Aria to run at Windows startup,
then hides the console and launches the main GUI.
"""

import itertools
import os
import sys
import threading
import time
import winreg

APP_NAME = "Aria"
_STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_APP_KEY = r"Software\Aria"


def _exe_path() -> str:
    """Return the path to this exe (or script when running unbuilt)."""
    if getattr(sys, "frozen", False):
        return sys.executable
    return os.path.abspath(__file__)


def _setup_done() -> bool:
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _APP_KEY)
        winreg.QueryValueEx(key, "SetupDone")
        winreg.CloseKey(key)
    except OSError:
        return False

    # If a startup entry exists, verify it still points to the current exe.
    # If the path is stale (exe moved/rebuilt), re-run setup to correct it.
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, _STARTUP_KEY)
        registered, _ = winreg.QueryValueEx(key, APP_NAME)
        winreg.CloseKey(key)
        expected = f'"{_exe_path()}"'
        if registered != expected:
            return False
    except OSError:
        pass  # no startup entry — user chose not to register, that's fine

    return True


def _mark_setup_done() -> None:
    try:
        key = winreg.CreateKey(winreg.HKEY_CURRENT_USER, _APP_KEY)
        winreg.SetValueEx(key, "SetupDone", 0, winreg.REG_DWORD, 1)
        winreg.CloseKey(key)
    except Exception:
        pass


def _register_startup(exe_path: str) -> bool:
    try:
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            _STARTUP_KEY,
            0,
            winreg.KEY_SET_VALUE,
        )
        winreg.SetValueEx(key, APP_NAME, 0, winreg.REG_SZ, f'"{exe_path}"')
        winreg.CloseKey(key)
        return True
    except Exception as exc:
        print(f"  [!] Could not write to registry: {exc}")
        return False


def main() -> None:
    import ctypes

    # ── Skip setup if already configured ────────────────────────────────────
    if not _setup_done():
        kernel32 = ctypes.windll.kernel32

        # Only allocate a console if one isn't already attached (i.e. windowed exe)
        allocated = False
        if not kernel32.GetConsoleWindow():
            kernel32.AllocConsole()
            sys.stdout = open("CONOUT$", "w")
            sys.stdin = open("CONIN$", "r")
            sys.stderr = open("CONOUT$", "w")
            allocated = True

        print("=" * 52)
        print("  Aria — Setup")
        print("=" * 52)
        print()

        # ── Startup question ─────────────────────────────────────────────────
        print("Would you like Aria to start automatically when Windows starts?")
        while True:
            answer = input("  [y/n]: ").strip().lower()
            if answer in ("y", "yes", "n", "no"):
                break
            print("  Please enter y or n.")

        print()

        if answer in ("y", "yes"):
            if _register_startup(_exe_path()):
                print("  ✓ Aria will now start automatically with Windows.")
        else:
            print("  Aria will not start automatically.")
            print("  You can run Aria.exe directly at any time.")

        _mark_setup_done()
        print()

        # ── Loader spinner while heavy dependencies are imported ─────────────
        # Enable ANSI color codes in the console
        _handle = ctypes.windll.kernel32.GetStdHandle(-11)
        _mode = ctypes.c_ulong()
        ctypes.windll.kernel32.GetConsoleMode(_handle, ctypes.byref(_mode))
        ctypes.windll.kernel32.SetConsoleMode(_handle, _mode.value | 0x0004)

        _CYAN = "\033[96m"
        _RESET = "\033[0m"

        stop_spinner = threading.Event()

        def _spinner() -> None:
            frames = itertools.cycle(r"-\|/")
            while not stop_spinner.is_set():
                print(f"\r  {_CYAN}Loading {next(frames)}{_RESET} ", end="", flush=True)
                time.sleep(0.1)
            print(f"\r  {_CYAN}✓ Starting Aria ...{_RESET}      ")

        t = threading.Thread(target=_spinner, daemon=True)
        t.start()

        from aria_claude import AriaApp  # heavy import happens here

        stop_spinner.set()
        t.join()

        # Hide the console window immediately, then detach
        hwnd = kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)  # SW_HIDE
        sys.stdout.close()
        sys.stdin.close()
        sys.stdout = sys.__stdout__
        sys.stdin = sys.__stdin__
        sys.stderr = sys.__stderr__
        if allocated:
            kernel32.FreeConsole()
    else:
        from aria_claude import AriaApp

    # ── Launch main app ─────────────────────────────────────────────────────
    AriaApp()


if __name__ == "__main__":
    main()
