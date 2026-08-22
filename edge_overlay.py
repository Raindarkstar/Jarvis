import ctypes
import math
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import tkinter as tk
from pathlib import Path

# Ubuntu ships GTK bindings in dist-packages rather than the project venv.
DIST_PACKAGES = "/usr/lib/python3/dist-packages"
if DIST_PACKAGES not in sys.path:
    sys.path.append(DIST_PACKAGES)

import cairo
import gi
import numpy as np

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gdk, GLib, Gtk
from PIL import Image, ImageDraw, ImageTk

from client_app import ClientWindow, client_app_controller


ORB_WIDTH = 138
ORB_HEIGHT = 100
TOP_OFFSET = 24
DISPLAY_FPS = 60
FPS_MS = 1000 // DISPLAY_FPS

STATE_PARAMS = {
    "idle": {"pulse_rate": 1.0, "bright": 1.0, "wave_speed": 1.0, "wave_amp": 1.0},
    "awake": {"pulse_rate": 2.0, "bright": 1.35, "wave_speed": 1.8, "wave_amp": 1.3},
    "listening": {"pulse_rate": 1.5, "bright": 1.2, "wave_speed": 1.5, "wave_amp": 1.2},
    "thinking": {"pulse_rate": 2.5, "bright": 1.3, "wave_speed": 2.5, "wave_amp": 1.4},
    "speaking": {"pulse_rate": 3.0, "bright": 1.45, "wave_speed": 2.0, "wave_amp": 1.5},
    "error": {"pulse_rate": 1.0, "bright": 0.85, "wave_speed": 0.8, "wave_amp": 0.8},
}


