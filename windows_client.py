#!/usr/bin/env python3
"""Windows desktop client for Jarvis.

The Linux client keeps its GTK/WebKit experience. This client uses Tkinter,
which ships with the official Windows Python distribution, and focuses on the
portable text assistant workflow: chat, history, memory, web access, and safe
file/application tools. Linux-only realtime AEC and wake-word features remain
disabled here until a Windows audio pipeline is implemented.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any

try:
    import tkinter as tk
    from tkinter import scrolledtext, ttk
except ImportError:  # pragma: no cover - Linux source installs may omit Tk.
    tk = None
    scrolledtext = None
    ttk = None

from dashscope import Generation
import dashscope
from dotenv import load_dotenv

from history_manager import history_manager
from memory import memory_manager
from system_tools import TOOLS_DEFINITION, dispatch_tool_call


load_dotenv()
load_dotenv(Path(__file__).resolve().with_name(".env"))
dashscope.api_key = dashscope.api_key or os.getenv("DASHSCOPE_API_KEY")


APP_BG = "#10131a"
PANEL_BG = "#171b24"
INPUT_BG = "#202633"
TEXT_FG = "#edf2f7"
MUTED_FG = "#9aa6b2"
ACCENT = "#5ec8ff"


class WindowsClient:
    """Tkinter desktop chat window with background model requests."""

    def __init__(self, root: tk.Tk):
        if tk is None or scrolledtext is None or ttk is None:
            raise RuntimeError("Windows 桌面版需要可用的 Tkinter；请安装 Python 的 Tcl/Tk 组件。")
        self.root = root
        self.root.title("Jarvis")
        self.root.geometry("1000x700")
        self.root.minsize(760, 520)
        self.root.configure(bg=APP_BG)
        self.active_session_id = history_manager.get_active_session_id()
        self._busy = False

        self._build_ui()
        self._refresh_sessions()
        self._load_session(self.active_session_id)
        self.root.protocol("WM_DELETE_WINDOW", self.root.destroy)

    def _build_ui(self):
        self.root.columnconfigure(1, weight=1)
        self.root.rowconfigure(1, weight=1)

        header = tk.Frame(self.root, bg=PANEL_BG, height=58)
        header.grid(row=0, column=0, columnspan=2, sticky="ew")
        header.grid_columnconfigure(1, weight=1)
        tk.Label(
            header,
            text="◈ Jarvis",
            bg=PANEL_BG,
            fg=TEXT_FG,
            font=("Segoe UI", 16, "bold"),
            padx=18,
        ).grid(row=0, column=0, sticky="w")
        self.status_var = tk.StringVar(value="Windows 桌面版 · 待命")
        tk.Label(
            header,
            textvariable=self.status_var,
            bg=PANEL_BG,
            fg=MUTED_FG,
            font=("Segoe UI", 9),
        ).grid(row=0, column=1, sticky="w")
        ttk.Button(header, text="记忆", command=self._show_memories).grid(row=0, column=2, padx=6)
        ttk.Button(header, text="新会话", command=self._new_session).grid(row=0, column=3, padx=(0, 14))

        sidebar = tk.Frame(self.root, bg=PANEL_BG, width=220)
        sidebar.grid(row=1, column=0, sticky="nsew")
        sidebar.grid_propagate(False)
        tk.Label(
            sidebar,
            text="会话历史",
            bg=PANEL_BG,
            fg=MUTED_FG,
            font=("Segoe UI", 10, "bold"),
            padx=14,
            pady=12,
        ).pack(anchor="w")
        self.session_list = tk.Listbox(
            sidebar,
            bg=PANEL_BG,
            fg=TEXT_FG,
            selectbackground="#284a63",
            selectforeground="white",
            relief="flat",
            highlightthickness=0,
            activestyle="none",
            font=("Segoe UI", 10),
        )
        self.session_list.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.session_list.bind("<<ListboxSelect>>", self._on_session_selected)

        content = tk.Frame(self.root, bg=APP_BG)
        content.grid(row=1, column=1, sticky="nsew", padx=18, pady=16)
        content.rowconfigure(0, weight=1)
        content.columnconfigure(0, weight=1)

        self.transcript = scrolledtext.ScrolledText(
            content,
            wrap="word",
            state="disabled",
            bg=APP_BG,
            fg=TEXT_FG,
            insertbackground=TEXT_FG,
            relief="flat",
            padx=16,
            pady=12,
            font=("Segoe UI", 11),
        )
        self.transcript.grid(row=0, column=0, sticky="nsew")
        self.transcript.tag_configure("user", foreground=ACCENT, spacing3=8)
        self.transcript.tag_configure("assistant", foreground=TEXT_FG, spacing3=14)
        self.transcript.tag_configure("system", foreground=MUTED_FG, spacing3=8)

        composer = tk.Frame(content, bg=APP_BG)
        composer.grid(row=1, column=0, sticky="ew", pady=(14, 0))
        composer.columnconfigure(0, weight=1)
        self.input_box = tk.Text(
            composer,
            height=4,
            wrap="word",
            bg=INPUT_BG,
            fg=TEXT_FG,
            insertbackground=TEXT_FG,
            relief="flat",
            padx=12,
            pady=10,
            font=("Segoe UI", 11),
        )
        self.input_box.grid(row=0, column=0, sticky="ew")
        self.input_box.bind("<Control-Return>", self._on_ctrl_enter)
        ttk.Button(composer, text="发送  Ctrl+Enter", command=self.send_message).grid(
            row=0, column=1, padx=(10, 0), sticky="ns"
        )

    def _on_ctrl_enter(self, _event):
        self.send_message()
        return "break"

    def _refresh_sessions(self):
        sessions = history_manager.list_sessions()
        self.session_list.delete(0, tk.END)
        selected = 0
        for index, session in enumerate(sessions):
            title = session.get("title") or "新对话"
            self.session_list.insert(tk.END, title)
            if session["id"] == self.active_session_id:
                selected = index
        if sessions:
            self.session_list.selection_set(selected)
            self.session_list.see(selected)

    def _on_session_selected(self, _event):
        selection = self.session_list.curselection()
        if not selection:
            return
        sessions = history_manager.list_sessions()
        index = selection[0]
        if index < len(sessions):
            self._load_session(sessions[index]["id"])

    def _load_session(self, session_id: str):
        if not history_manager.set_active_session(session_id):
            return
        self.active_session_id = session_id
        self.transcript.configure(state="normal")
        self.transcript.delete("1.0", tk.END)
        self.transcript.configure(state="disabled")
        for message in history_manager.get_session_messages(session_id):
            role = message.get("role", "assistant")
            label = "你" if role == "user" else "Jarvis"
            self._append_transcript(f"{label}\n{message.get('content', '')}\n", role)

    def _new_session(self):
        session = history_manager.create_session()
        self.active_session_id = session["id"]
        self._refresh_sessions()
        self._load_session(self.active_session_id)
        self.status_var.set("Windows 桌面版 · 新会话")

    def _append_transcript(self, text: str, role: str = "system"):
        self.transcript.configure(state="normal")
        self.transcript.insert(tk.END, text, role if role in {"user", "assistant", "system"} else "system")
        self.transcript.see(tk.END)
        self.transcript.configure(state="disabled")

    def _post(self, callback, *args):
        try:
            self.root.after(0, callback, *args)
        except tk.TclError:
            pass

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.status_var.set("Windows 桌面版 · 正在思考…" if busy else "Windows 桌面版 · 待命")

    def send_message(self):
        if self._busy:
            return
        query = self.input_box.get("1.0", tk.END).strip()
        if not query:
            return
        if not str(dashscope.api_key or "").strip():
            self._append_transcript("请先在 .env 中配置 DASHSCOPE_API_KEY。\n", "system")
            return

        self.input_box.delete("1.0", tk.END)
        session_id = self.active_session_id
        history_manager.add_message(session_id, "user", query)
        self._append_transcript(f"你\n{query}\n", "user")
        self._refresh_sessions()
        self._set_busy(True)
        threading.Thread(target=self._request_response, args=(session_id,), daemon=True).start()

    def _request_response(self, session_id: str):
        try:
            reply = self._generate_response(session_id)
            if reply and history_manager.get_session(session_id):
                history_manager.add_message(session_id, "assistant", reply)
            self._post(self._append_transcript, f"Jarvis\n{reply or '未收到有效回复'}\n", "assistant")
            self._post(self._refresh_sessions)
        except Exception as exc:
            self._post(self._append_transcript, f"Jarvis\n❌ 回复生成失败：{exc}\n", "system")
        finally:
            self._post(self._set_busy, False)

    def _generate_response(self, session_id: str) -> str:
        recent = history_manager.get_session_messages(session_id)[-8:]
        memory_context = memory_manager.get_system_prompt_context()
        system_prompt = (
            "你是 Jarvis Windows 桌面版，是一个简洁、可靠的个人电脑助手。"
            "始终使用简体中文回答；需要实时信息或电脑操作时调用工具，不要假装已经执行。"
        )
        if memory_context:
            system_prompt += f"\n{memory_context}"

        tool_dialog = []
        tool_result_text = ""
        if self._looks_like_tool_request(recent[-1]["content"] if recent else ""):
            selector_messages = [{"role": "system", "content": system_prompt}]
            selector_messages.extend({"role": item["role"], "content": item["content"]} for item in recent)
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
                assistant_message = {
                    "role": "assistant",
                    "content": self._message_content(selected_message),
                    "tool_calls": tool_calls,
                }
                tool_dialog.append(assistant_message)
                for tool_call in tool_calls[:3]:
                    function = tool_call.get("function", {})
                    name = function.get("name", "")
                    arguments = function.get("arguments", "{}")
                    if not name:
                        continue
                    self._post(self.status_var.set, f"正在执行工具：{name}")
                    result = dispatch_tool_call(name, arguments)
                    tool_dialog.append({"role": "tool", "name": name, "content": str(result)})
                    tool_result_text += f"\n[{name} 执行结果]: {result}\n"

        if tool_result_text:
            system_prompt += tool_result_text
        dialog = [{"role": "system", "content": system_prompt}]
        dialog.extend({"role": item["role"], "content": item["content"]} for item in recent)
        dialog.extend(tool_dialog)

        responses = Generation.call(
            model="qwen-turbo",
            messages=dialog,
            result_format="message",
            stream=True,
            incremental_output=True,
        )
        chunks = []
        for response in responses:
            if response.status_code != 200:
                raise RuntimeError(f"DashScope 请求失败 ({response.status_code})")
            content = self._message_content(response.output.choices[0].message)
            if content:
                chunks.append(content)
        return "".join(chunks).strip()

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
    def _looks_like_tool_request(query: str) -> bool:
        keywords = (
            "打开", "访问", "启动", "浏览器", "天气", "气温", "下雨", "搜索", "查一下",
            "记住", "忘掉", "回忆", "文件", "目录", "读取", "创建", "删除", "执行命令",
            "音量", "静音", "亮度", "系统状态", "cpu", "内存", "磁盘", "定位", "位置",
        )
        lowered = query.lower()
        return any(keyword in lowered for keyword in keywords)

    def _show_memories(self):
        window = tk.Toplevel(self.root)
        window.title("Jarvis 记忆")
        window.geometry("560x420")
        window.configure(bg=APP_BG)
        output = scrolledtext.ScrolledText(window, wrap="word", bg=APP_BG, fg=TEXT_FG, relief="flat")
        output.pack(fill="both", expand=True, padx=16, pady=16)
        memories = memory_manager.list_memories()
        if not memories:
            output.insert(tk.END, "暂无已保存的长期记忆。")
        else:
            for item in memories:
                output.insert(tk.END, f"[{item.get('category', 'general')}] {item.get('content', '')}\n")
        output.configure(state="disabled")


def main() -> int:
    if tk is None:
        print("Windows 桌面版需要可用的 Tkinter；请重新安装带 Tcl/Tk 的 Python。")
        return 1
    root = tk.Tk()
    WindowsClient(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
