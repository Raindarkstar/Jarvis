import json
import os
import sqlite3
import tempfile
import threading
import time
import unittest
from unittest import mock
import urllib.error
from pathlib import Path

from history_manager import HistoryManager
from memory.memory_manager import MemoryManager
import system_tools


class MemoryConcurrencyTests(unittest.TestCase):
    def test_two_managers_do_not_clobber_each_other(self):
        with tempfile.TemporaryDirectory() as directory:
            path = os.path.join(directory, "memory.json")
            voice_manager = MemoryManager(path)
            text_manager = MemoryManager(path)

            voice_manager.add_memory("voice memory")
            text_manager.add_memory("text memory")

            with open(path, "r", encoding="utf-8") as memory_file:
                contents = [item["content"] for item in json.load(memory_file)["memories"]]
            self.assertEqual(contents, ["voice memory", "text memory"])
            self.assertEqual(os.stat(path).st_mode & 0o777, 0o600)


class HistoryIntegrityTests(unittest.TestCase):
    def test_deleted_session_rejects_late_message(self):
        with tempfile.TemporaryDirectory() as directory:
            manager = HistoryManager(os.path.join(directory, "history.db"))
            session = manager.create_session()
            manager.delete_session(session["id"])

            with self.assertRaises(sqlite3.IntegrityError):
                manager.add_message(session["id"], "assistant", "late reply")

            with manager._get_connection() as connection:
                enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
            self.assertEqual(enabled, 1)


class ToolSafetyTests(unittest.TestCase):
    def test_shell_and_delete_are_disabled_by_default(self):
        with mock.patch.object(system_tools, "ALLOW_SHELL_COMMANDS", False), mock.patch.object(
            system_tools, "ALLOW_DESTRUCTIVE_ACTIONS", False
        ):
            self.assertIn("默认已禁用", system_tools.execute_shell_command("false"))
            self.assertIn(
                "默认已禁用",
                system_tools.manage_files("delete", "/tmp/rain-test-does-not-exist"),
            )

    def test_dynamic_system_values_reject_shell_syntax(self):
        self.assertIn("无效", system_tools.control_system("volume_up", "5; touch /tmp/bad"))
        self.assertIn("无效", system_tools.control_system("brightness_set", "50; id"))

    def test_enabled_shell_reports_nonzero_exit_as_failure(self):
        with mock.patch.object(system_tools, "ALLOW_SHELL_COMMANDS", True):
            result = system_tools.execute_shell_command("false")
        self.assertIn("命令执行失败", result)
        self.assertIn("退出码: 1", result)


class ToolProtocolTests(unittest.TestCase):
    def test_all_twelve_tools_use_qwen_nested_function_schema(self):
        tools = system_tools.TOOLS_DEFINITION
        self.assertEqual(len(tools), 12)
        self.assertEqual(len({item["function"]["name"] for item in tools}), 12)
        for item in tools:
            self.assertEqual(set(item), {"type", "function"})
            self.assertEqual(item["type"], "function")
            self.assertIn("name", item["function"])
            self.assertIn("description", item["function"])
            self.assertIn("parameters", item["function"])

        schemas = {item["function"]["name"]: item["function"]["parameters"] for item in tools}
        self.assertIn("timeout", schemas["execute_shell_command"]["properties"])
        self.assertEqual(schemas["open_application"]["required"], [])
        self.assertEqual(schemas["get_system_status"]["required"], [])

    def test_dispatch_rejects_invalid_json_and_missing_required_arguments(self):
        self.assertIn("参数解析失败", system_tools.dispatch_tool_call("web_search", "{"))
        self.assertIn("参数缺失", system_tools.dispatch_tool_call("web_search", {}))

    def test_dispatch_covers_every_registered_tool(self):
        cases = {
            "execute_shell_command": {"command": "true"},
            "open_application": {"app_name": "browser"},
            "web_search": {"query": "rain"},
            "manage_memory": {"action": "recall", "content": "rain"},
            "set_home_location": {"city": "上海"},
            "get_location": {},
            "get_weather": {},
            "browser_agent": {"action": "open_url", "target": "baidu.com"},
            "control_system": {"action": "mute"},
            "gui_control": {"action": "hotkey", "key": "ctrl+c"},
            "get_system_status": {},
            "manage_files": {"action": "read", "path": "/tmp/x"},
        }
        replacements = {name: mock.DEFAULT for name in cases}
        with mock.patch.multiple(system_tools, **replacements) as patched:
            for name, stub in patched.items():
                stub.return_value = f"called:{name}"
            for name, arguments in cases.items():
                self.assertEqual(
                    system_tools.dispatch_tool_call(name, arguments),
                    f"called:{name}",
                )