class OpticalGlassPebbleRenderer:
    """Fast vectorized optical glass pebble renderer with physical dispersion and caustics."""

    def __init__(self, width=ORB_WIDTH, height=ORB_HEIGHT):
        self.width = width
        self.height = height
        self.cx = (width - 1) / 2.0
        self.cy = (height - 1) / 2.0
        self.rx = width / 2.0 - 1.5
        self.ry = height / 2.0 - 1.5

        y_grid, x_grid = np.indices((height, width), dtype=np.float32)
        self.nx = (x_grid - self.cx) / self.rx
        self.ny = (y_grid - self.cy) / self.ry
        self.p = 2.45
        self.r = (np.abs(self.nx) ** self.p + np.abs(self.ny) ** self.p) ** (
            1.0 / self.p
        )

        # 1. Antialiased outer perimeter mask
        edge_d = (1.0 - self.r) * min(self.rx, self.ry)
        mask = np.clip(edge_d * 1.6, 0.0, 1.0)
        self.mask = mask * mask * (3.0 - 2.0 * mask)

        # 2. Top smoked glass dome (Dynamic Island area)
        top_factor = np.clip((-self.ny + 0.08) / 0.95, 0.0, 1.0)
        self.top_alpha = (top_factor ** 1.35) * 0.88 * self.mask
        self.top_rgb = np.array([8.0 / 255.0, 10.0 / 255.0, 14.0 / 255.0], dtype=np.float32)

        # 3. Base clear glass body translucency
        self.glass_alpha = 0.08 * (1.0 - 0.3 * self.ny) * self.mask
        self.glass_rgb = np.array([30.0 / 255.0, 36.0 / 255.0, 48.0 / 255.0], dtype=np.float32)

        # 4. Caustic rim & bevel geometry
        bottom_factor = np.clip((self.ny - 0.2) / 0.75, 0.0, 1.0)
        self.rim_dist = np.exp(-((self.r - 0.962) / 0.032) ** 2) * bottom_factor * self.mask
        self.rim_rgb = np.array([245.0 / 255.0, 250.0 / 255.0, 255.0 / 255.0], dtype=np.float32)

        self.edge_dist = np.exp(-((self.r - 0.978) / 0.018) ** 2) * 0.32 * self.mask
        self.edge_rgb = np.array([210.0 / 255.0, 225.0 / 255.0, 245.0 / 255.0], dtype=np.float32)

        self.top_sheen_dist = (
            np.exp(-(((self.ny + 0.75) / 0.18) ** 2) - ((self.nx / 0.55) ** 2))
            * 0.12
            * self.mask
        )

        # 5. Precomputed spectral dispersion colors & envelopes
        self.h_env = np.clip(1.0 - (self.nx * 1.06) ** 2, 0.0, 1.0) ** 0.62
        self.bounce_y = 0.52 + 0.32 * (self.nx * self.nx)
        self.bounce_rgb = np.array([105.0 / 255.0, 185.0 / 255.0, 255.0 / 255.0], dtype=np.float32)

        self.gold_rgb = np.array([255.0 / 255.0, 172.0 / 255.0, 36.0 / 255.0], dtype=np.float32)
        self.core_rgb = np.array([255.0 / 255.0, 255.0 / 255.0, 245.0 / 255.0], dtype=np.float32)
        self.cyan_rgb = np.array([0.0 / 255.0, 222.0 / 255.0, 255.0 / 255.0], dtype=np.float32)
        self.blue_rgb = np.array([16.0 / 255.0, 78.0 / 255.0, 242.0 / 255.0], dtype=np.float32)
        self.bloom_rgb = np.array([45.0 / 255.0, 160.0 / 255.0, 240.0 / 255.0], dtype=np.float32)

    def render_bgra_bytes(self, phase, params):
        """Renders premultiplied ARGB32 bytes for Cairo ImageSurface at 60 FPS."""
        w_speed = params["wave_speed"]
        w_amp = params["wave_amp"]

        w1 = 0.014 * w_amp * np.sin(phase * 2 * math.pi * w_speed + self.nx * 2.8)
        w2 = 0.007 * w_amp * np.cos(phase * 4 * math.pi * w_speed - self.nx * 4.2)
        w3 = 0.004 * np.sin(phase * 2 * math.pi * 0.5)

        arc_center_y = 0.015 - 0.062 * (1.0 - self.nx * self.nx) + w1 + w2 + w3
        dy = self.ny - arc_center_y

        pulse = 1.0 + 0.08 * math.sin(phase * 2 * math.pi * params["pulse_rate"])
        brightness = params["bright"] * pulse

        # Spectral dispersion bands
        gold_dist = np.exp(-((dy + 0.042) / 0.038) ** 2)
        core_dist = np.exp(-(dy / 0.020) ** 2)
        cyan_dist = np.exp(-((dy - 0.028) / 0.038) ** 2)
        blue_dist = np.exp(-((dy - 0.072) / 0.050) ** 2)
        bloom_dist = np.exp(-(dy / 0.12) ** 2)

        spectral_total = (
            self.gold_rgb * (gold_dist[:, :, None] * 0.96)
            + self.core_rgb * (core_dist[:, :, None] * 1.38)
            + self.cyan_rgb * (cyan_dist[:, :, None] * 1.10)
            + self.blue_rgb * (blue_dist[:, :, None] * 0.94)
            + self.bloom_rgb * (bloom_dist[:, :, None] * 0.38)
        ) * (self.h_env[:, :, None] * brightness * self.mask[:, :, None])

        # Internal glass caustic reflection
        dy_b = self.ny - self.bounce_y
        bounce_dist = np.exp(-(dy_b / 0.07) ** 2) * np.clip(1.0 - self.nx * self.nx, 0.0, 1.0)
        bounce_layer = self.bounce_rgb * (
            bounce_dist[:, :, None] * 0.25 * brightness * self.mask[:, :, None]
        )

        rgb = (
            self.top_rgb * self.top_alpha[:, :, None]
            + self.glass_rgb * self.glass_alpha[:, :, None]
            + spectral_total
            + bounce_layer
            + self.rim_rgb * (self.rim_dist[:, :, None] * 0.90)
            + self.edge_rgb * self.edge_dist[:, :, None]
            + self.top_sheen_dist[:, :, None]
        )

        spectral_alpha = np.clip(np.max(spectral_total, axis=2) * 0.95, 0.0, 1.0)
        alpha = np.clip(
            self.top_alpha
            + self.glass_alpha
            + spectral_alpha
            + bounce_dist * 0.20 * self.mask
            + self.rim_dist * 0.90
            + self.edge_dist
            + self.top_sheen_dist,
            0.0,
            1.0,
        )

        rgb = np.clip(rgb, 0.0, 1.0)
        # Cairo ARGB32 format on little-endian x86_64 expects BGRA order with premultiplied RGB
        alpha_u8 = (alpha * 255.0).astype(np.uint8)
        r_pre = np.clip(rgb[:, :, 0] * alpha * 255.0, 0, 255).astype(np.uint8)
        g_pre = np.clip(rgb[:, :, 1] * alpha * 255.0, 0, 255).astype(np.uint8)
        b_pre = np.clip(rgb[:, :, 2] * alpha * 255.0, 0, 255).astype(np.uint8)

        bgra = np.dstack((b_pre, g_pre, r_pre, alpha_u8))
        return bytearray(bgra.tobytes())

    def render_pil_frame(self, phase, params):
        """Renders standard un-premultiplied RGBA PIL image for Tkinter fallback."""
        w_speed = params["wave_speed"]
        w_amp = params["wave_amp"]

        w1 = 0.014 * w_amp * np.sin(phase * 2 * math.pi * w_speed + self.nx * 2.8)
        w2 = 0.007 * w_amp * np.cos(phase * 4 * math.pi * w_speed - self.nx * 4.2)
        w3 = 0.004 * np.sin(phase * 2 * math.pi * 0.5)

        arc_center_y = 0.015 - 0.062 * (1.0 - self.nx * self.nx) + w1 + w2 + w3
        dy = self.ny - arc_center_y

        pulse = 1.0 + 0.08 * math.sin(phase * 2 * math.pi * params["pulse_rate"])
        brightness = params["bright"] * pulse

        gold_dist = np.exp(-((dy + 0.042) / 0.038) ** 2)
        core_dist = np.exp(-(dy / 0.020) ** 2)
        cyan_dist = np.exp(-((dy - 0.028) / 0.038) ** 2)
        blue_dist = np.exp(-((dy - 0.072) / 0.050) ** 2)
        bloom_dist = np.exp(-(dy / 0.12) ** 2)

        spectral_total = (
            self.gold_rgb * (gold_dist[:, :, None] * 0.96)
            + self.core_rgb * (core_dist[:, :, None] * 1.38)
            + self.cyan_rgb * (cyan_dist[:, :, None] * 1.10)
            + self.blue_rgb * (blue_dist[:, :, None] * 0.94)
            + self.bloom_rgb * (bloom_dist[:, :, None] * 0.38)
        ) * (self.h_env[:, :, None] * brightness * self.mask[:, :, None])

        dy_b = self.ny - self.bounce_y
        bounce_dist = np.exp(-(dy_b / 0.07) ** 2) * np.clip(1.0 - self.nx * self.nx, 0.0, 1.0)
        bounce_layer = self.bounce_rgb * (
            bounce_dist[:, :, None] * 0.25 * brightness * self.mask[:, :, None]
        )

        rgb = (
            self.top_rgb * self.top_alpha[:, :, None]
            + self.glass_rgb * self.glass_alpha[:, :, None]
            + spectral_total
            + bounce_layer
            + self.rim_rgb * (self.rim_dist[:, :, None] * 0.90)
            + self.edge_rgb * self.edge_dist[:, :, None]
            + self.top_sheen_dist[:, :, None]
        )

        spectral_alpha = np.clip(np.max(spectral_total, axis=2) * 0.95, 0.0, 1.0)
        alpha = np.clip(
            self.top_alpha
            + self.glass_alpha
            + spectral_alpha
            + bounce_dist * 0.20 * self.mask
            + self.rim_dist * 0.90
            + self.edge_dist
            + self.top_sheen_dist,
            0.0,
            1.0,
        )

        rgb = np.clip(rgb, 0.0, 1.0)
        rgba = np.zeros((self.height, self.width, 4), dtype=np.uint8)
        rgba[:, :, :3] = (rgb * 255.0).astype(np.uint8)
        rgba[:, :, 3] = (alpha * 255.0).astype(np.uint8)
        return Image.fromarray(rgba, "RGBA")


