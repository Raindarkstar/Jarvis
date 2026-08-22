#!/usr/bin/env python3
"""Small Windows superellipse launcher used by the voice service.

The voice process owns the microphone and audio stream.  This process owns a
tiny Tk window so the orb remains responsive while WebView2 runs in its own
GUI process.  Clicking the orb starts (or toggles) the full desktop client.
"""

from __future__ import annotations

import json
import math
import os
import queue
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import tkinter as tk


CODE_ROOT = Path(__file__).resolve().parent
ORB_WIDTH = 208
ORB_HEIGHT = 88
TOP_OFFSET = 20
FPS_MS = 33
WINDOW_TITLE = "Jarvis"

STATE_COLORS = {
    "idle": ("#7b86b6", "#dce8ff", "#ffffff"),
    "awake": ("#8458dc", "#e7d8ff", "#ffffff"),
    "listening": ("#2c98c8", "#d6f4ff", "#ffffff"),
    "thinking": ("#b07b35", "#fff0c9", "#ffffff"),
    "speaking": ("#386dcc", "#dce7ff", "#ffffff"),
    "error": ("#b45362", "#ffe1e6", "#ffffff"),
}


def _superellipse_points(cx, cy, rx, ry, exponent=2.45, segments=64):
    points = []
    for index in range(segments):
        angle = (2.0 * math.pi * index) / segments
        cos_value = math.copysign(abs(math.cos(angle)) ** (2.0 / exponent), math.cos(angle))
        sin_value = math.copysign(abs(math.sin(angle)) ** (2.0 / exponent), math.sin(angle))
        points.extend((cx + rx * cos_value, cy + ry * sin_value))
    return points