class WebsiteToolTests(unittest.TestCase):
    def test_url_normalization_supports_aliases_and_bare_domains(self):
        self.assertEqual(system_tools.normalize_web_url("百度"), "https://www.baidu.com")
        self.assertEqual(system_tools.normalize_web_url("baidu.com"), "https://baidu.com")
        self.assertEqual(system_tools.normalize_web_url("www.zhihu.com"), "https://www.zhihu.com")
        self.assertEqual(system_tools.normalize_web_url("不是网址"), "")

    def test_open_application_opens_bare_domain_as_url(self):
        with mock.patch.object(system_tools, "_open_desktop_target", return_value=(True, "")) as opener:
            result = system_tools.open_application("browser", "baidu.com")
        opener.assert_called_once_with("https://baidu.com")
        self.assertIn("已在浏览器中打开网址", result)

    def test_chinese_website_alias_opens_in_browser(self):
        with mock.patch.object(system_tools, "_open_desktop_target", return_value=(True, "")) as opener:
            result = system_tools.open_application("百度")
        opener.assert_called_once_with("https://www.baidu.com")
        self.assertIn("已在浏览器中打开网址", result)

    def test_browser_agent_rejects_empty_target_and_reports_launcher_failure(self):
        self.assertIn("无效", system_tools.browser_agent("open_url", ""))
        with mock.patch.object(system_tools, "_open_desktop_target", return_value=(False, "no display")):
            result = system_tools.browser_agent("open_url", "百度")
        self.assertIn("失败", result)
        self.assertIn("no display", result)


class ToolBehaviorTests(unittest.TestCase):
    def test_gui_typing_removes_leading_dictation_period(self):
        self.assertEqual(system_tools._remove_leading_dictation_period("。你好"), "你好")
        self.assertEqual(system_tools._remove_leading_dictation_period("。"), "。")
        self.assertEqual(system_tools._remove_leading_dictation_period("你好。"), "你好。")
        with mock.patch.object(system_tools._simulator, "type_text", return_value=True) as typer:
            result = system_tools.gui_control("type", text="。需要输入的正文")
        typer.assert_called_once_with("需要输入的正文")
        self.assertNotIn("。需要", result)

    def test_system_and_gui_tools_do_not_claim_false_success(self):
        with mock.patch.object(system_tools, "_run_first_available", return_value=(False, "missing")):
            self.assertIn("失败", system_tools.control_system("mute"))
        with mock.patch.object(system_tools._simulator, "type_text", return_value=False):
            self.assertIn("失败", system_tools.gui_control("type", text="hello"))
        with mock.patch.object(system_tools, "_run_checked", return_value=(False, "no display")):
            self.assertIn("失败", system_tools.gui_control("click"))

    def test_web_search_honors_selected_engine(self):
        requested = []

        def fail_request(request, timeout):
            requested.append(request.full_url)
            raise urllib.error.URLError("offline")

        with mock.patch.object(system_tools.urllib.request, "urlopen", side_effect=fail_request):
            result = system_tools.web_search("测试", engine="bing")
        self.assertTrue(requested)
        self.assertTrue(all("bing.com" in url for url in requested))
        self.assertIn("搜索失败", result)

    def test_location_query_type_changes_output(self):
        payload = json.dumps({
            "status": "success", "country": "中国", "regionName": "上海", "city": "上海",
            "lat": 31.2, "lon": 121.5, "isp": "ISP", "query": "1.2.3.4",
            "timezone": "Asia/Shanghai",
        }).encode()

        class FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return payload

        with mock.patch.object(system_tools, "get_user_config", return_value={}), mock.patch.object(
            system_tools.urllib.request, "urlopen", return_value=FakeResponse()
        ):
            self.assertIn("1.2.3.4", system_tools.get_location("ip"))
            self.assertNotIn("31.2", system_tools.get_location("ip"))
            self.assertIn("31.2", system_tools.get_location("coordinates"))
            self.assertIn("上海", system_tools.get_location("city"))