class XRectangle(ctypes.Structure):
    _fields_ = [
        ("x", ctypes.c_short),
        ("y", ctypes.c_short),
        ("width", ctypes.c_ushort),
        ("height", ctypes.c_ushort),
    ]


def _shape_ellipse_and_make_click_through(window, width, height):
    """Shape X11 window into an ellipse and make it click-through."""
    if os.environ.get("XDG_SESSION_TYPE", "x11").lower() != "x11":
        return

    try:
        x11 = ctypes.cdll.LoadLibrary("libX11.so.6")
        xfixes = ctypes.cdll.LoadLibrary("libXfixes.so.3")

        x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        x11.XOpenDisplay.restype = ctypes.c_void_p
        x11.XFlush.argtypes = [ctypes.c_void_p]
        x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        x11.XQueryTree.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.c_ulong),
            ctypes.POINTER(ctypes.POINTER(ctypes.c_ulong)),
            ctypes.POINTER(ctypes.c_uint),
        ]
        x11.XQueryTree.restype = ctypes.c_int
        x11.XFree.argtypes = [ctypes.c_void_p]
        xfixes.XFixesCreateRegion.restype = ctypes.c_ulong
        xfixes.XFixesSetWindowShapeRegion.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_ulong,
        ]
        xfixes.XFixesDestroyRegion.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
        ]

        display = x11.XOpenDisplay(None)
        if not display:
            return

        radius_x = width / 2
        radius_y = height / 2
        rectangles = (XRectangle * height)()

        p = 2.45
        for y in range(height):
            dy = abs((y + 0.5 - radius_y) / radius_y)
            if dy < 1.0:
                half_width = radius_x * ((max(0.0, 1.0 - dy ** p)) ** (1.0 / p))
                left = max(0, round(radius_x - half_width))
                right = min(width, round(radius_x + half_width))
                rectangles[y] = XRectangle(left, y, max(1, right - left), 1)
            else:
                rectangles[y] = XRectangle(round(radius_x), y, 1, 1)

        xfixes.XFixesCreateRegion.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(XRectangle),
            ctypes.c_int,
        ]
        bounding_region = xfixes.XFixesCreateRegion(display, rectangles, height)
        xfixes.XFixesCreateRegion.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_int,
        ]
        input_region = xfixes.XFixesCreateRegion(display, None, 0)

        target_windows = [window.winfo_id()]
        root_return = ctypes.c_ulong()
        parent_return = ctypes.c_ulong()
        children_return = ctypes.POINTER(ctypes.c_ulong)()
        child_count = ctypes.c_uint()
        queried = x11.XQueryTree(
            display,
            ctypes.c_ulong(window.winfo_id()),
            ctypes.byref(root_return),
            ctypes.byref(parent_return),
            ctypes.byref(children_return),
            ctypes.byref(child_count),
        )

        if queried and parent_return.value != root_return.value:
            target_windows.append(parent_return.value)

        if children_return:
            x11.XFree(children_return)

        for target_window in target_windows:
            xfixes.XFixesSetWindowShapeRegion(
                display,
                ctypes.c_ulong(target_window),
                0,
                0,
                0,
                bounding_region,
            )
            xfixes.XFixesSetWindowShapeRegion(
                display,
                ctypes.c_ulong(target_window),
                2,
                0,
                0,
                input_region,
            )

        xfixes.XFixesDestroyRegion(display, bounding_region)
        xfixes.XFixesDestroyRegion(display, input_region)
        x11.XFlush(display)
        x11.XCloseDisplay(display)
    except (OSError, AttributeError):
        pass


