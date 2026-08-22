#!/usr/bin/env python3
import math
from pathlib import Path

import numpy as np
from PIL import Image

PROJECT_DIR = Path(__file__).parent
OUTPUT_WEBP = PROJECT_DIR / "assets" / "siri-glass-orb-loop.webp"
OUTPUT_GIF = PROJECT_DIR / "assets" / "siri-glass-orb-loop.gif"
OUTPUT_MATERIAL = PROJECT_DIR / "assets" / "glass-orb-material-v2.png"
OUTPUT_ORB_PNG = PROJECT_DIR / "assets" / "glass-orb.png"

WIDTH = 416
HEIGHT = 308
ANIMATION_FPS = 60
ANIMATION_DURATION_SECONDS = 4
FRAME_COUNT = ANIMATION_FPS * ANIMATION_DURATION_SECONDS
FRAME_DURATION_MS = round(1000 / ANIMATION_FPS)


class OpticalGlassPebbleRenderer:
    """Renders physical optical glass pebble with spectral dispersion and caustics."""

    def __init__(self, width=WIDTH, height=HEIGHT):
        self.width = width
        self.height = height
        self.cx = (width - 1) / 2.0
        self.cy = (height - 1) / 2.0
        self.rx = width / 2.0 - 2.0
        self.ry = height / 2.0 - 2.0

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

        # 2. Dynamic Island smoked black glass capsule cutout & camera aperture
        dx_pill = np.maximum(0.0, np.abs(self.nx) - 0.38)
        dy_pill = self.ny + 0.45
        d_pill = np.sqrt(dx_pill ** 2 + dy_pill ** 2) / 0.28
        pill_factor = np.clip((1.0 - d_pill) * 3.5, 0.0, 1.0)
        self.pill_alpha = pill_factor * 0.95 * self.mask
        self.pill_rgb = np.array([4.0 / 255.0, 6.0 / 255.0, 10.0 / 255.0], dtype=np.float32)

        # Camera lens aperture reflection
        d_lens = np.sqrt((self.nx - 0.38) ** 2 + (self.ny + 0.45) ** 2) / 0.075
        self.lens_mask = np.clip((1.0 - d_lens) * 3.0, 0.0, 1.0) * self.mask
        self.lens_rgb = np.array([14.0 / 255.0, 36.0 / 255.0, 85.0 / 255.0], dtype=np.float32)
        self.lens_glint = (
            np.exp(-(((self.nx - 0.395) / 0.02) ** 2 + ((self.ny + 0.47) / 0.02) ** 2))
            * self.mask
        )

        # 3. Base clear glass body translucency
        self.glass_alpha = 0.08 * (1.0 - 0.3 * self.ny) * self.mask
        self.glass_rgb = np.array([30.0 / 255.0, 36.0 / 255.0, 48.0 / 255.0], dtype=np.float32)

        # 4. Caustic rim & bevel geometry
        bottom_factor = np.clip((self.ny - 0.2) / 0.75, 0.0, 1.0)
        self.rim_dist = (
            np.exp(-((self.r - 0.962) / 0.032) ** 2) * bottom_factor * self.mask
        )
        self.rim_rgb = np.array([245.0 / 255.0, 250.0 / 255.0, 255.0 / 255.0], dtype=np.float32)

        self.edge_dist = np.exp(-((self.r - 0.978) / 0.018) ** 2) * 0.32 * self.mask
        self.edge_rgb = np.array([210.0 / 255.0, 225.0 / 255.0, 245.0 / 255.0], dtype=np.float32)

        self.top_sheen_dist = (
            np.exp(-(((self.ny + 0.75) / 0.18) ** 2) - ((self.nx / 0.55) ** 2))
            * 0.12
            * self.mask
        )

        # 5. Precomputed spectral dispersion colors
        self.h_env = np.clip(1.0 - (self.nx * 1.06) ** 2, 0.0, 1.0) ** 0.62
        self.bounce_y = 0.52 + 0.32 * (self.nx * self.nx)
        self.bounce_rgb = np.array([105.0 / 255.0, 185.0 / 255.0, 255.0 / 255.0], dtype=np.float32)

        self.gold_rgb = np.array([255.0 / 255.0, 172.0 / 255.0, 36.0 / 255.0], dtype=np.float32)
        self.core_rgb = np.array([255.0 / 255.0, 255.0 / 255.0, 245.0 / 255.0], dtype=np.float32)
        self.cyan_rgb = np.array([0.0 / 255.0, 222.0 / 255.0, 255.0 / 255.0], dtype=np.float32)
        self.blue_rgb = np.array([16.0 / 255.0, 78.0 / 255.0, 242.0 / 255.0], dtype=np.float32)
        self.bloom_rgb = np.array([45.0 / 255.0, 160.0 / 255.0, 240.0 / 255.0], dtype=np.float32)

    def render_frame(self, phase=0.0, state="idle", energy=1.0):
        """Renders an RGBA PIL Image for the given animation phase and state."""
        state_params = {
            "idle": {"pulse_rate": 1.0, "bright": 1.0, "wave_speed": 1.0, "wave_amp": 1.0},
            "awake": {"pulse_rate": 2.0, "bright": 1.35, "wave_speed": 1.8, "wave_amp": 1.3},
            "listening": {"pulse_rate": 1.5, "bright": 1.2, "wave_speed": 1.5, "wave_amp": 1.2},
            "thinking": {"pulse_rate": 2.5, "bright": 1.3, "wave_speed": 2.5, "wave_amp": 1.4},
            "speaking": {"pulse_rate": 3.0, "bright": 1.45, "wave_speed": 2.0, "wave_amp": 1.5},
            "error": {"pulse_rate": 1.0, "bright": 0.85, "wave_speed": 0.8, "wave_amp": 0.8},
        }.get(state, {"pulse_rate": 1.0, "bright": 1.0, "wave_speed": 1.0, "wave_amp": 1.0})

        w_speed = state_params["wave_speed"]
        w_amp = state_params["wave_amp"] * energy

        # Traveling harmonic wave along the arc
        w1 = 0.014 * w_amp * np.sin(phase * 2 * math.pi * w_speed + self.nx * 2.8)
        w2 = 0.007 * w_amp * np.cos(phase * 4 * math.pi * w_speed - self.nx * 4.2)
        w3 = 0.004 * np.sin(phase * 2 * math.pi * 0.5)

        arc_center_y = 0.015 - 0.062 * (1.0 - self.nx * self.nx) + w1 + w2 + w3
        dy = self.ny - arc_center_y

        pulse = 1.0 + 0.08 * math.sin(phase * 2 * math.pi * state_params["pulse_rate"])
        brightness = state_params["bright"] * pulse * energy

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

        # Composite all light components
        rgb = (
            self.pill_rgb * self.pill_alpha[:, :, None]
            + self.lens_rgb * (self.lens_mask[:, :, None] * 0.6)
            + self.lens_glint[:, :, None] * 0.8
            + self.glass_rgb * self.glass_alpha[:, :, None]
            + spectral_total
            + bounce_layer
            + self.rim_rgb * (self.rim_dist[:, :, None] * 0.90)
            + self.edge_rgb * self.edge_dist[:, :, None]
            + self.top_sheen_dist[:, :, None]
        )

        spectral_alpha = np.clip(np.max(spectral_total, axis=2) * 0.95, 0.0, 1.0)
        alpha = np.clip(
            self.pill_alpha
            + self.lens_mask * 0.6
            + self.lens_glint * 0.8
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


def prepare_gif_frame(frame):
    """Converts RGBA frame to high quality GIF frame."""
    alpha = frame.getchannel("A")
    rgb = Image.new("RGB", frame.size, (0, 0, 0))
    rgb.paste(frame.convert("RGB"), mask=alpha)

    paletted = rgb.quantize(
        colors=255,
        method=Image.Quantize.FASTOCTREE,
        dither=Image.Dither.FLOYDSTEINBERG,
    )
    palette = (paletted.getpalette() or [])[: 255 * 3]
    palette.extend([0] * (768 - len(palette)))
    paletted.putpalette(palette)

    transparent_pixels = alpha.point(lambda v: 255 if v < 12 else 0)
    paletted.paste(255, mask=transparent_pixels)
    paletted.info["transparency"] = 255
    paletted.info["disposal"] = 2
    return paletted


def generate_all():
    print("Generating optical glass pebble animations and assets...")
    renderer = OpticalGlassPebbleRenderer(WIDTH, HEIGHT)

    # Save static reference assets
    static_frame = renderer.render_frame(0.0, "idle")
    OUTPUT_MATERIAL.parent.mkdir(parents=True, exist_ok=True)
    static_frame.save(OUTPUT_MATERIAL)
    static_frame.save(OUTPUT_ORB_PNG)
    print(f"Saved {OUTPUT_MATERIAL} and {OUTPUT_ORB_PNG}")

    # Generate 60 FPS animation sequence (120 frames = 2 seconds smooth cycle)
    cycle_frames = 120
    frames = []
    for i in range(cycle_frames):
        phase = i / cycle_frames
        frames.append(renderer.render_frame(phase, "idle"))

    # Save WebP animation
    frames[0].save(
        OUTPUT_WEBP,
        save_all=True,
        append_images=frames[1:],
        duration=round(1000 / 60),
        loop=0,
        lossless=True,
        method=4,
    )
    print(f"Saved {OUTPUT_WEBP} ({len(frames)} frames @ 60 FPS)")

    # Save GIF animation
    gif_frames = [prepare_gif_frame(f) for f in frames]
    gif_frames[0].save(
        OUTPUT_GIF,
        save_all=True,
        append_images=gif_frames[1:],
        duration=round(1000 / 60),
        loop=0,
        disposal=2,
        transparency=255,
        optimize=False,
    )
    print(f"Saved {OUTPUT_GIF}")


if __name__ == "__main__":
    generate_all()
