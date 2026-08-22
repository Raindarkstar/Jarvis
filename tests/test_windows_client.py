import unittest
from pathlib import Path
from unittest import mock

import numpy as np

import doctor
import jarvis_cli
import rain_ai
import windows_client


class WindowsClientCompatibilityTests(unittest.TestCase):
    def test_client_exposes_entrypoint_without_creating_a_window(self):
        self.assertTrue(callable(windows_client.main))
        self.assertTrue(callable(windows_client.WindowsClient))

    def test_webview_api_does_not_expose_native_window_tree(self):
        client = windows_client.WindowsClient(Path.cwd())
        api = windows_client.WindowsBridgeApi(client)
        self.assertFalse(hasattr(api, "client"))
        self.assertTrue(callable(api.handle_action))

    def test_windows_client_reuses_ubuntu_desktop_document(self):
        client = windows_client.WindowsClient(Path.cwd())
        self.assertEqual(client.html_path, Path("client_ui.html").resolve())
        html = client.html_path.read_text(encoding="utf-8")
        self.assertIn("window.pywebview.api.handle_action", html)
        self.assertIn("pywebview-drag-region", html)
        self.assertIn("window.webkit.messageHandlers.clientAction", html)

    def test_window_uses_frameless_webview2_shell(self):
        class Event:
            def __iadd__(self, _handler):
                return self

        class FakeWindow:
            def __init__(self):
                self.events = type("Events", (), {
                    "loaded": Event(),
                    "maximized": Event(),
                    "restored": Event(),
                })()

        fake_window = FakeWindow()
        fake_webview = mock.Mock()
        fake_webview.settings = {"DRAG_REGION_DIRECT_TARGET_ONLY": True}
        fake_webview.create_window.return_value = fake_window

        with mock.patch.object(windows_client, "webview", fake_webview):
            client = windows_client.WindowsClient(Path.cwd())
            self.assertIs(client.create_window(), fake_window)

        kwargs = fake_webview.create_window.call_args.kwargs
        self.assertTrue(kwargs["frameless"])
        self.assertTrue(kwargs["shadow"])
        self.assertEqual(kwargs["width"], 840)
        self.assertEqual(kwargs["height"], 580)
        self.assertIn("bridge=pywebview", kwargs["url"])
        self.assertNotIn("file://", kwargs["url"])

    def test_doctor_treats_the_desktop_runtime_as_optional(self):
        with mock.patch.object(doctor.platform, "system", return_value="Windows"), mock.patch.object(
            doctor.importlib, "import_module"
        ), mock.patch.object(doctor, "_webview2_runtime_version", return_value=""):
            missing = doctor._check_gui()[0]
        self.assertEqual(missing.status, "warn")
        self.assertTrue(missing.optional)
        self.assertIn("未检测到", missing.detail)

        with mock.patch.object(doctor.platform, "system", return_value="Windows"), mock.patch.object(
            doctor.importlib, "import_module"
        ), mock.patch.object(doctor, "_webview2_runtime_version", return_value="135.0.3179.98"):
            available = doctor._check_gui()[0]
        self.assertEqual(available.status, "ok")
        self.assertIn("135.0.3179.98", available.detail)

    def test_tool_request_detection_covers_windows_workflows(self):
        self.assertTrue(windows_client.WindowsClient._looks_like_tool_request("打开计算器"))
        self.assertTrue(windows_client.WindowsClient._looks_like_tool_request("记住我喜欢咖啡"))
        self.assertFalse(windows_client.WindowsClient._looks_like_tool_request("你好，请介绍一下自己"))

    def test_desktop_entrypoint_selects_platform_specific_module(self):
        expected = "windows_client" if __import__("os").name == "nt" else "client_app"
        self.assertEqual(jarvis_cli._desktop_module(), expected)

    def test_default_cli_entrypoint_starts_the_voice_service(self):
        with mock.patch.object(jarvis_cli, "voice_main", return_value=0) as voice:
            self.assertEqual(jarvis_cli.main([]), 0)
        voice.assert_called_once_with()

    def test_voice_entrypoint_is_not_disabled_on_windows(self):
        with mock.patch.object(jarvis_cli, "_run_module", return_value=0) as runner:
            self.assertEqual(jarvis_cli.voice_main(), 0)
        runner.assert_called_once_with("rain_ai")

    def test_voice_service_falls_back_when_aec_is_unavailable(self):
        pipewire = mock.Mock()
        pipewire.start.return_value = False
        pipewire.active = False
        with mock.patch.object(rain_ai, "PipeWireWebRTCAEC", return_value=pipewire), mock.patch.object(
            rain_ai, "AcousticEchoCanceller", side_effect=OSError("no native AEC")
        ), mock.patch.object(rain_ai, "AsyncAudioPlayer"), mock.patch.object(
            rain_ai, "RealtimeCallback"
        ):
            assistant = rain_ai.QwenRealtimeAssistant(lambda _state: None)
        self.assertTrue(assistant.half_duplex_echo_guard)

    def test_windows_native_audio_rates_are_resampled_for_the_model(self):
        samples_48k = np.zeros(3840, dtype=np.int16)
        samples_44k = np.zeros(3528, dtype=np.int16)
        self.assertEqual(len(rain_ai._resample_pcm16(samples_48k, 48000, 16000)), 1280)
        self.assertEqual(len(rain_ai._resample_pcm16(samples_44k, 44100, 16000)), 1280)


if __name__ == "__main__":
    unittest.main()


class WindowsOverlayTests(unittest.TestCase):
    def test_optical_renderer_produces_valid_rgb_frames(self):
        import windows_overlay
        renderer = windows_overlay.OpticalGlassPebbleRenderer(width=108, height=80)
        params = windows_overlay.STATE_PARAMS["idle"]
        frame = renderer.render_pil_frame(0.0, params)
        self.assertEqual(frame.size, (108, 80))
        self.assertEqual(frame.mode, "RGB")

    def test_windows_overlay_module_exports_entrypoints(self):
        import windows_overlay
        self.assertTrue(callable(windows_overlay.main))
        self.assertTrue(callable(windows_overlay.WindowsOrb))
        self.assertEqual(windows_overlay.ORB_WIDTH, 138)
        self.assertEqual(windows_overlay.ORB_HEIGHT, 100)
        self.assertEqual(windows_overlay.TOP_OFFSET, 12)