class GtkGlassOrb:
    """Per-pixel transparent GTK3 renderer for crystal-clear optical glass lens."""

    def __init__(self, start_visible=False):
        self.commands = queue.Queue()
        self.state = "idle"
        self.visible = start_visible
        self.frame = 0
        self.startup_frames = 0
        self.surface = None
        self.surface_data = None

        # Smooth state transition interpolators
        self.current_params = dict(STATE_PARAMS["idle"])

        self.renderer = OpticalGlassPebbleRenderer(ORB_WIDTH, ORB_HEIGHT)

        screen = Gdk.Screen.get_default()
        display = Gdk.Display.get_default()
        monitor = display.get_primary_monitor() or display.get_monitor(0)
        geometry = monitor.get_geometry()
        self.left = geometry.x + round((geometry.width - ORB_WIDTH) / 2)

        self.window = Gtk.Window(type=Gtk.WindowType.TOPLEVEL)
        rgba_visual = screen.get_rgba_visual()
        if rgba_visual is not None:
            self.window.set_visual(rgba_visual)
        self.window.set_app_paintable(True)
        self.window.set_decorated(False)
        self.window.set_keep_above(True)
        self.window.set_skip_taskbar_hint(True)
        self.window.set_skip_pager_hint(True)
        self.window.set_accept_focus(False)
        self.window.set_default_size(ORB_WIDTH, ORB_HEIGHT)
        self.window.move(self.left, TOP_OFFSET)
        self.window.connect("draw", self._draw)
        self.window.add_events(Gdk.EventMask.BUTTON_PRESS_MASK)
        self.window.connect("button-press-event", self._on_button_press)

        # 实例化桌面级客户端窗口
        self.client_window = ClientWindow(overlay_controller=None)
        client_app_controller.bind_window(self.client_window)

        if self.visible:
            self.window.show_all()
        else:
            self.window.hide()

        GLib.timeout_add(FPS_MS, self._animate_gtk)

    def _on_button_press(self, _widget, event):
        if event.button == 1:
            # 鼠标左键点击超椭圆：切换桌面客户端界面的显隐
            client_app_controller.toggle()
            return True
        return False

    def show(self):
        if not self.visible:
            self.visible = True
            self.window.show_all()

    def hide(self):
        if self.visible:
            self.visible = False
            self.window.hide()

    def set_state(self, state):
        if state in ["hide", "hidden", "idle"]:
            self.hide()
            self.state = "idle"
            return

        if state == "show":
            self.show()
            return

        if state in STATE_PARAMS:
            self.state = state
            if not self.visible:
                self.show()

    def _update_params(self):
        target = STATE_PARAMS.get(self.state, STATE_PARAMS["idle"])
        lerp = 0.12
        for k in self.current_params:
            self.current_params[k] += (target[k] - self.current_params[k]) * lerp

    def _draw(self, _widget, context):
        context.set_operator(cairo.OPERATOR_SOURCE)
        context.set_source_rgba(0.0, 0.0, 0.0, 0.0)
        context.paint()

        if self.surface is not None and self.visible:
            context.set_operator(cairo.OPERATOR_OVER)
            context.set_source_surface(self.surface, 0, 0)
            context.paint()
        return False

    def _animate_gtk(self):
        try:
            while True:
                command = self.commands.get_nowait()
                if command == "quit":
                    Gtk.main_quit()
                    return False
                if command.startswith("{"):
                    try:
                        import json
                        data = json.loads(command)
                        action = data.get("action")
                        if action == "user_text":
                            client_app_controller.on_user_transcript(
                                data.get("text", ""),
                                data.get("final", False),
                            )
                        elif action == "ai_delta":
                            client_app_controller.on_ai_delta(data.get("delta", ""))
                        elif action == "ai_finish":
                            client_app_controller.on_turn_done(data.get("text", ""))
                        elif action == "tool_call":
                            client_app_controller.on_tool_call(
                                data.get("name", ""),
                                data.get("query", ""),
                            )
                        elif action == "toggle_chat":
                            client_app_controller.toggle()
                        elif action == "show_chat":
                            client_app_controller.show()
                        elif action == "hide_chat":
                            client_app_controller.hide()
                        continue
                    except Exception:
                        pass
                self.set_state(command)
        except queue.Empty:
            pass

        if not self.visible:
            return True

        self._update_params()
        phase = (self.frame % 240) / 240.0
        self.surface_data = self.renderer.render_bgra_bytes(phase, self.current_params)
        self.surface = cairo.ImageSurface.create_for_data(
            self.surface_data,
            cairo.FORMAT_ARGB32,
            ORB_WIDTH,
            ORB_HEIGHT,
            ORB_WIDTH * 4,
        )
        self.window.queue_draw()
        self.frame += 1
        return True