class WindowsOrb:
    def __init__(self, root):
        self.root = root
        self.commands = queue.Queue()
        self.state = "idle"
        self.visible = False
        self.phase = 0.0
        self._last_click_at = 0.0
        self.sync_path = Path(
            os.getenv(
                "JARVIS_WINDOWS_SYNC_FILE",
                Path(tempfile.gettempdir()) / f"jarvis-sync-{os.getpid()}.jsonl",
            )
        )
        self.sync_path.parent.mkdir(parents=True, exist_ok=True)
        self.sync_path.touch(exist_ok=True)
        self.sync_lock = threading.Lock()
        self.desktop_process = None
        self.desktop_lock = threading.RLock()

        root.withdraw()
        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.geometry(
            f"{ORB_WIDTH}x{ORB_HEIGHT}+{self._left_position()}+{TOP_OFFSET}"
        )
        self.transparent_color = "#010107"
        self.window.configure(background=self.transparent_color)
        try:
            self.window.attributes("-transparentcolor", self.transparent_color)
        except tk.TclError:
            # Some Windows display drivers do not expose color-keyed
            # transparency; the glass shape remains visible on a dark tile.
            pass

        self.canvas = tk.Canvas(
            self.window,
            width=ORB_WIDTH,
            height=ORB_HEIGHT,
            highlightthickness=0,
            borderwidth=0,
            background=self.transparent_color,
        )
        self.canvas.pack(fill="both", expand=True)
        self.canvas.bind("<Button-1>", self._on_click)
        self.window.bind("<ButtonRelease-1>", self._on_click)
        self.canvas.bind("<Double-Button-1>", self._on_click)
        self.canvas.bind("<Enter>", lambda _event: self.canvas.configure(cursor="hand2"))
        self.canvas.bind("<Leave>", lambda _event: self.canvas.configure(cursor=""))

        initial_points = _superellipse_points(ORB_WIDTH / 2, ORB_HEIGHT / 2, 98, 38)
        self.glow_item = self.canvas.create_polygon(initial_points, outline="", width=0)
        self.outer_item = self.canvas.create_polygon(initial_points, outline="#ffffff", width=1)
        self.body_item = self.canvas.create_polygon(initial_points, outline="#ffffff", width=1)
        self.wave_items = [
            self.canvas.create_line(initial_points[:4], fill="#ffffff", width=2, smooth=True),
            self.canvas.create_line(initial_points[:4], fill="#ffffff", width=2, smooth=True),
            self.canvas.create_line(initial_points[:4], fill="#ffffff", width=1, smooth=True),
        ]
        dome_points = _superellipse_points(
            ORB_WIDTH / 2,
            ORB_HEIGHT / 2 - 18,
            42,
            12,
            exponent=4.0,
        )
        self.dome_item = self.canvas.create_polygon(
            dome_points,
            fill="#090b14",
            outline="#556489",
            width=1,
        )
        self.logo_item = self.canvas.create_text(
            ORB_WIDTH // 2,
            ORB_HEIGHT // 2 - 8,
            text="J",
            font=("Segoe UI", 21, "bold"),
            fill="#ffffff",
        )
        self.label_item = self.canvas.create_text(
            ORB_WIDTH // 2,
            ORB_HEIGHT // 2 + 31,
            text="JARVIS",
            font=("Segoe UI", 7, "bold"),
            fill="#dce8ff",
        )
        self.status_item = self.canvas.create_oval(
            ORB_WIDTH // 2 - 3,
            7,
            ORB_WIDTH // 2 + 3,
            13,
        )
        self.root.after(FPS_MS, self._tick)

    def _left_position(self):
        return max(0, round((self.root.winfo_screenwidth() - ORB_WIDTH) / 2))

    def _on_click(self, _event):
        # Keep the click path on the Tk event loop.  The desktop launch itself
        # waits in a worker, so the orb remains animated and responsive.
        now = time.monotonic()
        if now - self._last_click_at < 0.35:
            return "break"
        self._last_click_at = now
        threading.Thread(target=self._toggle_desktop, daemon=True).start()
        return "break"

    def _find_desktop_hwnd(self):
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            process = self.desktop_process
            if process is not None and process.poll() is None:
                matched = []
                enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

                def enum_window(hwnd, _lparam):
                    process_id = wintypes.DWORD()
                    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
                    if process_id.value == process.pid and user32.IsWindowVisible(hwnd):
                        matched.append(hwnd)
                        return False
                    return True

                user32.EnumWindows(enum_proc(enum_window), 0)
                if matched:
                    return matched[0]
            user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
            user32.FindWindowW.restype = wintypes.HWND
            hwnd = user32.FindWindowW(None, WINDOW_TITLE)
            return hwnd if hwnd else None
        except (AttributeError, OSError, TypeError, ValueError):
            return None

    def _show_desktop(self, visible):
        hwnd = self._find_desktop_hwnd()
        if hwnd is None:
            return False
        try:
            import ctypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.ShowWindow(hwnd, 9 if visible else 0)
            if visible:
                # SetForegroundWindow can be denied when called from a worker
                # thread; a short topmost toggle reliably brings the desktop
                # above the editor without leaving it permanently topmost.
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003 | 0x0040)
                user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0003 | 0x0040)
                user32.SetForegroundWindow(hwnd)
            return True
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def _toggle_desktop(self):
        with self.desktop_lock:
            if self.desktop_process is None or self.desktop_process.poll() is not None:
                client_path = CODE_ROOT / "windows_client.py"
                env = os.environ.copy()
                env["JARVIS_DESKTOP_CHILD"] = "1"
                try:
                    self.desktop_process = subprocess.Popen(
                        [sys.executable, str(client_path)],
                        cwd=str(CODE_ROOT),
                        env=env,
                        creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0),
                    )
                except OSError:
                    self.desktop_process = None
                    return

                for _ in range(40):
                    if self._show_desktop(True):
                        return
                    if self.desktop_process.poll() is not None:
                        return
                    time.sleep(0.1)
                return

            hwnd = self._find_desktop_hwnd()
            if hwnd is None:
                return
            try:
                import ctypes

                user32 = ctypes.WinDLL("user32", use_last_error=True)
                visible = bool(user32.IsWindowVisible(hwnd))
            except (AttributeError, OSError, TypeError, ValueError):
                visible = False
            self._show_desktop(not visible)

    def _write_sync(self, payload):
        try:
            with self.sync_lock, self.sync_path.open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(payload, ensure_ascii=False) + "\n")
        except OSError:
            pass

    def _handle_command(self, command):
        if command == "quit":
            self.root.destroy()
            return
        try:
            data = json.loads(command) if command.startswith("{") else {"action": "state", "state": command}
        except json.JSONDecodeError:
            return

        action = data.get("action")
        if action == "toggle_chat":
            self._toggle_desktop()
            return
        if action in {"show_chat", "hide_chat"}:
            self._show_desktop(action == "show_chat")
            return
        if action == "state":
            state = str(data.get("state", "idle")).lower()
            if state in {"hide", "hidden", "idle"}:
                self.state = "idle"
                self.visible = False
                self.window.withdraw()
            else:
                self.state = state if state in STATE_COLORS else "awake"
                self.visible = True
                self.window.deiconify()
                self.window.lift()
            self._write_sync({"action": "state", "state": state})
            return
        self._write_sync(data)

    def _tick(self):
        try:
            while True:
                self._handle_command(self.commands.get_nowait())
        except queue.Empty:
            pass

        accent, highlight, white = STATE_COLORS.get(self.state, STATE_COLORS["idle"])
        pulse = 1.0 + 0.035 * math.sin(self.phase * 2.0 * math.pi)
        points = _superellipse_points(ORB_WIDTH / 2, ORB_HEIGHT / 2, 98 * pulse, 38 * pulse)
        body_points = _superellipse_points(ORB_WIDTH / 2, ORB_HEIGHT / 2, 94 * pulse, 34 * pulse)
        glow_points = _superellipse_points(ORB_WIDTH / 2, ORB_HEIGHT / 2, 103 * pulse, 43 * pulse)
        self.canvas.coords(self.outer_item, *points)
        self.canvas.coords(self.body_item, *body_points)
        self.canvas.coords(self.glow_item, *glow_points)
        self.canvas.itemconfigure(self.glow_item, fill="", outline=accent, width=2)
        self.canvas.itemconfigure(self.outer_item, fill="#080a11", outline=accent, width=2)
        self.canvas.itemconfigure(self.body_item, fill="#202942", outline="#91a4da", width=1)
        wave_colors = ("#8e5cff", "#40c7ff", "#ffbd63")
        for index, item in enumerate(self.wave_items):
            y = 52 + (index - 1) * 7
            wave = []
            for point_index in range(25):
                x = 44 + point_index * 5
                wave_y = y + math.sin(
                    self.phase * 2.0 * math.pi * (1.5 + index * 0.25)
                    + point_index * 0.42
                ) * (2.5 + index)
                wave.extend((x, wave_y))
            self.canvas.coords(item, *wave)
            self.canvas.itemconfigure(item, fill=wave_colors[index], width=2 if index < 2 else 1)
        self.canvas.itemconfigure(self.dome_item, fill="#080a11", outline=accent)
        self.canvas.itemconfigure(self.logo_item, fill=white)
        self.canvas.itemconfigure(self.label_item, fill=highlight)
        self.canvas.itemconfigure(self.status_item, fill=accent, outline=highlight)
        self.phase = (self.phase + 0.018) % 1.0
        if self.visible:
            self.window.lift()
        self.root.after(FPS_MS, self._tick)

    def close(self):
        with self.desktop_lock:
            process = self.desktop_process
            self.desktop_process = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                except OSError:
                    pass
        try:
            self.sync_path.unlink(missing_ok=True)
        except OSError:
            pass


def _read_commands(orb):
    for line in sys.stdin:
        orb.commands.put(line.strip())
    orb.commands.put("quit")


def main():
    root = tk.Tk()
    orb = WindowsOrb(root)
    reader = threading.Thread(target=_read_commands, args=(orb,), daemon=True)
    reader.start()
    try:
        root.mainloop()
    finally:
        orb.close()


if __name__ == "__main__":
    main()
