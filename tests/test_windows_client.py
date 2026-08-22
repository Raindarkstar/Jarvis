import unittest

import jarvis_cli
import windows_client


class WindowsClientCompatibilityTests(unittest.TestCase):
    def test_client_exposes_entrypoint_without_creating_a_window(self):
        self.assertTrue(callable(windows_client.main))
        self.assertTrue(callable(windows_client.WindowsClient))

    def test_tool_request_detection_covers_windows_workflows(self):
        self.assertTrue(windows_client.WindowsClient._looks_like_tool_request("打开计算器"))
        self.assertTrue(windows_client.WindowsClient._looks_like_tool_request("记住我喜欢咖啡"))
        self.assertFalse(windows_client.WindowsClient._looks_like_tool_request("你好，请介绍一下自己"))

    def test_desktop_entrypoint_selects_platform_specific_module(self):
        expected = "windows_client" if __import__("os").name == "nt" else "client_app"
        self.assertEqual(jarvis_cli._desktop_module(), expected)


if __name__ == "__main__":
    unittest.main()
