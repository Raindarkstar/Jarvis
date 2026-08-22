#!/usr/bin/env python3
"""
client_app.py - Jarvis 桌面客户端主程序
基于 GTK3 + WebKit2 4.1 构建，具备会话历史管理、多模态图文输入、流式 Markdown 渲染、系统工具卡片与记忆库可视化管理。
"""

import os
import sys
import json
import re
import threading
import time

from dotenv import load_dotenv


load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

DIST_PACKAGES = "/usr/lib/python3/dist-packages"
if DIST_PACKAGES not in sys.path:
    sys.path.append(DIST_PACKAGES)

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("Gdk", "3.0")
gi.require_version("WebKit2", "4.1")

from gi.repository import Gtk, Gdk, WebKit2, GLib

from history_manager import history_manager
from memory import memory_manager
from system_tools import TOOLS_DEFINITION, dispatch_tool_call, get_user_config, set_home_location
import dashscope
from dashscope import Generation

dashscope.api_key = dashscope.api_key or os.getenv("DASHSCOPE_API_KEY")

HTML_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "client_ui.html"))
LOGO_PATH = os.path.abspath(os.path.join(os.path.dirname(__file__), "assets", "jarvis-logo.png"))

# 让 GNOME/KDE 能通过 StartupWMClass=Jarvis 匹配桌面启动器与任务栏图标。
GLib.set_prgname("jarvis")
GLib.set_application_name("Jarvis")
Gdk.set_program_class("Jarvis")
Gtk.Window.set_default_icon_name("jarvis")


