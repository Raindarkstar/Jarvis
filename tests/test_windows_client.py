import unittest
from pathlib import Path
from unittest import mock

import doctor
import jarvis_cli
import windows_client


class WindowsClientCompatibilityTests(unittest.TestCase):
    def test_client_exposes_entrypoint_without_creating_a_window(self):
        self.assertTrue(callable(windows_client.main))
        self.assertTrue(callable(windows_client.WindowsClient))

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

    def test_doctor_requires_the_actual_webview2_runtime(self):
        with mock.patch.object(doctor.platform, "system", return_value="Windows"), mock.patch.object(
            doctor.importlib, "import_module"
        ), mock.patch.object(doctor, "_webview2_runtime_version", return_value=""):
            missing = doctor._check_gui()[0]
        self.assertEqual(missing.status, "error")
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


if __name__ == "__main__":
    unittest.main()