class GlassOrb:
    """Tkinter fallback renderer."""

    def __init__(self, root, start_visible=False):
        self.root = root
        self.commands = queue.Queue()
        self.state = "idle"
        self.visible = start_visible
        self.frame = 0
        self.photo = None
        self.current_params = dict(STATE_PARAMS["idle"])
        self.renderer = OpticalGlassPebbleRenderer(ORB_WIDTH, ORB_HEIGHT)

        root.withdraw()
        screen_width = root.winfo_screenwidth()
        self.left = round((screen_width - ORB_WIDTH) / 2)

        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.geometry(f"{ORB_WIDTH}x{ORB_HEIGHT}+{self.left}+{TOP_OFFSET}")
        self.window.attributes("-topmost", True)
        self.window.configure(background="#000000")

        self.canvas = tk.Canvas(
            self.window,
            width=ORB_WIDTH,
            height=ORB_HEIGHT,
            highlightthickness=0,
            borderwidth=0,
            background="#000000",
        )
        self.canvas.pack(fill="both", expand=True)
        self.image_item = self.canvas.create_image(0, 0, anchor="nw")

        self.window.update_idletasks()
        _shape_ellipse_and_make_click_through(self.window, ORB_WIDTH, ORB_HEIGHT)
        if self.visible:
            self.window.deiconify()
        self.root.after(FPS_MS, self._animate)

    def show(self):
        if not self.visible:
            self.visible = True
            self.window.deiconify()

    def hide(self):
        if self.visible:
            self.visible = False
            self.window.withdraw()

    def set_state(self, state):
        if state in ["hide", "hidden", "idle"]:
            self.hide()
            self.state = "idle"
            return

        if state == "show":
            self.show()
            return

        if state in STATE_PARAMS:
            self.state = state
            if not self.visible:
                self.show()

    def _update_params(self):
        target = STATE_PARAMS.get(self.state, STATE_PARAMS["idle"])
        lerp = 0.12
        for k in self.current_params:
            self.current_params[k] += (target[k] - self.current_params[k]) * lerp

    def _animate(self):
        try:
            while True:
                command = self.commands.get_nowait()
                if command == "quit":
                    self.root.destroy()
                    return
                self.set_state(command)
        except queue.Empty:
            pass

        if not self.visible:
            self.root.after(FPS_MS, self._animate)
            return

        self._update_params()
        phase = (self.frame % 240) / 240.0
        frame = self.renderer.render_pil_frame(phase, self.current_params)
        self.photo = ImageTk.PhotoImage(frame)
        self.canvas.itemconfigure(self.image_item, image=self.photo)
        self.window.lift()

        self.frame += 1
        self.root.after(FPS_MS, self._animate)