class ClientWindow(Gtk.Window):
    """Jarvis 桌面客户端窗口"""

    WINDOW_WIDTH = 840
    WINDOW_HEIGHT = 580

    def __init__(self, overlay_controller=None):
        super().__init__(type=Gtk.WindowType.TOPLEVEL)

        self.overlay = overlay_controller
        self.set_title("Jarvis")
        self.set_default_size(self.WINDOW_WIDTH, self.WINDOW_HEIGHT)
        self.set_size_request(680, 460)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_decorated(False)
        self.set_role("JarvisDesktopClient")
        if os.path.exists(LOGO_PATH):
            self.set_icon_from_file(LOGO_PATH)

        self._is_maximized = False
        self._is_fullscreen = False
        self._is_iconified = False
        self.connect("window-state-event", self._on_window_state)

        screen = self.get_screen()
        visual = screen.get_rgba_visual()
        if visual and screen.is_composited():
            self.set_visual(visual)

        self.set_app_paintable(True)
        self.connect("draw", self._on_draw)

        # 键盘快捷键监听 (Escape 收起窗口)
        self.add_events(Gdk.EventMask.KEY_PRESS_MASK)
        self.connect("key-press-event", self._on_key_press)

        # WebKit2 视图
        self.webview = WebKit2.WebView()
        settings = self.webview.get_settings()
        settings.set_enable_javascript(True)
        settings.set_enable_developer_extras(False)
        self.webview.set_background_color(Gdk.RGBA(0, 0, 0, 0))

        # 注册 JavaScript 消息桥接
        user_content = self.webview.get_user_content_manager()
        user_content.register_script_message_handler("clientAction")
        user_content.connect("script-message-received::clientAction", self._on_client_action)

        self.add(self.webview)
        self.webview.load_uri(f"file://{HTML_PATH}")

        self.is_visible_ui = False
        self.active_session_id = history_manager.get_active_session_id()
        self.voice_session_id = None
        self._text_generation_lock = threading.Lock()

    def _on_key_press(self, widget, event):
        if event.keyval == Gdk.KEY_F11:
            self.toggle_fullscreen()
            return True
        if event.keyval == Gdk.KEY_Escape:
            if self._is_fullscreen:
                self.unfullscreen()
                return True
            self.hide_window()
            return True
        return False

    def _on_window_state(self, _widget, event):
        state = event.new_window_state
        self._is_maximized = bool(state & Gdk.WindowState.MAXIMIZED)
        self._is_fullscreen = bool(state & Gdk.WindowState.FULLSCREEN)
        self._is_iconified = bool(state & Gdk.WindowState.ICONIFIED)
        self._sync_window_state_to_ui()
        return False

    def _sync_window_state_to_ui(self):
        expanded = self._is_maximized or self._is_fullscreen
        self.run_js(
            f"window.setWindowExpanded({'true' if expanded else 'false'}, "
            f"{'true' if self._is_fullscreen else 'false'});"
        )

    def _on_draw(self, widget, cr):
        cr.set_source_rgba(0, 0, 0, 0)
        cr.set_operator(0)  # CLEAR
        cr.paint()
        return False

    def show_window(self):
        self.is_visible_ui = True
        self.show_all()
        if self._is_iconified:
            self.deiconify()
        self.present()

    def hide_window(self):
        self.is_visible_ui = False
        self.hide()

    def toggle(self):
        if self._is_iconified:
            self.show_window()
        elif self.is_visible_ui and self.get_visible():
            self.hide_window()
        else:
            self.show_window()

    def minimize_window(self):
        self.iconify()

    def toggle_maximize(self):
        if self._is_fullscreen:
            self.unfullscreen()
        elif self._is_maximized:
            self.unmaximize()
        else:
            self.maximize()

    def toggle_fullscreen(self):
        if self._is_fullscreen:
            self.unfullscreen()
        else:
            self.fullscreen()

    def run_js(self, script: str):
        GLib.idle_add(self._eval_js, script)

    def _eval_js(self, script: str):
        try:
            self.webview.evaluate_javascript(script, -1, None, None, None, None, None)
        except Exception:
            pass
        return False

    def _on_client_action(self, user_content, message):
        try:
            data = json.loads(message.get_js_value().to_string())
            action = data.get("action")

            if action == "ready":
                self._sync_sessions_to_ui()
                self._load_session_messages(self.active_session_id)
                self._sync_memories_to_ui()
                self._sync_window_state_to_ui()

            elif action == "create_session":
                session = history_manager.create_session()
                self.active_session_id = session["id"]
                self._sync_sessions_to_ui()
                self._load_session_messages(self.active_session_id)

            elif action == "switch_session":
                session_id = data.get("session_id")
                if session_id:
                    self.active_session_id = session_id
                    history_manager.set_active_session(session_id)
                    self._sync_sessions_to_ui()
                    self._load_session_messages(session_id)

            elif action == "delete_session":
                session_id = data.get("session_id")
                if session_id:
                    history_manager.delete_session(session_id)
                    self.active_session_id = history_manager.get_active_session_id()
                    self._sync_sessions_to_ui()
                    self._load_session_messages(self.active_session_id)

            elif action == "clear_session":
                session_id = data.get("session_id", self.active_session_id)
                history_manager.delete_session(session_id)
                session = history_manager.create_session()
                self.active_session_id = session["id"]
                self._sync_sessions_to_ui()
                self._load_session_messages(self.active_session_id)

            elif action == "send_text":
                text = data.get("text", "").strip()
                session_id = data.get("session_id", self.active_session_id)
                request_id = data.get("request_id", "")
                if text:
                    self._handle_text_query(session_id, text, request_id)

            elif action == "get_memories":
                self._sync_memories_to_ui()

            elif action == "delete_memory":
                mem_id = data.get("id")
                if mem_id:
                    memory_manager.forget(mem_id)
                    self._sync_memories_to_ui()

            elif action == "get_settings":
                config = get_user_config()
                safe_json = json.dumps(config)
                self.run_js(f"window.renderSettings({safe_json});")

            elif action == "save_settings":
                city = data.get("home_location", "").strip()
                if city:
                    set_home_location(city)

            elif action == "start_drag":
                display = Gdk.Display.get_default()
                seat = display.get_default_seat()
                device = seat.get_pointer()
                _, x, y = device.get_position()
                self.begin_move_drag(1, x, y, Gtk.get_current_event_time())

            elif action == "minimize_window":
                self.minimize_window()

            elif action == "toggle_maximize":
                self.toggle_maximize()

            elif action == "toggle_fullscreen":
                self.toggle_fullscreen()

            elif action == "handle_escape":
                if self._is_fullscreen:
                    self.unfullscreen()
                else:
                    self.hide_window()

            elif action == "close_window":
                self.hide_window()

            elif action == "hide_window":
                self.hide_window()

        except Exception as e:
            print("Error handling client action:", e)

    def _sync_sessions_to_ui(self):
        sessions = history_manager.list_sessions()
        safe_sessions = json.dumps(sessions)
        safe_current = json.dumps(self.active_session_id)
        self.run_js(f"window.renderSessionsList({safe_sessions}, {safe_current});")

    def _sync_memories_to_ui(self):
        """Push the latest persisted memories to every visible memory surface."""
        memories = memory_manager.list_memories()
        safe_memories = json.dumps(memories, ensure_ascii=False)
        self.run_js(f"window.renderMemories({safe_memories});")

    def _load_session_messages(self, session_id):
        messages = history_manager.get_session_messages(session_id)
        safe_messages = json.dumps(messages)
        self.run_js(f"window.renderMessages({safe_messages});")

    @staticmethod
    def _extract_weather_city(query: str) -> str:
        match = re.search(
            r"([\u4e00-\u9fff]{2,12}?)(?:今天|明天|后天|现在|当前)?(?:的)?"
            r"(?:天气|气温|温度|多少度|(?:会不会|会|是否)?下雨)",
            query,
        )
        if not match:
            return ""
        city = match.group(1)
        for prefix in ("请帮我查一下", "帮我查一下", "帮我查", "查一下", "查询", "看看", "请问"):
            if city.startswith(prefix):
                city = city[len(prefix):]
        for temporal_prefix in ("今天", "明天", "后天", "现在", "当前"):
            if city.startswith(temporal_prefix):
                city = city[len(temporal_prefix):]
        if city in {"今天", "明天", "后天", "现在", "当前", "当地", "这里"}:
            return ""
        return city.strip()

    @staticmethod
    def _extract_open_request(query: str):
        """解析“打开/访问”指令，返回工具名和参数；普通聊天返回 None。"""
        url_match = re.search(
            r"https?://[^\s，。！？]+|www\.[^\s，。！？]+|"
            r"(?:[a-zA-Z0-9-]+\.)+[a-zA-Z]{2,}(?:/[^\s，。！？]*)?",
            query,
        )
        verb_match = re.search(r"(?:请|帮我|给我)?(?:打开|访问|进入|启动)(?:一下)?\s*(.*)", query)
        if not verb_match:
            return None
        target = (url_match.group(0) if url_match else verb_match.group(1)).strip()
        target = re.sub(r"(?:好吗|可以吗|行吗|吧|。|！|？)+$", "", target).strip()
        target = re.sub(r"^(?:这个|一个)", "", target).strip()
        target = re.sub(r"(?:网站|网页|官网)$", "", target).strip()

        website_names = {
            "百度", "baidu", "必应", "bing", "谷歌", "google", "b站", "哔哩哔哩",
            "bilibili", "油管", "youtube", "github", "知乎", "zhihu", "微博", "淘宝", "京东",
        }
        app_names = {
            "浏览器", "browser", "chrome", "终端", "terminal", "文件管理器", "files",
            "explorer", "计算器", "calculator", "设置", "settings", "音乐", "music",
            "编辑器", "code", "vscode", "text_editor",
        }
        if not target or target in {"网站", "网页", "浏览器"}:
            return "open_application", {"app_name": "浏览器"}
        if url_match or target.lower() in website_names or target in website_names:
            return "browser_agent", {"action": "open_url", "target": target}
        if target.lower() in app_names or target in app_names:
            return "open_application", {"app_name": target}
        return "open_application", {"app_name": target}

    @staticmethod
    def _extract_home_location(query: str) -> str:
        match = re.search(
            r"(?:我住在|我常住在|我的城市是|设置(?:默认|常驻)?城市(?:为|是)|记住我在)"
            r"\s*([一-鿿]{2,12}?)(?:市)?(?:[，。！？\s]|$)",
            query,
        )
        return match.group(1).strip() if match else ""

    @staticmethod
    def _looks_like_tool_request(query: str) -> bool:
        keywords = (
            "执行命令", "终端命令", "shell", "bash", "打开", "访问", "启动",
            "音量", "静音", "亮度", "播放", "暂停", "下一曲", "上一曲", "锁屏",
            "关机", "重启", "睡眠", "快捷键", "按下", "点击", "输入文字",
            "cpu", "内存", "磁盘", "系统状态", "活动窗口", "读取文件", "查看文件",
            "列出目录", "创建文件", "写入文件", "删除文件", "天气", "定位", "位置",
            "搜索", "查一下", "记住", "忘掉", "回忆", "常驻城市",
        )
        lowered = query.lower()
        return any(keyword in lowered for keyword in keywords)

    @staticmethod
    def _plain_value(value):
        if isinstance(value, dict):
            return {key: ClientWindow._plain_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [ClientWindow._plain_value(item) for item in value]
        if hasattr(value, "to_dict"):
            return ClientWindow._plain_value(value.to_dict())
        return value

    def _handle_text_query(self, session_id: str, query: str, request_id: str = ""):
        """在后台线程调用 Qwen 模型与工具链执行，流式返回至客户端界面"""
        if not history_manager.get_session(session_id):
            self.run_js("window.setTextBusy(false);")
            return
        if not self._text_generation_lock.acquire(blocking=False):
            self.run_js("window.setTextBusy(false);")
            return

        # 保存用户消息至数据库
        history_manager.add_message(session_id, "user", query)
        self._sync_sessions_to_ui()
        self.run_js("window.setTextBusy(true);")

        safe_session = json.dumps(session_id)
        safe_request = json.dumps(request_id)

        def run_for_active_session(js_call: str):
            if self.active_session_id == session_id:
                self.run_js(js_call)

        def worker():
            self.set_status("thinking", "Qwen 正在思考...")
            try:
                # 先执行明确的本地动作，再把真实结果交给模型组织回复。
                tool_result_text = ""
                tool_dialog = []
                open_request = self._extract_open_request(query)
                home_city = self._extract_home_location(query)
                if open_request:
                    tool_name, tool_args = open_request
                    self.show_tool_badge(tool_name, json.dumps(tool_args, ensure_ascii=False))
                    tool_res = dispatch_tool_call(tool_name, tool_args)
                    tool_result_text += f"\n[系统操作结果]: {tool_res}\n"

                elif home_city:
                    self.show_tool_badge("set_home_location", home_city)
                    tool_res = dispatch_tool_call("set_home_location", {"city": home_city})
                    tool_result_text += f"\n[常驻城市设置结果]: {tool_res}\n"

                elif any(w in query for w in ["天气", "气温", "下雨", "温度", "多少度"]):
                    self.show_tool_badge("get_weather", query)
                    city = self._extract_weather_city(query)
                    tool_res = dispatch_tool_call("get_weather", json.dumps({"city": city}))
                    tool_result_text += f"\n[实时天气数据]: {tool_res}\n"

                elif any(w in query for w in ["定位", "在哪", "位置", "经纬度", "ip"]):
                    self.show_tool_badge("get_location", "")
                    tool_res = dispatch_tool_call("get_location", "{}")
                    tool_result_text += f"\n[地理位置数据]: {tool_res}\n"

                elif any(w in query for w in ["搜索", "查一下", "找一下", "2025", "2026", "最新", "发布", "模型"]):
                    self.show_tool_badge("web_search", query)
                    tool_res = dispatch_tool_call("web_search", json.dumps({"query": query}))
                    tool_result_text += f"\n[网络搜索结果]: {tool_res}\n"

                elif any(w in query for w in ["记住", "偏好", "我的名字"]):
                    self.show_tool_badge("manage_memory", query)
                    tool_res = dispatch_tool_call("manage_memory", json.dumps({"action": "remember", "content": query}))
                    self._sync_memories_to_ui()
                    tool_result_text += f"\n[记忆库更新]: {tool_res}\n"

                # 其余系统、文件、GUI、Shell 等工具由模型按完整工具表选择。
                if not tool_result_text and self._looks_like_tool_request(query):
                    recent_for_tools = history_manager.get_session_messages(session_id)[-6:]
                    selector_messages = [{"role": "system", "content": (
                        "你是桌面助手。用户要求操作电脑或查询实时信息时，必须选择最合适的工具；"
                        "不要假装已经执行。参数必须完整、准确。"
                    )}]
                    selector_messages.extend(
                        {"role": item["role"], "content": item["content"]}
                        for item in recent_for_tools
                    )
                    selection = Generation.call(
                        model="qwen-turbo",
                        messages=selector_messages,
                        tools=TOOLS_DEFINITION,
                        result_format="message",
                    )
                    if selection.status_code != 200:
                        raise RuntimeError(
                            f"工具选择请求失败 ({selection.status_code}): "
                            f"{getattr(selection, 'message', '未知错误')}"
                        )
                    selected_message = selection.output.choices[0].message
                    tool_calls = self._plain_value(
                        getattr(selected_message, "tool_calls", None)
                        or (selected_message.get("tool_calls") if isinstance(selected_message, dict) else None)
                        or []
                    )
                    if tool_calls:
                        assistant_tool_message = {
                            "role": "assistant",
                            "content": getattr(selected_message, "content", "") or "",
                            "tool_calls": tool_calls,
                        }
                        tool_dialog.append(assistant_tool_message)
                        for tool_call in tool_calls[:3]:
                            function = tool_call.get("function", {})
                            tool_name = function.get("name", "")
                            arguments = function.get("arguments", "{}")
                            if not tool_name:
                                continue
                            self.show_tool_badge(tool_name, str(arguments))
                            tool_res = dispatch_tool_call(tool_name, arguments)
                            if tool_name == "manage_memory":
                                self._sync_memories_to_ui()
                            tool_dialog.append({
                                "role": "tool",
                                "name": tool_name,
                                "content": str(tool_res),
                            })
                            tool_result_text += f"\n[{tool_name} 执行结果]: {tool_res}\n"

                # 构造大模型提示词
                memory_ctx = memory_manager.get_system_prompt_context()
                system_prompt = "你是一个极简、高效、专业的个人智能助手。回答语言自然清晰、直奔主题，善用自然分段与加粗（**关键词**），不要输出多余的表格或机械寒暄。"
                if memory_ctx:
                    system_prompt += f"\n{memory_ctx}"
                if tool_result_text:
                    system_prompt += f"\n{tool_result_text}"

                # 获取会话最近上下文
                recent_msgs = history_manager.get_session_messages(session_id)[-6:]
                dialog_history = [{"role": "system", "content": system_prompt}]
                for m in recent_msgs:
                    dialog_history.append({"role": m["role"], "content": m["content"]})
                dialog_history.extend(tool_dialog)

                self.set_status("speaking", "AI 正在回答...")
                full_reply = ""

                # 流式生成调用
                responses = Generation.call(
                    model="qwen-turbo",
                    messages=dialog_history,
                    result_format="message",
                    stream=True,
                    incremental_output=True,
                )

                for r in responses:
                    if r.status_code != 200:
                        raise RuntimeError(
                            f"DashScope 请求失败 ({r.status_code}): "
                            f"{getattr(r, 'message', '未知错误')}"
                        )
                    delta = r.output.choices[0].message.content
                    if delta:
                        full_reply += delta
                        safe_delta = json.dumps(delta)
                        run_for_active_session(
                            f"window.appendAiDelta({safe_delta}, {safe_session}, {safe_request});"
                        )

                # 保存 AI 回复至数据库
                if full_reply and history_manager.get_session(session_id):
                    history_manager.add_message(session_id, "assistant", full_reply)

                run_for_active_session(
                    f"window.finishAiTurn({safe_session}, {safe_request});"
                )
                self.set_status("ready", "待命就绪")
                self._sync_sessions_to_ui()

            except Exception as e:
                err_msg = f"\n❌ 回复生成异常: {e}"
                run_for_active_session(
                    f"window.appendAiDelta({json.dumps(err_msg)}, {safe_session}, {safe_request});"
                )
                run_for_active_session(
                    f"window.finishAiTurn({safe_session}, {safe_request});"
                )
                self.set_status("ready", "待命就绪")
            finally:
                self._text_generation_lock.release()
                self.run_js("window.setTextBusy(false);")

        threading.Thread(target=worker, daemon=True).start()

    def set_status(self, status: str, text: str):
        safe_st = json.dumps(status)
        safe_txt = json.dumps(text)
        self.run_js(f"window.setStatus({safe_st}, {safe_txt});")

    # 外部语音流同步接口
    def on_voice_user_transcript(self, text: str, is_final: bool = False):
        if is_final:
            self.voice_session_id = self.active_session_id
            history_manager.add_message(self.voice_session_id, "user", text)
            self._sync_sessions_to_ui()
            if not self._text_generation_lock.locked():
                self.run_js("window.setTextBusy(true);")
        if self._text_generation_lock.locked():
            return
        safe_text = json.dumps(text)
        safe_final = "true" if is_final else "false"
        self.run_js(f"window.updateUserTranscript({safe_text}, {safe_final});")

    def on_voice_tool_call(self, tool_name: str, query: str = ""):
        safe_name = json.dumps(tool_name)
        safe_query = json.dumps(query)
        self.run_js(f"window.showToolBadge({safe_name}, {safe_query});")

    def on_voice_ai_delta(self, delta: str):
        if self._text_generation_lock.locked() or self.voice_session_id != self.active_session_id:
            return
        safe_delta = json.dumps(delta)
        self.run_js(f"window.appendAiDelta({safe_delta});")

    def on_voice_turn_done(self, full_text: str = ""):
        target_session = self.voice_session_id
        if full_text and target_session and history_manager.get_session(target_session):
            history_manager.add_message(target_session, "assistant", full_text)
            self._sync_sessions_to_ui()
        if target_session == self.active_session_id:
            self.run_js("window.finishAiTurn();")
        if not self._text_generation_lock.locked():
            self.run_js("window.setTextBusy(false);")
        self.voice_session_id = None


class ClientAppController:
    """桌面客户端单例全局控制器"""

    def __init__(self):
        self.window = None

    def bind_window(self, window: ClientWindow):
        self.window = window

    def show(self):
        if self.window:
            GLib.idle_add(self.window.show_window)

    def hide(self):
        if self.window:
            GLib.idle_add(self.window.hide_window)

    def toggle(self):
        if self.window:
            GLib.idle_add(self.window.toggle)

    def on_user_transcript(self, text: str, is_final: bool = False):
        if self.window:
            GLib.idle_add(self.window.on_voice_user_transcript, text, is_final)

    def on_tool_call(self, tool_name: str, query: str = ""):
        if self.window:
            GLib.idle_add(self.window.on_voice_tool_call, tool_name, query)

    def on_ai_delta(self, delta: str):
        if self.window:
            GLib.idle_add(self.window.on_voice_ai_delta, delta)

    def on_turn_done(self, full_text: str = ""):
        if self.window:
            GLib.idle_add(self.window.on_voice_turn_done, full_text)


client_app_controller = ClientAppController()


def run_standalone_client():
    """独立运行桌面客户端应用"""
    win = ClientWindow()
    client_app_controller.bind_window(win)
    win.show_window()
    Gtk.main()


if __name__ == "__main__":
    run_standalone_client()
