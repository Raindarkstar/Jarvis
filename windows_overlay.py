#!/usr/bin/env python3
"""Windows Optical Glass Dynamic Island & Lens Launcher for Jarvis.

Uses the exact extracted optical glass pebble texture (background-free cutout)
with a 60 FPS breathing animation.
Permanently placed at the top-center of the screen; clicking it immediately launches
or toggles the desktop client on Windows and Linux.
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

# Optional PIL support (with native Tkinter fallback)
try:
    from PIL import Image, ImageTk
    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:
    import numpy as np
    HAS_NUMPY = True
except ImportError:
    HAS_NUMPY = False


CODE_ROOT = Path(__file__).resolve().parent
ORB_WIDTH = 138
ORB_HEIGHT = 100
TOP_OFFSET = 12
ANIMATION_FPS = 60
FPS_MS = 1000 // ANIMATION_FPS
WINDOW_TITLE = "Jarvis"

STATE_PARAMS = {
    "idle": {"pulse_rate": 1.0, "bright": 1.0, "speed": 1.0},
    "awake": {"pulse_rate": 2.0, "bright": 1.30, "speed": 1.8},
    "listening": {"pulse_rate": 1.5, "bright": 1.20, "speed": 1.5},
    "thinking": {"pulse_rate": 2.5, "bright": 1.25, "speed": 2.2},
    "speaking": {"pulse_rate": 3.0, "bright": 1.35, "speed": 2.0},
    "error": {"pulse_rate": 1.0, "bright": 0.85, "speed": 0.8},
}


class OpticalGlassPebbleRenderer:
    """Renders frame-by-frame breathing animations using the extracted cutout asset."""

    def __init__(self, width: int = ORB_WIDTH, height: int = ORB_HEIGHT):
        self.width = width
        self.height = height
        self.source_path = CODE_ROOT / "assets" / "extracted_pebble_source.png"
        if not self.source_path.exists():
            self.source_path = CODE_ROOT / "assets" / "glass-orb.png"

        self.base_arr = None
        if HAS_PIL and self.source_path.exists():
            try:
                img = Image.open(self.source_path).convert("RGBA")
                resized = img.resize((width, height), Image.Resampling.LANCZOS)
                self.base_arr = np.array(resized, dtype=np.float32)
            except Exception:
                self.base_arr = None

        if self.base_arr is None and HAS_NUMPY:
            # Procedural fallback
            y_grid, x_grid = np.indices((height, width), dtype=np.float32)
            cx, cy = (width - 1) / 2.0, (height - 1) / 2.0
            rx, ry = width / 2.0 - 2.0, height / 2.0 - 2.0
            nx = (x_grid - cx) / rx
            ny = (y_grid - cy) / ry
            r = (np.abs(nx) ** 2.42 + np.abs(ny) ** 2.42) ** (1.0 / 2.42)
            mask = (r <= 1.0).astype(np.float32)
            arr = np.zeros((height, width, 4), dtype=np.float32)
            arr[:, :, 0] = 8.0 * mask
            arr[:, :, 1] = 10.0 * mask
            arr[:, :, 2] = 16.0 * mask
            arr[:, :, 3] = 255.0 * mask
            self.base_arr = arr

    def render_raw_rgb(self, phase: float, params: dict, bg_color: tuple = (1, 1, 7)) -> bytes:
        """Renders raw 24-bit RGB pixel buffer with subtle pulse modulation."""
        if self.base_arr is None:
            return bytes(list(bg_color) * (self.width * self.height))

        pulse_rate = params.get("pulse_rate", 1.0)
        bright = params.get("bright", 1.0)
        pulse = 1.0 + 0.07 * math.sin(phase * 2 * math.pi * pulse_rate) * bright

        f_arr = self.base_arr.copy()
        # Modulate RGB channels with subtle pulse
        f_rgb = np.clip(f_arr[:, :, :3] * pulse, 0, 255)
        alpha = f_arr[:, :, 3:] / 255.0

        bg = np.array(bg_color, dtype=np.float32)
        final_rgb = f_rgb * alpha + bg * (1.0 - alpha)
        final_u8 = np.clip(final_rgb, 0, 255).astype(np.uint8)
        return final_u8.tobytes()

    def render_pil_frame(self, phase: float, params: dict, bg_color: tuple = (1, 1, 7)):
        """Renders PIL Image if PIL is installed, or native Tk PhotoImage."""
        raw_bytes = self.render_raw_rgb(phase, params, bg_color)
        if HAS_PIL:
            return Image.frombytes("RGB", (self.width, self.height), raw_bytes)
        header = f"P6 {self.width} {self.height} 255\n".encode("ascii")
        return tk.PhotoImage(data=header + raw_bytes)

    def render_photo_image(self, phase: float, params: dict, bg_color: tuple = (1, 1, 7)):
        """Renders Tk-compatible PhotoImage using PIL if available, or native binary PPM."""
        raw_bytes = self.render_raw_rgb(phase, params, bg_color)
        if HAS_PIL:
            pil_img = Image.frombytes("RGB", (self.width, self.height), raw_bytes)
            return ImageTk.PhotoImage(pil_img)
        header = f"P6 {self.width} {self.height} 255\n".encode("ascii")
        return tk.PhotoImage(data=header + raw_bytes)


class WindowsOrb:
    """Compact animated Dynamic Island floating lens for Windows."""

    FRAME_CYCLE_COUNT = 60

    def __init__(self, root: tk.Tk):
        self.root = root
        self.commands = queue.Queue()
        self.state = "idle"
        # Wait for the offline wake-word detector before showing the lens.
        self.visible = False
        self.frame_index = 0
        self._last_click_at = 0.0
        self.current_params = dict(STATE_PARAMS["idle"])

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

        # Click and double click bindings to immediately show/toggle desktop
        self.canvas.bind("<Button-1>", self._on_click)
        self.window.bind("<Button-1>", self._on_click)
        self.canvas.bind("<Double-Button-1>", self._on_click)
        self.canvas.bind("<Enter>", lambda _event: self.canvas.configure(cursor="hand2"))
        self.canvas.bind("<Leave>", lambda _event: self.canvas.configure(cursor=""))

        # Initialize physical optical renderer
        self.renderer = OpticalGlassPebbleRenderer(ORB_WIDTH, ORB_HEIGHT)
        self.image_item = self.canvas.create_image(0, 0, anchor="nw")

        # Pre-render frame-by-frame animation sequences for smooth 60 FPS playback
        self._frame_cache: dict[str, list[any]] = {}
        self._pre_render_frames()

        self.root.after(FPS_MS, self._tick)

    def _left_position(self) -> int:
        screen_width = self.root.winfo_screenwidth()
        return max(0, round((screen_width - ORB_WIDTH) / 2))

    def _pre_render_frames(self):
        """Pre-renders frame sequences for each state for silky-smooth 60 FPS rendering."""
        gif_fallback = CODE_ROOT / "assets" / "siri-glass-orb-loop.gif"

        if HAS_NUMPY and self.renderer.base_arr is not None:
            for state_name, params in STATE_PARAMS.items():
                frames = []
                for i in range(self.FRAME_CYCLE_COUNT):
                    phase = i / self.FRAME_CYCLE_COUNT
                    img = self.renderer.render_photo_image(phase, params)
                    frames.append(img)
                self._frame_cache[state_name] = frames
        elif gif_fallback.exists():
            frames = []
            for i in range(self.FRAME_CYCLE_COUNT):
                try:
                    frames.append(tk.PhotoImage(file=str(gif_fallback), format=f"gif -index {i}"))
                except tk.TclError:
                    break
            self._frame_cache["idle"] = frames or [tk.PhotoImage(file=str(gif_fallback))]
        else:
            self._frame_cache["idle"] = []

    def _on_click(self, _event):
        """Immediately launches or toggles the desktop client on user click."""
        now = time.monotonic()
        if now - self._last_click_at < 0.25:
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

    def _show_desktop(self, visible: bool) -> bool:
        hwnd = self._find_desktop_hwnd()
        if hwnd is None:
            return False
        try:
            import ctypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.ShowWindow(hwnd, 9 if visible else 0)
            if visible:
                user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0003 | 0x0040)
                user32.SetWindowPos(hwnd, -2, 0, 0, 0, 0, 0x0003 | 0x0040)
                user32.SetForegroundWindow(hwnd)
            return True
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def _toggle_desktop(self):
        with self.desktop_lock:
            if self.desktop_process is None or self.desktop_process.poll() is not None:
                # Select platform-specific desktop client executable
                client_target = "windows_client.py" if os.name == "nt" else "client_app.py"
                client_path = CODE_ROOT / client_target
                env = os.environ.copy()
                env["JARVIS_DESKTOP_CHILD"] = "1"
                creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0
                try:
                    self.desktop_process = subprocess.Popen(
                        [sys.executable, str(client_path)],
                        cwd=str(CODE_ROOT),
                        env=env,
                        creationflags=creationflags,
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

    def _handle_command(self, command: str):
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
                self.state = state if state in STATE_PARAMS else "idle"
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

        if self.visible:
            frames = self._frame_cache.get(self.state, self._frame_cache.get("idle", []))
            if frames:
                self.frame_index = (self.frame_index + 1) % len(frames)
                self.canvas.itemconfigure(self.image_item, image=frames[self.frame_index])

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


def _read_commands(orb: WindowsOrb):
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