def _read_commands(orb):
    for line in sys.stdin:
        orb.commands.put(line.strip())
    orb.commands.put("quit")


def run_overlay():
    orb = GtkGlassOrb(start_visible=False)

    def stop_overlay(*_args):
        GLib.idle_add(Gtk.main_quit)

    signal.signal(signal.SIGINT, stop_overlay)

    reader = threading.Thread(
        target=_read_commands,
        args=(orb,),
        daemon=True,
    )
    reader.start()

    try:
        Gtk.main()
    except KeyboardInterrupt:
        Gtk.main_quit()


class EdgeOverlayController:
    def __init__(self):
        self.process = None

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return

        self.process = subprocess.Popen(
            [sys.executable, os.path.abspath(__file__)],
            stdin=subprocess.PIPE,
            text=True,
            bufsize=1,
        )

    def show(self):
        self.set_state("show")

    def hide(self):
        self.set_state("hide")

    def _send_cmd(self, cmd: str):
        if (
            self.process is None
            or self.process.poll() is not None
            or self.process.stdin is None
        ):
            return

        try:
            self.process.stdin.write(f"{cmd}\n")
            self.process.stdin.flush()
        except (BrokenPipeError, OSError):
            pass

    def set_state(self, state):
        self._send_cmd(state)

    def update_user_transcript(self, text: str, is_final: bool = False):
        import json
        payload = json.dumps({"action": "user_text", "text": text, "final": is_final})
        self._send_cmd(payload)

    def append_ai_delta(self, delta: str):
        import json
        payload = json.dumps({"action": "ai_delta", "delta": delta})
        self._send_cmd(payload)

    def finish_ai_turn(self, full_text: str = ""):
        import json
        payload = json.dumps({"action": "ai_finish", "text": full_text})
        self._send_cmd(payload)

    def show_tool_call(self, tool_name: str, query: str = ""):
        import json
        payload = json.dumps({"action": "tool_call", "name": tool_name, "query": query})
        self._send_cmd(payload)

    def toggle_chat(self):
        import json
        payload = json.dumps({"action": "toggle_chat"})
        self._send_cmd(payload)

    def close(self):
        if self.process is None or self.process.poll() is not None:
            return

        self.set_state("quit")
        try:
            self.process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            self.process.terminate()


if __name__ == "__main__":
    run_overlay()
