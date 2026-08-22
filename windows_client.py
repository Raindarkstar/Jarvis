#!/usr/bin/env python3
"""Windows WebView2 desktop client for Jarvis.

The Windows and Ubuntu clients intentionally share ``client_ui.html``. Ubuntu
hosts it in WebKitGTK, while Windows hosts the same document in WebView2 via
pywebview. This keeps the visual language and interaction model aligned while
allowing each platform to use its native window implementation.
"""

from __future__ import annotations

import json
import os
import re
import threading
import ctypes
import math
from ctypes import wintypes
from pathlib import Path
from typing import Any

import dashscope
from dashscope import Generation
from dotenv import load_dotenv

from history_manager import history_manager
from memory import memory_manager
from system_tools import (
    TOOLS_DEFINITION,
    dispatch_tool_call,
    get_user_config,
    set_home_location,
)

try:
    import webview
except ImportError:  # pragma: no cover - pywebview is Windows-only here.
    webview = None


PROJECT_ROOT = Path(__file__).resolve().parent
load_dotenv()
load_dotenv(PROJECT_ROOT / ".env")
dashscope.api_key = dashscope.api_key or os.getenv("DASHSCOPE_API_KEY")


def _env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _superellipse_points(
    width: int,
    height: int,
    radius: int = 24,
    exponent: float = 4.0,
    segments: int = 12,
) -> list[tuple[int, int]]:
    """Return a polygon for a rounded rectangle with superellipse corners."""

    width = max(2, int(width))
    height = max(2, int(height))
    radius = max(1, min(int(radius), width // 2, height // 2))
    power = 2.0 / max(2.0, float(exponent))
    points: list[tuple[int, int]] = [(radius, 0), (width - radius, 0)]

    def add_arc(values):
        for x, y in values:
            point = (round(x), round(y))
            if point != points[-1]:
                points.append(point)

    # Walk clockwise from the top edge. The four arcs are sampled from the
    # superellipse equation x^n + y^n = r^n rather than using a circle.
    add_arc(
        (
            (
                width - radius + radius * (abs(math.sin(t)) ** power),
                radius - radius * (abs(math.cos(t)) ** power),
            )
            for t in [math.pi * i / (2 * segments) for i in range(segments + 1)]
        )
    )
    points.append((width, height - radius))
    add_arc(
        (
            (
                width - radius + radius * (abs(math.cos(t)) ** power),
                height - radius + radius * (abs(math.sin(t)) ** power),
            )
            for t in [math.pi * i / (2 * segments) for i in range(segments + 1)]
        )
    )
    points.append((radius, height))
    add_arc(
        (
            (
                radius - radius * (abs(math.sin(t)) ** power),
                height - radius + radius * (abs(math.cos(t)) ** power),
            )
            for t in [math.pi * i / (2 * segments) for i in range(segments + 1)]
        )
    )
    points.append((0, radius))
    add_arc(
        (
            (
                radius - radius * (abs(math.cos(t)) ** power),
                radius - radius * (abs(math.sin(t)) ** power),
            )
            for t in [math.pi * i / (2 * segments) for i in range(segments + 1)]
        )
    )
    return points


def _set_windows_superellipse_region(window, expanded: bool = False) -> bool:
    """Clip a frameless pywebview window to a real Windows region."""

    if os.name != "nt" or window is None:
        return False

    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        # Do not access ``window.native`` here.  pywebview exposes that object
        # through a WebView2 COM proxy which is UI-thread-only; the retry timer
        # and resize events may run on another thread.  The top-level HWND is
        # safely discoverable by title through user32 instead.
        user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        user32.FindWindowW.restype = wintypes.HWND
        hwnd = user32.FindWindowW(None, "Jarvis")
        if not hwnd:
            return False
        user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL
        user32.SetWindowRgn.argtypes = [wintypes.HWND, ctypes.c_void_p, wintypes.BOOL]
        user32.SetWindowRgn.restype = ctypes.c_int
        gdi32.CreatePolygonRgn.argtypes = [
            ctypes.POINTER(wintypes.POINT),
            ctypes.c_int,
            ctypes.c_int,
        ]
        gdi32.CreatePolygonRgn.restype = ctypes.c_void_p
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteObject.restype = wintypes.BOOL

        if expanded:
            # A maximized/fullscreen window should touch all four screen edges.
            return bool(user32.SetWindowRgn(hwnd, None, True))

        rect = wintypes.RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        width = rect.right - rect.left
        height = rect.bottom - rect.top
        radius = min(28, max(18, round(min(width, height) * 0.05)))
        raw_points = _superellipse_points(width, height, radius=radius)
        points = (wintypes.POINT * len(raw_points))(
            *(wintypes.POINT(x, y) for x, y in raw_points)
        )
        region = gdi32.CreatePolygonRgn(points, len(raw_points), 1)
        if not region:
            return False
        applied = bool(user32.SetWindowRgn(hwnd, region, True))
        if not applied:
            gdi32.DeleteObject(region)
        return applied
    except (AttributeError, OSError, TypeError, ValueError):
        return False


class WindowsBridgeApi:
    """Narrow JavaScript API exposed to the shared desktop document."""

    def __init__(self, client: "WindowsClient"):
        # Keep the host private.  pywebview recursively exposes public API
        # attributes to JavaScript; exposing the whole WindowsClient would
        # make it walk ``client.window.native`` (a WinForms/WebView2 COM
        # object), causing recursion-depth and UI-thread errors.
        self._client = client

    def handle_action(self, payload: dict[str, Any] | str) -> dict[str, Any]:
        return self._client.handle_action(payload)


class WindowsClient:
    """WebView2 host with the same chat behavior as the Ubuntu client."""

    WINDOW_WIDTH = 840
    WINDOW_HEIGHT = 580

    def __init__(self, resource_dir: str | Path | None = None):
        configured_dir = resource_dir or os.getenv("JARVIS_RESOURCE_DIR") or PROJECT_ROOT
        self.resource_dir = Path(configured_dir).resolve()
        self.html_path = self.resource_dir / "client_ui.html"
        self.window = None
        self.api = WindowsBridgeApi(self)
        self.active_session_id = history_manager.get_active_session_id()
        self._text_generation_lock = threading.Lock()
        self._window_lock = threading.RLock()
        self._loaded = False
        self._pending_js: list[str] = []
        self._is_maximized = False
        self._is_fullscreen = False
        self._region_retry_timer = None
        self._region_retry_attempts = 0
        self._region_retry_lock = threading.Lock()
        configured_sync = os.getenv("JARVIS_WINDOWS_SYNC_FILE", "").strip()
        self._sync_path = Path(configured_sync) if configured_sync else None
        self._sync_offset = 0
        self._sync_stop = threading.Event()
        self._sync_thread = None

    def create_window(self):
        if webview is None:
            raise RuntimeError("Windows 桌面版需要 pywebview 与 Microsoft Edge WebView2 Runtime。")
        if not self.html_path.is_file():
            raise RuntimeError(f"找不到桌面界面文件：{self.html_path}")

        if "DRAG_REGION_DIRECT_TARGET_ONLY" in webview.settings:
            webview.settings["DRAG_REGION_DIRECT_TARGET_ONLY"] = False

        self.window = webview.create_window(
            "Jarvis",
            # Let pywebview serve the absolute local path through its static
            # HTTP server.  WebView2's file:// navigation is unreliable on
            # Windows and can show ERR_FILE_NOT_FOUND even when the file
            # exists.  A fragment survives the server path normalization and
            # still marks the page as the native bridge client.
            url=f"{self.html_path}#bridge=pywebview",
            js_api=self.api,
            width=self.WINDOW_WIDTH,
            height=self.WINDOW_HEIGHT,
            min_size=(680, 460),
            resizable=True,
            frameless=True,
            easy_drag=False,
            shadow=True,
            background_color="#f5f5f2",
            text_select=True,
        )
        self._attach_window_event("before_show", self._on_before_show)
        self._attach_window_event("shown", self._on_shown)
        self._attach_window_event("loaded", self._on_loaded)
        self._attach_window_event("resized", self._on_resized)
        self._attach_window_event("maximized", self._on_maximized)
        self._attach_window_event("restored", self._on_restored)
        return self.window

    def _attach_window_event(self, event_name: str, handler) -> None:
        events = getattr(self.window, "events", None)
        event = getattr(events, event_name, None)
        if event is not None:
            event += handler

    def run(self) -> int:
        self.create_window()
        webview.start(gui="edgechromium", debug=_env_flag("JARVIS_WEBVIEW_DEBUG"))
        self._sync_stop.set()
        return 0

    def _on_loaded(self):
        with self._window_lock:
            self._loaded = True
            queued = self._pending_js
            self._pending_js = []
        for script in queued:
            self.run_js(script)
        self._apply_window_region()
        self._sync_desktop_state()
        self._start_sync_poller()

    def _on_before_show(self, *_args):
        self._apply_window_region()

    def _on_shown(self, *_args):
        # The native HWND is guaranteed to exist by this event.  Keep the
        # retry path as a fallback for WebView2 builds that emit ``shown``
        # before the HWND is returned through pywebview.
        self._apply_window_region()

    def _on_resized(self, *_args):
        self._apply_window_region()

    def _on_maximized(self):
        self._is_maximized = True
        self._apply_window_region()
        self._sync_window_state_to_ui()

    def _on_restored(self):
        self._is_maximized = False
        if self._is_fullscreen:
            self._is_fullscreen = False
        self._apply_window_region()
        self._sync_window_state_to_ui()

    def _apply_window_region(self):
        if os.name != "nt":
            return
        expanded = self._is_maximized or self._is_fullscreen
        applied = _set_windows_superellipse_region(
            self.window,
            expanded=expanded,
        )
        if applied:
            with self._region_retry_lock:
                self._region_retry_attempts = 0
                if self._region_retry_timer is not None:
                    self._region_retry_timer.cancel()
                    self._region_retry_timer = None
            return

        # Window handles are occasionally published a few frames after the
        # pywebview event.  Retry briefly instead of silently leaving a square
        # frameless window on Windows.
        if expanded:
            return
        with self._region_retry_lock:
            if self._region_retry_timer is not None:
                return
            if self._region_retry_attempts >= 12:
                return
            self._region_retry_attempts += 1
            delay = min(0.5, 0.04 * (1.45 ** (self._region_retry_attempts - 1)))
            timer = threading.Timer(delay, self._retry_window_region)
            timer.daemon = True
            self._region_retry_timer = timer
            timer.start()

    def _retry_window_region(self):
        with self._region_retry_lock:
            self._region_retry_timer = None
        self._apply_window_region()

    def _start_sync_poller(self):
        if self._sync_path is None or self._sync_thread is not None:
            return
        self._sync_offset = self._find_sync_start_offset()
        self._sync_thread = threading.Thread(target=self._poll_voice_sync, daemon=True)
        self._sync_thread.start()

    def _find_sync_start_offset(self):
        """Replay only an unfinished voice turn when the desktop opens."""

        if self._sync_path is None:
            return 0
        start_offset = 0
        latest_complete_offset = 0
        try:
            with self._sync_path.open("r", encoding="utf-8") as stream:
                while True:
                    line_start = stream.tell()
                    line = stream.readline()
                    if not line:
                        break
                    try:
                        action = json.loads(line).get("action")
                    except json.JSONDecodeError:
                        continue
                    if action == "turn_start":
                        start_offset = line_start
                    elif action == "turn_complete":
                        latest_complete_offset = stream.tell()
                        start_offset = latest_complete_offset
        except OSError:
            return 0
        return start_offset if start_offset > latest_complete_offset else latest_complete_offset

    def _poll_voice_sync(self):
        while not self._sync_stop.wait(0.12):
            if self._sync_path is None:
                return
            try:
                size = self._sync_path.stat().st_size
                if size < self._sync_offset:
                    self._sync_offset = 0
                with self._sync_path.open("r", encoding="utf-8") as stream:
                    stream.seek(self._sync_offset)
                    while True:
                        line = stream.readline()
                        if not line:
                            break
                        self._sync_offset = stream.tell()
                        try:
                            event = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        self._apply_voice_sync_event(event)
            except (OSError, ValueError):
                continue

    def _apply_voice_sync_event(self, event: dict[str, Any]):
        action = event.get("action")
        if action in {"turn_start", "turn_complete"}:
            return
        if action == "state":
            state = str(event.get("state", "ready")).lower()
            labels = {
                "awake": "已唤醒",
                "listening": "正在聆听",
                "thinking": "正在思考",
                "speaking": "正在回应",
                "error": "语音服务异常",
                "hide": "语音已连接",
                "idle": "语音已连接",
            }
            status = "ready" if state in {"hide", "idle"} else state
            text = labels.get(state, "语音已连接")
            self.run_js(f"window.setStatus({json.dumps(status)}, {json.dumps(text)});")
        elif action == "user_text":
            text = str(event.get("text", ""))
            if text:
                final = "true" if event.get("final") else "false"
                self.run_js(
                    f"window.updateUserTranscript({json.dumps(text)}, {final});"
                )
                if event.get("final"):
                    self.run_js("window.setTextBusy(true);")
        elif action == "ai_delta":
            self.run_js(f"window.appendAiDelta({json.dumps(str(event.get('delta', '')))});")
        elif action == "ai_finish":
            self.run_js("window.finishAiTurn(); window.setTextBusy(false);")
        elif action == "tool_call":
            self.run_js(
                "window.showToolBadge(%s, %s);"
                % (
                    json.dumps(str(event.get("name", ""))),
                    json.dumps(str(event.get("query", ""))),
                )
            )

    def run_js(self, script: str):
        with self._window_lock:
            if not self.window or not self._loaded:
                self._pending_js.append(script)
                return
            target = self.window
        try:
            target.run_js(script)
        except Exception as exc:
            if _env_flag("JARVIS_WEBVIEW_DEBUG"):
                print(f"Windows WebView JavaScript error: {exc}")

    def _sync_desktop_state(self):
        self._sync_sessions_to_ui()
        self._load_session_messages(self.active_session_id)
        self._sync_memories_to_ui()
        self._sync_window_state_to_ui()
        self.set_status("ready", "Windows 文字模式")

    def handle_action(self, payload: dict[str, Any] | str) -> dict[str, Any]:
        try:
            data = json.loads(payload) if isinstance(payload, str) else dict(payload or {})
            action = str(data.get("action", ""))

            if action == "ready":
                self._sync_desktop_state()
            elif action == "create_session":
                session = history_manager.create_session()
                self.active_session_id = session["id"]
                self._sync_sessions_to_ui()
                self._load_session_messages(self.active_session_id)
            elif action == "switch_session":
                session_id = str(data.get("session_id", ""))
                if session_id and history_manager.set_active_session(session_id):
                    self.active_session_id = session_id
                    self._sync_sessions_to_ui()
                    self._load_session_messages(session_id)
            elif action == "delete_session":
                session_id = str(data.get("session_id", ""))
                if session_id:
                    history_manager.delete_session(session_id)
                    self.active_session_id = history_manager.get_active_session_id()
                    self._sync_sessions_to_ui()
                    self._load_session_messages(self.active_session_id)
            elif action == "clear_session":
                history_manager.delete_session(str(data.get("session_id") or self.active_session_id))
                session = history_manager.create_session()
                self.active_session_id = session["id"]
                self._sync_sessions_to_ui()
                self._load_session_messages(self.active_session_id)
            elif action == "send_text":
                text = str(data.get("text", "")).strip()
                session_id = str(data.get("session_id") or self.active_session_id)
                request_id = str(data.get("request_id", ""))
                if text:
                    self._handle_text_query(session_id, text, request_id)
            elif action == "get_memories":
                self._sync_memories_to_ui()
            elif action == "delete_memory":
                memory_id = str(data.get("id", ""))
                if memory_id:
                    memory_manager.forget(memory_id)
                    self._sync_memories_to_ui()
            elif action == "get_settings":
                safe_config = json.dumps(get_user_config(), ensure_ascii=False)
                self.run_js(f"window.renderSettings?.({safe_config});")
            elif action == "save_settings":
                city = str(data.get("home_location", "")).strip()
                if city:
                    set_home_location(city)
            elif action in {"minimize_window", "handle_escape"}:
                if self._is_fullscreen:
                    self._toggle_fullscreen()
                elif self.window:
                    self.window.minimize()
            elif action == "toggle_maximize":
                self._toggle_maximize()
            elif action == "toggle_fullscreen":
                self._toggle_fullscreen()
            elif action in {"close_window", "hide_window"}:
                if self.window:
                    self._sync_stop.set()
                    self.window.destroy()
            elif action == "start_drag":
                # The header uses pywebview-drag-region; no API call is needed.
                pass
            return {"ok": True}
        except Exception as exc:
            if _env_flag("JARVIS_WEBVIEW_DEBUG"):
                print(f"Error handling Windows client action: {exc}")
            return {"ok": False, "error": str(exc)}

    def _toggle_maximize(self):
        if not self.window:
            return
        if self._is_maximized:
            self.window.restore()
            self._is_maximized = False
        else:
            self.window.maximize()
            self._is_maximized = True
        self._apply_window_region()
        self._sync_window_state_to_ui()

    def _toggle_fullscreen(self):
        if not self.window:
            return
        self.window.toggle_fullscreen()
        self._is_fullscreen = not self._is_fullscreen
        self._apply_window_region()
        self._sync_window_state_to_ui()

    def _sync_window_state_to_ui(self):
        expanded = self._is_maximized or self._is_fullscreen
        self.run_js(
            f"window.setWindowExpanded({str(expanded).lower()}, "
            f"{str(self._is_fullscreen).lower()});"
        )

    def _sync_sessions_to_ui(self):
        sessions = history_manager.list_sessions()
        safe_sessions = json.dumps(sessions, ensure_ascii=False)
        safe_current = json.dumps(self.active_session_id)
        self.run_js(f"window.renderSessionsList({safe_sessions}, {safe_current});")

    def _sync_memories_to_ui(self):
        memories = memory_manager.list_memories()
        safe_memories = json.dumps(memories, ensure_ascii=False)
        self.run_js(f"window.renderMemories({safe_memories});")

    def _load_session_messages(self, session_id: str):
        messages = history_manager.get_session_messages(session_id)
        safe_messages = json.dumps(messages, ensure_ascii=False)
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
            "执行命令", "终端命令", "shell", "bash", "powershell", "打开", "访问", "启动",
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
            return {key: WindowsClient._plain_value(item) for key, item in value.items()}
        if isinstance(value, (list, tuple)):
            return [WindowsClient._plain_value(item) for item in value]
        if hasattr(value, "to_dict"):
            return WindowsClient._plain_value(value.to_dict())
        return value

    @staticmethod
    def _message_content(message: Any) -> str:
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            content = message.get("content", "")
        if isinstance(content, list):
            return "".join(
                item.get("text", "") if isinstance(item, dict) else str(item)
                for item in content
            )
        return str(content or "")

    def _handle_text_query(self, session_id: str, query: str, request_id: str = ""):
        if not history_manager.get_session(session_id):
            self.run_js("window.setTextBusy(false);")
            return
        if not self._text_generation_lock.acquire(blocking=False):
            self.run_js("window.setTextBusy(false);")
            return

        history_manager.add_message(session_id, "user", query)
        self._sync_sessions_to_ui()
        self.run_js("window.setTextBusy(true);")
        safe_session = json.dumps(session_id)
        safe_request = json.dumps(request_id)

        def run_for_active_session(js_call: str):
            if self.active_session_id == session_id:
                self.run_js(js_call)

        def worker():
            self.set_status("thinking", "Qwen 正在思考…")
            try:
                if not str(dashscope.api_key or "").strip():
                    raise RuntimeError("请先在 .env 中配置 DASHSCOPE_API_KEY")

                tool_result_text = ""
                tool_dialog = []
                open_request = self._extract_open_request(query)
                home_city = self._extract_home_location(query)
                if open_request:
                    tool_name, tool_args = open_request
                    result = dispatch_tool_call(tool_name, tool_args)
                    tool_result_text += f"\n[系统操作结果]: {result}\n"
                elif home_city:
                    result = dispatch_tool_call("set_home_location", {"city": home_city})
                    tool_result_text += f"\n[常驻城市设置结果]: {result}\n"
                elif any(word in query for word in ("天气", "气温", "下雨", "温度", "多少度")):
                    city = self._extract_weather_city(query)
                    result = dispatch_tool_call("get_weather", {"city": city})
                    tool_result_text += f"\n[实时天气数据]: {result}\n"
                elif any(word in query for word in ("定位", "在哪", "位置", "经纬度", "ip")):
                    result = dispatch_tool_call("get_location", {})
                    tool_result_text += f"\n[地理位置数据]: {result}\n"
                elif any(word in query for word in ("搜索", "查一下", "找一下", "2025", "2026", "最新", "发布", "模型")):
                    result = dispatch_tool_call("web_search", {"query": query})
                    tool_result_text += f"\n[网络搜索结果]: {result}\n"
                elif any(word in query for word in ("记住", "偏好", "我的名字")):
                    result = dispatch_tool_call(
                        "manage_memory",
                        {"action": "remember", "content": query},
                    )
                    self._sync_memories_to_ui()
                    tool_result_text += f"\n[记忆库更新]: {result}\n"

                if not tool_result_text and self._looks_like_tool_request(query):
                    recent_for_tools = history_manager.get_session_messages(session_id)[-6:]
                    selector_messages = [{
                        "role": "system",
                        "content": (
                            "你是 Windows 桌面助手。用户要求操作电脑或查询实时信息时，"
                            "必须选择最合适的工具；不要假装已经执行。参数必须完整、准确。"
                        ),
                    }]
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
                        raise RuntimeError(f"工具选择请求失败 ({selection.status_code})")
                    selected_message = selection.output.choices[0].message
                    tool_calls = self._plain_value(
                        getattr(selected_message, "tool_calls", None)
                        or (selected_message.get("tool_calls") if isinstance(selected_message, dict) else None)
                        or []
                    )
                    if tool_calls:
                        tool_dialog.append({
                            "role": "assistant",
                            "content": self._message_content(selected_message),
                            "tool_calls": tool_calls,
                        })
                        for tool_call in tool_calls[:3]:
                            function = tool_call.get("function", {})
                            tool_name = function.get("name", "")
                            arguments = function.get("arguments", "{}")
                            if not tool_name:
                                continue
                            result = dispatch_tool_call(tool_name, arguments)
                            if tool_name == "manage_memory":
                                self._sync_memories_to_ui()
                            tool_dialog.append({
                                "role": "tool",
                                "name": tool_name,
                                "content": str(result),
                            })
                            tool_result_text += f"\n[{tool_name} 执行结果]: {result}\n"

                memory_context = memory_manager.get_system_prompt_context()
                system_prompt = (
                    "你是 Jarvis Windows 桌面版，是一个极简、高效、专业的个人智能助手。"
                    "回答自然清晰、直奔主题；需要实时信息或电脑操作时使用工具，不要假装已经执行。"
                )
                if memory_context:
                    system_prompt += f"\n{memory_context}"
                if tool_result_text:
                    system_prompt += f"\n{tool_result_text}"

                recent_messages = history_manager.get_session_messages(session_id)[-6:]
                dialog = [{"role": "system", "content": system_prompt}]
                dialog.extend(
                    {"role": item["role"], "content": item["content"]}
                    for item in recent_messages
                )
                dialog.extend(tool_dialog)

                self.set_status("speaking", "AI 正在回答…")
                full_reply = ""
                responses = Generation.call(
                    model="qwen-turbo",
                    messages=dialog,
                    result_format="message",
                    stream=True,
                    incremental_output=True,
                )
                for response in responses:
                    if response.status_code != 200:
                        raise RuntimeError(f"DashScope 请求失败 ({response.status_code})")
                    delta = self._message_content(response.output.choices[0].message)
                    if delta:
                        full_reply += delta
                        run_for_active_session(
                            f"window.appendAiDelta({json.dumps(delta)}, {safe_session}, {safe_request});"
                        )

                if full_reply and history_manager.get_session(session_id):
                    history_manager.add_message(session_id, "assistant", full_reply)
                run_for_active_session(
                    f"window.finishAiTurn({safe_session}, {safe_request});"
                )
                self._sync_sessions_to_ui()
            except Exception as exc:
                error_message = f"\n❌ 回复生成异常：{exc}"
                run_for_active_session(
                    f"window.appendAiDelta({json.dumps(error_message)}, {safe_session}, {safe_request});"
                )
                run_for_active_session(
                    f"window.finishAiTurn({safe_session}, {safe_request});"
                )
            finally:
                self.set_status("ready", "Windows 文字模式")
                self._text_generation_lock.release()
                self.run_js("window.setTextBusy(false);")

        threading.Thread(target=worker, daemon=True).start()

    def set_status(self, status: str, text: str):
        self.run_js(
            f"window.setStatus({json.dumps(status)}, {json.dumps(text, ensure_ascii=False)});"
        )


def main() -> int:
    if webview is None:
        print("Windows 桌面版需要 pywebview 与 Microsoft Edge WebView2 Runtime；请重新运行 install.ps1。")
        return 1
    try:
        return WindowsClient().run()
    except Exception as exc:
        print(f"Windows 桌面版启动失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
