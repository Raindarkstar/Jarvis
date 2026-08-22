#!/usr/bin/env python3
"""Windows Optical Glass Dynamic Island & Lens Launcher for Jarvis.

Renders physical optical glass pebble animations matching Apple-style
acoustics and caustics:
- Proportional rounded squircle lens (138x100)
- Solid glossy dark glass body with Dynamic Island pill & lens aperture
- Vivid glowing rainbow spectral dispersion arc and crisp white specular rim
- Zero fringe/burrs against desktop wallpaper

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
import webbrowser
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
TRANSPARENT_RGB = (1, 1, 7)
TRANSPARENT_COLOR = "#010107"

STATE_PARAMS = {
    "idle": {"pulse_rate": 1.0, "bright": 1.0, "speed": 1.0},
    "awake": {"pulse_rate": 2.0, "bright": 1.30, "speed": 1.8},
    "listening": {"pulse_rate": 1.5, "bright": 1.20, "speed": 1.5},
    "thinking": {"pulse_rate": 2.5, "bright": 1.25, "speed": 2.2},
    "speaking": {"pulse_rate": 3.0, "bright": 1.35, "speed": 2.0},
    "error": {"pulse_rate": 1.0, "bright": 0.85, "speed": 0.8},
}


def get_python_exe() -> str:
    """Find the best Python executable (preferring project venv if available)."""
    for venv_path in [
        CODE_ROOT / ".venv" / "Scripts" / "python.exe",
        CODE_ROOT / "venv" / "Scripts" / "python.exe",
        CODE_ROOT / ".venv" / "bin" / "python",
        CODE_ROOT / "bin" / "python",
    ]:
        if venv_path.is_file() and os.access(str(venv_path), os.X_OK):
            return str(venv_path)
    return sys.executable


class OpticalGlassPebbleRenderer:
    """High-fidelity physical optical glass pebble renderer with rainbow caustics."""

    def __init__(self, width: int = ORB_WIDTH, height: int = ORB_HEIGHT):
        self.width = width
        self.height = height
        self.source_path = CODE_ROOT / "assets" / "extracted_pebble_source.png"
        self.base_arr = None
        if HAS_PIL and HAS_NUMPY and self.source_path.is_file():
            try:
                source = Image.open(self.source_path).convert("RGBA")
                source.thumbnail((width, height), Image.Resampling.LANCZOS)
                fitted = Image.new("RGBA", (width, height), (0, 0, 0, 0))
                fitted.alpha_composite(
                    source,
                    ((width - source.width) // 2, (height - source.height) // 2),
                )
                self.base_arr = np.asarray(fitted, dtype=np.float32) / 255.0
            except (OSError, ValueError):
                self.base_arr = None

        self.scale = 2  # 2x internal super-sampling for anti-aliasing
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
            self.p = 2.42
            self.r = (np.abs(self.nx) ** self.p + np.abs(self.ny) ** self.p) ** (1.0 / self.p)

            # 1. Clean boundary mask
            self.mask = (self.r <= 1.0).astype(np.float32)

            # 2. Solid dark glass body
            self.body_rgb = np.array([8.0 / 255.0, 11.0 / 255.0, 18.0 / 255.0], dtype=np.float32)

            # 3. Dynamic Island camera pill cutout
            dx_pill = np.maximum(0.0, np.abs(self.nx) - 0.38)
            dy_pill = self.ny + 0.45
            d_pill = np.sqrt(dx_pill ** 2 + dy_pill ** 2) / 0.28
            self.pill_factor = np.clip((1.0 - d_pill) * 3.5, 0.0, 1.0) * self.mask
            self.pill_rgb = np.array([3.0 / 255.0, 4.0 / 255.0, 7.0 / 255.0], dtype=np.float32)

            # Camera aperture reflection dot at (0.38, -0.45)
            d_lens = np.sqrt((self.nx - 0.38) ** 2 + (self.ny + 0.45) ** 2) / 0.08
            self.lens_mask = np.clip((1.0 - d_lens) * 3.0, 0.0, 1.0) * self.mask
            self.lens_rgb = np.array([18.0 / 255.0, 48.0 / 255.0, 115.0 / 255.0], dtype=np.float32)
            self.lens_glint = (
                np.exp(-(((self.nx - 0.40) / 0.02) ** 2 + ((self.ny + 0.47) / 0.02) ** 2))
                * self.mask
            )

            # 4. Vivid Rainbow Caustic Spectral Dispersion Beam
            self.h_env = np.clip(1.0 - (self.nx * 1.06) ** 2, 0.0, 1.0) ** 0.60
            self.gold_rgb = np.array([255.0 / 255.0, 175.0 / 255.0, 36.0 / 255.0], dtype=np.float32)
            self.core_rgb = np.array([255.0 / 255.0, 255.0 / 255.0, 250.0 / 255.0], dtype=np.float32)
            self.cyan_rgb = np.array([0.0 / 255.0, 225.0 / 255.0, 255.0 / 255.0], dtype=np.float32)
            self.blue_rgb = np.array([18.0 / 255.0, 85.0 / 255.0, 245.0 / 255.0], dtype=np.float32)
            self.bloom_rgb = np.array([45.0 / 255.0, 160.0 / 255.0, 240.0 / 255.0], dtype=np.float32)

            # 5. Subtle internal text refraction "Wed Apr 1" curve under rainbow
            text_y = 0.28 + 0.08 * self.nx * self.nx
            dy_txt = np.abs(self.ny - text_y)
            txt_wave = np.sin(self.nx * 18.0) * 0.02
            self.txt_dist = np.exp(-(((dy_txt + txt_wave) / 0.04) ** 2)) * np.clip(1.0 - self.nx * self.nx, 0.0, 1.0)
            self.txt_rgb = np.array([190.0 / 255.0, 210.0 / 255.0, 235.0 / 255.0], dtype=np.float32)

            # 6. Crisp white specular rim & bevel
            bottom_factor = np.clip((self.ny - 0.15) / 0.8, 0.0, 1.0)
            self.rim_dist = (
                np.exp(-((self.r - 0.96) / 0.030) ** 2)
                * (0.35 + 0.65 * bottom_factor)
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

    def render_raw_rgb(self, phase: float, params: dict, bg_color: tuple = TRANSPARENT_RGB) -> bytes:
        """Renders 2x super-sampled buffer downscaled to target size for crystal clarity."""
        if not HAS_NUMPY:
            return bytes(list(bg_color) * (self.width * self.height))

        speed = params.get("speed", 1.0)
        bright = params.get("bright", 1.0)
        pulse = 1.0 + 0.08 * math.sin(phase * 2 * math.pi * params.get("pulse_rate", 1.0)) * bright

        if self.base_arr is not None:
            # Prefer the extracted transparent optical-glass material.  The
            # procedural renderer below remains the installation fallback.
            source_rgb = np.clip(self.base_arr[:, :, :3] * pulse, 0.0, 1.0)
            source_alpha = self.base_arr[:, :, 3]
            bg = np.asarray(bg_color, dtype=np.float32) / 255.0
            final_rgb = (
                source_rgb * source_alpha[:, :, None]
                + bg * (1.0 - source_alpha[:, :, None])
            )
            final_u8 = np.clip(final_rgb * 255.0, 0, 255).astype(np.uint8)
            final_u8[source_alpha == 0.0] = np.asarray(bg_color, dtype=np.uint8)
            return final_u8.tobytes()

        w1 = 0.016 * speed * np.sin(phase * 2 * math.pi * speed + self.nx * 2.8)
        w2 = 0.008 * speed * np.cos(phase * 4 * math.pi * speed - self.nx * 4.2)

        arc_center_y = 0.01 - 0.065 * (1.0 - self.nx * self.nx) + w1 + w2
        dy = self.ny - arc_center_y

        gold_dist = np.exp(-((dy + 0.045) / 0.040) ** 2)
        core_dist = np.exp(-(dy / 0.022) ** 2)
        cyan_dist = np.exp(-((dy - 0.030) / 0.040) ** 2)
        blue_dist = np.exp(-((dy - 0.075) / 0.052) ** 2)
        bloom_dist = np.exp(-(dy / 0.12) ** 2)

        spectral = (
            self.gold_rgb * (gold_dist[:, :, None] * 0.95)
            + self.core_rgb * (core_dist[:, :, None] * 1.45)
            + self.cyan_rgb * (cyan_dist[:, :, None] * 1.15)
            + self.blue_rgb * (blue_dist[:, :, None] * 0.95)
            + self.bloom_rgb * (bloom_dist[:, :, None] * 0.35)
        ) * (self.h_env[:, :, None] * pulse * self.mask[:, :, None])

        rgb = (
            self.body_rgb * self.mask[:, :, None]
            + self.pill_rgb * (self.pill_factor[:, :, None] * 0.8)
            + self.lens_rgb * (self.lens_mask[:, :, None] * 0.7)
            + self.lens_glint[:, :, None] * 0.95
            + spectral
            + self.txt_rgb * (self.txt_dist[:, :, None] * 0.35 * self.mask[:, :, None])
            + self.rim_rgb * (self.rim_dist[:, :, None] * 0.95)
            + self.edge_rgb * (self.edge_dist[:, :, None] * 0.5)
            + self.top_sheen[:, :, None] * 0.4
        )

        rgb = np.clip(rgb, 0.0, 1.0)
        bg = np.array(bg_color, dtype=np.float32) / 255.0

        if HAS_PIL:
            # Resize color and coverage independently.  Resizing an already
            # composited image lets Lanczos bleed near-black body pixels into
            # the transparent-color area; Windows then sees an opaque black
            # rectangle instead of the keyed background.
            hi_rgb = Image.fromarray(
                np.clip(rgb * 255.0, 0, 255).astype(np.uint8), "RGB"
            )
            hi_alpha = Image.fromarray(
                np.clip(self.mask * 255.0, 0, 255).astype(np.uint8), "L"
            )
            low_rgb = np.asarray(
                hi_rgb.resize((self.width, self.height), Image.Resampling.LANCZOS),
                dtype=np.float32,
            ) / 255.0
            low_alpha = np.asarray(
                hi_alpha.resize((self.width, self.height), Image.Resampling.LANCZOS),
                dtype=np.float32,
            ) / 255.0
            # Pixels with no actual shape coverage must remain the *exact*
            # color key.  This exact equality is required by Tk on Windows.
            low_alpha[low_alpha < (1.0 / 255.0)] = 0.0
            final_rgb = low_rgb * low_alpha[:, :, None] + bg * (1.0 - low_alpha[:, :, None])
            final_u8 = np.clip(final_rgb * 255.0, 0, 255).astype(np.uint8)
            final_u8[low_alpha == 0.0] = np.asarray(bg_color, dtype=np.uint8)
            return final_u8.tobytes()

        # 2x downsample for numpy without PIL
        low_rgb = rgb.reshape(
            self.height, self.scale, self.width, self.scale, 3
        ).mean(axis=(1, 3))
        low_alpha = self.mask.reshape(
            self.height, self.scale, self.width, self.scale
        ).mean(axis=(1, 3))
        final_rgb = low_rgb * low_alpha[:, :, None] + bg * (1.0 - low_alpha[:, :, None])
        final_u8 = np.clip(final_rgb * 255.0, 0, 255).astype(np.uint8)
        final_u8[low_alpha == 0.0] = np.asarray(bg_color, dtype=np.uint8)
        return final_u8.tobytes()

    def render_pil_frame(self, phase: float, params: dict, bg_color: tuple = TRANSPARENT_RGB):
        """Renders PIL Image if PIL is installed, or native Tk PhotoImage."""
        raw_bytes = self.render_raw_rgb(phase, params, bg_color)
        if HAS_PIL:
            return Image.frombytes("RGB", (self.width, self.height), raw_bytes)
        header = f"P6 {self.width} {self.height} 255\n".encode("ascii")
        return tk.PhotoImage(data=header + raw_bytes)

    def render_photo_image(self, phase: float, params: dict, bg_color: tuple = TRANSPARENT_RGB):
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
        # Stay hidden while waiting for the offline wake-word detector.
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

        self.transparent_color = TRANSPARENT_COLOR
        self.window.configure(background=self.transparent_color)
        try:
            self.window.attributes("-transparentcolor", self.transparent_color)
        except tk.TclError:
            pass
        self.window.update_idletasks()
        self._apply_windows_color_key()

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

    def _apply_windows_color_key(self) -> bool:
        """Apply the native Windows color key after Tk creates the real HWND."""
        if os.name != "nt":
            return False
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.GetParent.argtypes = [wintypes.HWND]
            user32.GetParent.restype = wintypes.HWND
            hwnd = user32.GetParent(self.window.winfo_id()) or self.window.winfo_id()
            get_window_long = getattr(user32, "GetWindowLongPtrW", user32.GetWindowLongW)
            set_window_long = getattr(user32, "SetWindowLongPtrW", user32.SetWindowLongW)
            get_window_long.argtypes = [wintypes.HWND, ctypes.c_int]
            get_window_long.restype = ctypes.c_ssize_t
            set_window_long.argtypes = [wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t]
            set_window_long.restype = ctypes.c_ssize_t
            user32.SetLayeredWindowAttributes.argtypes = [
                wintypes.HWND,
                wintypes.COLORREF,
                wintypes.BYTE,
                wintypes.DWORD,
            ]
            user32.SetLayeredWindowAttributes.restype = wintypes.BOOL
            ex_style = get_window_long(hwnd, -20)
            set_window_long(hwnd, -20, ex_style | 0x00080000)  # WS_EX_LAYERED
            color_key = (
                TRANSPARENT_RGB[0]
                | (TRANSPARENT_RGB[1] << 8)
                | (TRANSPARENT_RGB[2] << 16)
            )
            return bool(user32.SetLayeredWindowAttributes(hwnd, color_key, 255, 0x00000001))
        except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
            return False

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
            # If the process is already alive, never spawn a duplicate while
            # WebView2 is still constructing its first native window.
            if self.desktop_process is not None and self.desktop_process.poll() is None:
                hwnd = self._find_desktop_hwnd()
                if hwnd is not None:
                    try:
                        import ctypes
                        user32 = ctypes.WinDLL("user32", use_last_error=True)
                        visible = bool(user32.IsWindowVisible(hwnd))
                    except (AttributeError, OSError, TypeError, ValueError):
                        visible = False
                    self._show_desktop(not visible)
                    return
                self._wait_for_desktop(self.desktop_process)
                return

            python_bin = get_python_exe()
            client_target = "windows_client.py" if os.name == "nt" else "client_app.py"
            script_path = CODE_ROOT / client_target

            env = os.environ.copy()
            env["JARVIS_DESKTOP_CHILD"] = "1"
            creationflags = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if os.name == "nt" else 0

            if script_path.exists():
                try:
                    proc = subprocess.Popen(
                        [python_bin, str(script_path)],
                        cwd=str(CODE_ROOT),
                        env=env,
                        creationflags=creationflags,
                    )
                    self.desktop_process = proc
                    if self._wait_for_desktop(proc):
                        return
                except OSError:
                    self.desktop_process = None

            # Only fall back after the native process has actually failed.
            html_path = CODE_ROOT / "client_ui.html"
            if html_path.exists():
                webbrowser.open_new_tab(html_path.resolve().as_uri())

    def _wait_for_desktop(self, process, timeout: float = 8.0) -> bool:
        """Bring WebView2 forward as soon as its HWND becomes available."""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._show_desktop(True):
                return True
            if process.poll() is not None:
                self.desktop_process = None
                return False
            time.sleep(0.05)
        # A slow but healthy WebView2 process may publish its window later;
        # leave it running instead of launching another client over it.
        return process.poll() is None

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
