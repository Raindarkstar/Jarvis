#!/usr/bin/env python3
"""Windows Optical Glass Dynamic Island & Lens Launcher for Jarvis.

Renders ultra-smooth physical optical glass pebble animations matching Apple-style
acoustics and caustics:
- Proportional rounded squircle lens (138x102, not squished/flat)
- Solid glossy black body with crisp white specular rim (zero fringe/burrs)
- High-vibrancy rainbow spectral dispersion arc and lens glint

Permanently placed at the top-center of the screen; clicking it immediately launches
or toggles the desktop client.
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
ORB_HEIGHT = 102
TOP_OFFSET = 12
ANIMATION_FPS = 60
FPS_MS = 1000 // ANIMATION_FPS
WINDOW_TITLE = "Jarvis"

STATE_PARAMS = {
    "idle": {"pulse_rate": 1.0, "bright": 1.0, "wave_speed": 1.0, "wave_amp": 1.0},
    "awake": {"pulse_rate": 2.0, "bright": 1.35, "wave_speed": 1.8, "wave_amp": 1.3},
    "listening": {"pulse_rate": 1.5, "bright": 1.2, "wave_speed": 1.5, "wave_amp": 1.2},
    "thinking": {"pulse_rate": 2.5, "bright": 1.3, "wave_speed": 2.5, "wave_amp": 1.4},
    "speaking": {"pulse_rate": 3.0, "bright": 1.45, "wave_speed": 2.0, "wave_amp": 1.5},
    "error": {"pulse_rate": 1.0, "bright": 0.85, "wave_speed": 0.8, "wave_amp": 0.8},
}


class OpticalGlassPebbleRenderer:
    """Super-sampled vectorized physical optical pebble renderer with crisp anti-aliasing."""

    def __init__(self, width: int = ORB_WIDTH, height: int = ORB_HEIGHT):
        self.width = width
        self.height = height
        self.scale = 2  # 2x internal super-sampling for silky smooth anti-aliased curves
        self.sw = width * self.scale
        self.sh = height * self.scale

        self.cx = (self.sw - 1) / 2.0
        self.cy = (self.sh - 1) / 2.0
        self.rx = self.sw / 2.0 - 2.5
        self.ry = self.sh / 2.0 - 2.5

        if HAS_NUMPY:
            y_grid, x_grid = np.indices((self.sh, self.sw), dtype=np.float32)
            self.nx = (x_grid - self.cx) / self.rx
            self.ny = (y_grid - self.cy) / self.ry
            self.p = 2.45
            self.r = (np.abs(self.nx) ** self.p + np.abs(self.ny) ** self.p) ** (1.0 / self.p)

            # 1. Clean, fringe-free outer boundary mask (sharp cutoff to avoid transparency burrs)
            self.mask = (self.r <= 1.0).astype(np.float32)

            # 2. Solid deep glossy black glass body
            self.solid_black_rgb = np.array([6.0 / 255.0, 8.0 / 255.0, 14.0 / 255.0], dtype=np.float32)

            # 3. Camera aperture reflection dot at upper-right
            d_lens = np.sqrt((self.nx - 0.38) ** 2 + (self.ny + 0.45) ** 2) / 0.08
            self.lens_mask = np.clip((1.0 - d_lens) * 3.0, 0.0, 1.0) * self.mask
            self.lens_rgb = np.array([16.0 / 255.0, 42.0 / 255.0, 95.0 / 255.0], dtype=np.float32)
            self.lens_glint = (
                np.exp(-(((self.nx - 0.40) / 0.02) ** 2 + ((self.ny + 0.47) / 0.02) ** 2))
                * self.mask
            )

            # 4. Crisp white specular rim & contour (drawn inside perimeter for zero burrs)
            bottom_factor = np.clip((self.ny - 0.15) / 0.8, 0.0, 1.0)
            self.rim_dist = (
                np.exp(-((self.r - 0.96) / 0.030) ** 2)
                * (0.4 + 0.6 * bottom_factor)
                * self.mask
            )
            self.rim_rgb = np.array([255.0 / 255.0, 255.0 / 255.0, 255.0 / 255.0], dtype=np.float32)

            self.edge_dist = np.exp(-((self.r - 0.985) / 0.015) ** 2) * 0.5 * self.mask
            self.edge_rgb = np.array([210.0 / 255.0, 225.0 / 255.0, 250.0 / 255.0], dtype=np.float32)

            self.top_sheen = (
                np.exp(-(((self.ny + 0.75) / 0.18) ** 2) - ((self.nx / 0.55) ** 2))
                * 0.25
                * self.mask
            )

            # 5. Spectral dispersion rainbow caustic colors
            self.h_env = np.clip(1.0 - (self.nx * 1.06) ** 2, 0.0, 1.0) ** 0.60
            self.gold_rgb = np.array([255.0 / 255.0, 175.0 / 255.0, 36.0 / 255.0], dtype=np.float32)
            self.core_rgb = np.array([255.0 / 255.0, 255.0 / 255.0, 250.0 / 255.0], dtype=np.float32)
            self.cyan_rgb = np.array([0.0 / 255.0, 225.0 / 255.0, 255.0 / 255.0], dtype=np.float32)
            self.blue_rgb = np.array([18.0 / 255.0, 85.0 / 255.0, 245.0 / 255.0], dtype=np.float32)

    def render_raw_rgb(self, phase: float, params: dict, bg_color: tuple = (1, 1, 7)) -> bytes:
        """Renders 2x super-sampled buffer downscaled to target size for crystal clarity."""
        w_speed = params["wave_speed"]
        w_amp = params["wave_amp"]

        w1 = 0.016 * w_amp * np.sin(phase * 2 * math.pi * w_speed + self.nx * 2.8)
        w2 = 0.008 * w_amp * np.cos(phase * 4 * math.pi * w_speed - self.nx * 4.2)

        arc_center_y = 0.01 - 0.065 * (1.0 - self.nx * self.nx) + w1 + w2
        dy = self.ny - arc_center_y

        pulse = 1.0 + 0.08 * math.sin(phase * 2 * math.pi * params["pulse_rate"])
        brightness = params["bright"] * pulse

        gold_dist = np.exp(-((dy + 0.045) / 0.040) ** 2)
        core_dist = np.exp(-(dy / 0.022) ** 2)
        cyan_dist = np.exp(-((dy - 0.030) / 0.040) ** 2)
        blue_dist = np.exp(-((dy - 0.075) / 0.052) ** 2)

        spectral = (
            self.gold_rgb * (gold_dist[:, :, None] * 0.95)
            + self.core_rgb * (core_dist[:, :, None] * 1.40)
            + self.cyan_rgb * (cyan_dist[:, :, None] * 1.10)
            + self.blue_rgb * (blue_dist[:, :, None] * 0.95)
        ) * (self.h_env[:, :, None] * brightness * self.mask[:, :, None])

        rgb = (
            self.solid_black_rgb * self.mask[:, :, None]
            + self.lens_rgb * (self.lens_mask[:, :, None] * 0.7)
            + self.lens_glint[:, :, None] * 0.9
            + spectral
            + self.rim_rgb * (self.rim_dist[:, :, None] * 0.95)
            + self.edge_rgb * (self.edge_dist[:, :, None] * 0.5)
            + self.top_sheen[:, :, None] * 0.4
        )

        rgb = np.clip(rgb, 0.0, 1.0)
        bg = np.array(bg_color, dtype=np.float32) / 255.0
        final_rgb = rgb * self.mask[:, :, None] + bg * (1.0 - self.mask[:, :, None])
        final_u8 = np.clip(final_rgb * 255.0, 0, 255).astype(np.uint8)

        if HAS_PIL:
            hi_img = Image.fromarray(final_u8, "RGB")
            low_img = hi_img.resize((self.width, self.height), Image.Resampling.LANCZOS)
            return low_img.tobytes()

        # Simple 2x downsample for pure Python/numpy without PIL
        downsampled = final_u8.reshape(self.height, self.scale, self.width, self.scale, 3).mean(axis=(1, 3)).astype(np.uint8)
        return downsampled.tobytes()

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
    """Proportional animated Dynamic Island floating lens for Windows."""

    FRAME_CYCLE_COUNT = 60

    def __init__(self, root: tk.Tk):
        self.root = root
        self.commands = queue.Queue()
        self.state = "idle"
        # Stay hidden until the voice service sends the first wake state.
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

        if HAS_NUMPY:
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