class TextClientTests(unittest.TestCase):
    def test_weather_city_is_extracted(self):
        from client_app import ClientWindow

        self.assertEqual(ClientWindow._extract_weather_city("北京天气怎么样"), "北京")
        self.assertEqual(ClientWindow._extract_weather_city("上海明天天气"), "上海")
        self.assertEqual(ClientWindow._extract_weather_city("明天北京会下雨吗"), "北京")
        self.assertEqual(ClientWindow._extract_weather_city("深圳现在多少度"), "深圳")
        self.assertEqual(ClientWindow._extract_weather_city("今天天气"), "")

    def test_open_requests_are_routed_to_real_tools(self):
        from client_app import ClientWindow

        self.assertEqual(
            ClientWindow._extract_open_request("打开百度"),
            ("browser_agent", {"action": "open_url", "target": "百度"}),
        )
        self.assertEqual(
            ClientWindow._extract_open_request("打开 baidu.com"),
            ("browser_agent", {"action": "open_url", "target": "baidu.com"}),
        )
        self.assertEqual(
            ClientWindow._extract_open_request("打开网站"),
            ("open_application", {"app_name": "浏览器"}),
        )
        self.assertIsNone(ClientWindow._extract_open_request("介绍一下百度"))

    def test_home_location_is_not_misrouted_to_generic_memory(self):
        from client_app import ClientWindow

        self.assertEqual(ClientWindow._extract_home_location("我住在上海"), "上海")
        self.assertEqual(ClientWindow._extract_home_location("记住我在深圳"), "深圳")


class WindowChromeTests(unittest.TestCase):
    def test_window_controls_cover_minimize_maximize_restore_and_close(self):
        html = Path("client_ui.html").read_text(encoding="utf-8")
        self.assertIn("minimizeClientWindow()", html)
        self.assertIn("toggleMaximizeWindow()", html)
        self.assertIn("setWindowExpanded", html)
        self.assertIn("assets/jarvis-logo.png", html)
        self.assertIn(">Jarvis<", html)
        self.assertIn("closeClientWindow()", html)
        self.assertNotIn(">Rain<", html)

        desktop = Path("jarvis.desktop").read_text(encoding="utf-8")
        self.assertIn("Name=Jarvis", desktop)
        self.assertIn("Icon=jarvis", desktop)
        self.assertIn("StartupWMClass=Jarvis", desktop)

    def test_logo_is_a_real_transparent_png(self):
        from PIL import Image

        image = Image.open("assets/jarvis-logo.png")
        self.assertEqual(image.mode, "RGBA")
        self.assertEqual(image.getchannel("A").getextrema(), (0, 255))

    def test_native_maximize_toggle_uses_current_window_state(self):
        from client_app import ClientWindow

        class FakeWindow:
            def __init__(self, maximized=False, fullscreen=False):
                self._is_maximized = maximized
                self._is_fullscreen = fullscreen
                self.calls = []

            def maximize(self):
                self.calls.append("maximize")

            def unmaximize(self):
                self.calls.append("unmaximize")

            def unfullscreen(self):
                self.calls.append("unfullscreen")

        normal = FakeWindow()
        ClientWindow.toggle_maximize(normal)
        maximized = FakeWindow(maximized=True)
        ClientWindow.toggle_maximize(maximized)
        fullscreen = FakeWindow(fullscreen=True)
        ClientWindow.toggle_maximize(fullscreen)
        self.assertEqual(normal.calls, ["maximize"])
        self.assertEqual(maximized.calls, ["unmaximize"])
        self.assertEqual(fullscreen.calls, ["unfullscreen"])

    def test_memory_panel_renders_saved_memory_details_and_count(self):
        html = Path("client_ui.html").read_text(encoding="utf-8")
        self.assertIn('id="memory-count"', html)
        self.assertIn("已记忆的内容", html)
        self.assertIn("m.created_at", html)
        self.assertIn("categoryLabels", html)

        import client_app

        scripts = []
        fake_window = type("FakeWindow", (), {"run_js": scripts.append})()
        saved = [{
            "id": "mem_1",
            "category": "profile",
            "content": "用户是一名全栈 AI 开发者",
            "created_at": "2026-08-21 21:31:02",
        }]
        with mock.patch.object(client_app.memory_manager, "list_memories", return_value=saved):
            client_app.ClientWindow._sync_memories_to_ui(fake_window)

        self.assertEqual(len(scripts), 1)
        self.assertIn("window.renderMemories", scripts[0])
        self.assertIn("用户是一名全栈 AI 开发者", scripts[0])


class RealtimeStateTests(unittest.TestCase):
    def test_each_voice_response_is_persisted_separately(self):
        import rain_ai

        class FakePlayer:
            def interrupt(self):
                pass

            def play(self, _data):
                pass

        with tempfile.TemporaryDirectory() as directory:
            manager = HistoryManager(os.path.join(directory, "voice.db"))
            with mock.patch.object(rain_ai, "history_manager", manager):
                callback = rain_ai.RealtimeCallback(FakePlayer(), lambda _state: None)
                callback.prepare_turn()
                for question, answer in (("问题一", "答案一"), ("问题二", "答案二")):
                    callback.on_event(
                        {
                            "type": "conversation.item.input_audio_transcription.completed",
                            "transcript": question,
                        }
                    )
                    callback.on_event({"type": "response.created"})
                    callback.on_event(
                        {"type": "response.audio_transcript.delta", "delta": answer}
                    )
                    callback.on_event({"type": "response.done"})

                messages = manager.get_session_messages(manager.get_active_session_id())
            self.assertEqual(
                [(item["role"], item["content"]) for item in messages],
                [
                    ("user", "问题一"),
                    ("assistant", "答案一"),
                    ("user", "问题二"),
                    ("assistant", "答案二"),
                ],
            )

    def test_function_event_submits_output_then_creates_followup_response(self):
        import rain_ai

        class FakePlayer:
            def interrupt(self):
                pass

        class FakeConversation:
            def __init__(self):
                self.items = []
                self.followup_created = threading.Event()

            def create_item(self, item):
                self.items.append(item)

            def create_response(self):
                self.followup_created.set()

        callback = rain_ai.RealtimeCallback(FakePlayer(), lambda _state: None)
        conversation = FakeConversation()
        callback.conversation = conversation
        with mock.patch.object(rain_ai, "dispatch_tool_call", return_value="opened") as dispatcher:
            callback.on_event({
                "type": "response.function_call_arguments.done",
                "call_id": "call-1",
                "name": "browser_agent",
                "arguments": '{"action":"open_url","target":"百度"}',
            })
            callback.on_event({"type": "response.done"})
            self.assertTrue(conversation.followup_created.wait(2))

        dispatcher.assert_called_once()
        self.assertEqual(conversation.items, [{
            "type": "function_call_output",
            "call_id": "call-1",
            "output": "opened",
        }])


if __name__ == "__main__":
    unittest.main()
